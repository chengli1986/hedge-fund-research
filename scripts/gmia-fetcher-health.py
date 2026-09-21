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
from collections import Counter
import html
import json
import logging
import os
import smtplib
import statistics
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
TRIAL_STATE_FILE = BASE_DIR / "config" / "trial-state.json"
LOGS_DIR = BASE_DIR / "logs"
STATE_FILE = LOGS_DIR / "gmia-fetcher-health.json"
log = logging.getLogger("gmia-fetcher-health")
# Written by fetch_articles.record_quality_metrics during the 03:45 BJT pipeline,
# read here at 04:30 -- a different measurement, taken 45 minutes earlier, which
# is why it is reported in its own section rather than mixed into probe results.
INSPECTION_STATE_FILE = BASE_DIR / "config" / "inspection_state.json"
ENV_FILE = Path.home() / ".stock-monitor.env"

# Validated-candidate URL probe (--include-validated): catches the 2026-05-08
# ares-management failure mode where status flipped to "visitable" but the
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


def _preserve_unreadable_state(exc: Exception) -> None:
    """Copy an unreadable state file aside before it is replaced. Never raises."""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup = STATE_FILE.with_name(f"{STATE_FILE.name}.corrupt-{stamp}")
        backup.write_bytes(STATE_FILE.read_bytes())
        log.error("STATE FILE UNREADABLE: %s could not be parsed (%s); kept a copy at %s. "
                  "Every source's alert streak restarts from this run.",
                  STATE_FILE, exc, backup.name)
    except OSError as copy_exc:
        log.error("STATE FILE UNREADABLE: %s could not be parsed (%s), and the copy failed "
                  "too (%s)", STATE_FILE, exc, copy_exc)


def load_state() -> dict:
    """The previous run's per-source status. An unreadable file is kept aside
    as evidence and read as empty, so the health check still runs and still
    emails: aborting here meant no FAIL/WARN mail until someone deleted the
    file by hand (audit D3, same family as B4)."""
    empty = {"last_run": None, "sources": {}}
    if not STATE_FILE.exists():
        return empty
    try:
        state = json.loads(STATE_FILE.read_text())
        if not isinstance(state, dict):
            raise ValueError(f"state is {type(state).__name__}, not an object")
        return state
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as exc:
        _preserve_unreadable_state(exc)
        return empty


def save_state(state: dict) -> None:
    """Write via a temp file and os.replace, so a save that dies midway
    leaves the previous file intact instead of a truncated one."""
    LOGS_DIR.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(json.dumps(state, indent=2, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(STATE_FILE))
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


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


def _observed_cadence_note(date_strings: list[str | None]) -> str:
    """Summarise the publishing cadence seen in THIS fetch, for appending to a
    staleness WARN. A bursty publisher (e.g. Matthews Asia) shows a small median
    gap with a much larger max gap — surfacing both lets on-call tell a normal
    quiet stretch (declared frequency too tight) from a genuine multi-month
    freeze, without opening the site. Returns '' if fewer than 2 dates parse.

    Note: the fetch only carries the most-recent N articles (max_articles), so
    this reflects recent cadence, not the source's full history — it is a
    diagnostic hint, not an authoritative reclassification."""
    parsed = [d.date() for s in date_strings if (d := _parse_article_date(s)) is not None]
    unique = sorted(set(parsed), reverse=True)
    if len(unique) < 2:
        return ""
    gaps = [(unique[i] - unique[i + 1]).days for i in range(len(unique) - 1)]
    median_gap = statistics.median(gaps)
    return (
        f"observed cadence: {len(parsed)} dated articles, "
        f"median gap {median_gap:.0f}d, max gap {max(gaps)}d"
    )


# ── per-source probe ─────────────────────────────────────────────────────────

def _is_transient(exc: Exception) -> bool:
    name = type(exc).__name__
    if name in TRANSIENT_EXC_NAMES:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "temporarily unavailable" in msg


# Path fragments that mark a country/role/eligibility interstitial rather than
# the content itself. MetLife IM's is /disclaimer/; the same pattern shows up as
# consent walls, investor-type attestations and terms gates across the fleet.
_GATE_PATH_MARKERS = (
    "disclaimer", "consent", "attestation", "eligibility",
    "investor-type", "terms-of-use", "accept-terms",
)


def _diagnose_zero_articles(source: dict) -> str:
    """Explain a 0-article result by reporting where the configured URL lands.

    Added 2026-08-03 after the metlife-im outage: the fetcher returned 0
    articles for 3 consecutive runs and the WARN said only "returned 0
    articles", so the root cause (MIM retired investments.metlife.com — the URL
    301s to a new host, then 302s to a country/role disclaimer) took a full
    investigation to find, when one request would have shown it.

    Returns a short suffix for the WARN reason, or "" when the URL resolves to
    itself (i.e. the listing genuinely is empty and the redirect angle is a dead
    end). Never raises: a diagnostic must not turn a WARN into a failed run.
    """
    url = (source.get("url") or "").strip()
    if not url:
        return ""

    import requests
    from urllib.parse import urlparse

    try:
        final = requests.get(url, headers=_DIAGNOSTIC_HEADERS, timeout=20,
                             allow_redirects=True).url
    except Exception as exc:
        # Worth reporting: a hard failure on the configured URL is itself the
        # explanation (DNS gone, TLS broken, connection refused).
        return f" — probing {url} raised {type(exc).__name__}: {str(exc)[:80]}"

    if final == url:
        return ""

    src_host = urlparse(url).netloc.lower()
    final_host = urlparse(final).netloc.lower()
    final_path = urlparse(final).path.lower()

    if final_host and final_host != src_host:
        return (f" — configured URL now redirects off-host to {final} "
                f"(site moved? expected host {src_host})")
    if any(marker in final_path for marker in _GATE_PATH_MARKERS):
        return f" — configured URL now lands on a gate page: {final}"
    return f" — configured URL now redirects to {final}"


_DIAGNOSTIC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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
        result["reason"] = "fetch_articles returned 0 articles" + _diagnose_zero_articles(source)
        return result

    # Staleness is reported relative to the most-recent date across ALL returned
    # articles. Some sources (e.g. Robeco) pin an older featured article at the
    # top of the page, so articles[0] may be older than later entries.
    # Content probe continues to use articles[0] onward (page order is fine for probing).
    all_dates = [a.get("date") for a in articles if a.get("date")]
    result["most_recent_date"] = max(all_dates) if all_dates else None

    # Step 2: content probe — write into a private tmp dir to avoid touching
    # production content/. Patch fetch_content.CONTENT_DIR for the call only.
    # Probe up to CONTENT_PROBE_TOP_N most-recent articles (or a per-source
    # override via "content_probe_top_n" in sources.json, for feeds that
    # regularly interleave several short teaser/video pages ahead of the next
    # full-length piece — e.g. Matthews Asia, confirmed 2026-07-04 after 2
    # consecutive daily FAILs: articles[0..2] are always short, article[3] is
    # the first full-length one), pass on the first that yields
    # ≥MIN_CONTENT_LENGTH chars with a TERMINAL_OK status.
    probe_top_n = source.get("content_probe_top_n", CONTENT_PROBE_TOP_N)
    original_content_dir = fetch_content.CONTENT_DIR
    chars = 0
    content_attempts: list[dict] = []
    content_success = False
    extraction_note = ""
    transient_exc_seen: Exception | None = None
    all_failures_transient = True  # only true if every attempt raised transient
    try:
        with tempfile.TemporaryDirectory(prefix="gmia-health-") as td:
            fetch_content.CONTENT_DIR = Path(td)
            for idx, article in enumerate(articles[:probe_top_n]):
                probe_article = dict(article)
                probe_article["id"] = f"healthprobe_{sid}_{idx}"
                attempt: dict = {"index": idx, "url": article.get("url", "")}
                # Per attempt, so a teaser that hit a fallback before the
                # article that succeeded cannot taint the verdict.
                fetch_content.drain_extraction_paths()
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

                off_primary = sorted({p for p in fetch_content.drain_extraction_paths()
                                      if p != "primary"})
                if off_primary:
                    extraction_note = (
                        f"content selector matched nothing; text came from "
                        f"{', '.join(off_primary)} (may include navigation, "
                        f"cookie banners or related-article lists)"
                    )
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

    # Step 3: date probe (warn-only). Skip for sources that intentionally omit
    # publish dates (no_publish_dates=True in sources.json) — Capital Group is
    # an example: the listing page has no date fields by design.
    if result["most_recent_date"] is None and not source.get("no_publish_dates", False):
        result["status"] = "WARN"
        result["reason"] = "most recent article has no parsed date"
        return _add_extraction_note(result, extraction_note)

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
            reason = (
                f"stale: most recent article {age_days}d old "
                f"(frequency={freq_label}, threshold {threshold}d)"
            )
            cadence = _observed_cadence_note(all_dates)
            if cadence:
                reason += f" — {cadence}"
            result["reason"] = reason

    return _add_extraction_note(result, extraction_note)


def _add_extraction_note(result: dict, note: str) -> dict:
    """WARN on a fallback extraction, keeping any reason already set."""
    if note:
        result["status"] = "FAIL" if result["status"] == "FAIL" else "WARN"
        result["reason"] = f"{result['reason']}; {note}" if result["reason"] else note
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


# The pipeline runs daily at 03:45 BJT, so any record the last run touched is
# well under a day old.  36h leaves margin for a late run without letting a
# frozen record alert forever.
ZERO_FETCH_FRESH_HOURS = 36


def pipeline_zero_fetches(state_path=None) -> list[tuple[str, dict]]:
    """Sources whose most recent pipeline fetch returned no articles.

    The pipeline already records this and check_anomalies already flags two in a
    row, but the only destination was a log.warning nobody reads.  A single zero
    is worth reporting on its own: the 04:30 probe runs 45 minutes after the
    fetch, so a site that was slow at 03:45 and fine at 04:30 leaves no other
    trace -- which is exactly how acadian-asset's 2026-09-06 miss went unseen.

    Two filters keep a frozen record from alerting forever.  config/
    inspection_state.json is append-only in practice -- record_quality_metrics
    writes and nothing prunes -- so it still holds pgim and pinebridge months
    after they were retired.  A source retired BECAUSE it stopped producing is
    precisely the one whose last count is 0, and without these it would alert
    every day with no way to clear it but hand-editing the file.  So: the id
    must still be configured, and the record must be one the last run actually
    refreshed.

    Never raises: this is extra reporting bolted onto a health check, and it
    must not be able to fail the thing it reports on.
    """
    path = INSPECTION_STATE_FILE if state_path is None else Path(state_path)
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return []
        configured = {s["id"] for s in load_sources()}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ZERO_FETCH_FRESH_HOURS)
        out = []
        for sid, rec in state.items():
            if sid not in configured or not isinstance(rec, dict):
                continue
            if rec.get("last_article_count") != 0:
                continue
            try:
                seen = datetime.fromisoformat(str(rec.get("last_inspected_at")))
            except (TypeError, ValueError):
                continue          # unreadable stamp: stay quiet rather than guess
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen >= cutoff:
                out.append((sid, rec))
        return sorted(out, key=lambda kv: kv[0])
    except Exception:
        return []


# One stray URL is already an outlier: all 42 configured sources sit at 0
# (measured 2026-09-16), and the log-only alert's own threshold of >3 meant
# three strays a night stayed invisible for as long as they kept happening.
INTAKE_MISMATCH_MIN = 1
INTAKE_GATED_RATIO = 0.5


def pipeline_intake_anomalies(state_path=None) -> list[tuple[str, list[str]]]:
    """Per-source intake problems the pipeline recorded but told nobody about.

    fetch_articles.check_anomalies writes these to log.warning and stops --
    the same last-hop whisper that hid acadian-asset's zero fetch. Two signals
    are real and reach the email here:

      · listing URLs that are not on the source's declared host (fetcher drift,
        or a config edit left half-done);
      · a listing that is mostly locked/members-only.

    The filters are the ones pipeline_zero_fetches argues for: the id must
    still be configured, and the record must be one the last run refreshed --
    config/inspection_state.json is append-only, so a frozen record would
    otherwise alert every day with no way to clear it.

    Never raises: extra reporting must not fail the check it rides on.
    """
    path = INSPECTION_STATE_FILE if state_path is None else Path(state_path)
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return []
        configured = {s["id"] for s in load_sources()}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ZERO_FETCH_FRESH_HOURS)
        out = []
        for sid, rec in sorted(state.items()):
            if sid not in configured or not isinstance(rec, dict):
                continue
            try:
                seen = datetime.fromisoformat(str(rec.get("last_inspected_at")))
            except (TypeError, ValueError):
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen < cutoff:
                continue
            notes = []
            mismatches = rec.get("last_mismatch_count") or 0
            if mismatches >= INTAKE_MISMATCH_MIN:
                plural = "" if mismatches == 1 else "s"
                notes.append(f"{mismatches} listing URL{plural} were not on the declared host "
                             "— the fetcher may have drifted, or a config edit is half-done")
            gated = rec.get("last_gated_ratio") or 0
            if gated > INTAKE_GATED_RATIO:
                notes.append(f"{gated:.0%} of the listing is locked or members-only")
            if notes:
                out.append((sid, notes))
        return out
    except Exception:
        return []


ARTICLES_FILE = BASE_DIR / "data" / "articles.jsonl"
DECLINE_SAMPLE_REASONS = 2


ENTRYPOINT_VALIDATION_FILE = BASE_DIR / "logs" / "entrypoint-validation.json"
ENTRYPOINT_FRESH_DAYS = 10          # the check runs weekly; 10 days allows one miss


def entrypoint_problems(path=None) -> list:
    """Sources whose entrypoint the weekly validation found not ok.

    run_pipeline.sh writes the validation result there; until 2026-09-20 it
    only echoed its findings, and cron-wrapper.sh alerts on the exit code
    alone, so the one check that can notice an entrypoint going bad had no
    destination (audit A2). A file nothing has refreshed for longer than
    ENTRYPOINT_FRESH_DAYS is ignored, for the reason pipeline_zero_fetches
    gives: a frozen verdict would alert every day with no way to clear it.
    Never raises.
    """
    try:
        f = Path(path) if path else ENTRYPOINT_VALIDATION_FILE
        if not f.exists():
            return []
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        if age > timedelta(days=ENTRYPOINT_FRESH_DAYS):
            return []
        data = json.loads(f.read_text())
        if not isinstance(data, dict):
            return []
        if data.get("_error"):
            return [("(validation failed)", str(data["_error"])[:300])]
        out = []
        for sid, entries in sorted(data.items()):
            if not isinstance(entries, list):
                continue
            bad = [e for e in entries if isinstance(e, dict) and e.get("status") != "ok"]
            if bad:
                out.append((sid, "; ".join(f"{e.get('status')} at {e.get('url')}" for e in bad[:3])))
        return out
    except Exception:
        return []


def corrupt_state_backups(path=None) -> list:
    """Copies fetch_articles kept of an unreadable inspection_state.json.

    Each one means a night where every source's consecutive_zero_count
    restarted, so silence alerts were delayed until the streaks rebuilt
    (audit B4). They stay on disk until a human has looked; reporting them is
    how anyone learns they exist. Never raises.
    """
    try:
        state_file = Path(path) if path else INSPECTION_STATE_FILE
        return sorted(p.name for p in state_file.parent.glob(f"{state_file.name}.corrupt-*"))
    except Exception:
        return []


def store_damage(path=None) -> int:
    """Rows in the article store that will not parse.

    jsonl_store reports these with log.error, and audit finding B8 is exactly
    that a log line is not a destination: a row torn by a kill takes the row
    after it with it, and nothing told anyone. Never raises.
    """
    try:
        import jsonl_store
        _, damaged = jsonl_store.read_rows(path or ARTICLES_FILE)
        return damaged
    except Exception:
        return 0


def recent_analysis_declines(data_path=None, since=None) -> list[tuple[str, list[dict]]]:
    """Articles stage 3 declined to summarise in the last run, by source.

    analyze_articles records analysis_status "insufficient_content" when the
    model declines or check_grounding rejects its summary (e10d923). A cluster
    on one source means stage 2 is saving the wrong text -- on 2026-09-13
    lazard-am had 27 (series intro + disclaimer), oaktree a broker-dealer
    disclosure, gmo an employee tax notice. Largest cluster first.

    Configured sources only. `since` is the previous health run's last_run:
    only declines recorded after it are reported, so each is reported once.
    d905f85 used the ZERO_FETCH_FRESH_HOURS window alone, and a decline
    recorded mid-day falls inside two consecutive 04:30 runs (09-13's 78 were
    34h old at the second). With no usable `since` -- first run, unreadable
    state -- the window applies. A long gap between health runs reports
    everything since the last one. Never raises.
    """
    path = ARTICLES_FILE if data_path is None else Path(data_path)
    try:
        configured = {s["id"] for s in load_sources()}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=ZERO_FETCH_FRESH_HOURS)
        try:
            previous = datetime.fromisoformat(str(since))
            if previous.tzinfo is not None:
                cutoff = previous
        except ValueError:
            pass
        groups: dict[str, list[dict]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("analysis_status") != "insufficient_content":
                continue
            if row.get("source_id") not in configured:
                continue
            ts = row.get("analysis_checked_at")
            if not ts:
                continue
            checked = datetime.fromisoformat(str(ts))
            if checked.tzinfo is None or checked <= cutoff:
                continue
            groups.setdefault(row["source_id"], []).append(row)
        return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    except Exception:
        return []


def _decline_line(rows: list[dict]) -> str:
    reasons = list(dict.fromkeys(str(r.get("analysis_reason") or "") for r in rows))
    shown = " | ".join(reasons[:DECLINE_SAMPLE_REASONS])
    more = f" (+{len(reasons) - DECLINE_SAMPLE_REASONS} other reasons)" if len(reasons) > DECLINE_SAMPLE_REASONS else ""
    n = len(rows)
    return f"{n} article{'s' if n != 1 else ''} not summarised: {shown}{more}"


QUALITY_WINDOW_DAYS = 7


def load_quality(days: int = QUALITY_WINDOW_DAYS, since=None) -> dict | None:
    """failure_stats.quality_summary over production data; None if unavailable.

    Never raises: extra reporting must not be able to fail the health check.
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "failure_stats", Path(__file__).resolve().parent / "failure_stats.py")
        fs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fs)
        rows = fs._rows(ARTICLES_FILE)
        configured = {s["id"] for s in load_sources()}
        return fs.quality_summary(rows, configured, fs.LEDGER_FILE, days, since=since)
    except Exception as exc:
        print(f"WARNING: content-quality summary unavailable: {exc}")
        return None


def _quality_section(quality: dict) -> str:
    import failure_labels
    esc = html.escape
    parts = []
    i = quality["intake"]
    parts.append(
        f'<p style="margin:6px 0;font-size:13px">最近 {quality["days"]} 天新入库 <b>{i["total"]}</b> 篇：'
        f'有正文 <b>{i["with_body"]}</b>（其中仅简介 {i["metadata_only"]}）· '
        f'未取得正文 <b>{i["without_body"]}</b> · AI 拒绝摘要 <b>{i["declined"]}</b></p>')
    if quality["alerts"]:
        rows = "".join(
            f'<tr><td style="padding:6px;font-weight:bold;color:#cf222e">{esc(sid)}</td>'
            f'<td style="padding:6px"><code>{esc(label)}</code> — '
            f'{esc((failure_labels.CONTENT_FAILURE_LABELS if kind == "content" else failure_labels.ANALYSIS_DECLINE_LABELS).get(label, ""))}'
            f'</td></tr>'
            for kind, sid, label in quality["alerts"])
        parts.append('<p style="margin:10px 0 4px;color:#cf222e;font-weight:bold">'
                     '⚠️ 本周首次出现的问题（通常意味着网站改版、被拦截或抓错文件）</p>'
                     f'<table style="width:100%;border-collapse:collapse;font-size:13px;background:#fff5f5">{rows}</table>')

    def by_label(counter, descriptions, title):
        if not counter:
            return f'<p style="margin:10px 0 4px;font-weight:bold">{title}</p><p style="margin:0;color:#586069">（无）</p>'
        totals = Counter()
        for (_, label), n in counter.items():
            totals[label] += n
        body = ""
        for label, n in totals.most_common():
            sources = " · ".join(f"{esc(sid)}×{m}" for (sid, lab), m in
                                 sorted(counter.items(), key=lambda kv: -kv[1]) if lab == label)
            body += (f'<tr><td style="padding:6px;white-space:nowrap"><code>{esc(label)}</code></td>'
                     f'<td style="padding:6px;text-align:right"><b>{n}</b></td>'
                     f'<td style="padding:6px">{esc(descriptions.get(label, ""))}<br>'
                     f'<span style="color:#586069;font-size:11px">{sources}</span></td></tr>')
        return (f'<p style="margin:10px 0 4px;font-weight:bold">{title}</p>'
                f'<table style="width:100%;border-collapse:collapse;font-size:13px;background:#f6f8fa">{body}</table>')

    parts.append(by_label(quality["content"], failure_labels.CONTENT_FAILURE_LABELS, "目前未取得正文（按原因）"))
    parts.append(by_label(quality["declines"], failure_labels.ANALYSIS_DECLINE_LABELS, "AI 拒绝摘要（按原因）"))
    parts.append('<p style="margin:6px 0;color:#8b949e;font-size:11px">标签说明与真实案例：'
                 '<code>docs/content-failure-casebook.md</code> · 明细：<code>python3 scripts/failure_stats.py</code></p>')
    return ('<h3 style="margin:14px 0 6px;color:#0969da">📋 正文获取质量（Content quality）</h3>' + "".join(parts))


def pipeline_did_not_run(state_path=None) -> bool:
    """True when the last pipeline run refreshed nothing at all.

    The freshness filter in pipeline_zero_fetches is right for a retired source
    and wrong for a dead pipeline: if gmia-daily stops running, every zero ages
    out and the check goes quiet exactly when it should be loudest.  A source
    that fetched 0 and was then never fetched again would alert for one more
    night and then never again -- the same "detected but never delivered"
    defect this whole feature exists to fix, inverted.

    gmia_liveness_audit.check_fetch does not cover it: that is whole-pipeline
    (newest article across all sources) on a 4-day threshold, and 4 days is
    already the largest gap in this repo's fetch history, so it cannot be
    tightened to reach back here.

    A missing state file counts as "did not run": in an established deployment
    it is written every night, so its absence means the zero-fetch check itself
    is dead, which is the silence this exists to prevent.
    """
    path = INSPECTION_STATE_FILE if state_path is None else Path(state_path)
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            return True
        configured = {s["id"] for s in load_sources()}
    except Exception:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ZERO_FETCH_FRESH_HOURS)
    for sid, rec in state.items():
        if sid not in configured or not isinstance(rec, dict):
            continue
        try:
            seen = datetime.fromisoformat(str(rec.get("last_inspected_at")))
        except (TypeError, ValueError):
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen >= cutoff:
            return False
    return True


def should_email(alerts: dict, zero_fetches: list, pipeline_stale: bool = False,
                 declines: list | None = None, quality: dict | None = None,
                 intake: list | None = None, damaged_rows: int = 0,
                 corrupt_state: list | None = None, entrypoints: list | None = None) -> bool:
    """Whether this run has anything worth sending.

    zero_fetches is part of the condition, not just part of the body: the email
    was suppressed whenever every probe passed, which is the precise case a
    silent zero-article fetch produces.
    """
    return bool(alerts["failing"] or alerts["warning"] or alerts["recovered"]
                or zero_fetches or pipeline_stale or declines or intake or damaged_rows
                or corrupt_state or entrypoints
                or (quality is not None and quality.get("alerts")))


def _zero_fetch_line(sid: str, rec: dict) -> str:
    consecutive = rec.get("consecutive_zero_count", 1)
    when = (rec.get("last_inspected_at") or "")[:19] or "unknown time"
    run = "run" if consecutive == 1 else "runs"
    refusal = rec.get("last_refusal")
    if refusal:
        # A zero WE caused. Without this it reads exactly like the site being
        # down, which is the pair the 2026-09-21 audit could not tell apart.
        return (f"0 articles because the batch was refused ({refusal}) · "
                f"{consecutive} consecutive {run} · {when}")
    return f"fetched 0 articles in the last pipeline run ({consecutive} consecutive {run}) · {when}"


# ── reporting ────────────────────────────────────────────────────────────────

def print_console_report(per_source: dict[str, dict], total_runtime_s: float,
                         zero_fetches: list | None = None, intake: list | None = None) -> None:
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
    if intake:
        print(f"🔀 INTAKE ANOMALIES ({len(intake)}):")
        for sid, notes in intake:
            for note in notes:
                print(f"  {sid:26s} {note}")
    if zero_fetches:
        print(f"📉 PIPELINE FETCHED NOTHING ({len(zero_fetches)}):")
        for sid, rec in zero_fetches:
            print(f"  {sid:25} {_zero_fetch_line(sid, rec)}")
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
    zero_fetches: list | None = None,
    pipeline_stale: bool = False,
    declines: list | None = None,
    quality: dict | None = None,
    intake: list | None = None,
    damaged_rows: int = 0,
    corrupt_state: list | None = None,
    entrypoints: list | None = None,
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
    if pipeline_stale:
        # The body must say what the subject shouts. When this is the only send
        # condition -- the case the check exists for -- the email used to carry
        # a subject reading "⛔ pipeline recorded nothing in 36h" above a body
        # containing nothing but the HEALTHY table. alerts_subject's docstring
        # already made this argument for the subject line; it was not carried
        # across to the body, which is the same last-hop whisper one commit
        # after it was fixed for zero_fetches.
        sections.append(section_table(
            "⛔ PIPELINE RECORDED NOTHING", "#cf222e",
            f'<tr><td style="padding:8px">No configured source was refreshed in the '
            f'last {ZERO_FETCH_FRESH_HOURS}h, so zero-fetch reporting is blind until '
            f'the pipeline runs again. Check the gmia-daily cron.</td></tr>'))
    if zero_fetches:
        zero_rows = "".join(
            f'<tr><td style="padding:8px;font-weight:bold;color:#9a6700">{sid}</td>'
            f'<td style="padding:8px">{_zero_fetch_line(sid, rec)}</td></tr>'
            for sid, rec in zero_fetches
        )
        sections.append(section_table(
            f"📉 PIPELINE FETCHED NOTHING ({len(zero_fetches)})", "#9a6700", zero_rows))
    if entrypoints:
        ep_rows = "".join(
            f'<tr><td style="padding:8px;font-weight:bold;color:#9a6700">{html.escape(sid)}</td>'
            f'<td style="padding:8px">{html.escape(detail)}</td></tr>'
            for sid, detail in entrypoints)
        sections.append(section_table(
            f"🚪 ENTRYPOINTS NOT OK ({len(entrypoints)})", "#9a6700",
            ep_rows +
            '<tr><td style="padding:8px">From the weekly entrypoint validation '
            '(logs/entrypoint-validation.json). A listing URL that stopped working is a source '
            'about to go quiet.</td></tr>'))
    if corrupt_state:
        state_rows = "".join(
            f'<tr><td style="padding:8px">{html.escape(name)}</td></tr>' for name in corrupt_state)
        sections.append(section_table(
            f"🗃️ STATE FILE WAS UNREADABLE ({len(corrupt_state)})", "#cf222e",
            state_rows +
            '<tr><td style="padding:8px">config/inspection_state.json could not be parsed, so every '
            "source's consecutive_zero_count restarted that night and silence alerts were delayed. "
            'The copies hold the old counters; delete them once checked.</td></tr>'))
    if damaged_rows:
        sections.append(section_table(
            f"🧨 DAMAGED ROWS ({damaged_rows})", "#cf222e",
            f'<tr><td style="padding:8px">{damaged_rows} line(s) in data/articles.jsonl will not '
            f'parse. A row torn by an interrupted write takes the row after it out of the id '
            f'index too, so both are missing from the site and will be re-fetched as duplicates. '
            f'See DAMAGED ROW lines in logs/fetch.log.</td></tr>'))
    if intake:
        intake_rows = "".join(
            f'<tr><td style="padding:8px;font-weight:bold;color:#9a6700">{html.escape(sid)}</td>'
            f'<td style="padding:8px">{"<br>".join(html.escape(n) for n in notes)}</td></tr>'
            for sid, notes in intake
        )
        sections.append(section_table(
            f"🔀 INTAKE ANOMALIES ({len(intake)})", "#9a6700", intake_rows))
    if declines:
        # analysis_reason is model output: escape it like any untrusted text.
        decline_rows = "".join(
            f'<tr><td style="padding:8px;font-weight:bold;color:#9a6700">{html.escape(sid)}</td>'
            f'<td style="padding:8px">{html.escape(_decline_line(rows))}</td></tr>'
            for sid, rows in declines
        )
        total = sum(len(rows) for _, rows in declines)
        sections.append(section_table(
            f"🤖 NOT SUMMARISED — text was not an article ({total})", "#9a6700", decline_rows))
    if quality is not None:
        sections.append(_quality_section(quality))
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


def send_email(html_body: str, summary_subject: str, to: str | None = None) -> bool:
    env = load_env()
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    mail_to = to or env.get("MAIL_TO", "")
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


def alerts_subject(alerts: dict, zero_fetches: list | None = None,
                   pipeline_stale: bool = False, declines: list | None = None,
                   quality: dict | None = None, intake: list | None = None,
                   damaged_rows: int = 0, corrupt_state: list | None = None,
                   entrypoints: list | None = None) -> str:
    """Subject line. Must name every condition that caused the send.

    zero_fetches is a send condition on its own, and it is the ONLY one that
    fires while failing/warning/recovered are all empty -- so without it here,
    the one email this feature exists to produce arrives titled "all OK",
    which is the whispering the feature was built to stop, reproduced at the
    last hop.
    """
    parts = []
    if alerts["failing"]:
        ids = ", ".join(sid for sid, _, _ in alerts["failing"][:3])
        more = f" +{len(alerts['failing']) - 3}" if len(alerts["failing"]) > 3 else ""
        parts.append(f"🚨 {len(alerts['failing'])} FAILING ({ids}{more})")
    if alerts["warning"]:
        parts.append(f"⚠️ {len(alerts['warning'])} WARN")
    if alerts["recovered"]:
        parts.append(f"✅ {len(alerts['recovered'])} recovered")
    if zero_fetches:
        ids = ", ".join(sid for sid, _ in zero_fetches[:3])
        more = f" +{len(zero_fetches) - 3}" if len(zero_fetches) > 3 else ""
        parts.append(f"📉 {len(zero_fetches)} fetched nothing ({ids}{more})")
    if pipeline_stale:
        parts.append(f"⛔ pipeline recorded nothing in {ZERO_FETCH_FRESH_HOURS}h")
    if entrypoints:
        ids = ", ".join(sid for sid, _ in entrypoints[:3])
        parts.append(f"🚪 {len(entrypoints)} entrypoint(s) not ok ({ids})")
    if corrupt_state:
        parts.append(f"🗃️ state file was unreadable ({len(corrupt_state)} copy/copies)")
    if damaged_rows:
        parts.append(f"🧨 {damaged_rows} damaged row(s) in articles.jsonl")
    if intake:
        ids = ", ".join(sid for sid, _ in intake[:3])
        more = f" +{len(intake) - 3}" if len(intake) > 3 else ""
        parts.append(f"🔀 {len(intake)} intake anomaly(ies) ({ids}{more})")
    if quality is not None and quality.get("alerts"):
        ids = ", ".join(dict.fromkeys(sid for _, sid, _ in quality["alerts"][:3]))
        parts.append(f"🧩 {len(quality['alerts'])} new content issue(s) ({ids})")
    if declines:
        total = sum(len(rows) for _, rows in declines)
        ids = ", ".join(sid for sid, _ in declines[:3])
        more = f" +{len(declines) - 3}" if len(declines) > 3 else ""
        parts.append(f"🤖 {total} not summarised ({ids}{more})")
    return f"GMIA fetcher health: {' / '.join(parts)}" if parts else "GMIA fetcher health: all OK"


# ── validated-candidate URL liveness (--include-validated) ───────────────────


def _active_trial_ids() -> set[str]:
    """Ids currently in trial per trial-state.json (candidate status stays
    'visitable' for the whole trial — trial-manager only flips it at verdict
    time: promoted/watchlist/inaccessible)."""
    if not TRIAL_STATE_FILE.exists():
        return set()
    state = json.loads(TRIAL_STATE_FILE.read_text())
    return {t["id"] for t in state.get("active_trials", [])}


def load_validated_candidates() -> list[dict]:
    """Load candidates whose status is 'visitable' — i.e. waiting for trial.

    These ARE NOT production sources (no fetcher registered), so the regular
    probe_source() pipeline won't reach them. The probe below is intentionally
    lightweight: just a GET to confirm the saved research_url still returns
    a real-looking page.

    Candidates already in an active trial are excluded: the trial itself
    exercises real fetching daily (including Playwright fallbacks for
    Cloudflare-403 sites like cohen-steers), so a plain-GET liveness probe
    would only re-report blocks the trial pipeline already handles.
    """
    if not CANDIDATES_FILE.exists():
        return []
    data = json.loads(CANDIDATES_FILE.read_text())
    in_trial = _active_trial_ids()
    return [
        c for c in data
        if c.get("status") == "visitable" and c.get("id") not in in_trial
    ]


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
    parser.add_argument("--test-email", metavar="ADDRESS", default=None,
                        help="run the real check, write no state, and always send the email "
                             "to ADDRESS only (subject prefixed [测试])")
    parser.add_argument("--include-validated", action="store_true",
                        help="also probe URL liveness of validated candidates "
                             "(catches the 'status=visitable but URL is 404' bug)")
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

    # Read once, before the --source early return below, so the console report
    # and the email decision see the same list.
    zero_fetches = pipeline_zero_fetches()
    intake = pipeline_intake_anomalies()
    damaged_rows = store_damage()
    # Both state files this script depends on: the fleet's (fetch_articles
    # keeps the copies) and its own (load_state keeps them).
    corrupt_state = sorted(set(corrupt_state_backups()) | set(corrupt_state_backups(STATE_FILE)))
    entrypoints = entrypoint_problems()
    pipeline_stale = pipeline_did_not_run()
    if pipeline_stale:
        print(f"⛔ the last pipeline run recorded nothing in the past "
              f"{ZERO_FETCH_FRESH_HOURS}h — zero-fetch reporting is blind until it runs")
    print_console_report(per_source, total_runtime_s, zero_fetches=zero_fetches, intake=intake)
    # Before this run's state is written: last_run is still the previous run.
    declines = recent_analysis_declines(since=load_state().get("last_run"))
    if declines:
        print(f"🤖 NOT SUMMARISED ({sum(len(r) for _, r in declines)}):")
        for sid, rows in declines:
            print(f"  {sid:25} {_decline_line(rows)}")
    # Alerts only for issues first seen since the previous run (state not yet written).
    quality = load_quality(since=load_state().get("last_run"))
    if quality is not None:
        i = quality["intake"]
        print(f"📋 CONTENT QUALITY (last {quality['days']}d): {i['total']} new, {i['with_body']} with body, "
              f"{i['without_body']} without, {i['declined']} declined; "
              f"{len(quality['alerts'])} first-seen alert(s)")
        for kind, sid, label in quality["alerts"]:
            print(f"  ⚠️ {sid:25} {kind}:{label}")

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

    if not args.dry_run and not args.test_email:
        next_state = merge_into_state(prev_state, per_source)
        save_state(next_state)
    else:
        next_state = merge_into_state(prev_state, per_source)
        print("[dry-run] state file NOT written")

    email_failed = False
    needs_alert = should_email(alerts, zero_fetches, pipeline_stale, declines=declines,
                               quality=quality, intake=intake, damaged_rows=damaged_rows, corrupt_state=corrupt_state, entrypoints=entrypoints)
    if args.test_email:
        html_body = render_html_email(per_source, alerts, next_state, total_runtime_s,
                                      zero_fetches=zero_fetches, pipeline_stale=pipeline_stale,
                                      declines=declines, quality=quality, intake=intake,
                                      damaged_rows=damaged_rows, corrupt_state=corrupt_state, entrypoints=entrypoints)
        subject = alerts_subject(alerts, zero_fetches, pipeline_stale, declines=declines,
                                 quality=quality, intake=intake, damaged_rows=damaged_rows, corrupt_state=corrupt_state, entrypoints=entrypoints)
        email_failed = not send_email(html_body, f"[测试] {subject}", to=args.test_email)
    elif args.email and needs_alert and not args.dry_run:
        html_body = render_html_email(per_source, alerts, next_state, total_runtime_s,
                                      zero_fetches=zero_fetches,
                                      pipeline_stale=pipeline_stale,
                                      declines=declines, quality=quality, intake=intake,
                                      damaged_rows=damaged_rows, corrupt_state=corrupt_state, entrypoints=entrypoints)
        email_failed = not send_email(
            html_body, alerts_subject(alerts, zero_fetches, pipeline_stale, declines=declines,
                                      quality=quality, intake=intake, damaged_rows=damaged_rows, corrupt_state=corrupt_state, entrypoints=entrypoints))
    elif args.email and not needs_alert:
        print("All sources OK and no recoveries — email suppressed.")

    candidate_fail = any(r["status"] == "FAIL" for r in per_candidate.values())

    # Exit code: 1 if any production FAIL OR any validated-candidate URL FAIL
    # (cron-wrapper picks this up and emails)
    # email_failed is in here because send_email returns False on missing SMTP
    # config and on any SMTP exception, and that return used to be discarded:
    # a detected problem whose email never left the box reached nobody, while
    # cron-wrapper saw exit 0 and called the night healthy.
    return 1 if (alerts["failing"] or candidate_fail or email_failed) else 0


if __name__ == "__main__":
    sys.exit(main())
