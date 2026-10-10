#!/usr/bin/env bash
# wrapper-profile-refresh.sh — monthly fund-profile freshness refresh.
# Launches a headless Claude (Max Plan) to web-verify AUM + corporate events,
# applies passing drafts via apply_refresh.py (which gates internally through
# validate_refresh), publishes, and emails a summary.
#   ALERT_ONLY=1 (default, Phase 1) => apply_refresh runs --dry-run: gate is
#   evaluated but nothing is written/published. ALERT_ONLY=0 (Phase 2) => apply
#   for real, run the test suite, publish, commit only publish.py +
#   config/sources.json. Tests or publish failing restores both files from the
#   copy taken before the first apply (stage-4 audit 2026-10-09: a bad apply
#   used to be published, committed and pushed with no test run, and the
#   commit took whatever else happened to be staged in this shared tree).
#   PROFILE_REFRESH_REPO / PROFILE_REFRESH_LOCK exist so tests can run this
#   against a throwaway repo; cron never sets them.
#   Pre-merge review (2026-10-10): pytest's own output went to logs/ inside the
#   repo, which tests/conftest.py fingerprints -- so the gate failed on every
#   real run; the agent could edit publish.py itself, after which the "backup"
#   was taken; a rollback copied the backup over whatever another job had
#   written meanwhile; and a rejected draft left in pending_profiles/ failed
#   the suite every month after.
set -uo pipefail

REPO="${PROFILE_REFRESH_REPO:-/home/ubuntu/hedge-fund-research}"
LOCK="${PROFILE_REFRESH_LOCK:-/tmp/cron-locks/profile-refresh.lock}"
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
ALERT_ONLY="${ALERT_ONLY:-1}"
DRY_RUN_FLAG=""
[[ "$ALERT_ONLY" == "1" ]] && DRY_RUN_FLAG="--dry-run"

mkdir -p "$(dirname "$LOCK")"
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
# Outside the repo: the suite fails any run that changes a file under logs/,
# config/, data/, content/ or pending_profiles/ (conftest _no_production_writes).
TEST_LOG="${PROFILE_REFRESH_TEST_LOG:-$HOME/logs/profile-refresh-pytest.log}"
mkdir -p "$(dirname "$TEST_LOG")"

# What the agent must not touch: everything tracked outside pending_profiles/
# and logs/. Taken BEFORE it runs, so its own edits cannot become the baseline.
BACKUP_DIR="$(mktemp -d "$REPO/logs/profile-refresh-backup.XXXXXX")" \
  && cp -p publish.py config/sources.json "$BACKUP_DIR/" \
  || { echo "[profile-refresh] could not back up publish.py/sources.json; nothing run"; exit 1; }
# Committed AND uncommitted content of every tracked file the agent has no
# business touching. Excluded: its own drafts and logs, and the state files
# other jobs commit on their own schedule. HEAD's tree is part of it, so an
# agent that commits its edit is caught too (pre-merge re-review, 2026-10-10).
PROTECTED=(. ':(exclude)pending_profiles' ':(exclude)logs' ':(exclude)config/fund_candidates.json'
           ':(exclude)config/trial-state.json' ':(exclude)config/inspection_state.json')
tracked_state() {
  # ls-tree takes no ":(exclude)" pathspec (it fails and prints nothing, which
  # hid a committed edit), so filter its listing instead.
  git ls-tree -r HEAD | grep -vE $'\t(pending_profiles/|logs/|config/(fund_candidates|trial-state|inspection_state)\\.json$)' | sha256sum
  git diff --no-ext-diff HEAD -- "${PROTECTED[@]}" 2>/dev/null | sha256sum
}
STATE_BEFORE="$(tracked_state)"
# IMPORTANT preamble: headless agents load ~/.claude/CLAUDE.md, whose "session
# start = daily log recap" ritual derails this run — the agent tries to read the
# out-of-repo daily-log dir, gets sandbox-blocked, and stalls asking the user a
# question that headless --print can never answer (observed 2026-07-01: zero AUM
# verification, applied=0 not because nothing changed but because the agent never
# did the work). wrapper-candidate-discovery.sh already prevents this with the
# same preamble; mirror it here.
PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat auto-promote/refresh-program.md)"

# 1) agent generates pending_profiles/*.refresh.json (only for funds that changed).
#    --dangerously-skip-permissions is REQUIRED: this repo is not trust-accepted
#    (hasTrustDialogAccepted:false), so without the flag Claude ignores the
#    settings.json allow-list and DENIES WebSearch/WebFetch/Bash(curl) — the exact
#    tools a web-verification task lives on. Confirmed 2026-07-20 dry-run: with the
#    preamble but no flag the agent reached the task correctly but every network
#    tool returned "Permission denied", so it produced zero drafts. Same fix
#    synthesis got in 9776a45. apply_refresh.py still gates every write, and the
#    monthly cron runs ALERT_ONLY-gated, so auto-approving tool use here is bounded.
timeout --kill-after=30 3000 "$CLAUDE_BIN" --print --dangerously-skip-permissions \
  --max-turns 120 "$PROMPT" \
  > logs/profile-refresh-agent.log 2>&1 || echo "[profile-refresh] agent exit $? (max-turns ok)"

APPLIED=(); FLAGGED=()
AGENT_TOUCHED=0
if [[ "$(tracked_state)" != "$STATE_BEFORE" ]]; then
  # The agent writes drafts, nothing else: an edit of its own to publish.py
  # went live with the next publish and was swept into the refresh commit.
  # This cannot tell the agent from a person or job working in the same
  # checkout, so nothing is overwritten: the run applies nothing, commits
  # nothing, and says what changed.
  AGENT_TOUCHED=1
  changed="$(git status --porcelain --untracked-files=no -- "${PROTECTED[@]}" | tr '\n' ' ')"
  FLAGGED+=("tracked files changed while the agent ran (by the agent or someone else: ${changed:-committed}); nothing applied or overwritten -- check by hand")
fi

# 2) apply each draft. apply_refresh.py gates internally via validate_refresh:
#    rc=0 => gate passed (applied, or "would apply" under --dry-run)
#    rc=1 => gate failed (route to human); other rc => skip + flag
shopt -s nullglob
if [[ $AGENT_TOUCHED -eq 0 ]]; then
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
fi
# A rejected draft left in pending_profiles/ is retried and rejected again every
# month, and the suite reads that directory: park it where a person looks.
if [[ "$ALERT_ONLY" != "1" ]]; then
  mkdir -p pending_profiles/flagged
  for draft in pending_profiles/*.refresh.json; do
    mv -f "$draft" pending_profiles/flagged/
  done
fi
APPLIED_STATE="$(sha256sum publish.py config/sources.json 2>/dev/null)"

# Put publish.py + sources.json back, and move this run's drafts out of
# applied/ into rolled_back/ (not back into pending_profiles/, where next
# month's loop would apply them again unseen).
restore_profiles() {
  local why="$1" fid restored=1
  if [[ "$(sha256sum publish.py config/sources.json 2>/dev/null)" != "$APPLIED_STATE" ]]; then
    # Someone else (auto-promote, a person) wrote these files after the apply:
    # copying the backup over them would delete that work.
    restored=0
    why="$why; publish.py/sources.json changed by another writer meanwhile, not overwritten"
  else
    cp -p "$BACKUP_DIR/publish.py" publish.py && cp -p "$BACKUP_DIR/sources.json" config/sources.json \
      || restored=0
  fi
  mkdir -p pending_profiles/rolled_back
  for fid in ${APPLIED[@]+"${APPLIED[@]}"}; do
    mv -f "pending_profiles/applied/$fid.refresh.json" pending_profiles/rolled_back/ 2>/dev/null
    FLAGGED+=("$fid (rolled back: $why)")
  done
  APPLIED=()
  if [[ $restored -eq 1 ]]; then
    echo "[profile-refresh] rolled back ($why): publish.py + sources.json restored"
  else
    FLAGGED+=("RESTORE FAILED after $why: check publish.py/sources.json by hand (copy in $BACKUP_DIR)")
    echo "[profile-refresh] RESTORE FAILED after $why; copy kept in $BACKUP_DIR"
  fi
}

# 3) for real runs that applied something: tests, publish, commit, push.
if [[ "$ALERT_ONLY" != "1" && ${#APPLIED[@]} -gt 0 ]]; then
  python3 -m pytest tests/ -q -x -p no:cacheprovider >>"$TEST_LOG" 2>&1
  test_rc=$?
  if [[ $test_rc -ne 0 ]]; then
    restore_profiles "tests failed (pytest rc=$test_rc)"
  else
    python3 publish.py >>logs/profile-refresh.log 2>&1
    publish_rc=$?
    # 3 = page written, docs-site sync failed: the page is fine, keep going.
    if [[ $publish_rc -ne 0 && $publish_rc -ne 3 ]]; then
      restore_profiles "publish.py exit $publish_rc"
      python3 publish.py >>logs/profile-refresh.log 2>&1 \
        || FLAGGED+=("republishing the restored page failed too: see logs/profile-refresh.log")
    else
      [[ $publish_rc -eq 3 ]] && FLAGGED+=("page published, but the docs-site copy failed to sync (publish.py exit 3): see logs/profile-refresh.log")
      # pathspec: commit these two files only, never whatever else is staged
      git add -- publish.py config/sources.json \
        && git commit -q -m "chore(profiles): monthly AUM/event refresh ($(date -u +%Y-%m-%d))" \
             -- publish.py config/sources.json \
        && git push -q \
        || FLAGGED+=("commit/push of the applied refresh failed: files changed but not in git")
    fi
  fi
fi
if [[ "${FLAGGED[*]-}" != *"RESTORE FAILED"* ]]; then
  rm -rf "$BACKUP_DIR"
fi

# 4) summary email (notification only — never affect exit code). cron does not
#    source the env file; pass the three settings explicitly, as the synthesis
#    and discovery wrappers do.
#    Newline-delimit so flagged entries (which contain spaces) stay intact.
set +u   # an unset reference inside the env file must not end the run here
source "$HOME/.stock-monitor.env" 2>/dev/null || true
set -u
applied_str="$(printf '%s\n' ${APPLIED[@]+"${APPLIED[@]}"})"
flagged_str="$(printf '%s\n' ${FLAGGED[@]+"${FLAGGED[@]}"})"
SMTP_USER="${SMTP_USER:-}" SMTP_PASS="${SMTP_PASS:-}" MAIL_TO="${MAIL_TO:-}" \
python3 scripts/send_refresh_summary.py \
  --applied "$applied_str" --flagged "$flagged_str" \
  --alert-only "$ALERT_ONLY" >>logs/profile-refresh.log 2>&1 || echo "[profile-refresh] summary email WARN (not sent)"

echo "[profile-refresh] done: applied=${#APPLIED[@]} flagged=${#FLAGGED[@]} alert_only=$ALERT_ONLY"
