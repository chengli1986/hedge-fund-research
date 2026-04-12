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

# --- Trial manager (runs regardless of discovery exit code) ---
echo "$LOG_PREFIX Running GMIA trial manager..."
python3 "$REPO_DIR/gmia-trial-manager.py" run 2>&1
TRIAL_EXIT=$?
if [ $TRIAL_EXIT -ne 0 ]; then
    echo "$LOG_PREFIX WARNING: trial manager exited with code $TRIAL_EXIT"
fi

# Commit trial state if changed
cd "$REPO_DIR"
if ! git diff --quiet config/trial-state.json config/fund_candidates.json 2>/dev/null; then
    git add config/trial-state.json config/fund_candidates.json
    git diff --cached --quiet || git commit -m "trial: update GMIA trial state $(TZ='Asia/Shanghai' date '+%Y-%m-%d')"
    echo "$LOG_PREFIX Trial state committed"
fi

# --- Email report ---
echo "$LOG_PREFIX Sending email report..."
source ~/.stock-monitor.env 2>/dev/null || true

export REPO_DIR EXIT_CODE SMTP_USER SMTP_PASS
python3 << 'PYEOF'
import json, os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
repo = Path(os.environ.get("REPO_DIR", "."))
exit_code = int(os.environ.get("EXIT_CODE", "1"))

# Load candidates
candidates = json.loads((repo / "config/fund_candidates.json").read_text())
seeds = json.loads((repo / "config/fund_seeds.json").read_text())

# Build summary
status_icon = "✅" if exit_code == 0 else ("⏰" if exit_code == 124 else "❌")
status_text = "Success" if exit_code == 0 else ("Timeout" if exit_code == 124 else f"Failed (exit {exit_code})")

rows = ""
for c in candidates:
    score = c.get("fit_score")
    score_str = f'{score:.3f}' if score is not None else "—"
    quality = c.get("quality", "—")
    topics = c.get("topics", "—")
    notes = (c.get("notes") or "")[:60]
    is_recommend = notes.startswith("RECOMMEND")
    bg = "#e6ffe6" if is_recommend else ""
    style = f' style="background:{bg}"' if bg else ""
    q_color = {"HIGH": "#22863a", "MEDIUM": "#e36209", "LOW": "#cb2431"}.get(quality, "#959da5")
    status_pill = {
        "validated": '<span style="color:#22863a;font-weight:bold">validated</span>',
        "screened": '<span style="color:#0366d6">screened</span>',
        "discovered": '<span style="color:#6f42c1">discovered</span>',
        "seed": '<span style="color:#959da5">seed</span>',
        "watchlist": '<span style="color:#e36209">watchlist</span>',
        "rejected": '<span style="color:#cb2431">rejected</span>',
    }.get(c["status"], c["status"])
    rows += (f'<tr{style}><td style="padding:4px 6px;border-bottom:1px solid #eee">{c["name"]}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee">{status_pill}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee">{score_str}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;color:{q_color};font-weight:bold">{quality}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{topics}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:12px">{notes}</td></tr>\n')

validated = sum(1 for c in candidates if c["status"] == "validated")
recommend = sum(1 for c in candidates if (c.get("notes") or "").startswith("RECOMMEND"))

html = f"""<html><body style="font-family:system-ui,-apple-system,sans-serif;max-width:700px;margin:0 auto;padding:20px">
<h2 style="margin:0">🔍 GMIA Candidate Fund Discovery</h2>
<p style="color:#586069;margin:4px 0">{now} &nbsp; {status_icon} {status_text}</p>

<table style="width:100%;border-collapse:collapse;font-size:13px;margin:16px 0">
<tr style="background:#f6f8fa"><td style="padding:8px"><strong>Seeds</strong></td><td>{len(seeds)}</td>
<td><strong>Validated</strong></td><td>{validated}</td>
<td><strong>Recommend</strong></td><td style="color:#22863a;font-weight:bold">{recommend}</td></tr>
</table>

<table style="width:100%;border-collapse:collapse;font-size:13px;margin:16px 0">
<tr style="background:#f6f8fa">
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Fund</th>
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Status</th>
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Fit</th>
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Quality</th>
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Topics</th>
<th style="text-align:left;padding:6px;border-bottom:2px solid #e1e4e8">Notes</th>
</tr>
{rows}
</table>

<p style="color:#586069;font-size:12px;margin-top:20px">
Auto-generated by GMIA candidate discovery pipeline<br>
Repo: <a href="https://github.com/chengli1986/hedge-fund-research">chengli1986/hedge-fund-research</a>
</p>
</body></html>"""

msg = MIMEMultipart("alternative")
msg["Subject"] = f"GMIA Fund Discovery: {len(seeds)} seeds, {validated} validated, {recommend} recommend — {now}"
msg["From"] = os.environ.get("SMTP_USER", "")
msg["To"] = "ch_w10@outlook.com"
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html"))

try:
    with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
        s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)
    print("Email sent to ch_w10@outlook.com")
except Exception as e:
    print(f"WARNING: Email failed: {e}")
PYEOF
