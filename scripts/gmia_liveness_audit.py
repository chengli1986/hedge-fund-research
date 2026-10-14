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
Alert by email when any stage looks silently stalled:
    python3 scripts/gmia_liveness_audit.py --email

Exit codes with --email (2026-10-14): 0 = nothing wrong, or every problem has been mailed
(today or within REMIND_DAYS); 2 = a problem needed mailing and the mail did
not go out; a crash exits 1. Without --email: 1 if any problem. The cron job runs WITHOUT --no-alert, so 1 and 2
reach a person through cron-wrapper. Until then it always exited 1 on a
problem under --no-alert: a failed mail, or a crash, was invisible.

A problem is mailed once, then again only every REMIND_DAYS while it lasts
(logs/liveness-alerted.json); one that clears and comes back is new. The
10-11 synthesis failure was mailed every day for a week although nothing
could change before the next weekly session.

The check logic is deliberately split into pure functions (classify_staleness,
scan_markers, newest_dt, ops_problems) so it is unit-tested without touching the
filesystem; the check_* wrappers do the I/O.
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
LOG_DIR = Path.home() / "logs"
# The synthesis session heartbeat is written to <repo>/logs/ by write_session_heartbeat.py
# (BASE_DIR/logs, shared by every synthesis script), NOT repo config/ and NOT ~/logs/ —
# reading the wrong dir made check_synthesis blind to healthy sessions (2026-07-23
# false-BAIL bug).
SYNTHESIS_HISTORY = REPO / "logs" / "fetcher-synthesis-history.jsonl"
# This audit's own cron job. It must exclude itself from the ops-status scan:
# it exits 1 on any problem, cron-wrapper logs that exit=1, and re-reading its own
# failure would create a self-perpetuating loop it could never recover from.
SELF_JOB = "gmia-liveness-audit"
ALERT_STATE = REPO / "logs" / "liveness-alerted.json"
REMIND_DAYS = 7
# Every GMIA cron job and the longest gap allowed between two of its runs, in
# days (daily 1.5, weekly 8, monthly 32), judged from ops-status.jsonl, which
# cron-wrapper writes for every run. Before 2026-10-14 only five stages were
# watched, and the trial manager's silent month or a job whose cron line
# vanished would never have shown here. A gmia job in the crontab that is not
# listed here is reported, so this list cannot quietly fall behind.
EXPECTED_JOBS = {
    "gmia-daily": 1.5, "gmia-nightly-test": 1.5, "gmia-candidate-discovery": 1.5,
    "gmia-fetcher-health": 1.5, "gmia-auto-promote": 1.5, "gmia-health-page": 1.5,
    "gmia-fetcher-synthesis": 8, "gmia-content-audit": 8, "gmia-ab-gate": 8,
    "gmia-profile-refresh": 32, "gmia-census": 32,
}

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


def run_reasons(r: dict) -> list[str]:
    """Why a single cron-wrapper run counts as bad (empty list = clean run)."""
    reasons = []
    if r.get("locked"):
        reasons.append("locked-out")
    if r.get("timeout"):
        reasons.append("timed-out")
    exit_code = r.get("exit")
    if exit_code not in (0, None):
        reasons.append(f"exit={exit_code}")
    return reasons


def runs_by_job(rows: Iterable[dict], jobs_prefix: str, now: datetime,
                window_days: float,
                exclude: Optional[set[str]] = None) -> dict[str, list[dict]]:
    """In-window ops-status rows grouped by job, each list sorted oldest-first
    BY TIMESTAMP (never by file order — a merged/restored log may interleave).

    Jobs in `exclude` are dropped — used to keep the auditor from reading its
    own past exit=1 rows (which would be a self-perpetuating failure loop)."""
    exclude = exclude or set()
    grouped: dict[str, list[tuple[datetime, dict]]] = {}
    for r in rows:
        # ⚠ ops-status.jsonl 不只装「一次运行」的记录：cron-wrapper 还会写
        #   「那次告警发出去没有」的事件。那种记录**没有 exit 字段**，而
        #   run_reasons() 把 exit=None 当成正常、下面又按时间取最新一条 ——
        #   于是紧随失败之后的一条 alert 记录会把失败**盖成 recovered**。
        #   2026-08-26 真实发生：gmia-fetcher-health exit=1 → 2 秒后 alert_sent
        #   → 巡检输出「recovered … 0 problems」。
        #   ⚠ 两个条件都留着：`event` 标记是约定（新格式），`没有 exit` 是硬事实
        #     （挡住修复前已写进历史的旧格式）。别只依赖上游守规矩。
        if r.get("event") == "alert" or "exit" not in r:
            continue
        job = str(r.get("job", ""))
        if not job.startswith(jobs_prefix) or job in exclude:
            continue
        dt = parse_iso(str(r.get("ts", "")))
        if dt is None or (now - dt).total_seconds() / 86400.0 > window_days:
            continue
        grouped.setdefault(job, []).append((dt, r))
    return {job: [r for _, r in sorted(pairs, key=lambda p: p[0])]
            for job, pairs in grouped.items()}


def ops_problems(rows: Iterable[dict], jobs_prefix: str, now: datetime,
                 window_days: float, exclude: Optional[set[str]] = None) -> list[dict]:
    """Jobs whose LATEST in-window run locked out, timed out, or exited non-zero.

    Only the latest run decides. Judging every row independently (the pre
    2026-08-18 behaviour) replayed an already-fixed failure for the whole
    window: the 8-17 playwright incident kept BAILing gmia-nightly-test and
    gmia-fetcher-health on 8-18 although both were green again hours later —
    a daily false alarm that stops only when the row ages out. Recoveries are
    reported separately by ops_recovered()."""
    out = []
    for job, runs in runs_by_job(rows, jobs_prefix, now, window_days, exclude).items():
        latest = runs[-1]
        reasons = run_reasons(latest)
        if reasons:
            out.append({"job": job, "ts": latest.get("ts"), "reasons": reasons})
    return out


def ops_recovered(rows: Iterable[dict], jobs_prefix: str, now: datetime,
                  window_days: float, exclude: Optional[set[str]] = None) -> list[dict]:
    """Jobs that failed inside the window but whose latest run came back clean.

    Informational only (never a problem, never an alert) — it keeps a fixed
    failure visible in the digest without re-raising it."""
    out = []
    for job, runs in runs_by_job(rows, jobs_prefix, now, window_days, exclude).items():
        if run_reasons(runs[-1]):
            continue
        failures = [r for r in runs[:-1] if run_reasons(r)]
        if failures:
            last = failures[-1]
            out.append({"job": job, "last_failure_ts": last.get("ts"),
                        "reasons": run_reasons(last), "recovered_ts": runs[-1].get("ts")})
    return out


def stale_jobs(rows: Iterable[dict], expected: dict[str, float],
               now: datetime) -> list[dict]:
    """Expected jobs with no run (any row with an exit field) within their gap."""
    last: dict[str, datetime] = {}
    for r in rows:
        if r.get("event") == "alert" or "exit" not in r:
            continue
        dt = parse_iso(str(r.get("ts", "")))
        job = str(r.get("job", ""))
        if dt and job in expected and (job not in last or dt > last[job]):
            last[job] = dt
    out = []
    for job, gap in expected.items():
        status, age = classify_staleness(now, last.get(job), gap)
        if status == STALE:
            out.append({"job": job, "age_days": age, "gap": gap})
    return out


def crontab_jobs(text: str) -> set[str]:
    """gmia job names (--name) on the crontab's active lines."""
    names = set()
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        m = re.search(r"--name\s+(gmia-[\w-]+)", line)
        if m:
            names.add(m.group(1))
    return names


def plan_alert(problems: list[dict], state: dict, now: datetime,
               remind_days: float = REMIND_DAYS) -> tuple[list[str], dict]:
    """(keys to mail now, state to save once the mail has gone).

    A key is mailed when it is new or was last mailed remind_days ago; keys no
    longer among the problems drop out of the state, so a recurrence is new."""
    due, new_state = [], {}
    for v in problems:
        key = v["key"]
        seen = state.get(key) or {}
        last = parse_iso(str(seen.get("last_alerted", "")))
        if last is None or (now - last).total_seconds() / 86400.0 >= remind_days:
            due.append(key)
            new_state[key] = {"first_alerted": seen.get("first_alerted") or now.isoformat(),
                              "last_alerted": now.isoformat()}
        else:
            new_state[key] = seen
    return due, new_state


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


def _read_ops_history() -> list[dict]:
    """ops-status.jsonl plus its weekly rotations (.N.gz, about 8 weeks).

    The current file alone starts at the last rotation, so a 3-day window read
    the day after it missed two days, and a monthly job is never in it."""
    rows = _read_jsonl(LOG_DIR / "ops-status.jsonl")
    for p in sorted(LOG_DIR.glob("ops-status.jsonl.*.gz")):
        try:
            text = gzip.open(p, "rt", errors="replace").read()
        except (OSError, EOFError):
            continue
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _read_crontab() -> Optional[str]:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


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
             age_days: Optional[float] = None, key: Optional[str] = None) -> dict:
    """key names the problem for the mail-once rule: stage+status by default
    (the detail carries an age that changes daily); a check that can tell one
    incident from the next (a failed run's timestamp) puts that in."""
    return {"stage": stage, "status": status, "detail": detail, "age_days": age_days,
            "key": key or f"{stage}:{status}"}


def check_fetch(now: datetime) -> dict:
    rows = _read_jsonl(REPO / "data" / "articles.jsonl")
    last = newest_dt(rows, "fetched_at")
    status, age = classify_staleness(now, last, threshold_days=4)
    if status == OK:
        return _verdict("daily-fetch", OK, f"newest article {age:.1f}d old", age)
    where = "no articles" if age is None else f"newest article {age:.1f}d old (>4d)"
    return _verdict("daily-fetch", STALE, f"metadata fetch may be stalled: {where}", age)


def decide_synthesis(now: datetime, last: Optional[datetime],
                     bail_labels: list[str],
                     threshold_days: float = 9,
                     agent_exit: Optional[int] = None) -> tuple[str, Optional[float], str]:
    """Pure synthesis verdict. A fresh heartbeat WINS: a lock-bail marker only
    matters when the heartbeat is genuinely stale (it then explains *why*). This
    stops a pre-fix lock-bail line lingering in a rotated log from masking a
    healthy recent session. But a fresh heartbeat only proves the wrapper ran:
    the newest session's ``agent_exit`` must be 0 too (2026-10-11 the agent
    exited 1 on an expired Claude login and this printed OK). None = a
    heartbeat that predates the field. Returns (status, age_days, detail)."""
    status, age = classify_staleness(now, last, threshold_days)
    if status == OK:
        if agent_exit:
            return BAIL, age, (f"last session {age:.1f}d ago, but the agent exited "
                               f"{agent_exit} (see gmia-fetcher-synthesis.log)")
        return OK, age, f"last session {age:.1f}d ago"
    if bail_labels:
        return BAIL, age, ("weekly run bailed without working ("
                           + ", ".join(bail_labels) + ")")
    d = "never ran" if age is None else f"no session in {age:.0f}d (>{threshold_days:.0f}d)"
    return STALE, age, d


def check_synthesis(now: datetime) -> dict:
    hist = _read_jsonl(SYNTHESIS_HISTORY)
    beats = [r for r in hist if r.get("id") == "_heartbeat"]
    last = newest_dt(beats, "timestamp")
    newest = [r for r in beats if parse_iso(str(r.get("timestamp", ""))) == last]
    agent_exit = newest[-1].get("agent_exit") if newest else None
    bail = scan_markers(_read_log_tail("gmia-fetcher-synthesis"), LOCK_BAIL_MARKERS)
    status, age, detail = decide_synthesis(now, last, bail, threshold_days=9,
                                           agent_exit=agent_exit)
    # The session is the incident: the next session failing too is news.
    key = f"fetcher-synthesis:{status}:{last.isoformat() if last else ''}"
    return _verdict("fetcher-synthesis", status, detail, age, key=key)


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


def check_ops(now: datetime, rows: Optional[list[dict]] = None) -> list[dict]:
    rows = _read_ops_history() if rows is None else rows
    kwargs = dict(window_days=3, exclude={SELF_JOB})
    verdicts = [
        _verdict(f"cron:{p['job']}", BAIL,
                 f"{', '.join(p['reasons'])} at {p['ts']}",
                 key=f"cron:{p['job']}:{p['ts']}")
        for p in ops_problems(rows, "gmia", now, **kwargs)
    ]
    verdicts.extend(
        _verdict(f"cron:{r['job']}", OK,
                 f"recovered — last failure {', '.join(r['reasons'])} "
                 f"at {r['last_failure_ts']}, clean since {r['recovered_ts']}")
        for r in ops_recovered(rows, "gmia", now, **kwargs)
    )
    return verdicts


def check_jobs_ran(now: datetime, rows: Optional[list[dict]] = None,
                   crontab: Optional[str] = "read") -> list[dict]:
    """Every expected job ran within its gap, is still in the crontab, and the
    crontab has no gmia job this list does not watch. crontab=None: unreadable
    (skipped, reported)."""
    rows = _read_ops_history() if rows is None else rows
    crontab = _read_crontab() if crontab == "read" else crontab
    out = [
        _verdict(f"ran:{s['job']}", STALE,
                 ("no run on record" if s["age_days"] is None
                  else f"last run {s['age_days']:.1f}d ago (>{s['gap']:g}d)"),
                 s["age_days"])
        for s in stale_jobs(rows, EXPECTED_JOBS, now)
    ]
    if crontab is None:
        out.append(_verdict("crontab", STALE, "crontab -l failed; job list not checked"))
        return out
    listed = crontab_jobs(crontab)
    for job in sorted(set(EXPECTED_JOBS) - listed):
        out.append(_verdict(f"ran:{job}", STALE, "not in the crontab any more",
                            key=f"ran:{job}:missing"))
    for job in sorted(listed - set(EXPECTED_JOBS) - {SELF_JOB}):
        out.append(_verdict(f"ran:{job}", STALE,
                            "in the crontab but not watched: add it to EXPECTED_JOBS",
                            key=f"ran:{job}:unwatched"))
    return out


CHECKS: list[Callable[[datetime], dict]] = [
    check_fetch, check_synthesis, check_profile_refresh,
    check_fetcher_health, check_discovery,
]


def run_audit(now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    verdicts = [c(now) for c in CHECKS]
    rows = _read_ops_history()
    verdicts.extend(check_ops(now, rows))
    verdicts.extend(check_jobs_ran(now, rows))
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


def render_html(verdicts: list[dict], state: Optional[dict] = None,
                due: Optional[list[str]] = None) -> str:
    """state/due: problems not due today are tagged with when they were first mailed."""
    import html as _html
    color = {OK: "#2e7d32", STALE: "#e65100", BAIL: "#c62828"}
    state, due = state or {}, set(due or [])

    def seen(v: dict) -> str:
        if v["status"] == OK or v["key"] in due or v["key"] not in state:
            return ""
        first = str(state[v["key"]].get("first_alerted", ""))[:10]
        return f' <span style="color:#888">(已报过，首次 {_html.escape(first)})</span>'

    rows = "".join(
        f'<tr><td style="padding:4px 10px;color:{color[v["status"]]};'
        f'font-weight:bold">{v["status"]}</td>'
        f'<td style="padding:4px 10px">{_html.escape(v["stage"])}</td>'
        f'<td style="padding:4px 10px">{_html.escape(v["detail"])}{seen(v)}</td></tr>'
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

    now = datetime.now(timezone.utc)
    verdicts = run_audit(now)
    print(render_text(verdicts))
    problems = [v for v in verdicts if v["status"] != OK]
    state = load_alert_state()
    due, new_state = plan_alert(problems, state, now)
    if problems and not due:
        print(f"all {len(problems)} problem(s) already mailed within {REMIND_DAYS}d; not mailing")

    if not (args.email or args.always_email):
        return 1 if problems else 0          # a run by hand: the verdict itself
    if not (args.always_email or due):
        save_alert_state(new_state)          # drops cleared problems
        return 0
    sent = False
    try:
        _load_env_file(Path.home() / ".stock-monitor.env")
        sys.path.insert(0, str(REPO / "scripts"))
        from send_synthesis_summary import send_email  # type: ignore
        subject = (
            f"⚠️ GMIA liveness: {len(due)} new problem(s), {len(problems)} open"
            if problems else "✅ GMIA liveness: all stages OK"
        )
        sent = send_email(render_html(verdicts, state, due), subject)
    except Exception as exc:  # noqa: BLE001 — reported through the exit code below
        print(f"WARNING: liveness email failed: {exc}")
    if not sent:
        print("ERROR: the liveness mail did not go out; exit 2 so cron-wrapper alerts")
        return 2 if due else 0
    if args.email:
        save_alert_state(new_state)
    return 0


def load_alert_state(path: Optional[Path] = None) -> dict:
    path = path or ALERT_STATE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_alert_state(state: dict, path: Optional[Path] = None) -> None:
    path = path or ALERT_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
