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
queue = [c for c in candidates if c["status"] == "validated"
         and c["id"] not in production_ids and c["id"] not in active_trial_ids]
inaccessible = [c for c in candidates if c["status"] == "inaccessible"]
seed_statuses = {"seed", "discovered", "screened", "screen_failed"}
seeds = [c for c in candidates if c["status"] in seed_statuses]
rejected = [c for c in candidates if c["status"] == "rejected"]

# 策略覆盖地图
def q_badge(quality: str) -> str:
    color = {"HIGH": "#22863a", "MEDIUM": "#e36209", "LOW": "#cb2431"}.get(quality, "#959da5")
    return f'<span style="color:{color};font-weight:bold;font-size:11px">{quality}</span>'

def fit_pct(score) -> str:
    if score is None:
        return '<span style="color:#959da5">—</span>'
    pct = int(score * 100)
    color = "#22863a" if pct >= 70 else ("#e36209" if pct >= 50 else "#cb2431")
    return f'<span style="color:{color};font-weight:bold">{pct}%</span>'

def tags_html(tags: list) -> str:
    return " ".join(
        f'<span style="background:#ddf4ff;color:#0969da;border-radius:3px;padding:1px 5px;font-size:10px">{t}</span>'
        for t in tags
    )

def notes_short(notes_full: str) -> str:
    n = (notes_full or "")
    return n[:90] + ("…" if len(n) > 90 else "")

def group_table(items: list, show_fit: bool = True, show_trial: bool = False) -> str:
    if not items:
        return '<p style="color:#959da5;font-size:12px;padding:6px 0;margin:0">（空）</p>'
    rows = ""
    for c in items:
        trial_day = ""
        if show_trial and c["id"] in active_trial_info:
            t = active_trial_info[c["id"]]
            start = datetime.fromisoformat(t["start_date"]).replace(tzinfo=timezone.utc)
            day = (now_dt.replace(tzinfo=None) - start.replace(tzinfo=None)).days + 1
            trial_day = f' <span style="color:#0969da;font-size:10px">Day {day}/3</span>'
        fit_cell = fit_pct(c.get("fit_score")) if show_fit else "—"
        notes_full = c.get("notes") or ""
        is_rec = notes_full.startswith("RECOMMEND")
        bg = " style=\"background:#f0fff4\"" if is_rec else ""
        rows += (
            f'<tr{bg}>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-weight:500">{c["name"]}{trial_day}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{fit_cell}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee">{q_badge(c.get("quality","—"))}</td>'
            f'<td style="padding:5px 8px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{c.get("topics","—")}</td>'
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
            show_fit: bool = True, show_trial: bool = False) -> str:
    count = len(items)
    return f"""
<div style="margin:0 0 20px">
  <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:6px">
    <span style="font-size:15px;font-weight:700;color:{color}">{emoji} {title}</span>
    <span style="background:{color};color:#fff;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600">{count}</span>
    <span style="color:#959da5;font-size:12px">{subtitle}</span>
  </div>
  {group_table(items, show_fit=show_fit, show_trial=show_trial)}
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
    "queue": len(queue),
    "inaccessible": len(inaccessible),
    "seed": len(seeds),
    "rejected": len(rejected),
}

stats_bar = " &nbsp;·&nbsp; ".join([
    f'<span style="color:#1a7f37;font-weight:600">🟢 Production {stats["production"]}</span>',
    f'<span style="color:#0969da;font-weight:600">🔵 Trials {stats["trials"]}</span>',
    f'<span style="color:#9a6700;font-weight:600">🟡 Queue {stats["queue"]}</span>',
    f'<span style="color:#cf222e;font-weight:600">🟠 Inaccessible {stats["inaccessible"]}</span>',
    f'<span style="color:#57606a;font-weight:600">🌱 Seed {stats["seed"]}</span>',
    f'<span style="color:#57606a;font-weight:600">🔴 Rejected {stats["rejected"]}</span>',
])

body_sections = (
    section("🟢", "Production", "每日 pipeline 正在抓取", prod_items, "#1a7f37", show_fit=False)
    + section("🔵", "Active Trials", "3天窗口·每日质量采样", active_trial_candidates, "#0969da", show_trial=True)
    + section("🟡", "Queue", "已验证·等待进入 Trial", queue, "#9a6700")
    + section("🟠", "Inaccessible", "JS渲染/403阻断·Fetcher Synthesis 目标", inaccessible, "#cf222e")
    + section("🌱", "Seed / Discovery", "待评估候选池", seeds, "#57606a")
    + section("🔴", "Rejected", "不适合纳入 pipeline", rejected, "#57606a")
)

html = f"""<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;max-width:780px;margin:0 auto;padding:24px;color:#24292f">

<h1 style="margin:0 0 2px;font-size:20px;font-weight:700;color:#1f2328">GMIA &mdash; Candidate Fund Discovery Report</h1>
<p style="margin:0 0 16px;font-size:13px;color:#57606a">{date_str} &nbsp;·&nbsp; Global Market Insight Aggregator</p>

<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 16px;margin:0 0 20px;font-size:13px;line-height:1.8">
{stats_bar}
</div>

{body_sections}

<div style="background:#f6f8fa;border:1px solid #d0d7de;border-radius:6px;padding:10px 14px;margin:20px 0 0;font-size:11px;color:#57606a;line-height:1.8">
  <strong style="color:#1f2328">如何解读本报告</strong><br>
  <strong>状态含义：</strong>
  🟢 Production — 已接入每日抓取 pipeline；
  🔵 Active Trials — 3天试运行，验证可访问性和内容质量；
  🟡 Queue — 已通过人工或 AI 验证，等待进入 Trial；
  🟠 Inaccessible — 技术阻断（JS渲染/403），Fetcher Synthesis 自动尝试生成新爬虫；
  🌱 Seed — 待评分候选；
  🔴 Rejected — 不适合（付费墙/零研究内容）<br>
  <strong>Fit（适配分）</strong>：规则引擎评分（域名可信度+路径结构+页面可访问性），反映「门好不好开」。≥70% 较高，50–70% 一般，&lt;50% 存在阻断。<br>
  <strong>Quality（内容质量）</strong>：LLM 深度分析评估研究价值，反映「门里有没有宝」。HIGH = 原创机构观点；MEDIUM = 一般资讯；LOW = 营销内容为主。<br>
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

try:
    with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
        s.login(os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASS", ""))
        s.send_message(msg)
    print(f"Email sent to ch_w10@outlook.com (Message-ID: {msg_id})")
except Exception as e:
    print(f"WARNING: Email failed: {e}")
PYEOF
