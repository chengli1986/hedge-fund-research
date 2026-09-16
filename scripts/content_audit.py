#!/usr/bin/env python3
"""Weekly content audit: re-fetch stored articles and catch silent drift.

Every source problem found in the week of 2026-09-11 was found by opening
pages one source at a time; man.com's new role gate surfaced only because a
test email happened to run the health probe that day. This re-fetches the
most recent stored articles of each source with today's fetchers -- into a
temporary directory, never production content/ -- and compares:

  extraction_drift  the fetch now takes a fallback or the whole page
  now_failing       the fetch now fails (its failure label is kept)
  page_gone         the page was removed: recorded, not an alert
  body_shrunk       under half the stored length
  body_grew         over twice the stored length: recorded, not an alert
  content_changed   the text no longer matches what is stored

Rows at a reused URL (an issue id, or a URL shared by several rows) are not
sampled: such a URL shows only the latest issue and would always differ.
Rows the analysis already declined are not sampled either.

A body stored before a selector fix may carry page chrome, so today's cleaner
fetch reads as body_shrunk. Text alone cannot tell that from a selector that
now catches half the article, so it stays an alert and the report shows how
much of today's text was inside the old body (new_in_old). Once a person has
checked the page, --accept saves today's fetch as that article's baseline and
later audits compare against it instead of the stored body.

  python3 scripts/content_audit.py --email          # weekly cron
  python3 scripts/content_audit.py --test-email ADDR
  python3 scripts/content_audit.py --accept ID [ID ...]
  python3 scripts/content_audit.py --mark-gone ID [ID ...]
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import failure_labels  # noqa: E402

BJT = timezone(timedelta(hours=8))
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
REPORT_DIR = BASE_DIR / "logs" / "content-audit"
BASELINE_DIR = REPORT_DIR / "baseline"
PER_SOURCE = 3

ALERT_FINDINGS = ("now_failing", "extraction_drift", "body_shrunk", "content_changed",
                  "site_wide_access_loss")
# Documents that are simply not there any more. One is housekeeping; several
# at one source in one run is a new gate (man.com added a role wall on
# 2026-09-15 and every article went at once), so mark_site_wide_losses raises
# those to an alert.
LOST_FINDINGS = ("page_gone", "access_denied")
SITE_WIDE_LOSS_MIN = 2
FINDING_LABELS = {
    "extraction_drift": "抓取方式变了：专用规则不再匹配，退回了通用容器或整页（多为网站改版）",
    "now_failing": "现在抓不到了",
    "page_gone": "页面已删除（仅记录）",
    "access_denied": "站方拒绝这一篇：撤下、需登录或地区限制（返回的是网站自己的拒绝页，仅记录）",
    "site_wide_access_loss": "同一网站有多篇同时打不开了——可能整站加了门槛",
    "body_shrunk": "正文比库里存的短了一半以上",
    "body_grew": "正文比库里存的长了一倍以上（可能混进了页面杂物，仅记录）",
    "content_changed": "正文内容与库里存的对不上（可能抓成了别的内容）",
}
SHRINK_RATIO, GROW_RATIO, MIN_OVERLAP, SHINGLE = 0.5, 2.0, 0.5, 8


def _shingles(text: str) -> set:
    compact = re.sub(r"[^0-9a-z]", "", (text or "").lower())[:20000]
    return {compact[i:i + SHINGLE] for i in range(0, max(len(compact) - SHINGLE + 1, 0))}


def compare_bodies(stored: str, new: str) -> list[str]:
    """Findings from comparing a stored body with today's extraction.

    Punctuation and whitespace are dropped before comparing, so bodies stored
    before the word-fusing fix (c149894) do not read as changed. Overlap is
    measured against the smaller text, so a body that gained or lost a
    section is judged by length, not as different text.
    """
    findings = []
    ls, ln = len(stored or ""), len(new or "")
    if ls >= 300 and ln < ls * SHRINK_RATIO:
        findings.append("body_shrunk")
    if ls and ln > ls * GROW_RATIO:
        findings.append("body_grew")
    a, b = _shingles(stored), _shingles(new)
    if a and b and len(a & b) / min(len(a), len(b)) < MIN_OVERLAP:
        findings.append("content_changed")
    return findings


def sample_articles(rows, configured, per_source: int = PER_SOURCE, content_dir: Path | None = None) -> list:
    import fetch_articles as fa

    content_dir = content_dir or BASE_DIR / "content"
    url_count = Counter(r.get("url") for r in rows)
    by_source: dict[str, list] = {}
    for r in rows:
        if r.get("source_id") not in configured or r.get("content_status") != "ok":
            continue
        if r.get("analysis_status") == "insufficient_content":
            continue
        if url_count[r.get("url")] > 1 or r.get("id") != fa.article_id(r["source_id"], r.get("url", "")):
            continue
        if not (content_dir / f"{r['id']}.txt").exists():
            continue
        by_source.setdefault(r["source_id"], []).append(r)
    out = []
    for sid in sorted(by_source):
        out += sorted(by_source[sid], key=lambda r: r.get("date") or "", reverse=True)[:per_source]
    return out


def _fetch_to_temp(article: dict, fetcher) -> tuple:
    """(result, evidence, text) from a fetch into a temporary CONTENT_DIR."""
    import fetch_content as fc

    original_dir = fc.CONTENT_DIR
    with tempfile.TemporaryDirectory(prefix="gmia-audit-") as tmp:
        fc.CONTENT_DIR = Path(tmp)
        try:
            result, evidence = fc.fetch_with_evidence(dict(article), fetcher)
            new = result[0].read_text(encoding="utf-8", errors="ignore") if result else ""
        finally:
            fc.CONTENT_DIR = original_dir
    return result, evidence, new


def audit_article(article: dict, fetcher, stored_dir: Path | None = None,
                  baseline_dir: Path | None = None) -> dict:
    import fetch_content as fc

    stored_dir = stored_dir or fc.CONTENT_DIR
    baseline = (baseline_dir or BASELINE_DIR) / f"{article['id']}.txt"
    compared_with = "baseline" if baseline.exists() else "stored"
    old_path = baseline if baseline.exists() else stored_dir / f"{article['id']}.txt"
    stored = old_path.read_text(encoding="utf-8", errors="ignore")
    result, evidence, new = _fetch_to_temp(article, fetcher)
    a, b = _shingles(stored), _shingles(new)
    out = {"source_id": article.get("source_id"), "id": article.get("id"), "title": article.get("title"),
           "url": article.get("url"), "stored_chars": len(stored), "new_chars": len(new),
           "compared_with": compared_with, "new_in_old": round(len(a & b) / len(b), 2) if b else None,
           "paths": evidence["extraction_paths"], "label": None, "detail": ""}
    if result is None:
        label, detail = failure_labels.classify_content_failure(evidence)
        out.update(label=label, detail=detail,
                   findings=[label if label in LOST_FINDINGS else "now_failing"])
        return out
    findings = []
    if any(p != "primary" for p in evidence["extraction_paths"]):
        findings.append("extraction_drift")
    if result[1] == "ok":
        findings += compare_bodies(stored, new)
    out["findings"] = findings
    return out


def accept_article(article: dict, fetcher, baseline_dir: Path | None = None) -> tuple[bool, str]:
    """Save today's fetch as the article's baseline; only a clean primary body."""
    result, evidence, new = _fetch_to_temp(article, fetcher)
    if result is None:
        return False, "fetch failed: " + "; ".join(failure_labels.classify_content_failure(evidence))
    if result[1] != "ok":
        return False, f"fetch status is {result[1]}, not ok"
    if not evidence["extraction_paths"] or any(p != "primary" for p in evidence["extraction_paths"]):
        return False, f"extraction was not primary: {evidence['extraction_paths']}"
    baseline_dir = baseline_dir or BASELINE_DIR
    baseline_dir.mkdir(parents=True, exist_ok=True)
    (baseline_dir / f"{article['id']}.txt").write_text(new, encoding="utf-8")
    return True, f"{len(new)} chars"


def mark_site_wide_losses(results: list) -> None:
    """Raise per-source clusters of lost documents to an alert, in place."""
    lost = Counter(r["source_id"] for r in results
                   if any(f in LOST_FINDINGS for f in r.get("findings") or []))
    for r in results:
        if lost[r["source_id"]] >= SITE_WIDE_LOSS_MIN \
                and any(f in LOST_FINDINGS for f in r.get("findings") or []):
            r["findings"].append("site_wide_access_loss")


def mark_gone(article_ids: list, data_file: Path | None = None) -> int:
    """Stamp url_status="gone" on rows whose original the publisher removed.

    Evidence-gated on purpose: each page is fetched and only page_gone or
    access_denied is accepted. A timeout is not a takedown, and a stamp on a
    live article would print "original removed" under a working link.
    Returns the number of ids it refused.
    """
    import fetch_content as fc

    data_file = data_file or DATA_FILE
    rows = [json.loads(l) for l in data_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {r["id"]: r for r in rows}
    stamped, refused = 0, 0
    for aid in article_ids:
        row = by_id.get(aid)
        if row is None:
            print(f"  ✗ {aid}: no such article"); refused += 1; continue
        result, evidence, _ = _fetch_to_temp(row, fc.content_fetcher_for(row))
        if result is not None:
            print(f"  ✗ {aid}: the page still serves an article"); refused += 1; continue
        label, detail = failure_labels.classify_content_failure(evidence)
        if label not in LOST_FINDINGS:
            print(f"  ✗ {aid}: {label} ({detail}) is not a takedown"); refused += 1; continue
        row["url_status"] = "gone"
        row["url_checked_at"] = datetime.now(BJT).strftime("%Y-%m-%d")
        row["url_note"] = f"{label}: {detail}"[:300]
        stamped += 1
        print(f"  ✓ {aid} {row.get('source_id')}: {label}")
    if stamped:
        tmp = data_file.with_name(data_file.name + ".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(data_file)
    print(f"marked {stamped} article(s) as withdrawn, refused {refused}")
    return refused


def is_alert(result: dict) -> bool:
    return any(f in ALERT_FINDINGS for f in result.get("findings") or [])


def run_audit(per_source: int = PER_SOURCE) -> tuple[list, int]:
    import logging
    import fetch_content as fc

    rows = [json.loads(l) for l in DATA_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    configured = {s["id"] for s in json.loads(SOURCES_FILE.read_text())["sources"]}
    root = logging.getLogger()
    for h in list(root.handlers):          # the audit must not write the pipeline's logs
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    results = []
    for article in sample_articles(rows, configured, per_source):
        try:
            results.append(audit_article(article, fc.content_fetcher_for(article)))
        except Exception as exc:
            results.append({"source_id": article.get("source_id"), "id": article.get("id"),
                            "title": article.get("title"), "url": article.get("url"),
                            "findings": ["now_failing"], "label": "fetch_error",
                            "detail": f"audit raised {type(exc).__name__}: {exc}"[:300]})
        time.sleep(0.5)
    mark_site_wide_losses(results)
    return results, len({r["source_id"] for r in results})


def render_email(results: list, sources_audited: int) -> str:
    esc = html.escape
    alerts = [r for r in results if is_alert(r)]
    info = [r for r in results if not is_alert(r) and r.get("findings")]
    clean_sources = sorted({r["source_id"] for r in results} - {r["source_id"] for r in alerts + info})

    def rows(items, color):
        out = ""
        for r in items:
            desc = "；".join(FINDING_LABELS.get(f, f) for f in r["findings"])
            label = (f'<br><code>{esc(r["label"])}</code> {esc(failure_labels.CONTENT_FAILURE_LABELS.get(r["label"], ""))}'
                     f' <span style="color:#586069">{esc(r.get("detail") or "")}</span>') if r.get("label") else ""
            old = "已确认的基线" if r.get("compared_with") == "baseline" else "库里"
            hint = ""
            if "body_shrunk" in r["findings"] and r.get("new_in_old") is not None:
                hint = f'；本次正文有 {r["new_in_old"]:.0%} 在旧正文里'
                if r["new_in_old"] >= 0.95 and r.get("compared_with") != "baseline":
                    hint += (f'（多半是旧存档混了页面杂物；打开网页核对正文完整后运行 '
                             f'<code>--accept {esc(r.get("id") or "")}</code>）')
            size = (f'<br><span style="color:#586069;font-size:11px">{old} {r.get("stored_chars", "?")} 字 → '
                    f'本次 {r.get("new_chars", "?")} 字{hint}</span>')
            out += (f'<tr><td style="padding:6px;font-weight:bold;color:{color};vertical-align:top">{esc(r["source_id"])}</td>'
                    f'<td style="padding:6px"><a href="{esc(r.get("url") or "")}">{esc(r.get("title") or "")}</a><br>'
                    f'{esc(desc)}{label}{size}</td></tr>')
        return out

    parts = [
        '<h2 style="margin:0">GMIA 每周正文体检</h2>',
        f'<p style="color:#586069;margin:4px 0">{datetime.now(BJT):%Y-%m-%d %H:%M BJT} · 抽查 {len(results)} 篇 · '
        f'{sources_audited} 个网站 · 需要处理 <b>{len(alerts)}</b> 篇</p>',
        '<p style="font-size:12px;color:#586069">做法：每个网站抽最近几篇已存正文的文章，用今天的抓取程序重抓，与库里存的比较。</p>',
    ]
    if alerts:
        parts.append('<h3 style="color:#cf222e">⚠️ 需要处理（抓取方式变了 / 抓不到了 / 正文变短 / 内容对不上）</h3>'
                     f'<table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff5f5">{rows(alerts, "#cf222e")}</table>')
    if info:
        parts.append('<h3 style="color:#9a6700">ℹ️ 仅记录（页面已删除 / 正文变长）</h3>'
                     f'<table style="width:100%;border-collapse:collapse;font-size:13px;background:#f6f8fa">{rows(info, "#9a6700")}</table>')
    parts.append(f'<h3 style="color:#1a7f37">✅ 抽查全部正常的网站（{len(clean_sources)}）</h3>'
                 f'<p style="font-size:12px;color:#586069">{esc(" · ".join(clean_sources))}</p>')
    parts.append('<p style="color:#8b949e;font-size:11px">标签说明与案例：docs/content-failure-casebook.md · '
                 '完整报告：logs/content-audit/</p>')
    return '<html><body style="font-family:-apple-system,sans-serif;padding:20px;max-width:720px">' + "".join(parts) + "</body></html>"


def send_email(html_body: str, subject: str, to: str | None = None) -> bool:
    spec = importlib.util.spec_from_file_location("gmia_fetcher_health", Path(__file__).resolve().parent / "gmia-fetcher-health.py")
    health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(health)
    return health.send_email(html_body, subject, to=to)


def main() -> int:
    parser = argparse.ArgumentParser(description="GMIA weekly content audit")
    parser.add_argument("--per-source", type=int, default=PER_SOURCE)
    parser.add_argument("--email", action="store_true", help="email the report when there is something to fix")
    parser.add_argument("--test-email", metavar="ADDRESS", help="always email the report to ADDRESS only")
    parser.add_argument("--report-dir", default=str(REPORT_DIR))
    parser.add_argument("--accept", nargs="+", metavar="ID",
                        help="after checking the page, save today's fetch as these articles' baseline")
    parser.add_argument("--mark-gone", nargs="+", metavar="ID",
                        help="stamp url_status=gone on articles whose original the publisher removed")
    args = parser.parse_args()

    if args.mark_gone:
        import logging
        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
        return 1 if mark_gone(args.mark_gone) else 0

    if args.accept:
        import logging
        import fetch_content as fc

        for h in list(logging.getLogger().handlers):
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
        rows = {r["id"]: r for r in (json.loads(l) for l in DATA_FILE.read_text(encoding="utf-8").splitlines() if l.strip())}
        failed = 0
        for aid in args.accept:
            if aid not in rows:
                print(f"  ✗ {aid}: no such article"); failed += 1; continue
            ok, why = accept_article(rows[aid], fc.content_fetcher_for(rows[aid]))
            print(f"  {'✓' if ok else '✗'} {aid} {rows[aid].get('source_id')}: {why}")
            failed += not ok
        return 1 if failed else 0

    results, sources_audited = run_audit(args.per_source)
    alerts = [r for r in results if is_alert(r)]
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(BJT).strftime("%Y%m%d-%H%M")
    (report_dir / f"content-audit-{stamp}.json").write_text(json.dumps(
        {"at": datetime.now(BJT).isoformat(timespec="seconds"), "sources_audited": sources_audited,
         "alerts": len(alerts), "results": results}, ensure_ascii=False, indent=1))
    print(f"content audit: {len(results)} articles, {sources_audited} sources, {len(alerts)} alert(s)")
    for r in alerts:
        print(f"  ⚠️ {r['source_id']:26s} {','.join(r['findings'])} {r.get('label') or ''} | {r.get('title', '')[:60]}")

    subject = (f"GMIA 每周正文体检：{len(alerts)} 篇需要处理" if alerts else "GMIA 每周正文体检：全部正常")
    ok = True
    if args.test_email:
        ok = send_email(render_email(results, sources_audited), f"[测试] {subject}", to=args.test_email)
    elif args.email and alerts:
        ok = send_email(render_email(results, sources_audited), subject)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
