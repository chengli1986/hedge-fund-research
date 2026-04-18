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
# Re-export the API key for Haiku quality sampling
export ANTHROPIC_API_KEY="$SAVED_ANTHROPIC_API_KEY"
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
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push (trial state) failed"
fi

# --- Email report ---
echo "$LOG_PREFIX Sending email report..."
source ~/.stock-monitor.env 2>/dev/null || true

export REPO_DIR EXIT_CODE SMTP_USER SMTP_PASS
python3 << 'PYEOF'
import json, os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import make_msgid
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
repo = Path(os.environ.get("REPO_DIR", "."))
exit_code = int(os.environ.get("EXIT_CODE", "1"))

# Load candidates
candidates = json.loads((repo / "config/fund_candidates.json").read_text())
seeds = json.loads((repo / "config/fund_seeds.json").read_text())
sources_data = json.loads((repo / "config/sources.json").read_text())
active_sources = sources_data.get("sources", [])

# Build summary
status_icon = "✅" if exit_code == 0 else ("⏰" if exit_code == 124 else "❌")
status_text = "Success" if exit_code == 0 else ("Timeout" if exit_code == 124 else f"Failed (exit {exit_code})")

STATUS_ORDER = ["validated", "watchlist", "inaccessible", "screen_failed", "screened", "discovered", "seed", "rejected"]
sorted_candidates = sorted(candidates, key=lambda c: (STATUS_ORDER.index(c["status"]) if c["status"] in STATUS_ORDER else 99))

STATUS_PILL = {
    "validated":     '<span style="color:#22863a;font-weight:bold">validated</span>',
    "inaccessible":  '<span style="color:#cb2431">inaccessible</span>',
    "screen_failed": '<span style="color:#e36209">screen_failed</span>',
    "screened":      '<span style="color:#0366d6">screened</span>',
    "discovered":    '<span style="color:#6f42c1">discovered</span>',
    "seed":          '<span style="color:#959da5">seed</span>',
    "watchlist":     '<span style="color:#e36209">watchlist</span>',
    "rejected":      '<span style="color:#959da5;text-decoration:line-through">rejected</span>',
}

# 策略覆盖地图
ALL_TAGS = [
    "fixed_income", "private_credit", "event_driven", "macro",
    "quant", "private_equity", "real_assets", "equity",
    "multi_asset", "esg_climate", "emerging_markets", "venture_capital"
]
TAG_LABELS = {
    "fixed_income": "Fixed Income", "private_credit": "Private Credit",
    "event_driven": "Event Driven", "macro": "Macro",
    "quant": "Quant", "private_equity": "Private Equity",
    "real_assets": "Real Assets", "equity": "Equity",
    "multi_asset": "Multi Asset", "esg_climate": "ESG/Climate",
    "emerging_markets": "Emerging Mkts", "venture_capital": "Venture Capital"
}

tag_counts = {t: 0 for t in ALL_TAGS}
for s in active_sources:
    for t in s.get("strategy_tags", []):
        if t in tag_counts:
            tag_counts[t] += 1
for c in candidates:
    if c.get("status") == "validated":
        for t in c.get("strategy_tags", []):
            if t in tag_counts:
                tag_counts[t] += 1

def coverage_bar(count):
    filled = min(count, 5)
    return "█" * filled + "░" * (5 - filled)

map_cells = ""
for i, tag in enumerate(ALL_TAGS):
    count = tag_counts[tag]
    color = "#22863a" if count > 0 else "#cb2431"
    bar = coverage_bar(count)
    label = TAG_LABELS[tag]
    map_cells += (
        f'<td style="padding:4px 8px;width:25%">'
        f'<span style="color:{color};font-family:monospace;font-size:11px">{bar}</span> '
        f'<span style="font-size:12px">{label}</span> '
        f'<span style="color:#586069;font-size:11px">({count})</span>'
        f'</td>'
    )
    if (i + 1) % 4 == 0:
        map_cells += "</tr><tr>"

coverage_map_html = f"""
<div style="margin:0 0 4px">
<div style="font-weight:600;font-size:13px;margin-bottom:6px">策略覆盖地图 — 活跃来源 + 已验证候选</div>
<table style="width:100%;border-collapse:collapse;background:#f6f8fa;border:1px solid #e1e4e8;border-radius:4px 4px 0 0">
<tr>{map_cells}</tr>
</table>
<div style="background:#f6f8fa;border:1px solid #e1e4e8;border-top:none;border-radius:0 0 4px 4px;padding:5px 10px;font-size:11px;color:#586069">
  <strong>如何阅读：</strong>
  进度条 █ 每格 = 1 个覆盖来源（上限 5 格）；括号内数字 = 活跃来源 + 已验证候选的实际总数；
  <span style="color:#22863a">绿色</span> = 已有覆盖，<span style="color:#cb2431">红色</span> = 暂无来源覆盖此策略类别，可作为未来扩充方向参考。
</div>
</div>
<div style="margin:0 0 16px"></div>
"""

rows = ""
for c in sorted_candidates:
    score = c.get("fit_score")
    score_str = f'{score:.3f}' if score is not None else "—"
    quality = c.get("quality", "—")
    topics = c.get("topics", "—")
    notes_full = (c.get("notes") or "")
    is_recommend = notes_full.startswith("RECOMMEND")
    notes = notes_full[:100] + ("…" if len(notes_full) > 100 else "")
    bg = "#e6ffe6" if is_recommend else ("#fff8f8" if c["status"] == "rejected" else "")
    style = f' style="background:{bg}"' if bg else ""
    q_color = {"HIGH": "#22863a", "MEDIUM": "#e36209", "LOW": "#cb2431"}.get(quality, "#959da5")
    status_pill = STATUS_PILL.get(c["status"], f'<span style="color:#959da5">{c["status"]}</span>')
    rows += (f'<tr{style}><td style="padding:4px 6px;border-bottom:1px solid #eee">{c["name"]}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;white-space:nowrap">{status_pill}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;white-space:nowrap">{score_str}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;color:{q_color};font-weight:bold;white-space:nowrap">{quality}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{topics}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee">{" ".join(f"<span style=\\"background:#ddf4ff;color:#0969da;border-radius:3px;padding:1px 5px;font-size:10px;margin-right:2px\\">{t}</span>" for t in c.get("strategy_tags", []))}</td>'
             f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:12px">{notes}</td></tr>\n')

validated = sum(1 for c in candidates if c["status"] == "validated")
inaccessible = sum(1 for c in candidates if c["status"] == "inaccessible")
recommend = sum(1 for c in candidates if c["status"] == "validated" and (c.get("notes") or "").startswith("RECOMMEND"))

stats_bar = (
    f'<span style="margin-right:16px"><strong>Seeds</strong>&nbsp;{len(seeds)}</span>'
    f'<span style="margin-right:16px;color:#22863a"><strong>Validated</strong>&nbsp;{validated}</span>'
    f'<span style="margin-right:16px;color:#cb2431"><strong>Inaccessible</strong>&nbsp;{inaccessible}</span>'
    f'<span style="color:#22863a;font-weight:bold"><strong>Recommend</strong>&nbsp;{recommend}</span>'
)

html = f"""<html><body style="font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:0 auto;padding:20px">
<h2 style="margin:0">🔍 GMIA Candidate Fund Discovery</h2>
<p style="color:#586069;margin:4px 0 12px">{now} &nbsp; {status_icon} {status_text}</p>

<p style="background:#f6f8fa;border:1px solid #e1e4e8;border-radius:4px;padding:10px 14px;margin:0 0 16px;font-size:13px">
{stats_bar}
</p>

{coverage_map_html}
<table style="width:100%;border-collapse:collapse;font-size:13px;margin:0 0 16px">
<tr style="background:#f6f8fa">
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Fund</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Status</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Fit</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Quality</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Topics</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Tags</th>
<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Notes</th>
</tr>
{rows}
</table>

<div style="background:#f6f8fa;border:1px solid #e1e4e8;border-radius:4px;padding:8px 12px;margin:0 0 16px;font-size:11px;color:#586069;line-height:1.7">
  <strong style="color:#24292e">如何阅读表格：</strong><br>
  <strong>Status</strong>：<span style="color:#22863a;font-weight:bold">validated</span> 可直接抓取研究报告（已纳入每日 pipeline）／
  <span style="color:#cb2431">inaccessible</span> JS 渲染无法直接抓取／
  <span style="color:#e36209">watchlist</span> 值得持续关注但暂不纳入／
  <span style="color:#959da5;text-decoration:line-through">rejected</span> 不适合（无研究内容或付费墙）<br>
  <strong>Fit</strong>：0–1 综合适配分（域名可信度 + 内容结构 + 研究深度），≥0.6 代表较高适配性<br>
  <strong>Quality</strong>：LLM 评估研究深度 — <span style="color:#22863a;font-weight:bold">HIGH</span> 有原创观点／<span style="color:#e36209;font-weight:bold">MEDIUM</span> 一般资讯／<span style="color:#cb2431;font-weight:bold">LOW</span> 营销内容为主<br>
  <strong>Tags</strong>：策略分类标签，12 类固定标签集（与覆盖地图对应），用于追踪策略多样性<br>
  <strong>Notes</strong>：<span style="background:#e6ffe6">绿色高亮行</span> = RECOMMEND，建议优先跟进并考虑加入正式 pipeline
</div>

<p style="color:#586069;font-size:12px;margin-top:8px">
Auto-generated by GMIA candidate discovery pipeline<br>
Repo: <a href="https://github.com/chengli1986/hedge-fund-research">chengli1986/hedge-fund-research</a>
</p>
</body></html>"""

msg_id = make_msgid(domain="ec2.sinostor.com.cn")
msg = MIMEMultipart("alternative")
msg["Subject"] = f"GMIA Fund Discovery: {len(seeds)} seeds, {validated} validated, {recommend} recommend — {now}"
msg["From"] = os.environ.get("SMTP_USER", "")
msg["To"] = "ch_w10@outlook.com"
msg["Message-ID"] = msg_id
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html"))

try:
    with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
        s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)
    print(f"Email sent to ch_w10@outlook.com (Message-ID: {msg_id})")
except Exception as e:
    print(f"WARNING: Email failed: {e}")
PYEOF
