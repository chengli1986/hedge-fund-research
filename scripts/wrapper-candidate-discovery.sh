#!/bin/bash
# Cron wrapper for candidate fund discovery with LLM analysis
# Runs daily: Python pipeline (rules) + Claude agent (deep analysis + search)
# Schedule: 03:00 BJT (19:00 UTC)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# A timestamp per line: LOG_PREFIX was computed once, so every line of a
# run read 09:00:01 and the log could not say how long any step took
# (stage 6 minor, 2026-10-18).
ts() { TZ='Asia/Shanghai' date '+[%Y-%m-%d %H:%M:%S]'; }

# Time limits, all inside cron-wrapper's --timeout for this job (stage 6
# #11): the agent used to get 930s+30s of a 960s job, so a long session left
# the guard, trial manager, push and report no time, and a kill there could
# land mid-write.
JOB_TIMEOUT_S=960        # = cron-wrapper --timeout of gmia-candidate-discovery
PHASE1_TIMEOUT_S=180
POST_RESERVE_S=240       # guard, stall routing, trial manager, push, report
AGENT_MAX_S=600          # sessions take ~1 min; the longest seen was 470s

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$(ts) Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

# A push that fails used to print a WARNING nobody read; the commits sat
# local and the job exited 0 (stage 6 minor). Collected here, shown in the
# report and turned into a non-zero exit at the end.
PUSH_FAILED=""
# Set when the status guard could not check the agent's edits (no snapshot,
# crash, no "guard: N" line). Then nothing from this run is pushed: an
# unchecked "promoted" must not reach GitHub (2026-10-20 audit).
GUARD_FAILED=""
push_or_flag() {
    if [ -n "$GUARD_FAILED" ]; then
        echo "$(ts) WARNING: git push ($1) held: status guard did not run ($GUARD_FAILED)"
        return
    fi
    if ! git push 2>&1; then
        echo "$(ts) WARNING: git push ($1) failed"
        PUSH_FAILED="${PUSH_FAILED:+$PUSH_FAILED, }$1"
    fi
}

echo "$(ts) Starting candidate fund discovery session..."

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
# Save it first — the trial manager needs it later for direct Haiku API calls
# Read from env file if not already in environment (cron doesn't source ~/.stock-monitor.env)
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
    # cron doesn't source env files; search both locations, strip surrounding quotes
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

# Read the program instructions
PROGRAM_MD="$REPO_DIR/candidate-discovery/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$(ts) ERROR: $PROGRAM_MD not found"
    exit 1
fi

# 15-minute timeout + 30s grace
# --allowedTools: scoped to what the agent actually needs
#   Bash — run Python scripts, git operations
#   Read/Edit/Write — update config files
#   WebFetch — fetch fund research pages for analysis
#   WebSearch — discover new candidate funds

# --- Phase 1: the rule pipeline, run here, before the guard's snapshot ---
# Stage 6 #9 (2026-10-18): the agent used to run these three scripts itself,
# inside the guard's before/after window, so the guard had to accept their
# status changes on the evidence of a timestamp in the same file the agent
# edits. Run first, they are outside the window; the agent only reads them.
echo "$(ts) Phase 1: discover / screen / entrypoints..."
PHASE1_OUT=$(timeout --kill-after=10 "$PHASE1_TIMEOUT_S" \
    bash "$SCRIPT_DIR/run-candidate-discovery.sh" 2>&1)
PHASE1_EXIT=$?
echo "$PHASE1_OUT"
if [ "$PHASE1_EXIT" -ne 0 ]; then
    echo "$(ts) WARNING: Phase 1 pipeline exited $PHASE1_EXIT"
fi

AGENT_TIMEOUT_S=$(( JOB_TIMEOUT_S - SECONDS - POST_RESERVE_S - 30 ))
[ "$AGENT_TIMEOUT_S" -gt "$AGENT_MAX_S" ] && AGENT_TIMEOUT_S=$AGENT_MAX_S
[ "$AGENT_TIMEOUT_S" -lt 60 ] && AGENT_TIMEOUT_S=60

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of $(( AGENT_TIMEOUT_S / 60 )) minutes for this session
- Cost control: the WebSearch / WebFetch / new-seed limits under Constraints above are this session's limits
- After all work, commit any changes but do NOT push: the wrapper pushes after its checks
- Output a brief summary at the end

## Phase 1 output (the wrapper already ran the three scripts before this session)
$(printf '%s\n' "$PHASE1_OUT" | tail -n 120)
"

# Snapshot candidate statuses BEFORE the agent runs so the post-run guard can
# detect any illegal status change it makes (agent may only set watchlist/
# rejected; promotion belongs to trial-manager after a trial).
GUARD_BEFORE="/tmp/gmia-guard-before-$$.json"
cp "$REPO_DIR/config/fund_candidates.json" "$GUARD_BEFORE" 2>/dev/null || true

CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
# The agent commits, the wrapper pushes -- after the guard has stamped
# added_at/status_since. 2026-10-17 the agent pushed its new seed itself
# (d63457c, no added_at) and CI went red until the guard's stamp landed in a
# later commit. NO_PUSH_ENV points origin's push URL nowhere for the agent's
# git only (env-scoped config, not written anywhere).
NO_PUSH_ENV=(GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl
             GIT_CONFIG_VALUE_0=/nonexistent/push-disabled-wrapper-pushes)
env "${NO_PUSH_ENV[@]}" timeout --kill-after=30 "$AGENT_TIMEOUT_S" "$CLAUDE_BIN" -p \
    --allowedTools "Bash Read Edit Write WebFetch WebSearch" \
    --model sonnet \
    "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$(ts) Candidate discovery TIMED OUT after ${AGENT_TIMEOUT_S}s"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$(ts) Candidate discovery failed (exit code: $EXIT_CODE)"
else
    echo "$(ts) Candidate discovery finished successfully"
fi

# --- Status guard: revert any illegal status change the agent made ---
# The agent must never set "promoted" (trial-manager owns promotion, only after a
# trial PASS). If it did, revert + commit + alert. Self-heals the 2026-06-10
# Guggenheim-class orphan bug. Quiet no-op ("guard: 0") on a well-behaved run.
if [ ! -f "$GUARD_BEFORE" ]; then
    GUARD_FAILED="no pre-agent snapshot"
else
    GUARD_OUT=$(cd "$REPO_DIR" && python3 guard_candidate_status.py \
        --before "$GUARD_BEFORE" --after config/fund_candidates.json 2>&1)
    GUARD_RC=$?
    echo "$GUARD_OUT"
    GUARD_N=$(printf '%s\n' "$GUARD_OUT" | sed -n 's/^guard: \([0-9]*\) .*/\1/p')
    # A crash used to read as "0 illegal changes" (empty GUARD_N -> 0): the
    # stamps were committed and the agent's edits pushed unchecked.
    if [ "$GUARD_RC" -ne 0 ] || [ -z "$GUARD_N" ]; then
        GUARD_FAILED="guard_candidate_status.py exit $GUARD_RC"
        echo "$(ts) ERROR: status guard failed ($GUARD_FAILED); nothing from this run will be pushed"
    fi
fi
if [ -z "$GUARD_FAILED" ]; then
    # Stamps (added_at, status_since) are committed even when nothing was
    # reverted, so the push below never carries a seed without added_at.
    # candidate_entrypoints.json: Phase 1 writes it; the agent may not commit.
    # Commits below name their files: whatever else is staged in this shared
    # checkout stays staged and is not pushed (2026-10-19 review). They name
    # only what was staged: a path git doesn't know fails the whole commit
    # (2026-10-20 review).
    GUARD_FILES="config/fund_candidates.json config/candidate_entrypoints.json"
    git add $GUARD_FILES 2>/dev/null \
        || { GUARD_FILES="config/fund_candidates.json"; git add $GUARD_FILES; }
    if [ "${GUARD_N:-0}" -gt 0 ]; then
        git diff --cached --quiet -- $GUARD_FILES || git commit -m "guard: revert $GUARD_N illegal agent status change(s) (auto)" -- $GUARD_FILES
    else
        git diff --cached --quiet -- $GUARD_FILES || git commit -m "guard: stamp added_at/status_since (auto)" -- $GUARD_FILES
    fi
    if [ "${GUARD_N:-0}" -gt 0 ]; then
        echo "$(ts) GUARD reverted $GUARD_N illegal status change(s)"
        source ~/.stock-monitor.env 2>/dev/null || true
        GUARD_REVERTS="$(printf '%s\n' "$GUARD_OUT" | grep '^REVERTED' || true)" \
        GUARD_SUBJECT="[GMIA GUARD] reverted $GUARD_N illegal agent status change(s)" \
        GUARD_TO="${MAIL_TO:-ch_w10@outlook.com}" SMTP_USER="${SMTP_USER:-}" SMTP_PASS="${SMTP_PASS:-}" \
        python3 -c "
import os, smtplib
from email.mime.text import MIMEText
u=os.environ.get('SMTP_USER',''); to=os.environ['GUARD_TO']
if not u or not os.environ.get('SMTP_PASS'): raise SystemExit(0)
body=('Discovery agent set a forbidden status and was auto-reverted:\n\n'
      + os.environ.get('GUARD_REVERTS','')
      + '\n\nPromotion is trial-managers job, only after a trial PASS. If recurring, check candidate-discovery/program.md.')
m=MIMEText(body,'plain','utf-8'); m['Subject']=os.environ['GUARD_SUBJECT']; m['From']=u; m['To']=to
try:
    with smtplib.SMTP_SSL('smtp.163.com',465,timeout=10) as s:
        s.login(u, os.environ['SMTP_PASS']); s.sendmail(u,[to],m.as_string())
except Exception as e:
    print('guard alert email failed:', e)
" 2>&1 || true
    fi
fi
rm -f "$GUARD_BEFORE"

# Push the agent's commits and the guard's, regardless of exit code
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
# No origin/main ref (pruned, never fetched) is not "nothing to push": try.
if [ -z "$REMOTE_HEAD" ] || [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$(ts) Pushing new commits..."
    push_or_flag "discovery + guard"
else
    echo "$(ts) No new commits to push"
fi

# --- Stall detection: auto-route candidates stuck without progress ---
# Runs after guard (so status_since is accurate for the agent's own legal
# edits too) and before trial-manager. Self-heals the 2026-07-14 Nuveen/
# Longleaf/Lord Abbett/Invesco class of bug (seed candidates stuck forever
# because stage1's httpx crawl has no JS-render fallback) instead of relying
# on a manual audit to notice.
cd "$REPO_DIR"
STALL_OUT=$(python3 detect_stalled_candidates.py 2>&1)
echo "$STALL_OUT"
STALL_N=$(printf '%s\n' "$STALL_OUT" | sed -n 's/^stall: \([0-9]*\) .*/\1/p')
if [ "${STALL_N:-0}" -gt 0 ]; then
    echo "$(ts) Auto-routed $STALL_N stalled candidate(s)"
    git add config/fund_candidates.json
    git diff --cached --quiet -- config/fund_candidates.json || git commit -m "stall: auto-route $STALL_N stalled candidate(s) (auto)" -- config/fund_candidates.json
    push_or_flag "stall"
fi

# --- Trial manager (runs regardless of discovery exit code) ---
# Re-export the API key for Haiku quality sampling
export ANTHROPIC_API_KEY="$SAVED_ANTHROPIC_API_KEY"
echo "$(ts) Running GMIA trial manager..."
python3 "$REPO_DIR/gmia-trial-manager.py" run 2>&1
TRIAL_EXIT=$?
if [ $TRIAL_EXIT -ne 0 ]; then
    echo "$(ts) WARNING: trial manager exited with code $TRIAL_EXIT"
fi

# Commit trial state if changed
cd "$REPO_DIR"
if ! git diff --quiet config/trial-state.json config/fund_candidates.json 2>/dev/null; then
    git add config/trial-state.json config/fund_candidates.json
    git diff --cached --quiet -- config/trial-state.json config/fund_candidates.json || git commit -m "trial: update GMIA trial state $(TZ='Asia/Shanghai' date '+%Y-%m-%d')" -- config/trial-state.json config/fund_candidates.json
    echo "$(ts) Trial state committed"
    push_or_flag "trial state"
fi

# --- Immediate synthesis trigger for trial-pass candidates ---
# If any synthesis_priority candidate has no registered fetcher, kick off
# fetcher-synthesis now instead of waiting up to 7 days for the weekly cron.
# The synthesis wrapper has its own flock guard, so concurrent weekly cron
# runs exit cleanly without double-processing.
# Counted by synthesize_fetchers' own rule (stage 6 minor, 2026-10-18): the
# copy that was here imported fetch_articles, and on an import error counted
# every flagged candidate -- 8 of them already in production -- as fetcherless
# and launched a synthesis. Any error now means "no trigger", said in the log.
SYNTH_PRIORITY_COUNT=$(cd "$REPO_DIR" && python3 -c "
import synthesize_fetchers as sf
print(len(sf.list_priority_targets()))" 2>&1)
if ! [[ "$SYNTH_PRIORITY_COUNT" =~ ^[0-9]+$ ]]; then
    echo "$(ts) WARNING: could not count synthesis_priority targets, not triggering: $SYNTH_PRIORITY_COUNT"
    SYNTH_PRIORITY_COUNT=0
fi

if [ "${SYNTH_PRIORITY_COUNT:-0}" -gt 0 ]; then
    echo "$(ts) $SYNTH_PRIORITY_COUNT synthesis_priority target(s) detected — launching fetcher-synthesis in background"
    SYNTH_LOG="$HOME/logs/gmia-fetcher-synthesis.log"
    # Restore original API key state (synthesis wrapper will unset it itself for Max plan auth)
    export ANTHROPIC_API_KEY="$SAVED_ANTHROPIC_API_KEY"
    # Through cron-wrapper, like the weekly run: its lock, its ops-status line
    # and its failure mail (stage 6 minor -- a triggered run had none of them).
    CRON_WRAPPER="${CRON_WRAPPER:-$HOME/cron-wrapper.sh}"
    nohup "$CRON_WRAPPER" --name gmia-fetcher-synthesis --timeout 1200 --lock -- \
        bash "$REPO_DIR/scripts/wrapper-fetcher-synthesis.sh" >> "$SYNTH_LOG" 2>&1 &
    SYNTH_PID=$!
    disown $SYNTH_PID
    echo "$(ts) Fetcher-synthesis started (PID $SYNTH_PID) — log: $SYNTH_LOG"
else
    echo "$(ts) No synthesis_priority targets without fetchers — skipping immediate trigger"
fi

# --- Email report ---
echo "$(ts) Sending email report..."
source ~/.stock-monitor.env 2>/dev/null || true

export REPO_DIR EXIT_CODE SMTP_USER SMTP_PASS PUSH_FAILED GUARD_FAILED
python3 << 'PYEOF'
import html, importlib.util, json, os, smtplib, sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.environ.get("REPO_DIR", "."))
from status_util import awaiting_auto_promote, days_since, days_since_last_new_candidate
from detect_stalled_candidates import DEFAULT_THRESHOLD_DAYS as STALL_THRESHOLD_DAYS

BJT = timezone(timedelta(hours=8))
now_dt = datetime.now(BJT)
now_str = now_dt.strftime("%Y-%m-%d %H:%M BJT")
date_str = now_dt.strftime("%Y-%m-%d")
repo = Path(os.environ.get("REPO_DIR", "."))
exit_code = int(os.environ.get("EXIT_CODE", "1"))

# Load data
candidates = json.loads((repo / "config/fund_candidates.json").read_text())
sources_data = json.loads((repo / "config/sources.json").read_text())
production_sources = sources_data.get("sources", [])
production_ids = {s["id"] for s in production_sources}

trial_state_path = repo / "config/trial-state.json"
trial_data = json.loads(trial_state_path.read_text()) if trial_state_path.exists() else {}
active_trial_ids = {t["id"] for t in trial_data.get("active_trials", [])}
active_trial_info = {t["id"]: t for t in trial_data.get("active_trials", [])}

# Classify candidates into groups
cand_map = {c["id"]: c for c in candidates}

# Waiting for tonight's auto-promote: the rule wrapper-auto-promote.sh wires
# by (status "promoted", not yet in sources.json), not "has a trial pass" --
# PineBridge passed in May, was rejected in July and was still listed here.
pass_pending_ids = set(awaiting_auto_promote(candidates, production_ids))
_pass_history = {h["id"]: h for h in trial_data.get("history", []) if h.get("outcome") == "pass"}
pass_pending = []
for cid in pass_pending_ids:
    if cid not in cand_map:
        continue
    c = dict(cand_map[cid])
    h = _pass_history.get(cid, {})
    c["_trial_days"] = h.get("days_with_articles", 0)
    c["_trial_quality"] = h.get("avg_quality_score", 0.0)
    c["_trial_articles"] = h.get("total_articles", 0)
    pass_pending.append(c)
pass_pending.sort(key=lambda c: -(c.get("fit_score") or 0))

# The trial manager's own queue rule, not a copy (stage 6 minor, 2026-10-18):
# the copy counted every visitable candidate, incl. LOW quality and ones
# whose trial verdict stands, which the trial manager will never start.
_spec = importlib.util.spec_from_file_location("gmia_trial_manager", repo / "gmia-trial-manager.py")
_tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tm)
queue = [c for c in _tm.get_trial_queue(trial_data)
         if c["id"] not in production_ids and c["id"] not in pass_pending_ids]
inaccessible = [c for c in candidates if c["status"] == "inaccessible"]
seed_statuses = {"seed", "discovered", "screened", "screen_failed"}
seeds = [c for c in candidates if c["status"] in seed_statuses]
rejected = [c for c in candidates if c["status"] == "rejected"]

# 策略覆盖地图
def q_badge(quality: str) -> str:
    color = {"HIGH": "#22863a", "MEDIUM": "#e36209", "LOW": "#cb2431"}.get(quality, "#959da5")
    return f'<span style="color:{color};font-weight:bold;font-size:11px">{html.escape(str(quality))}</span>'

def fit_pct(score) -> str:
    if score is None:
        return '<span style="color:#959da5">—</span>'
    pct = int(score * 100)
    color = "#22863a" if pct >= 70 else ("#e36209" if pct >= 50 else "#cb2431")
    return f'<span style="color:{color};font-weight:bold">{pct}%</span>'

def tags_html(tags: list) -> str:
    return " ".join(
        f'<span style="background:#ddf4ff;color:#0969da;border-radius:3px;padding:1px 5px;font-size:10px">{html.escape(str(t))}</span>'
        for t in tags
    )

def notes_short(notes_full: str) -> str:
    n = (notes_full or "")
    return html.escape(n[:90]) + ("…" if len(n) > 90 else "")

def stall_badge(c: dict) -> str:
    """Days stuck in the current status, once >= the auto-stall threshold.
    Mirrors detect_stalled_candidates.py's own threshold so the email warns
    before the next cron run would auto-route the candidate."""
    days = days_since(c)
    if days is None or days < STALL_THRESHOLD_DAYS:
        return ""
    color = "#cf222e" if days >= STALL_THRESHOLD_DAYS * 2 else "#9a6700"
    return f' <span style="color:{color};font-weight:bold;font-size:10px">⚠️ {days}d</span>'

def group_table(items: list, show_fit: bool = True, show_trial: bool = False, show_stall: bool = False) -> str:
    if not items:
        return '<p style="color:#959da5;font-size:12px;padding:6px 0;margin:0">（空）</p>'
    rows = ""
    for c in items:
        trial_day = ""
        if show_trial and c["id"] in active_trial_info:
            t = active_trial_info[c["id"]]
            start = datetime.fromisoformat(t["start_date"]).replace(tzinfo=timezone.utc)
            day = (now_dt.replace(tzinfo=None) - start.replace(tzinfo=None)).days + 1
            trial_day = f' <span style="color:#0969da;font-size:10px">Day {day}/7</span>'
        stall = stall_badge(c) if show_stall else ""
        fit_cell = fit_pct(c.get("fit_score")) if show_fit else "—"
        notes_full = c.get("notes") or ""
        is_rec = notes_full.startswith("RECOMMEND")
        bg = " style=\"background:#f0fff4\"" if is_rec else ""
        rows += (
            f'<tr{bg}>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:500">{html.escape(str(c["name"]))}{trial_day}{stall}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{fit_cell}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{q_badge(c.get("quality","—"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{html.escape(str(c.get("topics") or "—"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{tags_html(c.get("strategy_tags",[]))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{notes_short(notes_full)}</td>'
            f'</tr>\n'
        )
    header = (
        '<tr style="background:#f6f8fa">'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Fund</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Fit</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Quality</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Topics</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Tags</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Notes</th>'
        '</tr>'
    )
    return f'<table style="width:100%;border-collapse:collapse;font-size:13px">{header}{rows}</table>'

def section(emoji: str, title: str, subtitle: str, items: list, color: str,
            show_fit: bool = True, show_trial: bool = False, show_stall: bool = False) -> str:
    count = len(items)
    return f"""
<div style="margin:0 0 20px">
  <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
    <span style="font-size:15px;font-weight:700;color:{color}">{emoji} {title}</span>
    <span style="background:{color};color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600">{count}</span>
    <span style="color:#959da5;font-size:12px">{subtitle}</span>
  </div>
  {group_table(items, show_fit=show_fit, show_trial=show_trial, show_stall=show_stall)}
</div>"""

def pass_pending_table(items: list) -> str:
    if not items:
        return '<p style="color:#959da5;font-size:12px;padding:6px 0;margin:0">（空）</p>'
    rows = ""
    for c in items:
        q_score = c.get("_trial_quality", 0.0)
        q_color = "#22863a" if q_score >= 0.7 else ("#e36209" if q_score >= 0.5 else "#cb2431")
        rows += (
            f'<tr style="background:#faf0ff">'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:500">{html.escape(str(c["name"]))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{fit_pct(c.get("fit_score"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{q_badge(c.get("quality","—"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:600;color:{q_color}">'
            f'{q_score:.2f}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;color:#586069;font-size:11px">'
            f'{c.get("_trial_days",0)}天 / {c.get("_trial_articles",0)}篇</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{tags_html(c.get("strategy_tags",[]))}</td>'
            f'</tr>\n'
        )
    header = (
        '<tr style="background:#f6f8fa">'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Fund</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Fit</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Quality</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Trial Score</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Trial Stats</th>'
        '<th style="text-align:left;padding:5px 8px;border-bottom:2px solid #e1e4e8;font-size:12px">Tags</th>'
        '</tr>'
    )
    return f'<table style="width:100%;border-collapse:collapse;font-size:13px">{header}{rows}</table>'

def pass_pending_section(items: list) -> str:
    count = len(items)
    color = "#8250df"
    return f"""
<div style="margin:0 0 20px;border-left:3px solid {color};padding-left:10px">
  <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
    <span style="font-size:15px;font-weight:700;color:{color}">✅ Trial Passed — Awaiting Auto-Promote</span>
    <span style="background:{color};color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600">{count}</span>
    <span style="color:#959da5;font-size:12px">Trial 已通过·等 auto-promote 次日 02:30 BJT 自动 wire 入 sources.json（无需人工操作）</span>
  </div>
  {pass_pending_table(items)}
</div>"""

# Build active trial candidates list (validated with active trial)
active_trial_candidates = [
    cand_map[tid] for tid in active_trial_info if tid in cand_map
]

# Production section uses sources.json entries (not fund_candidates)
prod_items = [
    {"id": s["id"], "name": s.get("name", s["id"]),
     "fit_score": None, "quality": "HIGH",
     "topics": s.get("topics", "—"),
     "strategy_tags": s.get("strategy_tags", []),
     "notes": "Active production source"}
    for s in production_sources
]

stats = {
    "production": len(prod_items),
    "trials": len(active_trial_candidates),
    "pass_pending": len(pass_pending),
    "queue": len(queue),
    "inaccessible": len(inaccessible),
    "seed": len(seeds),
    "rejected": len(rejected),
}

stats_bar = " &nbsp;·&nbsp; ".join([
    f'<span style="color:#1a7f37;font-weight:600">🟢 Production {stats["production"]}</span>',
    f'<span style="color:#0969da;font-weight:600">🔵 Trials {stats["trials"]}</span>',
    f'<span style="color:#8250df;font-weight:600">✅ Awaiting Auto-Promote {stats["pass_pending"]}</span>',
    f'<span style="color:#9a6700;font-weight:600">🟡 Queue {stats["queue"]}</span>',
    f'<span style="color:#cf222e;font-weight:600">🟠 Inaccessible {stats["inaccessible"]}</span>',
    f'<span style="color:#57606a;font-weight:600">🌱 Seed {stats["seed"]}</span>',
    f'<span style="color:#57606a;font-weight:600">🔴 Rejected {stats["rejected"]}</span>',
])
NEW_CANDIDATE_WARN_DAYS = 7
gap = days_since_last_new_candidate(candidates, now_dt)
gap_color = "#9a6700" if gap is None or gap > NEW_CANDIDATE_WARN_DAYS else "#57606a"
stats_bar += (f' &nbsp;·&nbsp; <span style="color:{gap_color};font-weight:600">🆕 距上次新增候选 '
              f'{"—" if gap is None else gap} 天</span>')

body_sections = (
    section("🟢", "Production", "每日 pipeline 正在抓取", prod_items, "#1a7f37", show_fit=False)
    + section("🔵", "Active Trials", "7天窗口·每日质量采样", active_trial_candidates, "#0969da", show_trial=True)
    + pass_pending_section(pass_pending)
    + section("🟡", "Queue", "已验证·等待进入 Trial", queue, "#9a6700")
    + section("🟠", "Inaccessible", "JS渲染/403阻断·Fetcher Synthesis 目标", inaccessible, "#cf222e", show_stall=True)
    + section("🌱", "Seed / Discovery", "待评估候选池", seeds, "#57606a", show_stall=True)
    + section("🔴", "Rejected", "不适合纳入 pipeline", rejected, "#57606a")
)

push_failed = os.environ.get("PUSH_FAILED", "")
push_banner = "" if not push_failed else (
    '<div style="background:#ffebe9;border:1px solid #cf222e;border-radius:6px;padding:8px 14px;'
    'margin:0 0 16px;font-size:13px;color:#cf222e;font-weight:600">'
    f'⚠️ git push 失败（{html.escape(push_failed)}）：今天的改动只在本机，没有推到 GitHub</div>')
guard_failed = os.environ.get("GUARD_FAILED", "")
guard_banner = "" if not guard_failed else (
    '<div style="background:#ffebe9;border:1px solid #cf222e;border-radius:6px;padding:8px 14px;'
    'margin:0 0 16px;font-size:13px;color:#cf222e;font-weight:600">'
    f'⚠️ 状态守卫没有正常运行（{html.escape(guard_failed)}）：发现代理的改动未经检查，本次一律没有推送。'
    '下次 discovery 运行前请人工检查本机未推送的提交，否则它们会被一起推上去</div>')
agent_banner = "" if exit_code == 0 else (
    '<div style="background:#ffebe9;border:1px solid #cf222e;border-radius:6px;padding:8px 14px;'
    'margin:0 0 16px;font-size:13px;color:#cf222e;font-weight:600">'
    f'⚠️ 发现代理本次失败（exit {exit_code}），深度分析与找新基金都没有完成；详见 gmia-candidate-discovery.log</div>')

html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;max-width:780px;margin:0 auto;padding:24px;color:#24292f">

<h1 style="margin:0 0 2px;font-size:20px;font-weight:700;color:#1f2328">GMIA &mdash; Candidate Fund Discovery Report</h1>
<p style="margin:0 0 16px;font-size:13px;color:#57606a">{date_str} &nbsp;·&nbsp; Global Market Insight Aggregator</p>

<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 16px;margin:0 0 20px;font-size:13px;line-height:1.8">
{stats_bar}
</div>
{agent_banner}
{guard_banner}{push_banner}

{body_sections}

<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 14px;margin:20px 0 0;font-size:11px;color:#57606a;line-height:1.8">
  <strong style="color:#1f2328">如何解读本报告</strong><br>
  <strong>状态含义：</strong>
  🟢 Production — 已接入每日抓取 pipeline；
  🔵 Active Trials — 7天试运行，验证可访问性和内容质量；
  ✅ Trial Passed — Trial 已通过，等 auto-promote 次日 02:30 BJT 自动 wire 入 sources.json（无需人工操作）；
  🟡 Queue — 已通过人工或 AI 验证，等待进入 Trial；
  🟠 Inaccessible — 技术阻断（JS渲染/403），Fetcher Synthesis 自动尝试生成新爬虫；
  🌱 Seed — 待评分候选；
  🔴 Rejected — 不适合（付费墙/零研究内容）<br>
  <strong>Fit（适配分）</strong>：规则引擎评分（域名可信度+路径结构+页面可访问性），反映「门好不好开」。≥70% 较高，50–70% 一般，&lt;50% 存在阻断。<br>
  <strong>Quality（内容质量）</strong>：LLM 深度分析评估研究价值，反映「门里有没有宝」。HIGH = 原创机构观点；MEDIUM = 一般资讯；LOW = 营销内容为主。<br>
  <strong>⚠️ Nd 标记</strong>：候选卡在当前状态已 N 天（Seed/Discovery、Inaccessible 分组）。达到 {STALL_THRESHOLD_DAYS} 天会被下次运行自动分流（seed→discovered 重新尝试 / screen_failed→inaccessible+needs_playwright 转交 fetcher-synthesis），≥{STALL_THRESHOLD_DAYS * 2} 天标红提示已分流多轮仍未解决；没有研究页地址的 seed 满 7 天转 inaccessible，交给 fetcher-synthesis。<br>
  <strong>🆕 距上次新增候选</strong>：按候选的 added_at（守卫在新候选出现那天写入）计算，超过 {NEW_CANDIDATE_WARN_DAYS} 天标黄——发现代理每次都应找新基金。<br>
  <em>💡 简单记忆：Fit 是「门好不好开」，Quality 是「门里有没有宝」。两者都高才是理想的 Production 候选。</em>
</div>

<p style="color:#57606a;font-size:11px;margin-top:12px">
Auto-generated by GMIA candidate discovery pipeline &nbsp;·&nbsp;
<a href="https://github.com/chengli1986/hedge-fund-research" style="color:#0969da">chengli1986/hedge-fund-research</a>
</p>
</body></html>"""

msg_id = make_msgid(domain="ec2.sinostor.com.cn")
msg = MIMEMultipart("alternative")
msg["Subject"] = f"GMIA — Candidate Fund Discovery Report · {date_str}"
msg["From"] = os.environ.get("SMTP_USER", "")
msg["To"] = "ch_w10@outlook.com"
msg["Message-ID"] = msg_id
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html"))

if not os.environ.get("SMTP_USER") or not os.environ.get("SMTP_PASS"):
    print("WARNING: SMTP not configured, report not sent")
    sys.exit(0)
try:
    with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
        s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)
    print(f"Email sent to ch_w10@outlook.com (Message-ID: {msg_id})")
except Exception as e:
    print(f"WARNING: Email failed: {e}")
PYEOF
REPORT_EXIT=$?

# The job's exit is the work's first: until 2026-10-14 it was the email
# step's, so on 09-13 (expired login) and 09-22 (API 500) the agent failed and
# cron-wrapper recorded exit 0. A mail that SMTP refuses is caught above and
# stays a warning; but a report that crashed while being built means nobody
# got one, and that fails the job too once the work itself is clean (review
# 2026-10-15: from 10-14 such a crash had exited 0).
if [ "$EXIT_CODE" -ne 0 ]; then
    echo "$(ts) Exiting $EXIT_CODE: the discovery agent failed"
    exit "$EXIT_CODE"
fi
if [ -n "$GUARD_FAILED" ]; then
    echo "$(ts) Exiting 4: status guard failed ($GUARD_FAILED); nothing was pushed"
    exit 4
fi
if [ "$TRIAL_EXIT" -ne 0 ]; then
    echo "$(ts) Exiting $TRIAL_EXIT: the trial manager failed"
    exit "$TRIAL_EXIT"
fi
if [ "$REPORT_EXIT" -ne 0 ]; then
    echo "$(ts) Exiting $REPORT_EXIT: the report crashed and was not sent"
    exit "$REPORT_EXIT"
fi
if [ -n "$PUSH_FAILED" ]; then
    echo "$(ts) Exiting 3: git push failed ($PUSH_FAILED); the commits are local only"
    exit 3
fi
exit 0
