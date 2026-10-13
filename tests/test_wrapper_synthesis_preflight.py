"""A synthesis session where Claude cannot start must not charge the targets a failure.

backfill_failed_synthesis.py marks every planned target the agent left
untouched as ``failed``, and three failures permanently reject the candidate.
That is right when the agent ran and skipped a target (franklin-templeton,
"stuck 8 weeks"), but on 2026-10-11 02:00 BJT the agent never ran at all:

    Failed to authenticate: OAuth session expired and could not be refreshed
    Agent exited with code 1

and rbc-bluebay was still recorded as synthesis failure 1 of 3. The wrapper
now checks that Claude answers before starting the agent; if it does not, the
session stops without the backfill, writes a heartbeat with the failing exit
code and exits non-zero so cron-wrapper alerts.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

TARGETS = '[{"id": "rbc-bluebay", "name": "RBC BlueBay"}]'

# The agent call is the one with --dangerously-skip-permissions; the preflight
# has no tools. PREFLIGHT_FAIL makes the preflight fail like the 10-11 night.
FAKE_CLAUDE = """#!/bin/bash
cat > /dev/null
if [[ "$*" == *--version* ]]; then echo "9.9.9 (fake)"; exit 0; fi
if [[ "$*" != *--dangerously-skip-permissions* ]]; then
    if [ -n "${PREFLIGHT_FAIL:-}" ]; then
        echo "Failed to authenticate: OAuth session expired and could not be refreshed"
        exit 1
    fi
    echo OK; exit 0
fi
echo "agent call" >> "$(dirname "$0")/agent-calls"
if [ -n "${AGENT_PROCESS:-}" ]; then
    # Processes the target the old way: writes code, never tries a template.
    python3 - <<'PY'
import json, datetime
from pathlib import Path
f = Path("config/fund_candidates.json")
d = json.loads(f.read_text())
c = d["candidates"][0]
c["synthesis_attempted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
c["synthesis_outcome"] = "success"
f.write_text(json.dumps(d))
PY
    echo "agent processed the target without a template attempt"
    exit 0
fi
echo "agent ran and skipped every target"
exit 0
"""


def _sandbox(tmp_path: Path) -> Path:
    for d in ("scripts", "logs", "config", "home"):
        (tmp_path / d).mkdir()
    for rel in ("scripts/wrapper-fetcher-synthesis.sh", "scripts/write_session_heartbeat.py",
                "scripts/backfill_failed_synthesis.py", "scripts/sync_synthesis_history.py",
                "scripts/check_template_first.py"):
        shutil.copy2(REPO / rel, tmp_path / rel)          # verbatim, not a rewrite
    (tmp_path / "synthesize_fetchers.py").write_text(f"print({TARGETS!r})\n")
    (tmp_path / "fetcher-synthesis").mkdir()
    (tmp_path / "fetcher-synthesis" / "program.md").write_text("test playbook\n")
    (tmp_path / "config" / "fund_candidates.json").write_text(json.dumps(
        {"candidates": [{"id": "rbc-bluebay", "name": "RBC BlueBay",
                         "status": "inaccessible"}]}))
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    return tmp_path


def _run(sandbox: Path, preflight_fail: bool, agent_process: bool = False):
    env = dict(os.environ,
               SYNTHESIS_LOCK_FILE=str(sandbox / "test.lock"),
               CLAUDE_BIN=str(sandbox / "claude"),
               HOME=str(sandbox / "home"))   # no ~/.stock-monitor.env: no mail is sent
    env.pop("PREFLIGHT_FAIL", None)
    if preflight_fail:
        env["PREFLIGHT_FAIL"] = "1"
    env.pop("AGENT_PROCESS", None)
    if agent_process:
        env["AGENT_PROCESS"] = "1"
    # stdin=DEVNULL: the fake claude starts with `cat`, and the bare
    # `claude --version` call would otherwise wait on the terminal's stdin.
    return subprocess.run(["bash", str(sandbox / "scripts/wrapper-fetcher-synthesis.sh")],
                          capture_output=True, text=True, timeout=120, env=env,
                          stdin=subprocess.DEVNULL)


def _history(sandbox: Path) -> list[dict]:
    f = sandbox / "logs" / "fetcher-synthesis-history.jsonl"
    if not f.exists():
        return []
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]


def _candidate(sandbox: Path) -> dict:
    return json.loads((sandbox / "config" / "fund_candidates.json").read_text())["candidates"][0]


def test_claude_that_cannot_start_charges_no_failure(tmp_path):
    sandbox = _sandbox(tmp_path)
    proc = _run(sandbox, preflight_fail=True)
    failed = [e for e in _history(sandbox) if e.get("outcome") == "failed"]
    assert failed == [], (
        "Claude never started, yet the target was recorded as a synthesis failure — "
        "three of these permanently reject a candidate nobody tried")
    assert "synthesis_outcome" not in _candidate(sandbox)
    assert not (sandbox / "agent-calls").exists(), "agent was started after a failed preflight"
    assert proc.returncode != 0, "a session that could not run exited 0 — no one is alerted"


def test_failed_preflight_still_leaves_a_heartbeat_with_the_error(tmp_path):
    sandbox = _sandbox(tmp_path)
    proc = _run(sandbox, preflight_fail=True)
    hb = [e for e in _history(sandbox) if e.get("id") == "_heartbeat"]
    assert len(hb) == 1, proc.stdout
    assert hb[0]["agent_exit"] != 0
    assert hb[0]["targets_count"] == 1
    assert "Failed to authenticate" in proc.stdout


def test_agent_that_ran_and_skipped_the_target_is_still_charged(tmp_path):
    # The franklin-templeton guard must keep working: Claude answered, the
    # agent ran, and it left the target untouched.
    sandbox = _sandbox(tmp_path)
    _run(sandbox, preflight_fail=False)
    assert (sandbox / "agent-calls").exists()
    failed = [e for e in _history(sandbox) if e.get("outcome") == "failed"]
    assert [e["id"] for e in failed] == ["rbc-bluebay"]


# code review 2026-10-12: with no `set -e`, a failing target listing left
# TARGET_COUNT empty, the zero-target exit was skipped and the agent started.
def test_a_broken_target_listing_stops_before_the_agent(tmp_path):
    sandbox = _sandbox(tmp_path)
    (sandbox / "synthesize_fetchers.py").write_text("import sys; sys.exit('fund_candidates.json unreadable')\n")
    proc = _run(sandbox, preflight_fail=False)
    assert proc.returncode != 0
    assert not (sandbox / "agent-calls").exists(), "agent started with no target list"
    assert "could not list synthesis targets" in proc.stdout


# 2026-10-13 template first: the wrapper reads try_template's record.
def test_a_target_processed_without_a_template_attempt_alerts(tmp_path):
    sandbox = _sandbox(tmp_path)
    proc = _run(sandbox, preflight_fail=False, agent_process=True)
    assert "TEMPLATE-FIRST VIOLATION: rbc-bluebay" in proc.stdout, proc.stdout
    assert proc.returncode == 2


def test_a_skipped_target_is_not_a_template_violation(tmp_path):
    sandbox = _sandbox(tmp_path)
    proc = _run(sandbox, preflight_fail=False)
    assert "TEMPLATE-FIRST VIOLATION" not in proc.stdout
