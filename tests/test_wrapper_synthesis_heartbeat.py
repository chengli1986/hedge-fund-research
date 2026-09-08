"""A synthesis session that finds no work must still leave a heartbeat.

scripts/gmia_liveness_audit.py judges the weekly synthesis job by the freshest
heartbeat in logs/fetcher-synthesis-history.jsonl.  The wrapper writes one at
the end of a session -- its comment says "always write one line per session,
even when agent did nothing" -- but an earlier `exit 0` on the no-target path
skips it, so a healthy run with nothing to do is indistinguishable from a job
that never ran.

Seen in production: the 2026-09-05 run logged

    Starting fetcher synthesis session...
    No inaccessible targets to process. Exiting.

and wrote nothing, so the 2026-09-08 audit reported "no session in 9d".  It had
happened before (2026-08-22, same two lines) and stayed invisible because
tests/test_session_heartbeat.py was itself writing fake heartbeats into the
production file on every pytest run, which the daily 02:30 BJT cron triggers.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _sandbox(tmp_path, targets_json: str) -> Path:
    """A minimal repo layout the wrapper can run against."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "logs").mkdir()
    for rel in ("scripts/wrapper-fetcher-synthesis.sh", "scripts/write_session_heartbeat.py"):
        shutil.copy2(REPO / rel, tmp_path / rel)          # verbatim, not a rewrite
    (tmp_path / "synthesize_fetchers.py").write_text(f"print({targets_json!r})\n")
    return tmp_path


def _run(sandbox: Path):
    env = dict(os.environ, SYNTHESIS_LOCK_FILE=str(sandbox / "test.lock"))
    return subprocess.run(["bash", str(sandbox / "scripts/wrapper-fetcher-synthesis.sh")],
                          capture_output=True, text=True, timeout=120, env=env)


def _heartbeats(sandbox: Path) -> list[dict]:
    f = sandbox / "logs" / "fetcher-synthesis-history.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines()
            if l.strip() and json.loads(l).get("id") == "_heartbeat"]


def test_no_target_session_still_writes_a_heartbeat(tmp_path):
    sandbox = _sandbox(tmp_path, "[]")
    proc = _run(sandbox)
    assert proc.returncode == 0, proc.stderr
    assert "No inaccessible targets" in proc.stdout
    hb = _heartbeats(sandbox)
    assert len(hb) == 1, (
        "a session that ran and found nothing to do left no trace — the liveness "
        "audit cannot tell it apart from a job that never fired")
    assert hb[0]["targets_count"] == 0


def test_the_no_work_heartbeat_does_not_raise_an_alert(tmp_path):
    # detect_inconsistency only alerts when targets_count > 0, so a zero-target
    # session must be recorded as healthy rather than trading one false signal
    # for another.
    sandbox = _sandbox(tmp_path, "[]")
    proc = _run(sandbox)
    assert proc.returncode == 0, (
        "a no-work session exited non-zero — cron-wrapper would alert on it")
    hb = _heartbeats(sandbox)[0]
    assert hb["reconcile_appended"] == 0
    assert hb["agent_exit"] == 0
