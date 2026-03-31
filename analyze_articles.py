#!/usr/bin/env python3
"""
Hedge Fund Research — Stage 3: LLM Analysis

Reads article content files, sends to LLM for bilingual analysis (EN/ZH),
and writes structured summaries back to the JSONL.

Multi-model fallback chain: Gemini 2.5 Pro -> GPT-4.1 Mini -> Claude Sonnet

Usage:
  python3 analyze_articles.py                     # analyze all pending
  python3 analyze_articles.py --dry-run            # show what would be analyzed
"""

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
CONTENT_DIR = BASE_DIR / "content"
LOG_FILE = BASE_DIR / "logs" / "analyze_articles.log"

VALID_THEMES = {
    "AI/Tech", "Macro/Rates", "Oil/Energy", "Credit/Fixed Income",
    "Equities/Value", "China/EM", "Risk/Volatility", "Geopolitics",
    "ESG/Climate", "Quant/Factor", "Asset Allocation", "Crypto/Digital",
    "Real Estate", "Private Markets", "Behavioral/Sentiment",
}

MODEL_CHAIN = ["gemini-2.5-pro", "gpt-4.1-mini", "claude-sonnet-4-6"]
MAX_ATTEMPTS = 2
MAX_CONTENT_CHARS = 15000

ANALYSIS_PROMPT = """You are a senior investment analyst. Analyze the following hedge fund research article and produce a structured JSON response.

Article title: {title}
Source: {source}
Date: {date}

Article content:
{content}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{"summary_en": "...", "summary_zh": "...", "themes": [...], "key_takeaway_en": "...", "key_takeaway_zh": "..."}}"""

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API key loading
# ---------------------------------------------------------------------------

def _load_api_keys() -> dict:
    """Read API keys from ~/.stock-monitor.env and ~/.secrets.env."""
    keys = {}
    for env_file in [
        Path.home() / ".stock-monitor.env",
        Path.home() / ".secrets.env",
    ]:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Strip optional 'export ' prefix
            if line.startswith("export "):
                line = line[7:]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            keys[key] = value
    return keys


# ---------------------------------------------------------------------------
# LLM call functions
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, api_key: str) -> tuple[str, dict, str]:
    """Call Gemini 2.5 Pro. Returns (text, usage_dict, model_name)."""
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4000},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})
    return (text, usage, "gemini-2.5-pro")


def _call_openai(prompt: str, api_key: str, model: str = "gpt-4.1-mini") -> tuple[str, dict, str]:
    """Call OpenAI API. Returns (text, usage_dict, model_name)."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": 4000,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return (text, usage, model)


def _call_anthropic(prompt: str, api_key: str, model: str = "claude-sonnet-4-6") -> tuple[str, dict, str]:
    """Call Anthropic API. Returns (text, usage_dict, model_name)."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": 4000,
            "temperature": 0.4,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["content"][0]["text"]
    usage = data.get("usage", {})
    return (text, usage, model)


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _should_analyze(article: dict) -> bool:
    """Return True if article is eligible for analysis."""
    if article.get("summarized"):
        return False
    if article.get("source_id") == "bridgewater":
        return False
    if article.get("content_status") != "ok":
        return False
    return True


def _parse_llm_output(raw: str) -> Optional[dict]:
    """Parse LLM JSON output, stripping markdown fences if present.

    Returns dict with validated fields, or None on failure.
    """
    text = raw.strip()
    # Strip markdown ```json ... ``` fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    # Validate required fields
    required = {"summary_en", "summary_zh", "themes", "key_takeaway_en", "key_takeaway_zh"}
    if not required.issubset(data.keys()):
        return None

    # Filter themes to valid set only
    if isinstance(data["themes"], list):
        data["themes"] = [t for t in data["themes"] if t in VALID_THEMES]
    else:
        data["themes"] = []

    return data


def _analyze_with_fallback(
    content: str,
    api_keys: dict,
    title: str = "",
    source: str = "",
    date: str = "",
) -> Optional[dict]:
    """Try each model in MODEL_CHAIN with MAX_ATTEMPTS each.

    Returns result dict with _model and _usage metadata, or None if all fail.
    """
    prompt = ANALYSIS_PROMPT.format(
        title=title,
        source=source,
        date=date,
        content=content[:MAX_CONTENT_CHARS],
    )

    model_to_caller = {
        "gemini-2.5-pro": ("GEMINI_API_KEY", _call_gemini),
        "gpt-4.1-mini": ("OPENAI_API_KEY", _call_openai),
        "claude-sonnet-4-6": ("ANTHROPIC_API_KEY", _call_anthropic),
    }

    for model_name in MODEL_CHAIN:
        key_name, caller = model_to_caller[model_name]
        api_key = api_keys.get(key_name)
        if not api_key:
            log.info("  Skipping %s (no API key)", model_name)
            continue

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log.info("  Trying %s (attempt %d/%d)", model_name, attempt, MAX_ATTEMPTS)
                raw_text, usage, used_model = caller(prompt, api_key)
                parsed = _parse_llm_output(raw_text)
                if parsed is not None:
                    parsed["_model"] = used_model
                    parsed["_usage"] = usage
                    return parsed
                log.warning("  %s: failed to parse output (attempt %d)", model_name, attempt)
            except Exception as e:
                log.warning("  %s: error (attempt %d): %s", model_name, attempt, e)

    return None


# ---------------------------------------------------------------------------
# JSONL I/O (same pattern as fetch_content.py)
# ---------------------------------------------------------------------------

def load_articles() -> list[dict]:
    """Load all articles from the JSONL data file."""
    articles = []
    if DATA_FILE.exists():
        for line in DATA_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def save_articles(articles: list[dict]) -> None:
    """Rewrite all articles to JSONL data file atomically."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(json.dumps(a, ensure_ascii=False) for a in articles) + "\n"
    tmp_path = DATA_FILE.with_suffix(".jsonl.tmp")
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(str(tmp_path), str(DATA_FILE))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge Fund Research — LLM Analysis")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be analyzed")
    args = parser.parse_args()

    api_keys = _load_api_keys()
    articles = load_articles()
    pending = [a for a in articles if _should_analyze(a)]

    log.info("Found %d articles pending analysis (of %d total)", len(pending), len(articles))

    if args.dry_run:
        for a in pending:
            log.info("  [PENDING] %s — %s — %s", a.get("source_id", "?"), a.get("date", "n/a"), a.get("title", "?"))
        return

    success_count = 0
    fail_count = 0

    for a in pending:
        content_path = CONTENT_DIR / f"{a['id']}.txt"
        if not content_path.exists():
            log.warning("Content file missing for %s: %s", a["id"], content_path)
            fail_count += 1
            continue

        content = content_path.read_text(encoding="utf-8")
        log.info("Analyzing: %s — %s", a.get("source_id", "?"), a.get("title", "?"))

        result = _analyze_with_fallback(
            content,
            api_keys,
            title=a.get("title", ""),
            source=a.get("source_id", ""),
            date=a.get("date", ""),
        )

        if result is not None:
            a["summary_en"] = result["summary_en"]
            a["summary_zh"] = result["summary_zh"]
            a["themes"] = result["themes"]
            a["key_takeaway_en"] = result["key_takeaway_en"]
            a["key_takeaway_zh"] = result["key_takeaway_zh"]
            a["summarized"] = True
            a["analysis_model"] = result["_model"]
            success_count += 1
            log.info("  Success (%s): %d themes", result["_model"], len(result["themes"]))
        else:
            log.error("  All models failed for %s", a["id"])
            fail_count += 1

    save_articles(articles)
    log.info("Analysis complete: %d ok, %d failed", success_count, fail_count)

    print(f"\n{'='*60}")
    print(f"LLM Analysis — {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*60}")
    print(f"Pending: {len(pending)} | Success: {success_count} | Failed: {fail_count}")
    print()


if __name__ == "__main__":
    main()
