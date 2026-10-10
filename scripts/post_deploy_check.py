#!/usr/bin/env python3
"""Temporary post-deployment check for the 2026-10-10 stage-4 merge (ee80263).

Runs after the nightly pipeline (cron 04:20 BJT) and answers one question:
did tonight's run, on the merged code, do what the merge promised? Each
check is PASS / WARN / FAIL; any FAIL exits 1 so cron-wrapper alerts. A
summary mail goes out every run. After CLEAN_NIGHTS_TO_CLOSE consecutive
nights without a FAIL it mails "stable, loop closed", records that, and from
then on does nothing -- the cron line is then removed by hand (the script
does not edit crontab).

    python3 scripts/post_deploy_check.py --init-baseline   # once, right after the merge
    python3 scripts/post_deploy_check.py --email           # nightly (cron)
    python3 scripts/post_deploy_check.py                   # print only

Delete this script and its cron line once the loop is closed.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from html import escape
from pathlib import Path

BJT = timezone(timedelta(hours=8))
REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "articles.jsonl"
PIPELINE_LOG = Path.home() / "logs" / "gmia.log"
PAGE = Path("/var/www/overview/hedge-fund-research.html")
BASELINE = REPO / "logs" / "post-deploy-baseline.json"
STATE = REPO / "logs" / "post-deploy-state.json"
ENV_FILE = Path.home() / ".stock-monitor.env"
MERGE = "ee80263"
CLEAN_NIGHTS_TO_CLOSE = 3
NO_REUSED_URLS = ("de-shaw", "capital-group")       # a second row at a stored url is a duplicate
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def read_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def make_baseline(rows: list[dict]) -> dict:
    """What tonight must not change: dates written on 10-10 (page / listing) and
    the stored urls of the sources the page-date fill touches."""
    return {
        "merge": MERGE,
        "taken_at": datetime.now(BJT).isoformat(timespec="seconds"),
        "dates": {r["id"]: [r.get("date"), r.get("date_listed"), r.get("date_basis")]
                  for r in rows if r.get("date_basis") in ("page", "listing")},
        "urls": {sid: sorted({r.get("url") for r in rows if r.get("source_id") == sid})
                 for sid in NO_REUSED_URLS},
    }


def _last_pipeline_line(log_text: str) -> tuple[datetime, str] | None:
    found = None
    for m in re.finditer(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)\] (Pipeline (?:complete|FAILED).*)$",
                         log_text, flags=re.M):
        found = (datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc), m.group(2))
    return found


def _tonight_log(log_text: str, since: datetime) -> str:
    """The pipeline log from the last run start on (cron-wrapper and the run
    print ISO timestamps; take everything after the newest 'Starting' marker or
    the last 4000 lines)."""
    lines = log_text.split("\n")
    starts = [i for i, ln in enumerate(lines) if "Pipeline starting" in ln or "=== GMIA pipeline" in ln]
    return "\n".join(lines[starts[-1]:] if starts else lines[-4000:])


def check_pipeline(log_text: str, now: datetime) -> tuple[str, str, str]:
    last = _last_pipeline_line(log_text)
    if last is None:
        return "pipeline", FAIL, "no 'Pipeline complete/FAILED' line in the log"
    when, line = last
    age_h = (now - when).total_seconds() / 3600
    if age_h > 20:
        return "pipeline", FAIL, f"last run ended {age_h:.0f} h ago ({line})"
    if line.startswith("Pipeline complete"):
        return "pipeline", PASS, f"all stages OK ({when.astimezone(BJT):%m-%d %H:%M} BJT)"
    return "pipeline", FAIL, line


def check_page(page: Path, now: datetime) -> tuple[str, str, str]:
    if not page.exists():
        return "live page", FAIL, f"{page} missing"
    html = page.read_text(encoding="utf-8")
    m = re.search(r'data-built="(\d{4}-\d\d-\d\d)"', html)
    today = now.astimezone(BJT).strftime("%Y-%m-%d")
    if not m or m.group(1) != today:
        return "live page", FAIL, f"built {m.group(1) if m else '?'}, expected {today} (pre-check held it back?)"
    gz = page.with_name(page.name + ".gz")
    try:
        same = hashlib.md5(gzip.decompress(gz.read_bytes())).digest() == hashlib.md5(html.encode("utf-8")).digest()
    except OSError as exc:
        return "live page", FAIL, f"gz unreadable: {exc}"
    if not same:
        return "live page", FAIL, ".gz is not this page"
    stats = dict(re.findall(r'data-stat="([a-z-]+)">(\d+)<', html))
    return "live page", PASS, f"built today; {stats}"


def check_stage3(log_tonight: str, rows: list[dict]) -> tuple[str, str, str]:
    failing = sum(1 for r in rows if int(r.get("analysis_failures") or 0) > 0)
    given_up = [r["id"] for r in rows if int(r.get("analysis_failures") or 0) >= 3 and not r.get("summarized")]
    marked = sum(1 for r in rows if r.get("analysis_unanswered_at"))
    alarms = [k for k in ("TOTAL ANALYSIS OUTAGE", "STAGE STOPPED", "GAVE UP") if k in log_tonight]
    detail = f"rows with failures {failing}, given up {len(given_up)}, unanswered-marked {marked}"
    if alarms:
        return "stage 3", WARN, detail + f"; tonight: {', '.join(alarms)}"
    return "stage 3", PASS, detail


def check_stage2(log_tonight: str) -> tuple[str, str, str]:
    dated = len(re.findall(r"Dated from the article page", log_tonight))
    m = re.search(r"re-tried after a code change: (\d+)", log_tonight)
    return "stage 2", PASS, f"dated from the page tonight: {dated}; re-tried after the code change: {m.group(1) if m else '?'}"


def check_data(rows: list[dict], baseline: dict, log_tonight: str) -> tuple[str, str, str]:
    by_id = {r["id"]: r for r in rows}
    problems = []
    for rid, want in baseline.get("dates", {}).items():
        r = by_id.get(rid)
        if r is None:
            problems.append(f"{rid} missing")
        elif [r.get("date"), r.get("date_listed"), r.get("date_basis")] != want and not r.get("analysis_label") == "duplicate_body":
            problems.append(f"{rid} date changed {want} -> {[r.get('date'), r.get('date_listed'), r.get('date_basis')]}")
    for sid in NO_REUSED_URLS:
        seen: dict[str, int] = {}
        for r in rows:
            if r.get("source_id") == sid:
                seen[r.get("url")] = seen.get(r.get("url"), 0) + 1
        dups = [u for u, n in seen.items() if n > 1]
        if dups:
            problems.append(f"{sid}: {len(dups)} url(s) stored twice, e.g. {dups[0]}")
    for sid in NO_REUSED_URLS:
        if re.search(rf"LISTING_WENT_BACKWARDS: {re.escape(sid)}\b", log_tonight):
            problems.append(f"{sid}: listing refused as gone backwards")
    if problems:
        return "data", FAIL, "; ".join(problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
    new = {sid: len({r.get("url") for r in rows if r.get("source_id") == sid}) - len(baseline["urls"].get(sid, []))
           for sid in NO_REUSED_URLS}
    return "data", PASS, f"{len(baseline.get('dates', {}))} dates unchanged; no duplicates; new rows {new}"


def check_ci(run=subprocess.run) -> tuple[str, str, str]:
    try:
        r = run(["gh", "run", "list", "-L", "1", "--branch", "main", "--workflow", "test.yml",
                 "--json", "status,conclusion,headSha"], capture_output=True, text=True, timeout=60, cwd=REPO)
        latest = json.loads(r.stdout)[0]
    except Exception as exc:
        return "CI", WARN, f"could not read GitHub Actions: {type(exc).__name__}"
    sha = latest.get("headSha", "")[:7]
    if latest.get("status") != "completed":
        return "CI", WARN, f"{sha} still {latest.get('status')}"
    if latest.get("conclusion") == "success":
        return "CI", PASS, f"{sha} success"
    return "CI", FAIL, f"{sha} {latest.get('conclusion')}"


def advance(state: dict, checks: list[tuple[str, str, str]], today: str) -> dict:
    """Count consecutive nights without a FAIL; one record per night."""
    if state.get("last_night") == today:
        return state                                   # a rerun the same night does not count twice
    clean = not any(st == FAIL for _, st, _ in checks)
    state = dict(state, last_night=today,
                 consecutive_clean=(state.get("consecutive_clean", 0) + 1) if clean else 0)
    state.setdefault("history", []).append({"night": today, "clean": clean,
                                            "fails": [n for n, st, _ in checks if st == FAIL]})
    if state["consecutive_clean"] >= CLEAN_NIGHTS_TO_CLOSE:
        state["closed"] = today
    return state


def render(checks, state, night: str) -> tuple[str, str]:
    fails = [n for n, st, _ in checks if st == FAIL]
    if state.get("closed") == night:
        subject = f"GMIA 上线核对：连续 {CLEAN_NIGHTS_TO_CLOSE} 晚通过，本次上线已稳定（闭环结束）"
    elif fails:
        subject = f"GMIA 上线核对 {night}：{len(fails)} 项失败（{', '.join(fails)}）"
    else:
        subject = f"GMIA 上线核对 {night}：全部通过（连续 {state.get('consecutive_clean', 0)}/{CLEAN_NIGHTS_TO_CLOSE} 晚）"
    color = {PASS: "#1a7f37", WARN: "#9a6700", FAIL: "#cf222e"}
    rows = "".join(f"<tr><td>{escape(n)}</td><td style='color:{color[st]};font-weight:600'>{st}</td>"
                   f"<td>{escape(d)}</td></tr>" for n, st, d in checks)
    body = (f"<html><body style='font-family:sans-serif'><h3>{escape(subject)}</h3>"
            f"<p>合并 {MERGE} 后的夜跑核对（临时任务；闭环结束后删除 cron 行与本脚本）。</p>"
            f"<table cellpadding='6' style='border-collapse:collapse'>{rows}</table></body></html>")
    return subject, body


def send(subject: str, body: str) -> bool:
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    user, pw, to = env.get("SMTP_USER"), env.get("SMTP_PASS"), env.get("MAIL_TO")
    if not (user and pw and to):
        print("WARNING: SMTP settings missing; mail not sent")
        return False
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"], msg["From"], msg["To"], msg["MIME-Version"] = subject, user, to, "1.0"
    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.sendmail(user, [to], msg.as_string())
        return True
    except (OSError, smtplib.SMTPException) as exc:
        print(f"WARNING: mail failed: {type(exc).__name__}: {exc}")
        return False


def run_checks(now: datetime, *, data=DATA, log=PIPELINE_LOG, page=PAGE, baseline_path=BASELINE,
               ci=check_ci) -> list[tuple[str, str, str]]:
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    tonight = _tonight_log(log_text, now)
    rows = read_rows(data)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return [check_pipeline(log_text, now), check_page(page, now), check_stage2(tonight),
            check_stage3(tonight, rows), check_data(rows, baseline, tonight), ci()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--init-baseline", action="store_true")
    ap.add_argument("--email", action="store_true")
    args = ap.parse_args(argv)
    if args.init_baseline:
        BASELINE.write_text(json.dumps(make_baseline(read_rows(DATA)), ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"baseline written: {BASELINE}")
        return 0
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if state.get("closed"):
        print(f"loop closed on {state['closed']}; remove the cron line and this script")
        return 0
    now = datetime.now(timezone.utc)
    night = now.astimezone(BJT).strftime("%Y-%m-%d")
    checks = run_checks(now)
    state = advance(state, checks, night)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    subject, body = render(checks, state, night)
    for n, st, d in checks:
        print(f"{st:4}  {n:10}  {d}")
    print(subject)
    if args.email:
        send(subject, body)
    return 1 if any(st == FAIL for _, st, _ in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
