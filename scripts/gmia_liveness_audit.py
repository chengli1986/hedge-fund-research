#!/usr/bin/env python3
"""Outcome-based liveness audit for the GMIA pipeline.

Exit codes lie: every GMIA cron wrapper can exit 0 while its stage silently did
no real work — fetcher-synthesis self-deadlocked on a double flock for ~5 weeks
and profile-refresh's AUM agent derailed on the global "session start" ritual for
~4 weeks, both behind "Pipeline complete — all stages OK" / exit 0 (2026-07-20
audit). This checks each stage's LAST REAL PRODUCTIVE ACTIVITY from its
authoritative artifact (not its exit code) and scans recent logs for known
silent-bail / derail markers, so the next silent no-op surfaces in days, not
weeks.

Run standalone for a digest:
    python3 scripts/gmia_liveness_audit.py
Alert by email when any stage looks silently stalled (exit 1 on problems):
    python3 scripts/gmia_liveness_audit.py --email

The check logic is deliberately split into pure functions (classify_staleness,
scan_markers, newest_dt, ops_problems) so it is unit-tested without touching the
filesystem; the check_* wrappers do the I/O.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = Path.home() / "logs"

OK, STALE, BAIL = "OK", "STALE", "BAIL"

# Markers that mean "a run fired but produced nothing" — the silent-no-op smell.
# NOTE: a bare "has not been trusted" warning is NOT a bail: candidate-discovery
# logs it every run yet produces correct output. The real derail signals are the
# agent doing the global session-start ritual or stalling on a question that a
# headless `claude --print` can never be answered.
LOCK_BAIL_MARKERS = {
    "lock-bail": r"[Aa]nother .*instance is running",
}
AGENT_DERAIL_MARKERS = {
    "did-daily-log-ritual": r"每日日志回顾|会话开始.*日志|session start routines",
    "stalled-asking-user": r"你想怎么进行|你想怎么继续|请问你想",
}


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested; no I/O)
# ----------------------------------------------------------------------------
def parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, tolerating a trailing 'Z'. Naive -> UTC."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def newest_dt(records: Iterable[dict], key: str) -> Optional[datetime]:
    """Newest parseable timestamp under `key` across records (None if none)."""
    best: Optional[datetime] = None
    for r in records:
        dt = parse_iso(str(r.get(key, "")))
        if dt and (best is None or dt > best):
            best = dt
    return best


def classify_staleness(
    now: datetime, last: Optional[datetime], threshold_days: float
) -> tuple[str, Optional[float]]:
    """OK if last activity is within threshold; STALE if too old or never seen.

    Returns (status, age_days). age_days is None when `last` is None.
    """
    if last is None:
        return STALE, None
    age = (now - last).total_seconds() / 86400.0
    return (STALE if age > threshold_days else OK), age


def scan_markers(text: str, markers: dict[str, str]) -> list[str]:
    """Return labels whose regex is found in text."""
    return [label for label, pat in markers.items() if re.search(pat, text)]


def ops_problems(rows: Iterable[dict], jobs_prefix: str, now: datetime,
                 window_days: float) -> list[dict]:
    """From cron-wrapper ops-status rows, return recent runs that locked out,
    timed out, or exited non-zero for jobs whose name starts with jobs_prefix."""
    out = []
    for r in rows:
        job = str(r.get("job", ""))
        if not job.startswith(jobs_prefix):
            continue
        dt = parse_iso(str(r.get("ts", "")))
        if dt is None or (now - dt).total_seconds() / 86400.0 > window_days:
            continue
        reasons = []
        if r.get("locked"):
            reasons.append("locked-out")
        if r.get("timeout"):
            reasons.append("timed-out")
        exit_code = r.get("exit")
        if exit_code not in (0, None):
            reasons.append(f"exit={exit_code}")
        if reasons:
            out.append({"job": job, "ts": r.get("ts"), "reasons": reasons})
    return out


# ----------------------------------------------------------------------------
# I/O helpers
# ----------------------------------------------------------------------------
def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_log_tail(basename: str, max_bytes: int = 200_000) -> str:
    """Concatenate a job's current log + its most recent rotation (.1)."""
    text = ""
    for suffix in (".log", ".log.1"):
        p = LOG_DIR / f"{basename}{suffix}"
        if p.exists():
            try:
                text += p.read_text(errors="replace")[-max_bytes:]
            except OSError:
                pass
    return text


def _file_mtime(path: Path) -> Optional[datetime]:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


# ----------------------------------------------------------------------------
# Stage checks (I/O) -> Verdict dict
# ----------------------------------------------------------------------------
def _verdict(stage: str, status: str, detail: str,
             age_days: Optional[float] = None) -> dict:
    return {"stage": stage, "status": status, "detail": detail, "age_days": age_days}


def check_fetch(now: datetime) -> dict:
    rows = _read_jsonl(REPO / "data" / "articles.jsonl")
    last = newest_dt(rows, "fetched_at")
    status, age = classify_staleness(now, last, threshold_days=4)
    if status == OK:
        return _verdict("daily-fetch", OK, f"newest article {age:.1f}d old", age)
    where = "no articles" if age is None else f"newest article {age:.1f}d old (>4d)"
    return _verdict("daily-fetch", STALE, f"metadata fetch may be stalled: {where}", age)


def check_synthesis(now: datetime) -> dict:
    hist = _read_jsonl(REPO / "config" / "fetcher-synthesis-history.jsonl")
    last = newest_dt(hist, "timestamp")
    status, age = classify_staleness(now, last, threshold_days=9)
    bail = scan_markers(_read_log_tail("gmia-fetcher-synthesis"), LOCK_BAIL_MARKERS)
    if bail:
        return _verdict("fetcher-synthesis", BAIL,
                        "weekly run bailed without working ("
                        + ", ".join(bail) + ")", age)
    if status == STALE:
        d = "never ran" if age is None else f"no session in {age:.0f}d (>9d)"
        return _verdict("fetcher-synthesis", STALE, d, age)
    return _verdict("fetcher-synthesis", OK, f"last session {age:.1f}d ago", age)


def check_profile_refresh(now: datetime) -> dict:
    # No state file: derive last run from the wrapper's "done:" summary line.
    text = _read_log_tail("gmia-profile-refresh")
    done_ts: Optional[datetime] = None
    for suffix in (".log", ".log.1"):
        p = LOG_DIR / f"gmia-profile-refresh{suffix}"
        if p.exists() and "done:" in p.read_text(errors="replace"):
            done_ts = _file_mtime(p) if done_ts is None else max(done_ts, _file_mtime(p))
    status, age = classify_staleness(now, done_ts, threshold_days=40)
    derail = scan_markers(_read_agent_log("profile-refresh-agent"),
                          AGENT_DERAIL_MARKERS)
    if derail:
        return _verdict("profile-refresh", BAIL,
                        "AUM agent derailed instead of verifying ("
                        + ", ".join(derail) + ")", age)
    if status == STALE:
        d = "never completed" if age is None else f"no run in {age:.0f}d (>40d)"
        return _verdict("profile-refresh", STALE, d, age)
    return _verdict("profile-refresh", OK, f"last run {age:.1f}d ago", age)


def _read_agent_log(basename: str, max_bytes: int = 100_000) -> str:
    p = REPO / "logs" / f"{basename}.log"
    if not p.exists():
        return ""
    try:
        return p.read_text(errors="replace")[-max_bytes:]
    except OSError:
        return ""


def check_fetcher_health(now: datetime) -> dict:
    p = REPO / "logs" / "gmia-fetcher-health.json"
    last = None
    if p.exists():
        try:
            last = parse_iso(str(json.loads(p.read_text()).get("last_run", "")))
        except (json.JSONDecodeError, OSError):
            last = None
    status, age = classify_staleness(now, last, threshold_days=2)
    if status == OK:
        return _verdict("fetcher-health", OK, f"last run {age:.1f}d ago", age)
    d = "never ran" if age is None else f"no run in {age:.1f}d (>2d)"
    return _verdict("fetcher-health", STALE, d, age)


def check_discovery(now: datetime) -> dict:
    last = _file_mtime(REPO / "config" / "fund_candidates.json")
    status, age = classify_staleness(now, last, threshold_days=2)
    if status == OK:
        return _verdict("candidate-discovery", OK, f"pool touched {age:.1f}d ago", age)
    d = "no candidate file" if age is None else f"pool unchanged {age:.1f}d (>2d)"
    return _verdict("candidate-discovery", STALE, d, age)


def check_ops(now: datetime) -> list[dict]:
    rows = _read_jsonl(LOG_DIR / "ops-status.jsonl")
    probs = ops_problems(rows, "gmia", now, window_days=3)
    return [
        _verdict(f"cron:{p['job']}", BAIL,
                 f"{', '.join(p['reasons'])} at {p['ts']}")
        for p in probs
    ]


CHECKS: list[Callable[[datetime], dict]] = [
    check_fetch, check_synthesis, check_profile_refresh,
    check_fetcher_health, check_discovery,
]


def run_audit(now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    verdicts = [c(now) for c in CHECKS]
    verdicts.extend(check_ops(now))
    return verdicts


def render_text(verdicts: list[dict]) -> str:
    icon = {OK: "OK  ", STALE: "STALE", BAIL: "BAIL"}
    lines = ["GMIA pipeline liveness audit", "=" * 40]
    for v in verdicts:
        lines.append(f"[{icon[v['status']]}] {v['stage']:<20} {v['detail']}")
    problems = [v for v in verdicts if v["status"] != OK]
    lines.append("-" * 40)
    lines.append(f"{len(problems)} problem(s), {len(verdicts) - len(problems)} OK")
    return "\n".join(lines)


def render_html(verdicts: list[dict]) -> str:
    import html as _html
    color = {OK: "#2e7d32", STALE: "#e65100", BAIL: "#c62828"}
    rows = "".join(
        f'<tr><td style="padding:4px 10px;color:{color[v["status"]]};'
        f'font-weight:bold">{v["status"]}</td>'
        f'<td style="padding:4px 10px">{_html.escape(v["stage"])}</td>'
        f'<td style="padding:4px 10px">{_html.escape(v["detail"])}</td></tr>'
        for v in verdicts
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        "</head><body>"
        '<h2>GMIA pipeline liveness audit</h2>'
        '<table style="border-collapse:collapse;font-family:monospace">'
        f"{rows}</table></body></html>"
    )


def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from an env file into os.environ (self-contained
    email, same pattern as gmia-fetcher-health.py — no reliance on the cron
    sourcing creds first)."""
    import os
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GMIA pipeline liveness audit")
    ap.add_argument("--email", action="store_true",
                    help="email the digest when any stage is STALE/BAIL")
    ap.add_argument("--always-email", action="store_true",
                    help="email even when everything is OK")
    args = ap.parse_args(argv)

    verdicts = run_audit()
    print(render_text(verdicts))
    problems = [v for v in verdicts if v["status"] != OK]

    if (args.always_email or (args.email and problems)):
        try:
            _load_env_file(Path.home() / ".stock-monitor.env")
            sys.path.insert(0, str(REPO / "scripts"))
            from send_synthesis_summary import send_email  # type: ignore
            subject = (
                f"⚠️ GMIA liveness: {len(problems)} stage(s) silently stalled"
                if problems else "✅ GMIA liveness: all stages OK"
            )
            send_email(render_html(verdicts), subject)
        except Exception as exc:  # noqa: BLE001 — alert failure must not mask verdict
            print(f"WARNING: liveness email failed: {exc}")

    # Non-zero exit so a cron-wrapper / caller can react to silent stalls.
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
