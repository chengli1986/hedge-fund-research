#!/usr/bin/env bash
# wrapper-profile-refresh.sh — monthly fund-profile freshness refresh.
# Launches a headless Claude (Max Plan) to web-verify AUM + corporate events,
# applies passing drafts via apply_refresh.py (which gates internally through
# validate_refresh), publishes, and emails a summary.
#   ALERT_ONLY=1 (default, Phase 1) => apply_refresh runs --dry-run: gate is
#   evaluated but nothing is written/published. ALERT_ONLY=0 (Phase 2) => apply
#   for real + publish.
set -uo pipefail

REPO="/home/ubuntu/hedge-fund-research"
LOCK="/tmp/cron-locks/profile-refresh.lock"
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
ALERT_ONLY="${ALERT_ONLY:-1}"
DRY_RUN_FLAG=""
[[ "$ALERT_ONLY" == "1" ]] && DRY_RUN_FLAG="--dry-run"

mkdir -p /tmp/cron-locks
exec 9>"$LOCK"
if ! flock -n 9; then echo "[profile-refresh] another run holds the lock; exit"; exit 0; fi

# --- Max Plan auth: unset API key so claude uses the subscription, restore after
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  SAVED_KEY="$(grep -h '^ANTHROPIC_API_KEY=' "$HOME/.openclaw/.env" "$HOME/.stock-monitor.env" 2>/dev/null | head -1 | cut -d= -f2-)"
else
  SAVED_KEY="${ANTHROPIC_API_KEY}"
fi
unset ANTHROPIC_API_KEY

cleanup() {
  local pids; pids=$(jobs -p 2>/dev/null)
  [[ -n "$pids" ]] && kill $pids 2>/dev/null
  [[ -n "${SAVED_KEY:-}" ]] && export ANTHROPIC_API_KEY="$SAVED_KEY"
}
trap cleanup EXIT

cd "$REPO" || exit 1
mkdir -p logs
PROMPT="$(cat auto-promote/refresh-program.md)"

# 1) agent generates pending_profiles/*.refresh.json (only for funds that changed)
timeout --kill-after=30 1500 "$CLAUDE_BIN" --print --max-turns 120 "$PROMPT" \
  > logs/profile-refresh-agent.log 2>&1 || echo "[profile-refresh] agent exit $? (max-turns ok)"

# 2) apply each draft. apply_refresh.py gates internally via validate_refresh:
#    rc=0 => gate passed (applied, or "would apply" under --dry-run)
#    rc=1 => gate failed (route to human); other rc => skip + flag
APPLIED=(); FLAGGED=()
shopt -s nullglob
for draft in pending_profiles/*.refresh.json; do
  fid="$(basename "$draft" .refresh.json)"
  python3 scripts/apply_refresh.py "$fid" $DRY_RUN_FLAG >>logs/profile-refresh.log 2>&1
  rc=$?
  if [[ $rc -eq 0 ]]; then
    APPLIED+=("$fid")
  else
    FLAGGED+=("$fid (apply_refresh rc=$rc)")
  fi
done

# 3) publish only when something was actually applied for real (not alert-only)
if [[ "$ALERT_ONLY" != "1" && ${#APPLIED[@]} -gt 0 ]]; then
  python3 publish.py >>logs/profile-refresh.log 2>&1 \
    && git add publish.py config/sources.json \
    && git commit -m "chore(profiles): monthly AUM/event refresh ($(date -u +%Y-%m-%d))" \
    && git push
fi

# 4) summary email (notification only — never affect exit code).
#    Newline-delimit so flagged entries (which contain spaces) stay intact.
applied_str="$(printf '%s\n' ${APPLIED[@]+"${APPLIED[@]}"})"
flagged_str="$(printf '%s\n' ${FLAGGED[@]+"${FLAGGED[@]}"})"
python3 scripts/send_refresh_summary.py \
  --applied "$applied_str" --flagged "$flagged_str" \
  --alert-only "$ALERT_ONLY" >>logs/profile-refresh.log 2>&1 || echo "[profile-refresh] summary email WARN"

echo "[profile-refresh] done: applied=${#APPLIED[@]} flagged=${#FLAGGED[@]} alert_only=$ALERT_ONLY"
