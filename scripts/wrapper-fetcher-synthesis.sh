#!/bin/bash
# Weekly wrapper for GMIA fetcher synthesis
# Invokes Claude Code agent to auto-generate fetchers for inaccessible funds
# Schedule: weekly Sunday 02:00 BJT (18:00 UTC Saturday)

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

echo "$LOG_PREFIX Starting fetcher synthesis session..."

# 若无目标则提前退出，不启动 Agent
TARGET_COUNT=$(cd "$REPO_DIR" && python3 synthesize_fetchers.py | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))")
if [ "$TARGET_COUNT" -eq 0 ]; then
    echo "$LOG_PREFIX No inaccessible targets to process. Exiting."
    exit 0
fi
echo "$LOG_PREFIX Found $TARGET_COUNT target(s)."

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

PROGRAM_MD="$REPO_DIR/fetcher-synthesis/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- 最长 20 分钟
- 最多处理 2 个基金
- 注入 fetcher 后必须运行 pytest；若失败则回滚
- 每次成功注入后立即 commit + push
"

echo "$LOG_PREFIX Invoking Claude Code agent..."
echo "$PROMPT" | claude --print \
    --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
    --max-turns 60 \
    2>&1

EXIT_CODE=$?
echo "$LOG_PREFIX Agent exited with code $EXIT_CODE"
exit $EXIT_CODE
