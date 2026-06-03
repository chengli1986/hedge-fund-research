#!/usr/bin/env python3
"""Email a summary of a fetcher-synthesis session.

The weekly (and fast-path) fetcher-synthesis run auto-generates fetchers for
inaccessible candidate funds but, until now, left its results only in logs and
fetcher-synthesis-history.jsonl — so the only way to learn the outcome was to
open a Claude Code session and ask. This script turns each synthesis session
into a digest email.

Invoked by wrapper-fetcher-synthesis.sh at the end of a run. Data source is the
same time-series log the stats tool reads, filtered to the entries this session
appended (attempted_at >= --since). The agent records per-fund outcome in
fund_candidates.json (synthesis_outcome=success|failed) and sync_synthesis_history.py
reconciles it into the jsonl; we read the jsonl so the email matches the stats.

Email rules (per repo conventions): MIME-Version header, html.escape() on all
free-text, single style attribute per element.
"""
import argparse
import html
import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from pathlib import Path

BJT = timezone(timedelta(hours=8))
REPO_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = REPO_DIR / "logs" / "fetcher-synthesis-history.jsonl"
CANDIDATES_FILE = REPO_DIR / "config" / "fund_candidates.json"


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO timestamp (UTC 'Z' or offset) to an aware datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except ValueError:
        return None


def load_history(history_path: Path) -> list[dict]:
    """Read all non-heartbeat synthesis attempts from the jsonl log."""
    if not history_path.exists():
        return []
    entries = []
    for line in history_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("id") == "_heartbeat":
            continue
        entries.append(e)
    return entries


def session_results(entries: list[dict], since: datetime | None) -> list[dict]:
    """Return synthesis attempts at or after ``since`` (this session's work).

    ``since`` None → return all attempts (caller wants the full history).
    Entries without a parseable attempted_at are excluded when filtering, so a
    malformed row never silently inflates the session summary.
    """
    if since is None:
        return list(entries)
    out = []
    for e in entries:
        dt = _parse_iso(e.get("attempted_at", ""))
        if dt is not None and dt >= since:
            out.append(e)
    return out


def rolling_success_rate(entries: list[dict], window_days: int, now: datetime) -> tuple[int, int]:
    """Return (successes, total) for attempts within the trailing window."""
    cutoff = now - timedelta(days=window_days)
    succ = total = 0
    for e in entries:
        dt = _parse_iso(e.get("attempted_at", ""))
        if dt is None or dt < cutoff:
            continue
        total += 1
        if e.get("outcome") == "success":
            succ += 1
    return succ, total


def _candidate_index(candidates: list[dict]) -> dict[str, dict]:
    return {c["id"]: c for c in candidates}


def _next_step(cand: dict) -> str:
    """Describe what happens next for a successfully-synthesized fund, based on
    its current status (set by the synthesis status-flow in program.md)."""
    status = cand.get("status", "?")
    if status == "promoted":
        return "trial 已通过，fetcher 补齐 → 直接进生产（auto-promote 接线）"
    if status == "visitable":
        return "状态 → visitable，排队重新 trial 验证内容质量"
    return f"状态 = {status}"


def build_html(results: list[dict], targets_count: int, candidates: list[dict],
               entries: list[dict], now: datetime) -> str:
    """Render the synthesis summary email body."""
    idx = _candidate_index(candidates)
    succeeded = [r for r in results if r.get("outcome") == "success"]
    failed = [r for r in results if r.get("outcome") != "success"]

    date_str = now.astimezone(BJT).strftime("%Y-%m-%d %H:%M BJT")
    r30s, r30t = rolling_success_rate(entries, 30, now)
    rate30 = f"{r30s}/{r30t} ({100 * r30s / r30t:.0f}%)" if r30t else "n/a"

    chip = ("display:inline-block;padding:4px 10px;margin:2px;"
            "border-radius:12px;font-size:12px")
    chips = (
        f"<span style='{chip};background:#dafbe1;color:#1a7f37'>{len(succeeded)} 成功</span>"
        f"<span style='{chip};background:#ffebe9;color:#cf222e'>{len(failed)} 失败</span>"
        f"<span style='{chip};background:#ddf4ff;color:#0969da'>{targets_count} 个目标</span>"
        f"<span style='{chip};background:#f6f8fa;color:#57606a'>30d {html.escape(rate30)}</span>"
    )

    def row(r: dict, ok: bool) -> str:
        cand = idx.get(r.get("id", ""), {})
        name = html.escape(r.get("name") or cand.get("name") or r.get("id", "?"))
        quality = html.escape(str(r.get("quality") or cand.get("quality") or "?"))
        if ok:
            commit = html.escape((r.get("commit") or "")[:8])
            detail = html.escape(_next_step(cand))
            third = f"<code>{commit}</code> · {detail}"
        else:
            reason = html.escape((cand.get("notes") or "原因未记录")[:90])
            third = reason
        return (
            f"<tr>"
            f"<td style='padding:6px 10px'>{name}</td>"
            f"<td style='padding:6px 10px'>{quality}</td>"
            f"<td style='padding:6px 10px;font-size:12px'>{third}</td>"
            f"</tr>\n"
        )

    def section(title: str, rows: str, color: str) -> str:
        if not rows:
            return ""
        return (
            f"<h3 style='margin:18px 0 6px;color:{color}'>{title}</h3>"
            f"<table style='width:100%;border-collapse:collapse;font-size:13px;"
            f"border:1px solid #e1e4e8;border-radius:6px'>"
            f"<tr style='background:#f6f8fa'>"
            f"<th style='padding:6px 10px;text-align:left'>Fund</th>"
            f"<th style='padding:6px 10px;text-align:left'>Quality</th>"
            f"<th style='padding:6px 10px;text-align:left'>结果 / 下一步</th></tr>\n"
            f"{rows}</table>"
        )

    succ_rows = "".join(row(r, True) for r in succeeded)
    fail_rows = "".join(row(r, False) for r in failed)

    if not results:
        body = ("<p style='color:#cf222e'>本次 synthesis 启动了 agent（"
                f"{targets_count} 个目标）但未记录任何结果——可能 agent 超时或"
                "中断。请查 <code>~/logs/gmia-fetcher-synthesis.log</code>。</p>")
    else:
        body = (
            section("✅ 成功合成 fetcher", succ_rows, "#1a7f37")
            + section("❌ 合成失败（仍 inaccessible）", fail_rows, "#cf222e")
        )

    return (
        "<html><body style='font-family:-apple-system,sans-serif;padding:20px;max-width:720px'>"
        f"<h2 style='margin:0'>🔧 GMIA Fetcher Synthesis — {date_str}</h2>"
        f"<p style='color:#586069;margin:8px 0 16px'>{chips}</p>"
        f"{body}"
        "<p style='color:#8b949e;font-size:11px;margin-top:24px'>"
        "Fetcher synthesis 为 inaccessible 候选基金自动生成抓取器。成功后："
        "promoted 直接进生产；其余转 visitable 排队重新 trial 验质量。"
        "<br>Source: logs/fetcher-synthesis-history.jsonl · 周日 02:00 BJT + trial-PASS 快速通道触发。"
        "</p></body></html>"
    )


def send_email(html_body: str, subject: str, dry_run: bool = False) -> bool:
    """Send via 163 SMTP using env creds. Returns True on send (or dry-run)."""
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    mail_to = os.environ.get("MAIL_TO", "")

    if dry_run:
        print(html_body)
        return True
    if not smtp_user or not smtp_pass or not mail_to:
        print("WARNING: SMTP not configured (SMTP_USER/SMTP_PASS/MAIL_TO), skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["Message-ID"] = make_msgid()
    msg["MIME-Version"] = "1.0"
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"Synthesis summary email sent to {mail_to}")
        return True
    except Exception as exc:  # noqa: BLE001 — notification failure must not crash wrapper
        print(f"WARNING: synthesis summary email failed: {exc}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Email a fetcher-synthesis session summary")
    ap.add_argument("--since", default="",
                    help="ISO UTC timestamp; only attempts at/after this are in the summary")
    ap.add_argument("--targets-count", type=int, default=0,
                    help="number of targets the synthesis run was given")
    ap.add_argument("--dry-run", action="store_true", help="print HTML, do not send")
    args = ap.parse_args(argv)

    since = _parse_iso(args.since) if args.since else None
    entries = load_history(HISTORY_FILE)
    results = session_results(entries, since)

    # Nothing attempted and no targets → genuinely nothing to report, stay quiet.
    if not results and args.targets_count == 0:
        print("No synthesis results this session and no targets — skipping email")
        return 0

    try:
        candidates = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Candidate file missing/corrupt → still send the digest from history
        # alone (names come through from the jsonl), just without status detail.
        print(f"WARNING: cannot load candidates file: {exc}")
        candidates = []
    now = datetime.now(timezone.utc)
    html_body = build_html(results, args.targets_count, candidates, entries, now)

    n_ok = sum(1 for r in results if r.get("outcome") == "success")
    n_fail = len(results) - n_ok
    subject = (f"GMIA Fetcher Synthesis: {n_ok} 成功 / {n_fail} 失败 "
               f"· {now.astimezone(BJT).strftime('%Y-%m-%d')}")

    send_email(html_body, subject, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
