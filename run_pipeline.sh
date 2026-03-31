#!/bin/bash
set -eo pipefail
cd ~/hedge-fund-research

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline starting"

# Stage 1: fetch metadata (source identity validated internally)
python3 fetch_articles.py

# Stage 2: fetch + validate + normalize content
python3 fetch_content.py

# Stage 3: LLM analysis (only processes content_status="ok" articles)
python3 analyze_articles.py

# Stage 4: publish (always runs — shows whatever data is available)
python3 publish.py

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline complete"
