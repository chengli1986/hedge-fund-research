#!/bin/bash
# Daily wrapper for GMIA auto-promote
# Invokes Claude Code agent to wire trial-passed candidates into production
# (sources.json + BADGE_COLORS + CONTENT_FETCHERS + _FUND_PROFILES draft)
# Schedule: daily 02:30 BJT (18:30 UTC)
#
# Uses Claude Code Max Plan (NOT API) — same pattern as wrapper-fetcher-synthesis.sh

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

echo "$LOG_PREFIX Starting auto-promote session..."

# 若无目标则提前退出，不启动 Agent
TARGET_COUNT=$(cd "$REPO_DIR" && python3 - << 'PYEOF'
import json
import sys
from pathlib import Path
sys.path.insert(0, ".")
candidates = json.loads(Path("config/fund_candidates.json").read_text())
sources = json.loads(Path("config/sources.json").read_text())
prod_ids = {s["id"] for s in sources["sources"]}
try:
    from fetch_articles import FETCHERS
except Exception as exc:
    # Not "no targets": a broken fetch_articles must fail the job (2026-10-14).
    print(f"ERROR: cannot import fetch_articles: {exc}", file=sys.stderr)
    sys.exit(1)

n = sum(
    1 for c in candidates
    if c.get("status") == "promoted"
    and c["id"] not in prod_ids
    # A passing listing_template (scripts/try_template.py) stands in for a
    # hand-written fetcher since 2026-10-13 ("template first").
    and (c["id"] in FETCHERS or c.get("listing_template"))
)
print(n)
PYEOF
)
TARGET_EXIT=$?
if [ "$TARGET_EXIT" -ne 0 ] || ! [[ "$TARGET_COUNT" =~ ^[0-9]+$ ]]; then
    echo "$LOG_PREFIX ERROR: could not count promote targets (exit $TARGET_EXIT); agent not started"
    exit 1
fi

if [ "$TARGET_COUNT" -eq 0 ]; then
    echo "$LOG_PREFIX No promoted candidates needing wiring. Exiting."
    exit 0
fi
echo "$LOG_PREFIX Found $TARGET_COUNT candidate(s) to promote."

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

PROGRAM_MD="$REPO_DIR/auto-promote/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- 最长 20 分钟
- 最多处理 2 个基金
- 接入后必须运行 pytest；若失败则回滚（git checkout 已修改的文件 + 删
  pending_profiles/FUNDID.json 和 pending_profiles/FUNDID.validation.json 草稿）
- **Phase 4.5 auto-graduate**：validation \`ok=true\` AND \`high_risk=false\` 时立即
  跑 \`python3 scripts/graduate_pending.py FUNDID\`，让 _FUND_PROFILES 条目直接进 publish.py；
  否则保留 pending 等人工。这是 commit 前的最后一步代码修改，必须在 pytest 之前完成
  （Phase 5 pytest 验证 graduated 后的状态）。
- **单基金原子操作顺序（不可拆，不可推迟，不可批量化）**：
  1. \`git commit\` → 2. \`git push origin main\` → 3. \`logs/auto-promote-history.jsonl\` 追加一行
  → 才能进入下一个基金或退出 session
- Wrapper 用 history 文件认定哪些 commit 已成功；若 commit/push 后 history 未写完就撞 --max-turns
  被截，那笔工作会被误判为 deferred / 漏报。**永远不要把 history-write 推到 session 末尾批量做**。
- history 条目必须显式记录 \`auto_graduated: true|false\`（成功 graduate 或跳过）+
  \`auto_graduate_skip_reason\`（跳过时填 "validation_ok=false" / "high_risk=true" /
  "exit_code=N"；成功时填 null）—— 方便日后审计哪些条目自动进了 publish.py
- 即使 deferred / failed 也要写一行到 logs/auto-promote-history.jsonl（commit/push 跳过，
  但 history 写入照常）
"

echo "$LOG_PREFIX Invoking Claude Code agent..."
# Ensure the npm-global claude (2.x, supports --print) takes precedence over
# the system /usr/bin/claude (1.0.65) that lives on cron's default PATH.
export PATH="$HOME/.npm-global/bin:$PATH"
echo "$PROMPT" | claude --print \
    --allowedTools "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch" \
    --max-turns 120 \
    2>&1

EXIT_CODE=$?
echo "$LOG_PREFIX Agent exited with code $EXIT_CODE"

# ─── Phase 6.5: commit-msg evidence validation ───────────────────────────────
# program.md Phase 3 requires the agent's commit message to embed 4 hard-gate
# evidence fields (Live test articles+chars, Haiku quality avg+rel, Method,
# Preview). Without this check, the agent could fabricate evidence in prose
# without anyone noticing. We validate every today's "promoted" commit; failures
# get reverted before health probe runs.
echo "$LOG_PREFIX Running commit-msg evidence validation..."
cd "$REPO_DIR"
python3 scripts/verify_promotions.py --phase validate
VALIDATE_EXIT=$?
echo "$LOG_PREFIX Commit-msg validation done (exit $VALIDATE_EXIT)"

# ─── Phase 7: post-commit health probe + auto-revert ─────────────────────────
# Probes each promotion still standing (not the ones reverted above) via
# gmia-fetcher-health --source <id> --dry-run; FAIL → git revert + push +
# history row "auto_reverted".
echo "$LOG_PREFIX Running post-commit health probe..."
python3 scripts/verify_promotions.py --phase probe
PROBE_EXIT=$?
echo "$LOG_PREFIX Post-commit probe done (exit $PROBE_EXIT)"

# ─── Exit code ───────────────────────────────────────────────────────────────
# A revert, or a promotion that could not be checked or reverted, fails the
# job so cron-wrapper mails (until 2026-10-14 both checks always exited 0 and
# a revert went unreported).
if [ "$VALIDATE_EXIT" -ne 0 ] || [ "$PROBE_EXIT" -ne 0 ]; then
    echo "$LOG_PREFIX Exiting 3: a promotion was reverted or could not be verified (see above)"
    exit 3
fi

# Agent's exit code is unreliable: hitting --max-turns produces exit=1 even when
# all commits succeeded. If promotions still stand today and both checks
# passed, the work landed cleanly regardless of how the agent exited.
PROMOTED_TODAY=$(cd "$REPO_DIR" && python3 scripts/promote_today.py)
if [ "$EXIT_CODE" -ne 0 ] && [ "${PROMOTED_TODAY:-0}" -gt 0 ]; then
    echo "$LOG_PREFIX Agent exit=$EXIT_CODE but $PROMOTED_TODAY promotion(s) verified (validate-msg + probe) — treating as success"
    exit 0
fi

exit $EXIT_CODE
