#!/bin/bash
# Candidate fund discovery pipeline runner
# Called by OpenClaw cron job or manually
# Runs all 3 stages sequentially, outputs JSON summary

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$REPO_DIR"

LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

echo "$LOG_PREFIX Starting candidate fund discovery..."

# Stage 1: Discovery
echo "$LOG_PREFIX Stage 1: Discovering research pages..."
python3 discover_fund_sites.py "$@" 2>&1
STAGE1=$?
[ $STAGE1 -ne 0 ] && echo "$LOG_PREFIX WARNING: Discovery had errors (exit $STAGE1)"

# Stage 2: Screening
echo "$LOG_PREFIX Stage 2: Screening candidates..."
python3 screen_fund_candidates.py "$@" 2>&1
STAGE2=$?
[ $STAGE2 -ne 0 ] && echo "$LOG_PREFIX WARNING: Screening had errors (exit $STAGE2)"

# Stage 3: Entrypoint scoring
echo "$LOG_PREFIX Stage 3: Scoring entrypoints..."
python3 discover_candidate_entrypoints.py "$@" 2>&1
STAGE3=$?
[ $STAGE3 -ne 0 ] && echo "$LOG_PREFIX WARNING: Scoring had errors (exit $STAGE3)"

# Summary
echo ""
echo "$LOG_PREFIX === CANDIDATE DISCOVERY SUMMARY ==="
python3 -c "
import json
candidates = json.load(open('config/fund_candidates.json'))
for c in candidates:
    score = c.get('fit_score')
    score_str = f'{score:.3f}' if score is not None else 'n/a'
    print(f\"  {c['id']:15s} status={c['status']:12s} fit={score_str:8s} research={c.get('research_url', 'none')}\")
print()
validated = [c for c in candidates if c['status'] == 'validated']
screened = [c for c in candidates if c['status'] == 'screened']
discovered = [c for c in candidates if c['status'] == 'discovered']
print(f'  Total: {len(candidates)} candidates, {len(validated)} validated, {len(screened)} screened, {len(discovered)} discovered')
"

echo "$LOG_PREFIX Done."
