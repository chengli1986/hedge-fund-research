#!/bin/bash
# Weekly wrapper for GMIA fetcher synthesis
# Invokes Claude Code agent to auto-generate fetchers for inaccessible funds
# Schedule: weekly Sunday 02:00 BJT (18:00 UTC Saturday)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

# Prevent concurrent synthesis runs (trial-pass immediate trigger + weekly cron)
LOCK_FILE="/tmp/cron-locks/gmia-fetcher-synthesis.lock"
mkdir -p /tmp/cron-locks
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$LOG_PREFIX Another fetcher-synthesis instance is running. Exiting."
    exit 0
fi

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
TARGETS_JSON=$(cd "$REPO_DIR" && python3 synthesize_fetchers.py)
TARGET_COUNT=$(echo "$TARGETS_JSON" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
# The first 2 ids are what this session is expected to process (program.md's
# "handle at most 2 funds"). backfill marks any of these the agent leaves undone.
PLANNED_IDS=$(echo "$TARGETS_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join(t['id'] for t in d[:2]))")
if [ "$TARGET_COUNT" -eq 0 ]; then
    echo "$LOG_PREFIX No inaccessible targets to process. Exiting."
    exit 0
fi
echo "$LOG_PREFIX Found $TARGET_COUNT target(s)."

# Timestamp before the agent runs — the summary email reports only the history
# entries appended at/after this instant (this session's work), so concurrent
# or prior runs don't bleed into the digest.
SYNTH_RUN_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

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

CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
echo "$LOG_PREFIX Invoking Claude Code agent ($("$CLAUDE_BIN" --version 2>/dev/null | head -1))..."
echo "$PROMPT" | "$CLAUDE_BIN" --print \
    --dangerously-skip-permissions \
    --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
    --max-turns 60 \
    2>&1

EXIT_CODE=$?
echo "$LOG_PREFIX Agent exited with code $EXIT_CODE"

# ── Backfill: mark this session's planned targets the agent left unprocessed ──
# The agent can silently skip a target (never writing synthesis_outcome). Without
# this the candidate stays inaccessible forever — no history, no auto_reject
# 3-strike, no alert (the franklin-templeton "stuck 8 weeks" mode). Run BEFORE
# reconcile so the auto-marked failures flow into history this session.
echo "$LOG_PREFIX Backfilling unprocessed targets..."
BACKFILL_OUTPUT=$(python3 "$REPO_DIR/scripts/backfill_failed_synthesis.py" \
    --planned-ids "$PLANNED_IDS" --run-start "$SYNTH_RUN_START" 2>&1) || \
    echo "$LOG_PREFIX WARN: backfill_failed_synthesis exited non-zero"
echo "$BACKFILL_OUTPUT"
BACKFILLED=$(echo "$BACKFILL_OUTPUT" | grep -oP 'marked \K\d+' | head -1)
BACKFILLED=${BACKFILLED:-0}

# Reconcile any synthesis outcomes the agent recorded in fund_candidates.json
# into the time-series log (now also picks up the backfilled failures above).
# Idempotent — safe to run repeatedly.
# Capture appended count so heartbeat can detect "agent ran but recorded nothing".
echo "$LOG_PREFIX Syncing synthesis history..."
RECONCILE_OUTPUT=$(python3 "$REPO_DIR/scripts/sync_synthesis_history.py" --lookback-days 1 2>&1) || \
    echo "$LOG_PREFIX WARN: sync_synthesis_history exited non-zero"
echo "$RECONCILE_OUTPUT"

# Parse "appended N entries" from reconcile output; default 0 if not found
APPENDED=$(echo "$RECONCILE_OUTPUT" | grep -oP 'appended \K\d+' | head -1)
APPENDED=${APPENDED:-0}

# ── Heartbeat: always write one line per session, even when agent did nothing ──
# Without this, an agent that crashed silently or stopped writing
# fund_candidates.json fields would yield zero history entries and stats would
# lie ("0 attempts"). Heartbeat exit 1 → wrapper alerts via cron-wrapper.
python3 "$REPO_DIR/scripts/write_session_heartbeat.py" \
    --targets-count "$TARGET_COUNT" \
    --reconcile-appended "$APPENDED" \
    --agent-exit "$EXIT_CODE" \
    --backfilled-count "$BACKFILLED"
HEARTBEAT_EXIT=$?

# Quick stats line for the log (does not affect exit code)
python3 "$REPO_DIR/scripts/fetcher_synthesis_stats.py" --window 30 2>&1 | head -10 || true

# ── Summary email: one digest per synthesis session (this run's results) ──────
# Notification only — never affects EXIT_CODE. Reads SMTP/recipient from
# ~/.stock-monitor.env (same as trial daily summary). --since scopes the digest
# to entries appended by this session.
echo "$LOG_PREFIX Sending synthesis summary email..."
# shellcheck disable=SC1090
source "$HOME/.stock-monitor.env" 2>/dev/null || true
SMTP_USER="${SMTP_USER:-}" SMTP_PASS="${SMTP_PASS:-}" MAIL_TO="${MAIL_TO:-}" \
    python3 "$REPO_DIR/scripts/send_synthesis_summary.py" \
        --since "$SYNTH_RUN_START" \
        --targets-count "$TARGET_COUNT" \
    || echo "$LOG_PREFIX WARN: summary email step failed (non-fatal)"

# Propagate inconsistency: if heartbeat detected agent ran without recording
# anything, exit non-zero so cron-wrapper alerts (even if agent itself returned 0).
if [ "$HEARTBEAT_EXIT" -ne 0 ] && [ "$EXIT_CODE" -eq 0 ]; then
    EXIT_CODE=2
fi

exit $EXIT_CODE
