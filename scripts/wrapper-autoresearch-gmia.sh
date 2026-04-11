#!/bin/bash
# Cron wrapper for GMIA entrypoint autoresearch
# Runs 5-7 experiments per session (evaluate < 1s), 15-minute timeout
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

echo "$LOG_PREFIX Starting GMIA autoresearch session..."

# Skip if last 5 experiments are all SAME (scorer saturated, nothing to optimize)
RESULTS_TSV="$REPO_DIR/autoresearch/results.tsv"
if [ -f "$RESULTS_TSV" ] && [ "$(wc -l < "$RESULTS_TSV")" -ge 5 ]; then
    CONSECUTIVE_SAME=$(tail -5 "$RESULTS_TSV" | awk -F'\t' '{print $3}' | grep -c "^SAME$" || true)
    if [ "$CONSECUTIVE_SAME" -eq 5 ]; then
        echo "$LOG_PREFIX Skipping: last 5 experiments all SAME — scorer saturated, collect more negative samples first"
        exit 0
    fi
fi

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

PROGRAM_MD="$REPO_DIR/autoresearch/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 15 minutes for this session
- Run 5-7 experiments, then stop (evaluate is < 1 second, so you can iterate fast)
- After all experiments, if any commits were kept, run: cd $REPO_DIR && git push
- Log EVERY experiment (kept or discarded) to autoresearch/results.tsv
"

# 15-minute timeout + 30s grace
# --dangerously-skip-permissions: required because AR edits scorer_weights.json
# and uses git reset --hard to discard failed experiments
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 900 "$CLAUDE_BIN" -p --dangerously-skip-permissions --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX GMIA autoresearch TIMED OUT after 15 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX GMIA autoresearch failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX GMIA autoresearch finished successfully"
fi

# Push any kept commits
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
else
    echo "$LOG_PREFIX No new commits to push"
fi

# Sync experiment history to docs page
echo "$LOG_PREFIX Syncing experiment history to autoresearch.html..."
python3 /home/ubuntu/infra-scripts/sync-ar-history.py gmia "$REPO_DIR/autoresearch/results.tsv" Yield 2>&1 || echo "$LOG_PREFIX WARNING: history sync failed (non-fatal)"
