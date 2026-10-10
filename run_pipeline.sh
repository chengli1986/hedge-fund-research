#!/bin/bash
set -uo pipefail
cd ~/hedge-fund-research || { echo "FATAL: cannot cd to ~/hedge-fund-research"; exit 1; }

# Captured before any stage runs, and handed to the Stage 5 checker so it can
# tell "publish wrote this page" from "yesterday's page is still on disk".
# Stage 4 runs unconditionally after Stages 1-3 fail, so a publish that
# silently no-ops used to leave the old page in place and the checker blessed
# it -- a copy touched to 2020-01-01 passed all six checks.
PIPELINE_START_EPOCH=$(date +%s)

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline starting"

# --- Weekly entrypoint validation pre-check (non-fatal) ---
LAST_VALIDATE_FILE="config/.last_validated"
RUN_VALIDATION=0
if [[ ! -f "$LAST_VALIDATE_FILE" ]]; then
  RUN_VALIDATION=1
elif [[ -n "$(find "$LAST_VALIDATE_FILE" -mtime +7 2>/dev/null)" ]]; then
  RUN_VALIDATION=1
fi

# The result goes to logs/, not /tmp: the daily fetcher-health email reads it
# from there and reports any source whose entrypoint is not ok. Until
# 2026-09-20 this check only echoed its findings, and cron-wrapper.sh alerts on
# the exit code and nothing else -- so the one check that can notice an
# entrypoint going bad had no destination (audit finding A2).
VALIDATE_OUT="logs/entrypoint-validation.json"
if [[ "$RUN_VALIDATION" -eq 1 ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running entrypoint validation..."
  mkdir -p logs
  VALIDATE_ERR=$(mktemp)
  if python3 validate_entrypoints.py --json > "$VALIDATE_OUT.tmp" 2>"$VALIDATE_ERR"; then
    mv "$VALIDATE_OUT.tmp" "$VALIDATE_OUT"
    touch "$LAST_VALIDATE_FILE"
    BAD_SOURCES=$(python3 -c "
import json, sys
data = json.load(open('$VALIDATE_OUT'))
bad = [src for src, entries in data.items() if any(e.get('status') != 'ok' for e in entries)]
if bad:
    print('WARN: entrypoint issues detected for: ' + ', '.join(bad))
" 2>/dev/null)
    if [[ -n "$BAD_SOURCES" ]]; then
      echo "$BAD_SOURCES"
    else
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Entrypoint validation passed — all sources ok"
    fi
  else
    # Keep the reason: `2>/dev/null` used to throw away why it failed, and the
    # marker file is deliberately NOT touched, so it is retried tomorrow.
    python3 -c "
import json, sys
err = open('$VALIDATE_ERR').read()[-2000:]
json.dump({'_error': err or 'validate_entrypoints.py exited non-zero with no stderr'},
          open('$VALIDATE_OUT', 'w'))
"
    echo "WARN: entrypoint validation script failed — continuing pipeline anyway"
  fi
  rm -f "$VALIDATE_ERR" "$VALIDATE_OUT.tmp"
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skipping entrypoint validation (last run <7d ago)"
fi
# --- end entrypoint validation pre-check ---

failed_stages=()

# Stage 1: fetch metadata (source identity validated internally)
if python3 fetch_articles.py; then
  # Stage 1b: a listing that shows only "October 2026" is stored as the month's
  # last day; read the real day from the article page (scripts/refine_dates.py,
  # 2026-10-08). Best effort: a failure costs only a less precise date.
  if ! python3 scripts/refine_dates.py; then
    echo "WARN: Stage 1b (date refinement) failed; month-only dates stay at the month end"
  fi
  # Stage 2: fetch + validate + normalize content (depends on Stage 1)
  if python3 fetch_content.py; then
    # Stage 3: LLM analysis (depends on Stage 2). Any non-zero alerts: 1 = no
    # answers / stage stopped, 2 = quota or auth, 3 = an article given up after
    # MAX_ANALYSIS_NIGHTS failed nights (announced once).
    if ! python3 analyze_articles.py; then
      failed_stages+=("Stage3:analyze")
    fi
  else
    failed_stages+=("Stage2:content")
    echo "WARN: Stage 2 failed — skipping Stage 3 (LLM analysis)"
  fi
else
  failed_stages+=("Stage1:fetch")
  echo "WARN: Stage 1 failed — skipping Stage 2 and Stage 3"
fi

# Stage 3b: tag every summarised article that has no tags yet (taxonomy.py,
# the method accepted 2026-10-08). Runs even when an earlier stage failed:
# yesterday's summaries can still be tagged. Exit 1 = some articles got no
# valid answer; they stay untagged and are retried tomorrow, so it is logged,
# not alerted. Anything else alerts: 2 = quota/billing/auth stop, 3 = an
# article failed its third night and is no longer asked (once per article),
# 4 = damaged store or a crash outside the per-article loop.
python3 scripts/tag_articles.py --nightly
tag_rc=$?
if [[ $tag_rc -eq 1 ]]; then
  echo "WARN: Stage 3b left some articles untagged; they are retried next run"
elif [[ $tag_rc -ne 0 ]]; then
  failed_stages+=("Stage3b:tag")
fi

# Stage 4: publish always runs — shows whatever data is available
# but mark output as degraded if any prerequisite failed
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "WARN: publishing with degraded data (failed: ${failed_stages[*]})"
fi
# Exit 3 = the dashboard was written but the docs-site sync (commit/push)
# failed: an alert, not a reason to skip Stage 5 -- the page is live.
# Exit 4 = the new page failed the stage-5 checks before going live; nothing
# was written and yesterday's page is still up (stage-4 audit 2026-10-09).
python3 publish.py
publish_rc=$?
if [[ $publish_rc -eq 3 ]]; then
  failed_stages+=("Stage4:docs-sync")
elif [[ $publish_rc -eq 4 ]]; then
  failed_stages+=("Stage4:precheck")
elif [[ $publish_rc -ne 0 ]]; then
  failed_stages+=("Stage4:publish")
fi

# Stage 5: the same checks as publish.py's pre-check, on the live file:
# every fund has its section, the numbers match a recount of the data, the
# data islands parse, the scripts parse, the .gz is this page, and the file
# was written by this run. Skipped when nothing new was published.
# Failure is reported as a stage failure so cron-wrapper alerts.
if [[ ! " ${failed_stages[*]} " =~ " Stage4:publish " && ! " ${failed_stages[*]} " =~ " Stage4:precheck " ]]; then
  if ! python3 scripts/check_dashboard_html.py --written-after "$PIPELINE_START_EPOCH"; then
    failed_stages+=("Stage5:dashboard-sanity")
    echo "WARN: dashboard HTML sanity check failed — page may be broken at /var/www/overview/hedge-fund-research.html"
  fi
fi

# Explicit success/failure log
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline FAILED — ${failed_stages[*]}"
  exit 1
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline complete — all stages OK"
  exit 0
fi
