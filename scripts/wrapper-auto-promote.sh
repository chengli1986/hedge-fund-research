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
exit $EXIT_CODE
