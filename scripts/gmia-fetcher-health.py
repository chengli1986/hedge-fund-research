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
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = LOGS_DIR / "gmia-fetcher-health.json"
ENV_FILE = Path.home() / ".stock-monitor.env"

# Validated-candidate URL probe (--include-validated): catches the 2026-05-08
# ares-management failure mode where status flipped to "validated" but the
# saved research_url was 404 — no test or daily cron noticed for 2 days.
CANDIDATE_PROBE_TIMEOUT_S = 15
CANDIDATE_PROBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CANDIDATE_SHELL_HTML_THRESHOLD = 5000  # below this is probably an error page

WARN_ALERT_THRESHOLD = 3        # send WARN email after this many consecutive WARNs

# Match fetch_content.process_articles' terminal_statuses set — pipeline accepts
# metadata_only (used by ARK Invest's RSS-fallback path on restricted articles)
# as a successful terminal state, so the health probe must too.
TERMINAL_OK_STATUSES = {"ok", "metadata_only"}

# Transient errors get one retry with this back-off before being reported as FAIL.
TRANSIENT_EXC_NAMES = {"TimeoutError", "ReadTimeout", "ConnectionError", "ConnectTimeout"}
RETRY_SLEEP_S = 5

# Content probe walks the top N most-recent articles, passing on the first that
# yields ≥MIN_CONTENT_LENGTH chars. Sites like Apollo intermix podcast/video
# preview cards (which the per-source content fetcher correctly filters as
# "too short") with full articles; probing only articles[0] confused legitimate
# filtering with selector regression. 2026-05-13 incident.
CONTENT_PROBE_TOP_N = 3

# Staleness thresholds: how old can the most-recent article be before we WARN?
# Set generous so known silent periods (Bridgewater monthly, AQR ~quarterly) don't
# trip on normal cadence. Beyond these, the site is publishing materially less than
# its own stated frequency — worth a heads-up.
FREQ_TO_STALE_DAYS = {
    "daily": 14,
    "weekly": 30,
    "biweekly": 45,
    "monthly": 90,
    "quarterly": 240,
    "annual": 540,
    "yearly": 540,
}
DEFAULT_STALE_DAYS = 120  # source missing or unknown frequency


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


def _parse_article_date(raw: str | None) -> datetime | None:
    """Parse YYYY-MM-DD-style date strings produced by fetch_articles. Returns None
    on unparseable input — caller treats that as 'no parsed date' (already a WARN)."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 4], fmt).replace(tzinfo=BJT)
        except ValueError:
            continue
    # Last resort: ISO 8601 with timezone
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(BJT)
    except (ValueError, TypeError):
        return None


def _stale_threshold_days(source: dict) -> int:
    freq = (source.get("frequency") or "").strip().lower()
    return FREQ_TO_STALE_DAYS.get(freq, DEFAULT_STALE_DAYS)


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

    # Staleness/date is always reported relative to articles[0] — the genuinely
    # most-recent article. The content probe may succeed on a later article
    # (e.g. when articles[0] is a podcast preview card filtered by min-length),
    # but that does NOT redefine which article is "most recent".
    result["most_recent_date"] = articles[0].get("date")

    # Step 2: content probe — write into a private tmp dir to avoid touching
    # production content/. Patch fetch_content.CONTENT_DIR for the call only.
    # Probe up to CONTENT_PROBE_TOP_N most-recent articles, pass on the first
    # that yields ≥MIN_CONTENT_LENGTH chars with a TERMINAL_OK status.
    original_content_dir = fetch_content.CONTENT_DIR
    chars = 0
    content_attempts: list[dict] = []
    content_success = False
    transient_exc_seen: Exception | None = None
    all_failures_transient = True  # only true if every attempt raised transient
    try:
        with tempfile.TemporaryDirectory(prefix="gmia-health-") as td:
            fetch_content.CONTENT_DIR = Path(td)
            for idx, article in enumerate(articles[:CONTENT_PROBE_TOP_N]):
                probe_article = dict(article)
                probe_article["id"] = f"healthprobe_{sid}_{idx}"
                attempt: dict = {"index": idx, "url": article.get("url", "")}
                try:
                    outcome = content_fetcher(probe_article)
                except Exception as exc:
                    attempt["reason"] = (
                        f"raised {type(exc).__name__}: {str(exc)[:120]}"
                    )
                    if _is_transient(exc):
                        transient_exc_seen = exc
                    else:
                        all_failures_transient = False
                    content_attempts.append(attempt)
                    continue

                if outcome is None:
                    attempt["reason"] = "returned None (selector regression or HTTP error)"
                    all_failures_transient = False
                    content_attempts.append(attempt)
                    continue

                path, status = outcome
                if status not in TERMINAL_OK_STATUSES:
                    attempt["reason"] = f"status={status!r}"
                    all_failures_transient = False
                    content_attempts.append(attempt)
                    continue

                try:
                    this_chars = len(path.read_text(encoding="utf-8"))
                except Exception:
                    this_chars = 0
                if this_chars < fetch_content.MIN_CONTENT_LENGTH:
                    attempt["reason"] = (
                        f"too short: {this_chars} chars (threshold "
                        f"{fetch_content.MIN_CONTENT_LENGTH})"
                    )
                    all_failures_transient = False
                    content_attempts.append(attempt)
                    continue

                attempt["reason"] = f"ok ({this_chars} chars)"
                content_attempts.append(attempt)
                chars = this_chars
                result["content_chars"] = chars
                result["content_status"] = status
                result["content_probe_index"] = idx
                content_success = True
                break
    finally:
        fetch_content.CONTENT_DIR = original_content_dir

    if not content_success:
        n_tried = len(content_attempts)
        # Surface each attempt's reason for diagnostics. Distinct reason texts
        # let on-call distinguish "selector broke for all 3" from "all 3 short"
        # (the latter is a content-mix signal, not a regression).
        summary = "; ".join(
            f"#{a['index']}={a['reason']}" for a in content_attempts
        )
        result["status"] = "FAIL"
        result["reason"] = (
            f"all top {n_tried} articles failed content probe: {summary}"
        )
        if transient_exc_seen is not None and all_failures_transient:
            result["transient_exc"] = transient_exc_seen
        return result

    # Step 3: date probe (warn-only)
    if result["most_recent_date"] is None:
        result["status"] = "WARN"
        result["reason"] = "most recent article has no parsed date"
        return result

    # Step 4: staleness vs declared frequency. Catches the "site stopped publishing
    # but old article index still serves" failure mode — fetcher returns articles
    # so step 1+2 pass, but the newest is months past the cadence threshold.
    parsed = _parse_article_date(result["most_recent_date"])
    if parsed is not None:
        threshold = _stale_threshold_days(source)
        age_days = (datetime.now(BJT).date() - parsed.date()).days
        result["most_recent_age_days"] = age_days
        result["stale_threshold_days"] = threshold
        if age_days > threshold:
            freq_label = source.get("frequency") or "unknown frequency"
            result["status"] = "WARN"
            result["reason"] = (
                f"stale: most recent article {age_days}d old "
                f"(frequency={freq_label}, threshold {threshold}d)"
            )

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
Probes per source: (1) fetch_articles ≥1 article · (2) fetch_content ≥{__import__("fetch_content").MIN_CONTENT_LENGTH} chars (tries top {CONTENT_PROBE_TOP_N}, passes on first OK) · (3) date parse OK<br>
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


# ── validated-candidate URL liveness (--include-validated) ───────────────────


def load_validated_candidates() -> list[dict]:
    """Load candidates whose status is 'validated' — i.e. ready for trial.

    These ARE NOT production sources (no fetcher registered), so the regular
    probe_source() pipeline won't reach them. The probe below is intentionally
    lightweight: just a GET to confirm the saved research_url still returns
    a real-looking page.
    """
    if not CANDIDATES_FILE.exists():
        return []
    data = json.loads(CANDIDATES_FILE.read_text())
    return [c for c in data if c.get("status") == "validated"]


def probe_candidate_url(candidate: dict) -> dict:
    """HTTP GET the candidate's research_url, return liveness verdict.

    Returns a dict with:
      status: "OK" | "WARN" | "FAIL"
      http_code: int or None
      reason: human-readable
      content_size: bytes
      elapsed_ms: int
    """
    import httpx  # local import — only needed for this code path

    url = (candidate.get("research_url") or "").strip()
    if not url:
        return {"status": "FAIL", "http_code": None, "content_size": 0,
                "reason": "candidate has no research_url", "elapsed_ms": 0}

    started = time.monotonic()
    try:
        with httpx.Client(
            headers={"User-Agent": CANDIDATE_PROBE_UA},
            timeout=CANDIDATE_PROBE_TIMEOUT_S,
            follow_redirects=True,
        ) as client:
            resp = client.get(url)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            size = len(resp.content)
            if resp.status_code >= 400:
                return {"status": "FAIL", "http_code": resp.status_code,
                        "content_size": size, "elapsed_ms": elapsed_ms,
                        "reason": f"HTTP {resp.status_code}"}
            if size < CANDIDATE_SHELL_HTML_THRESHOLD:
                return {"status": "WARN", "http_code": resp.status_code,
                        "content_size": size, "elapsed_ms": elapsed_ms,
                        "reason": f"thin body ({size}b < {CANDIDATE_SHELL_HTML_THRESHOLD}b) "
                                  f"— likely shell HTML or error page"}
            return {"status": "OK", "http_code": resp.status_code,
                    "content_size": size, "elapsed_ms": elapsed_ms,
                    "reason": "ok"}
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {"status": "FAIL", "http_code": None, "content_size": 0,
                "elapsed_ms": elapsed_ms,
                "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}


def print_candidate_report(per_candidate: dict[str, dict]) -> None:
    """Print a section for validated-candidate liveness probes."""
    if not per_candidate:
        return
    n_fail = sum(1 for r in per_candidate.values() if r["status"] == "FAIL")
    n_warn = sum(1 for r in per_candidate.values() if r["status"] == "WARN")
    n_ok = sum(1 for r in per_candidate.values() if r["status"] == "OK")
    print()
    print("─" * 72)
    print(f"Validated-candidate URL liveness — {len(per_candidate)} probed "
          f"({n_ok} OK · {n_warn} WARN · {n_fail} FAIL)")
    print("─" * 72)
    for cid, r in sorted(per_candidate.items()):
        icon = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}[r["status"]]
        code = r.get("http_code") or "-"
        size = r.get("content_size", 0)
        print(f"  {icon} {cid:30s} HTTP {code!s:>3s}  {size:>8d}b  {r['reason']}")


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="GMIA fetcher health check")
    parser.add_argument("--email", action="store_true",
                        help="send HTML alert email per alert logic")
    parser.add_argument("--source", default=None,
                        help="probe a single source by id (no state mutation)")
    parser.add_argument("--dry-run", action="store_true",
                        help="run probes but skip state-write and email")
    parser.add_argument("--include-validated", action="store_true",
                        help="also probe URL liveness of validated candidates "
                             "(catches the 'status=validated but URL is 404' bug)")
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

    # Validated-candidate URL probes (decoupled from production source state /
    # email logic): purely informational, but a FAIL bumps the script's exit
    # code so the cron-wrapper escalates via its standard non-zero alert path.
    per_candidate: dict[str, dict] = {}
    if args.include_validated:
        for c in load_validated_candidates():
            per_candidate[c["id"]] = probe_candidate_url(c)
        print_candidate_report(per_candidate)

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

    candidate_fail = any(r["status"] == "FAIL" for r in per_candidate.values())

    # Exit code: 1 if any production FAIL OR any validated-candidate URL FAIL
    # (cron-wrapper picks this up and emails)
    return 1 if (alerts["failing"] or candidate_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
