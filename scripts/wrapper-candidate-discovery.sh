#!/bin/bash
# Cron wrapper for candidate fund discovery with LLM analysis
# Runs daily: Python pipeline (rules) + Claude agent (deep analysis + search)
# Schedule: 03:00 BJT (19:00 UTC)

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

echo "$LOG_PREFIX Starting candidate fund discovery session..."

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

# Read the program instructions
PROGRAM_MD="$REPO_DIR/candidate-discovery/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 15 minutes for this session
- Cost control: max 5 WebSearch, max 10 WebFetch, max 2 new seeds
- After all work, commit and push any changes
- Output a brief summary at the end
"

# 15-minute timeout + 30s grace
# --allowedTools: scoped to what the agent actually needs
#   Bash — run Python scripts, git operations
#   Read/Edit/Write — update config files
#   WebFetch — fetch fund research pages for analysis
#   WebSearch — discover new candidate funds
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 930 "$CLAUDE_BIN" -p \
    --allowedTools "Bash Read Edit Write WebFetch WebSearch" \
    --model sonnet \
    "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX Candidate discovery TIMED OUT after 15 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX Candidate discovery failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX Candidate discovery finished successfully"
fi

# Push any commits regardless of exit code
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
else
    echo "$LOG_PREFIX No new commits to push"
fi
