#!/bin/bash
# Daily wrapper for GMIA auto-promote
# Invokes Claude Code agent to wire trial-passed candidates into production
# (sources.json + BADGE_COLORS + CONTENT_FETCHERS + _FUND_PROFILES draft)
# Schedule: daily 02:30 BJT (18:30 UTC)
#
# Uses Claude Code Max Plan (NOT API) — same pattern as wrapper-fetcher-synthesis.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "$LOG_PREFIX Starting auto-promote session..."

# 若无目标则提前退出，不启动 Agent
TARGET_COUNT=$(cd "$REPO_DIR" && python3 - << 'PYEOF'
import json
import sys
from pathlib import Path
sys.path.insert(0, ".")
candidates = json.loads(Path("config/fund_candidates.json").read_text())
sources = json.loads(Path("config/sources.json").read_text())
prod_ids = {s["id"] for s in sources["sources"]}
try:
    from fetch_articles import FETCHERS
except Exception as exc:
    print(0)
    sys.exit(0)

n = sum(
    1 for c in candidates
    if c.get("status") == "promoted"
    and c["id"] not in prod_ids
    and c["id"] in FETCHERS
)
print(n)
PYEOF
)

if [ "$TARGET_COUNT" -eq 0 ]; then
    echo "$LOG_PREFIX No promoted candidates needing wiring. Exiting."
    exit 0
fi
echo "$LOG_PREFIX Found $TARGET_COUNT candidate(s) to promote."

# 取消 API key 使 Claude 走 Max plan（不走付费 API）
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    SAVED_ANTHROPIC_API_KEY="$(
        grep '^ANTHROPIC_API_KEY=' "$HOME/.openclaw/.env" "$HOME/.stock-monitor.env" 2>/dev/null \
        | head -1 | cut -d= -f2- | tr -d '"'"'"
    )"
else
    SAVED_ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
fi
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

PROGRAM_MD="$REPO_DIR/auto-promote/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- 最长 20 分钟
- 最多处理 2 个基金
- 接入后必须运行 pytest；若失败则回滚（git checkout 已修改的文件 + 删 pending_profiles 草稿）
- 每次成功接入后立即 commit + push
- 即使 deferred / failed 也要写一行到 logs/auto-promote-history.jsonl
"

echo "$LOG_PREFIX Invoking Claude Code agent..."
echo "$PROMPT" | claude --print \
    --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
    --max-turns 60 \
    2>&1

EXIT_CODE=$?
echo "$LOG_PREFIX Agent exited with code $EXIT_CODE"

# ─── Phase 7: post-commit health probe + auto-revert ─────────────────────────
# Read today's promoted entries from history; probe each via gmia-fetcher-health
# --source <id> --dry-run; if FAIL → git revert + push + mark auto_reverted.
echo "$LOG_PREFIX Running post-commit health probe..."

cd "$REPO_DIR"
python3 - << 'PYEOF'
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
today = datetime.now(BJT).strftime("%Y-%m-%d")
log = Path("logs/auto-promote-history.jsonl")

if not log.exists():
    print("[probe] no history file, nothing to verify")
    sys.exit(0)

# Find today's promoted entries (newest last in jsonl)
promoted_today = []
for line in log.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        continue
    if e.get("date") == today and e.get("outcome") == "promoted":
        promoted_today.append(e)

if not promoted_today:
    print("[probe] no promoted entries today, nothing to verify")
    sys.exit(0)

print(f"[probe] verifying {len(promoted_today)} promotion(s)...")

# Probe each in REVERSE order (newest commit first — git revert sequencing)
reverted = []
for e in reversed(promoted_today):
    sid = e["id"]
    commit = e.get("commit", "")
    print(f"[probe] {sid} (commit {commit[:8]})...")
    proc = subprocess.run(
        ["python3", "scripts/gmia-fetcher-health.py",
         "--source", sid, "--dry-run"],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode == 0:
        print(f"[probe]   OK ✓")
        continue

    print(f"[probe]   FAIL (exit {proc.returncode}) → reverting {commit[:8]}")
    print(f"[probe]   stderr tail: {proc.stderr[-300:] if proc.stderr else '(empty)'}")

    if not commit:
        print(f"[probe]   ERROR: no commit sha in history entry, cannot revert {sid}")
        continue

    revert = subprocess.run(
        ["git", "revert", commit, "--no-edit"],
        capture_output=True, text=True,
    )
    if revert.returncode != 0:
        print(f"[probe]   git revert FAILED: {revert.stderr[:300]}")
        continue

    push = subprocess.run(
        ["git", "push", "origin", "main"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        print(f"[probe]   git push FAILED, leaving local revert in place: {push.stderr[:200]}")
        continue

    revert_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()

    revert_entry = {
        "date": today,
        "timestamp": datetime.now(BJT).isoformat(),
        "id": sid,
        "name": e.get("name", sid),
        "outcome": "auto_reverted",
        "notes": (f"Post-commit health probe FAIL — reverted {commit[:8]}. "
                  f"Health stderr tail: {(proc.stderr or '')[-200:]}"),
        "commit": revert_sha,
        "reverted_commit": commit,
    }
    with log.open("a") as f:
        f.write(json.dumps(revert_entry, ensure_ascii=False) + "\n")
    print(f"[probe]   ✓ reverted to {revert_sha[:8]}, history updated")
    reverted.append(sid)

if reverted:
    print(f"[probe] ⚠️ auto-reverted {len(reverted)} source(s): {reverted}")
    print(f"[probe] ⚠️ Daily summary email will surface this; manual review urgent")
else:
    print(f"[probe] all {len(promoted_today)} promotion(s) verified healthy")
PYEOF

PROBE_EXIT=$?
echo "$LOG_PREFIX Post-commit probe done (exit $PROBE_EXIT)"

exit $EXIT_CODE
