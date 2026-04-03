#!/bin/bash
set -uo pipefail
cd ~/hedge-fund-research || { echo "FATAL: cannot cd to ~/hedge-fund-research"; exit 1; }

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline starting"

# --- Weekly entrypoint validation pre-check (non-fatal) ---
LAST_VALIDATE_FILE="config/.last_validated"
RUN_VALIDATION=0
if [[ ! -f "$LAST_VALIDATE_FILE" ]]; then
  RUN_VALIDATION=1
elif [[ -n "$(find "$LAST_VALIDATE_FILE" -mtime +7 2>/dev/null)" ]]; then
  RUN_VALIDATION=1
fi

if [[ "$RUN_VALIDATION" -eq 1 ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running entrypoint validation..."
  if python3 validate_entrypoints.py --json > /tmp/gmia-validate.json 2>/dev/null; then
    touch "$LAST_VALIDATE_FILE"
    # Check for any non-"ok" statuses
    BAD_SOURCES=$(python3 -c "
import json, sys
data = json.load(open('/tmp/gmia-validate.json'))
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
    echo "WARN: entrypoint validation script failed — continuing pipeline anyway"
  fi
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Skipping entrypoint validation (last run <7d ago)"
fi
# --- end entrypoint validation pre-check ---

failed_stages=()

# Stage 1: fetch metadata (source identity validated internally)
if python3 fetch_articles.py; then
  # Stage 2: fetch + validate + normalize content (depends on Stage 1)
  if python3 fetch_content.py; then
    # Stage 3: LLM analysis (depends on Stage 2)
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

# Stage 4: publish always runs — shows whatever data is available
# but mark output as degraded if any prerequisite failed
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "WARN: publishing with degraded data (failed: ${failed_stages[*]})"
fi
if ! python3 publish.py; then
  failed_stages+=("Stage4:publish")
fi

# Explicit success/failure log
if [[ ${#failed_stages[@]} -gt 0 ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline FAILED — ${failed_stages[*]}"
  exit 1
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline complete — all stages OK"
  exit 0
fi
