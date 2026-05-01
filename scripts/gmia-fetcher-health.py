#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GMIA Fetcher Health Check.

Runs against every source listed in config/sources.json:
  1) fetch_articles.FETCHERS[id](source)  — must not raise; ≥1 article returned
  2) fetch_content.CONTENT_FETCHERS[id](most_recent_article)  — must yield
     ≥MIN_CONTENT_LENGTH chars (catches Referer/selector-class regressions
     such as the GMO 14-day silent failure on 2026-04-16 → 2026-04-30).
  3) Most recent article must have a parsed date (warn-only).

State persists per-source consecutive-fail and consecutive-warn counters so
alerts can be throttled (FAIL alerts every run, WARN alerts only after the
3rd consecutive run, RECOVERED alerts once on the OK transition).

Email is HTML; SMTP creds load from ~/.stock-monitor.env (same pattern as
gmia-trial-manager.py).

CLI:
  python3 gmia-fetcher-health.py            run + write state + console report
  python3 gmia-fetcher-health.py --email    run + write state + send email per
                                            alert logic (FAIL/WARN3+/RECOVERED)
  python3 gmia-fetcher-health.py --source X probe a single source; no state
                                            mutation, no email
  python3 gmia-fetcher-health.py --dry-run  run all sources but skip state
                                            write and email (used for ad-hoc
                                            verification outside cron)
"""

from __future__ import annotations

import argparse
import json
import smtplib
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = LOGS_DIR / "gmia-fetcher-health.json"
ENV_FILE = Path.home() / ".stock-monitor.env"

WARN_ALERT_THRESHOLD = 3        # send WARN email after this many consecutive WARNs

# Match fetch_content.process_articles' terminal_statuses set — pipeline accepts
# metadata_only (used by ARK Invest's RSS-fallback path on restricted articles)
# as a successful terminal state, so the health probe must too.
TERMINAL_OK_STATUSES = {"ok", "metadata_only"}

# Transient errors get one retry with this back-off before being reported as FAIL.
TRANSIENT_EXC_NAMES = {"TimeoutError", "ReadTimeout", "ConnectionError", "ConnectTimeout"}
RETRY_SLEEP_S = 5


# ── shared helpers ───────────────────────────────────────────────────────────

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


def load_sources() -> list[dict]:
    data = json.loads(SOURCES_FILE.read_text())
    return data.get("sources", [])


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_run": None, "sources": {}}


def save_state(state: dict) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def now_iso() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def now_human() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")


# ── per-source probe ─────────────────────────────────────────────────────────

def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in TRANSIENT_EXC_NAMES:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "temporarily unavailable" in msg


def _probe_once(source: dict) -> dict:
    """Single attempt of all 3 probes (no retry)."""
    import sys as _sys
    if str(BASE_DIR) not in _sys.path:
        _sys.path.insert(0, str(BASE_DIR))
    import fetch_articles
    import fetch_content

    sid = source["id"]
    result: dict = {
        "status": "OK",
        "reason": "",
        "articles_count": 0,
        "content_chars": 0,
        "most_recent_date": None,
        "transient_exc": None,
    }

    fetcher = fetch_articles.FETCHERS.get(sid)
    if fetcher is None:
        result["status"] = "FAIL"
        result["reason"] = "no fetch_articles handler registered"
        return result

    content_fetcher = fetch_content.CONTENT_FETCHERS.get(sid)
    if content_fetcher is None:
        result["status"] = "FAIL"
        result["reason"] = "no fetch_content handler registered"
        return result

    # Step 1: fetch_articles probe
    try:
        articles = fetcher(source)
    except Exception as exc:
        result["status"] = "FAIL"
        result["reason"] = f"fetch_articles raised {type(exc).__name__}: {str(exc)[:120]}"
        result["transient_exc"] = exc if _is_transient(exc) else None
        return result

    result["articles_count"] = len(articles)
    if not articles:
        result["status"] = "WARN"
        result["reason"] = "fetch_articles returned 0 articles"
        return result

    most_recent = articles[0]
    result["most_recent_date"] = most_recent.get("date")

    # Step 2: content probe — write into a private tmp dir to avoid touching
    # production content/. Patch fetch_content.CONTENT_DIR for the call only.
    original_content_dir = fetch_content.CONTENT_DIR
    chars = 0
    try:
        with tempfile.TemporaryDirectory(prefix="gmia-health-") as td:
            fetch_content.CONTENT_DIR = Path(td)
            probe_article = dict(most_recent)
            probe_article["id"] = f"healthprobe_{sid}"
            try:
                outcome = content_fetcher(probe_article)
            except Exception as exc:
                result["status"] = "FAIL"
                result["reason"] = f"fetch_content raised {type(exc).__name__}: {str(exc)[:120]}"
                result["transient_exc"] = exc if _is_transient(exc) else None
                return result

            if outcome is None:
                result["status"] = "FAIL"
                result["reason"] = "fetch_content returned None (selector regression or HTTP error)"
                return result

            path, status = outcome
            if status not in TERMINAL_OK_STATUSES:
                result["status"] = "FAIL"
                result["reason"] = f"fetch_content status={status!r}"
                return result

            try:
                chars = len(path.read_text(encoding="utf-8"))
            except Exception:
                chars = 0
            result["content_chars"] = chars
            result["content_status"] = status
    finally:
        fetch_content.CONTENT_DIR = original_content_dir

    if chars < fetch_content.MIN_CONTENT_LENGTH:
        result["status"] = "FAIL"
        result["reason"] = (
            f"content too short: {chars} chars (threshold {fetch_content.MIN_CONTENT_LENGTH})"
        )
        return result

    # Step 3: date probe (warn-only)
    if result["most_recent_date"] is None:
        result["status"] = "WARN"
        result["reason"] = "most recent article has no parsed date"

    return result


def probe_source(source: dict) -> dict:
    """Run probes against one source with one transient-error retry."""
    started = time.monotonic()
    result = _probe_once(source)
    if result["status"] == "FAIL" and result.get("transient_exc") is not None:
        time.sleep(RETRY_SLEEP_S)
        retry = _probe_once(source)
        if retry["status"] != "FAIL":
            retry["reason"] = (retry["reason"] or "recovered after 1 retry").strip()
            result = retry
        else:
            # Surface that we already retried, so emails reflect persistence.
            retry["reason"] = f"{retry['reason']} (1 retry attempted)"
            result = retry
    result.pop("transient_exc", None)
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return result


# ── state-driven alert classification ────────────────────────────────────────

def classify_alerts(per_source: dict[str, dict], prev_state: dict) -> dict:
    """Decide which sources need alert emails.

    A source is "alerting" in a given run when status is FAIL, OR status is
    WARN with consecutive_warns >= WARN_ALERT_THRESHOLD. "Recovered" fires
    on the transition from alerting to non-alerting (incl. FAIL → WARN-below-
    threshold, so the user gets confirmation when a fix moves a source from
    daily-alerting to silent).

    Returns:
      {
        "failing":   [(sid, result, prev_status)] — FAIL this run
        "warning":   [(sid, result, consecutive_warns)] — WARN-streak this run
        "recovered": [(sid, result, prev_status)] — was alerting, now silent
        "healthy":   [(sid, result)] — silent this run AND silent before
      }
    """
    failing: list[tuple] = []
    warning: list[tuple] = []
    recovered: list[tuple] = []
    healthy: list[tuple] = []

    prev = prev_state.get("sources", {}) if isinstance(prev_state, dict) else {}

    def _was_alerting(prev_record: dict) -> bool:
        if not prev_record:
            return False
        s = prev_record.get("status")
        if s == "FAIL":
            return True
        if s == "WARN" and prev_record.get("consecutive_warns", 0) >= WARN_ALERT_THRESHOLD:
            return True
        return False

    for sid, result in per_source.items():
        prev_record = prev.get(sid, {})
        prev_status = prev_record.get("status")
        new_status = result["status"]

        if new_status == "FAIL":
            failing.append((sid, result, prev_status))
        elif new_status == "WARN":
            consecutive_warns = prev_record.get("consecutive_warns", 0) + 1
            if consecutive_warns >= WARN_ALERT_THRESHOLD:
                warning.append((sid, result, consecutive_warns))
            elif _was_alerting(prev_record):
                # WARN-below-threshold doesn't alert, but transitioning here
                # from a previously-alerting state is a real recovery signal.
                recovered.append((sid, result, prev_status))
        else:  # OK
            if _was_alerting(prev_record):
                recovered.append((sid, result, prev_status))
            else:
                healthy.append((sid, result))

    return {
        "failing": failing,
        "warning": warning,
        "recovered": recovered,
        "healthy": healthy,
    }


def merge_into_state(prev_state: dict, per_source: dict[str, dict]) -> dict:
    """Build the next state dict from probe results + previous state."""
    prev_sources = prev_state.get("sources", {}) if isinstance(prev_state, dict) else {}
    next_sources: dict[str, dict] = {}
    iso = now_iso()

    for sid, result in per_source.items():
        prev = prev_sources.get(sid, {})
        record = {
            "status": result["status"],
            "last_articles_count": result["articles_count"],
            "last_content_chars": result["content_chars"],
            "last_most_recent_date": result["most_recent_date"],
            "last_elapsed_ms": result["elapsed_ms"],
        }
        if result["status"] == "OK":
            record["consecutive_fails"] = 0
            record["consecutive_warns"] = 0
            record["last_ok_at"] = iso
            record["last_fail_at"] = prev.get("last_fail_at")
            record["last_failure_reason"] = ""
        elif result["status"] == "FAIL":
            record["consecutive_fails"] = prev.get("consecutive_fails", 0) + 1
            record["consecutive_warns"] = 0
            record["last_ok_at"] = prev.get("last_ok_at")
            record["last_fail_at"] = iso
            record["last_failure_reason"] = result["reason"]
        else:  # WARN
            record["consecutive_fails"] = 0
            record["consecutive_warns"] = prev.get("consecutive_warns", 0) + 1
            record["last_ok_at"] = prev.get("last_ok_at")
            record["last_fail_at"] = prev.get("last_fail_at")
            record["last_failure_reason"] = result["reason"]
        next_sources[sid] = record

    return {"last_run": iso, "sources": next_sources}


# ── reporting ────────────────────────────────────────────────────────────────

def print_console_report(per_source: dict[str, dict], total_runtime_s: float) -> None:
    print(f"\n=== GMIA Fetcher Health — {now_human()} ===")
    print(f"Sources probed: {len(per_source)}    Total runtime: {total_runtime_s:.1f}s\n")

    fail_rows = [(sid, r) for sid, r in per_source.items() if r["status"] == "FAIL"]
    warn_rows = [(sid, r) for sid, r in per_source.items() if r["status"] == "WARN"]
    ok_rows = [(sid, r) for sid, r in per_source.items() if r["status"] == "OK"]

    if fail_rows:
        print(f"🚨 FAILING ({len(fail_rows)}):")
        for sid, r in fail_rows:
            print(f"  {sid:25} {r['reason']}")
        print()
    if warn_rows:
        print(f"⚠️  WARNINGS ({len(warn_rows)}):")
        for sid, r in warn_rows:
            print(f"  {sid:25} {r['reason']}")
        print()
    print(f"✅ HEALTHY ({len(ok_rows)}):")
    for sid, r in ok_rows:
        print(
            f"  {sid:25} {r['articles_count']} articles · "
            f"{r['content_chars']} chars · {r['elapsed_ms']}ms"
        )


def render_html_email(
    per_source: dict[str, dict],
    alerts: dict,
    state: dict,
    total_runtime_s: float,
) -> str:
    """HTML body with same visual idiom as gmia-trial-manager email."""
    sources_state = state.get("sources", {})

    def section_table(title: str, color: str, rows_html: str) -> str:
        return (
            f'<h3 style="margin:14px 0 6px;color:{color}">{title}</h3>'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;'
            f'background:#f6f8fa;border-radius:6px;">'
            f'{rows_html}</table>'
        )

    fail_rows = ""
    for sid, r, _prev_status in alerts["failing"]:
        rec = sources_state.get(sid, {})
        last_ok = rec.get("last_ok_at") or "never"
        if last_ok != "never":
            last_ok = last_ok[:10]
        consecutive = rec.get("consecutive_fails", 1)
        fail_rows += (
            f'<tr><td style="padding:8px;font-weight:bold;color:#cf222e">{sid}</td>'
            f'<td style="padding:8px">{r["reason"]}<br>'
            f'<span style="color:#586069;font-size:11px">'
            f'last OK: {last_ok}  ·  consecutive fails: {consecutive}</span></td></tr>'
        )

    warn_rows = ""
    for sid, r, consecutive in alerts["warning"]:
        rec = sources_state.get(sid, {})
        last_ok = rec.get("last_ok_at") or "never"
        if last_ok != "never":
            last_ok = last_ok[:10]
        warn_rows += (
            f'<tr><td style="padding:8px;font-weight:bold;color:#9a6700">{sid}</td>'
            f'<td style="padding:8px">{r["reason"]}<br>'
            f'<span style="color:#586069;font-size:11px">'
            f'last OK: {last_ok}  ·  persisted {consecutive} runs</span></td></tr>'
        )

    recovered_rows = ""
    for sid, r, prev_status in alerts["recovered"]:
        recovered_rows += (
            f'<tr><td style="padding:8px;font-weight:bold;color:#1a7f37">{sid}</td>'
            f'<td style="padding:8px">recovered from {prev_status} → OK · '
            f'{r["articles_count"]} articles · {r["content_chars"]} chars</td></tr>'
        )

    healthy_names = sorted(sid for sid, _ in alerts["healthy"])
    healthy_html = (
        '<p style="margin:6px 0;color:#586069;font-size:12px">'
        + " · ".join(healthy_names)
        + "</p>"
    )

    sections = []
    if fail_rows:
        sections.append(section_table(
            f"🚨 FAILING ({len(alerts['failing'])})", "#cf222e", fail_rows))
    if warn_rows:
        sections.append(section_table(
            f"⚠️ WARNINGS ({len(alerts['warning'])}) — persisted ≥{WARN_ALERT_THRESHOLD} runs",
            "#9a6700", warn_rows))
    if recovered_rows:
        sections.append(section_table(
            f"✅ RECOVERED ({len(alerts['recovered'])})", "#1a7f37", recovered_rows))
    sections.append(
        f'<h3 style="margin:14px 0 6px;color:#1a7f37">✅ HEALTHY ({len(alerts["healthy"])})</h3>'
        f'{healthy_html}'
    )

    body_html = "".join(sections)

    return f"""<html><body style="font-family:-apple-system,sans-serif;padding:20px;max-width:680px">
<h2 style="margin:0">GMIA Fetcher Health Check</h2>
<p style="color:#586069;margin:4px 0">{now_human()}  ·  {len(per_source)} sources probed  ·  runtime {total_runtime_s:.1f}s</p>

{body_html}

<p style="color:#8b949e;font-size:11px;margin-top:20px">
Probes per source: (1) fetch_articles ≥1 article · (2) fetch_content ≥{__import__("fetch_content").MIN_CONTENT_LENGTH} chars · (3) date parse OK<br>
State: <code>~/hedge-fund-research/logs/gmia-fetcher-health.json</code>  ·  Cron: 04:30 BJT daily
</p>
</body></html>"""


def send_email(html_body: str, summary_subject: str) -> bool:
    env = load_env()
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    mail_to = env.get("MAIL_TO", "")
    if not smtp_user or not smtp_pass or not mail_to:
        print("WARNING: SMTP not configured (missing SMTP_USER/SMTP_PASS/MAIL_TO)")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = summary_subject
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["MIME-Version"] = "1.0"
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"Health alert email sent to {mail_to}")
        return True
    except Exception as exc:
        print(f"WARNING: health email failed: {exc}")
        return False


def alerts_subject(alerts: dict) -> str:
    parts = []
    if alerts["failing"]:
        ids = ", ".join(sid for sid, _, _ in alerts["failing"][:3])
        more = f" +{len(alerts['failing']) - 3}" if len(alerts["failing"]) > 3 else ""
        parts.append(f"🚨 {len(alerts['failing'])} FAILING ({ids}{more})")
    if alerts["warning"]:
        parts.append(f"⚠️ {len(alerts['warning'])} WARN")
    if alerts["recovered"]:
        parts.append(f"✅ {len(alerts['recovered'])} recovered")
    return f"GMIA fetcher health: {' / '.join(parts)}" if parts else "GMIA fetcher health: all OK"


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="GMIA fetcher health check")
    parser.add_argument("--email", action="store_true",
                        help="send HTML alert email per alert logic")
    parser.add_argument("--source", default=None,
                        help="probe a single source by id (no state mutation)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run probes but skip state-write and email")
    args = parser.parse_args()

    sources = load_sources()
    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
        if not sources:
            print(f"ERROR: source {args.source!r} not in config/sources.json")
            return 2

    started = time.monotonic()
    per_source: dict[str, dict] = {}
    for src in sources:
        sid = src["id"]
        try:
            per_source[sid] = probe_source(src)
        except Exception as exc:
            traceback.print_exc()
            per_source[sid] = {
                "status": "FAIL",
                "reason": f"probe wrapper raised {type(exc).__name__}: {str(exc)[:100]}",
                "articles_count": 0,
                "content_chars": 0,
                "most_recent_date": None,
                "elapsed_ms": 0,
            }

    total_runtime_s = time.monotonic() - started

    print_console_report(per_source, total_runtime_s)

    # Single-source debug mode bypasses state and email entirely.
    if args.source:
        return 0 if all(r["status"] == "OK" for r in per_source.values()) else 1

    prev_state = load_state()
    alerts = classify_alerts(per_source, prev_state)

    if not args.dry_run:
        next_state = merge_into_state(prev_state, per_source)
        save_state(next_state)
    else:
        next_state = merge_into_state(prev_state, per_source)
        print("[dry-run] state file NOT written")

    needs_alert = bool(alerts["failing"] or alerts["warning"] or alerts["recovered"])
    if args.email and needs_alert and not args.dry_run:
        html_body = render_html_email(per_source, alerts, next_state, total_runtime_s)
        send_email(html_body, alerts_subject(alerts))
    elif args.email and not needs_alert:
        print("All sources OK and no recoveries — email suppressed.")

    # Exit code: 1 if any FAIL, 0 otherwise (cron-wrapper picks this up)
    return 1 if alerts["failing"] else 0


if __name__ == "__main__":
    sys.exit(main())
