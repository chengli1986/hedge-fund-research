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
import hashlib
import json
import sys
from functools import partial
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

import failure_labels

import jsonl_store

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

# Order set 2026-09-07 from a measured 30-article bake-off (content 25-26,639
# chars) run with the theme-allowlist prompt.  All three parse 30/30; cost per
# month at 369 articles was luna $0.36, gpt-4.1-mini $0.57, gemini-2.5-pro
# $8.76.  Luna leads on behaviour rather than price: it says when the source
# text does not support an answer instead of writing around the gap, and it
# files articles under the theme they are actually about (gpt-4.1-mini put
# "AI/Tech" first on 14 of 30, including a fixed-income outlook).
# gemini-2.5-pro stays last so an OpenAI-wide outage still leaves a tier;
# claude-sonnet-4-6 left the chain because _load_api_keys never sees an
# ANTHROPIC_API_KEY (it lives only in ~/.openclaw/.env), so that tier could
# never run - the chain is now three tiers that all actually have credentials.
# 2026-09-07: the last tier moved gemini-2.5-pro -> gemini-2.5-flash. It only
# runs when BOTH OpenAI tiers fail, so it is insurance rather than a running
# cost, but flash is ~4x cheaper per call ($0.30/$2.50 vs $1.25/$10) and keeps
# the chain from being single-provider. Measured on 3 real articles: 3/3
# parseable, no truncation at 12000, themes populated, and the same usage
# fields as pro (promptTokenCount + candidatesTokenCount + thoughtsTokenCount
# == totalTokenCount), so the accounting needed no new case.
MODEL_CHAIN = ["gpt-5.6-luna", "gpt-4.1-mini", "gemini-2.5-flash"]
OPENAI_MODELS = frozenset({"gpt-5.6-luna", "gpt-4.1-mini"})
MAX_ATTEMPTS = 2
MAX_CONTENT_CHARS = 15000

# Rendered from VALID_THEMES so the prompt and the allowlist cannot drift: a
# theme added to the set but not shown to the model can never be chosen, and one
# shown but no longer in the set is offered and then silently filtered away.
# Sorted for a stable prompt prefix (volatile prompts defeat caching).
_THEME_LINES = "\n".join(f'  - "{t}"' for t in sorted(VALID_THEMES))

_THEME_INSTRUCTION = """

Allowed themes - choose 1 to 3 that fit the article, copying each label exactly
as written below. Never invent a label or reword one: anything not on this list
is discarded, and the article ends up with no theme at all. Every article gets
at least one theme; if none fits well, choose the single closest. List the
most important theme first - the research page files each article under its
first theme and shows the rest only in the sidebar.
""" + _THEME_LINES

# Shown to the model in both prompts. Until 2026-09-13 neither prompt gave the
# model a way to decline, so navigation, a disclaimer or chart source notes
# saved in place of an article came back as a confident summary written from
# the title (research-affiliates 1115: "The author likely uses quantitative
# analysis ... The discussion probably extends"), and METADATA_PROMPT asked for
# title-based analysis outright. check_grounding() enforces this after the call.
_GROUNDING_INSTRUCTION = """
Rules for what you may write:
- Use ONLY the text given below. Every claim in the summary and takeaway must
  be stated in that text. Never infer an article's argument from its title,
  author, source or tags, and never describe what it "likely" or "probably" says.
- If the text is not the article itself -- navigation, a cookie or consent
  banner, a legal disclaimer or risk disclosure, chart source notes, a list of
  other articles, a registration or login wall -- or is too thin to summarise
  faithfully, do not write a summary. Respond instead with ONLY:
  {{"insufficient_content": true, "reason": "<what the text actually is>"}}
"""

ANALYSIS_PROMPT = """You are a senior investment analyst. Analyze the following hedge fund research article and produce a structured JSON response.
""" + _GROUNDING_INSTRUCTION + """
Article title: {title}
Source: {source}
Date: {date}

Article content:
{content}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{"summary_en": "...", "summary_zh": "...", "themes": [...], "key_takeaway_en": "...", "key_takeaway_zh": "..."}}""" + _THEME_INSTRUCTION

METADATA_PROMPT = """You are a senior investment analyst. You have LIMITED metadata (title, category, publisher's description) from a hedge fund research article, not the article itself. Summarise only what the description states; do not extend it into the article's likely argument.
""" + _GROUNDING_INSTRUCTION + """
Article title: {title}
Source: {source}
Date: {date}

Available metadata:
{content}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{"summary_en": "...", "summary_zh": "...", "themes": [...], "key_takeaway_en": "...", "key_takeaway_zh": "..."}}""" + _THEME_INSTRUCTION

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
# Token accounting
# ---------------------------------------------------------------------------

USAGE_LOG_FILE = BASE_DIR / "logs" / "analyze-usage.jsonl"

# Each provider names the same two numbers differently.  Reaching for one
# spelling with a `or 0` fallback would book an unrecognised payload as a free
# call, which is exactly the failure this table exists to prevent: unknown must
# stay unknown so an aggregate can say "incomplete" instead of "cheap".
# Each provider names the same numbers differently, and not all of them are
# obvious.  gemini-2.5-pro is a reasoning model: a live probe (2026-09-06) came
# back promptTokenCount 43 / candidatesTokenCount 34 / thoughtsTokenCount 305 /
# totalTokenCount 382 -- the thinking tokens are billed as output and are 90% of
# it, so mapping output to candidatesTokenCount alone understates spend ~9x.
# Layout: (input_key, output_keys, total_key).  Output keys after the first are
# optional (a non-reasoning reply carries no thoughtsTokenCount).
_USAGE_FIELDS = {
    "gemini-2.5-pro": ("promptTokenCount",
                       ("candidatesTokenCount", "thoughtsTokenCount"),
                       "totalTokenCount"),
    # Same shape, and flash thinks hard too: measured 1550 thinking tokens
    # against 198 of visible content on one article, all billed as output.
    "gemini-2.5-flash": ("promptTokenCount",
                         ("candidatesTokenCount", "thoughtsTokenCount"),
                         "totalTokenCount"),
    "gpt-4.1-mini": ("prompt_tokens", ("completion_tokens",), "total_tokens"),
    # reasoning_tokens is a breakdown of completion_tokens, not an addition to
    # it (verified live: prompt + completion == total while reasoning was 58).
    "gpt-5.6-luna": ("prompt_tokens", ("completion_tokens",), "total_tokens"),
    "claude-sonnet-4-6": ("input_tokens", ("output_tokens",), None),
}

_UNKNOWN_USAGE = {"input_tokens": None, "output_tokens": None,
                  "provider_total_tokens": None}


def _normalize_usage(model: str, usage: dict) -> dict:
    """Map a provider's usage payload onto input/output/provider total.

    Returns None for every field when the model is unknown or the payload does
    not carry what we expect -- never 0.  A model added to MODEL_CHAIN without a
    matching entry here shows up as unmeasured, not as free.

    provider_total_tokens is the provider's own total, recorded so a field we
    failed to map surfaces as a reconciliation gap (input + output != total) in
    the data itself instead of waiting to be spotted by eye.
    """
    fields = _USAGE_FIELDS.get(model)
    if not fields:
        return dict(_UNKNOWN_USAGE)
    in_key, out_keys, total_key = fields
    if in_key not in usage or out_keys[0] not in usage:
        return dict(_UNKNOWN_USAGE)
    return {
        "input_tokens": usage[in_key],
        "output_tokens": sum(usage[k] for k in out_keys if k in usage),
        "provider_total_tokens": usage.get(total_key) if total_key else None,
    }


def _append_usage_log(article_id_: str, model: str, usage: dict, path=None,
                      parsed: bool | None = None) -> None:
    """Append one token-accounting row.  Never raises.

    Instrumentation must not be able to kill the pipeline it measures, so every
    failure here is swallowed after a warning.  The row is written even when the
    counts are unknown: "a call happened and we cannot price it" is information,
    and dropping it would understate the total.
    """
    path = USAGE_LOG_FILE if path is None else path
    row = {
        "at": datetime.now(BJT).isoformat(timespec="seconds"),
        "article_id": article_id_,
        "model": model,
        "parsed": parsed,
        **_normalize_usage(model, usage),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("  usage log write failed (%s): %s", path, e)


# ---------------------------------------------------------------------------
# LLM call functions
# ---------------------------------------------------------------------------

def _call_gemini(prompt: str, api_key: str, model: str = "gemini-2.5-flash") -> tuple[str, dict, str]:
    """Call a Gemini model. Returns (text, usage_dict, model_name).

    The model id was hard-coded into the URL, so pointing the chain at a
    different Gemini model would have kept calling the expensive one while
    reporting the cheap one's name in the usage log.
    """
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            # 12000, not 4000: gemini-2.5-pro is a reasoning model and its
            # thinking tokens are billed and counted as output, so a long
            # article exhausted the budget mid-JSON -- observed twice on
            # 2026-09-06 at exactly 3996/4000, both unparseable.  It is now the
            # last tier, so a truncation here fails the whole chain.
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 12000},
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    candidate = data["candidates"][0]
    # A truncated reply still carries parts, so without this it surfaces only
    # as "failed to parse output" -- the symptom that made the 09-06 incident
    # look like a model quirk rather than an exhausted output budget.  Warn and
    # still return the text: the tokens were spent and the HTTP-boundary
    # accounting should book them.
    if candidate.get("finishReason") == "MAX_TOKENS":
        log.warning("  %s: reply truncated (finishReason=MAX_TOKENS, "
                    "output %s of maxOutputTokens) -- thinking exhausted the budget",
                    model, (data.get("usageMetadata") or {}).get("candidatesTokenCount"))
    content = candidate.get("content", {})
    parts = content.get("parts", [])
    if not parts:
        finish_reason = candidate.get("finishReason", "UNKNOWN")
        raise ValueError(f"Gemini returned no content parts (finishReason={finish_reason})")
    text = parts[0]["text"]
    usage = data.get("usageMetadata", {})
    return (text, usage, model)


# Per-model request shape.  Verified live 2026-09-07: gpt-5.6-luna rejects
# `max_tokens` (400 "Use 'max_completion_tokens' instead") and rejects
# `temperature: 0.4` (400 "Only the default (1) value is supported").  Sending
# one shape to both models makes the newer one fail on every call and the chain
# falls through to the next tier - which reads as a working fallback rather than
# a misconfiguration.  A model with no entry raises instead of guessing; its
# reasoning tokens are already inside `completion_tokens` (verified: prompt +
# completion == total with reasoning_tokens 58), so no special accounting.
_OPENAI_PARAMS = {
    "gpt-4.1-mini": {"temperature": 0.4, "max_tokens": 4000},
    # 8000, not 4000: reasoning shares the completion budget, and a cap that the
    # reasoning can exhaust is exactly how gemini-2.5-pro truncated its JSON at
    # 3996/4000 and failed to parse.  Observed completions run ~520.
    "gpt-5.6-luna": {"max_completion_tokens": 8000},
}


def _call_openai(prompt: str, api_key: str, model: str = "gpt-4.1-mini") -> tuple[str, dict, str]:
    """Call OpenAI API. Returns (text, usage_dict, model_name)."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            **_OPENAI_PARAMS[model],
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

# This file's fingerprint, stamped on every decline our own rules make (see
# _should_analyze). Same idea as fetch_content.CODE_VERSION.
CODE_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]

# The only decline a code change can invalidate: the model produced a summary
# and check_grounding rejected it. Everything else is the model saying the text
# is not an article, and the text will not have changed.
RULE_MADE_DECLINE = "grounding_failed"


def _should_analyze(article: dict) -> bool:
    """Return True if article is eligible for analysis."""
    if article.get("summarized"):
        return False
    if article.get("content_status") not in ("ok", "metadata_only"):
        return False
    # Declined or rejected once: the text will not have changed by tomorrow,
    # and re-asking every night is how a model eventually says yes. The one
    # exception is a rejection this file's own rules made: when those rules
    # change, the articles they rejected get one more run under the new rules.
    if article.get("analysis_status") == INSUFFICIENT:
        return (article.get("analysis_label") == RULE_MADE_DECLINE
                and article.get("analysis_code_version") != CODE_VERSION)
    return True


def strip_code_fences(raw: str) -> str:
    """Return `raw` with a surrounding markdown code fence removed.

    Shared so there is exactly one fence stripper.  backfill_themes.py grew a
    private one that required a newline after the opening fence, so a
    single-line ```json {...} ``` reply crashed its run while this regex (whose
    \n? is optional) handled it fine.
    """
    text = (raw or "").strip()
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def _parse_llm_output(raw: str) -> Optional[dict]:
    """Parse LLM JSON output, stripping markdown fences if present.

    Returns dict with validated fields, or None on failure.
    """
    text = strip_code_fences(raw)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    if data.get("insufficient_content") is True:
        reason = str(data.get("reason") or "").strip() or "model reported insufficient content"
        return {"insufficient_content": True, "reason": reason}

    # Validate required fields
    required = {"summary_en", "summary_zh", "themes", "key_takeaway_en", "key_takeaway_zh"}
    if not required.issubset(data.keys()):
        return None

    # Filter themes to valid set only
    if isinstance(data["themes"], list):
        # Fuzzy match themes: "Macro" → "Macro/Rates", "Oil" → "Oil/Energy", etc.
        matched_themes = []
        for t in data["themes"]:
            # Gemini sometimes returns {"name": "AI/Tech", "rationale": "..."} instead of strings
            if isinstance(t, dict):
                t = t.get("name") or t.get("theme") or t.get("label") or ""
            if not isinstance(t, str) or not t:
                continue
            if t in VALID_THEMES:
                matched_themes.append(t)
            else:
                # Try partial match on first part before /
                t_lower = t.lower().strip()
                for valid in VALID_THEMES:
                    parts = valid.lower().split("/")
                    if t_lower in parts or any(t_lower.startswith(p) for p in parts):
                        matched_themes.append(valid)
                        break
        data["themes"] = list(dict.fromkeys(matched_themes))  # deduplicate, preserve order
    else:
        data["themes"] = []

    return data


INSUFFICIENT = "insufficient_content"

# Coverage: the share of the English summary's content words (stemmed to five
# letters) that occur in the article text. Measured on 2026-09-13 across the
# corpus: 1,351 ordinary summaries have p01 0.39 and median ~0.75; the 38
# title-only ARK summaries top out at 0.31 and the 21 summaries of known-bad
# bodies have median ~0.14. 0.25 rejects the chart-notes and navigation cases
# (metlife 0.11, research-affiliates 0.19) while a faithful paraphrase of a
# 267-char teaser (robeco, 0.28) and a summary of chart numbers (gmo, 0.39)
# pass. Coverage alone misses some bad bodies (damaged max 0.49), which is why
# the phrase checks and the model's own refusal exist -- no single layer is
# the fix.
MIN_SUMMARY_COVERAGE = 0.25
_COVERAGE_STEM = 5
_COVERAGE_STOPWORDS = frozenset("""
about above after again against also although among another around because been
before being below between both could does doing down during each even every
from further have having here however into itself just more most much must near
need once only other over same should since some such than that their them then
there these they this those though through thus under until upon very were what
when where which while whom will with within without would your
""".split())

# Speculation about the article itself. Deliberately not "may"/"could"/"可能"
# on their own: hedging about markets is normal analysis.
_SPECULATION_EN = re.compile(
    r"\bbased on (the )?(article'?s? |paper'?s? |report'?s? )?(title|headline|tags?|authors?)\b"
    r"|\b(title|headline) (suggests|implies|indicates)\b"
    r"|\b(the )?(article|paper|piece|report|author|authors|analysis|discussion|it)\s+"
    r"(likely|probably|presumably|possibly)\b"
    r"|\b(likely|probably|presumably)\s+(argues?|discuss|discusses|explores?|examines?|"
    r"investigates?|uses?|covers?|addresses|extends?|delves?|focuses|contrasts?|highlights?)\b",
    re.IGNORECASE,
)
# The hedge must sit directly on the subject ("作者可能", "讨论可能还会"). A gap
# means the summary is reporting the article's own view -- the first version
# allowed six characters and flagged "文章警示可能出现衰退" and "作者认为这很
# 可能是噪音" in the corpus, and "推测" alone flagged "文章推测...", which is
# the article conjecturing, not the model.
_SPECULATION_ZH = re.compile(
    r"(根据|从|依据)(文章)?(的)?(标题|题目)"
    r"|(文章|作者|报告|该文|本文|论文|讨论)(很|大|也|还)?(可能|大概|或许|想必)"
)
# The summary describing its input rather than an article. Extended 2026-09-14:
# two lazard-am summaries passed the first version by saying the same thing in
# other words -- "The article, titled ..., provides no substantive
# macroeconomic analysis ... the remainder consists of ... disclaimers" and
# "The provided article ... consists solely of an introduction ... and a legal
# disclaimer".
#
# Every form must name the INPUT as its subject. The first extension did not
# ("(provides|contains) no substantive", "no substantive ... analysis",
# 未提供实质) and rejected ordinary market sentences -- "The Fed provides no
# substantive guidance", "美联储并未提供实质性指引" -- and a rejection is
# permanent. "article" is accepted as the subject only with an article-ish
# object (analysis/content/commentary/views/discussion), because a real
# summary may say "the article does not provide a price target but argues...".
_INPUT_NOUN = r"(?:text|excerpt|page|document|material|content)"
_ARTICLE_OBJECT = r"(?:analysis|content|commentary|discussion|views|insights?|information)"
_NOT_AN_ARTICLE = re.compile(
    r"\bthe (?:provided|given|supplied) (?:text|content|material|document|article|excerpt|page)\b"
    rf"|\b(?:the |this )?{_INPUT_NOUN} (?:does not|doesn't|did not) (?:contain|include|provide)\b"
    rf"|\b(?:the |this )?{_INPUT_NOUN} (?:contains|provides|includes|offers) no\b"
    rf"|\b(?:article|piece)\b[^.;]{{0,80}}?\b(?:provides|contains|offers|includes) no substantive "
    rf"(?:\w+ ){{0,2}}{_ARTICLE_OBJECT}\b"
    rf"|\b(?:article|piece) (?:does not|doesn't) (?:contain|include|provide) (?:any |the )?"
    rf"(?:substantive|actual) (?:\w+ ){{0,2}}{_ARTICLE_OBJECT}\b"
    r"|提供的(?:文本|内容|材料|文章)"
    r"|(?:文本|本文|该文|原文|文章)(?:中)?(?:并?未|没有|不)(?:包含|提供)(?:任何)?实质",
    re.IGNORECASE,
)


# The wording rule alone means the model read the article and wrote about
# "the provided text" (franklin-templeton 2026-09-16, a real 9,287-char
# article). Asking once for the same summary without that framing keeps the
# article; the instruction repeats the refusal so a page that really has no
# article can still be declined, and check_grounding runs again on the answer.
_WORDING_PROBLEM = "describes its input"
_WORDING_RETRY_INSTRUCTION = """

IMPORTANT -- your previous answer was rejected: it wrote about the input
("the provided text", "the document contains no ...") instead of about the
subject. Write the summary about the subject matter itself, never referring to
the text, the page, the document or the article as such. Do not add anything
that is not in the text. If the text genuinely holds no article to summarise,
answer {"insufficient_content": true, "reason": "<what the text actually is>"}
instead of summarising it."""


def _content_words(text: str) -> set[str]:
    return {w[:_COVERAGE_STEM] for w in re.findall(r"[a-z]{4,}", (text or "").lower())
            if w not in _COVERAGE_STOPWORDS}


def _mostly_latin(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(ch.isascii() for ch in letters) >= 0.5 * len(letters)


def check_grounding(result: dict, content: str) -> list[str]:
    """Reasons the summary is not supported by `content`; [] when it is.

    Runs on every summary a model returns, independent of the prompt: a
    prompt is a request, this is the check. Covers all four text fields for
    wording, and the English summary for coverage. Coverage is skipped for a
    non-Latin article (a Japanese source summarised in English shares almost
    no words with it); the wording checks still apply.
    """
    problems = []
    en = " ".join(str(result.get(k) or "") for k in ("summary_en", "key_takeaway_en"))
    zh = " ".join(str(result.get(k) or "") for k in ("summary_zh", "key_takeaway_zh"))

    if _SPECULATION_EN.search(en) or _SPECULATION_ZH.search(zh):
        hit = (_SPECULATION_EN.search(en) or _SPECULATION_ZH.search(zh)).group(0)
        problems.append(f"speculates about the article ({hit!r})")
    if _NOT_AN_ARTICLE.search(en) or _NOT_AN_ARTICLE.search(zh):
        hit = (_NOT_AN_ARTICLE.search(en) or _NOT_AN_ARTICLE.search(zh)).group(0)
        problems.append(f"{_WORDING_PROBLEM}, not an article ({hit!r})")

    if _mostly_latin(content):
        words = _content_words(str(result.get("summary_en") or ""))
        if words:
            coverage = len(words & _content_words(content)) / len(words)
            if coverage < MIN_SUMMARY_COVERAGE:
                problems.append(f"coverage {coverage:.2f} < {MIN_SUMMARY_COVERAGE}: "
                                "most of the summary's content words are not in the text")
    return problems


# A metadata_only file holding nothing but "Title: ..." (all 38 ARK rows as of
# 2026-09-13, avg 63 chars) gives a model nothing to summarise but the title.
MIN_METADATA_DESCRIPTION_CHARS = 150


def is_title_only(content: str) -> bool:
    body = "\n".join(line for line in (content or "").splitlines()
                     if not line.strip().lower().startswith("title:"))
    return len(body.strip()) < MIN_METADATA_DESCRIPTION_CHARS


def _analyze_with_fallback(
    content: str,
    api_keys: dict,
    title: str = "",
    source: str = "",
    date: str = "",
    metadata_only: bool = False,
    article_id: str = "",
) -> Optional[dict]:
    """Try each model in MODEL_CHAIN with MAX_ATTEMPTS each.

    Returns result dict with _model and _usage metadata, or None if all fail.
    When metadata_only=True, uses a lighter prompt for RSS-summary-level content.
    """
    template = METADATA_PROMPT if metadata_only else ANALYSIS_PROMPT
    prompt = template.format(
        title=title,
        source=source,
        date=date,
        content=content[:MAX_CONTENT_CHARS],
    )

    # partial, not the bare function: the dispatcher calls caller(prompt, key),
    # so without binding the model every OpenAI tier would silently run
    # _call_openai's default model and the chain would have two identical tiers.
    model_to_caller = {
        "gpt-5.6-luna": ("OPENAI_API_KEY", partial(_call_openai, model="gpt-5.6-luna")),
        "gpt-4.1-mini": ("OPENAI_API_KEY", partial(_call_openai, model="gpt-4.1-mini")),
        "gemini-2.5-flash": ("GEMINI_API_KEY", partial(_call_gemini, model="gemini-2.5-flash")),
    }

    def call(model_prompt: str, caller, api_key: str):
        """One model call: raw -> parsed, booked at the HTTP boundary."""
        raw_text, usage, used_model = caller(model_prompt, api_key)
        parsed = _parse_llm_output(raw_text)
        # Book the call here -- a response that fails to parse burned the same
        # tokens as one that succeeds. An exception never got a usage payload,
        # so it books nothing: inventing a zero row would be fabricating data.
        _append_usage_log(article_id, used_model, usage, parsed=parsed is not None)
        return parsed, usage, used_model

    for model_name in MODEL_CHAIN:
        key_name, caller = model_to_caller[model_name]
        api_key = api_keys.get(key_name)
        if not api_key:
            log.info("  Skipping %s (no API key)", model_name)
            continue

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log.info("  Trying %s (attempt %d/%d)", model_name, attempt, MAX_ATTEMPTS)
                parsed, usage, used_model = call(prompt, caller, api_key)
                if parsed is not None:
                    # A decline or a rejected summary is final: the next tier
                    # would get the same text, and a weaker model is the one
                    # likelier to invent an answer that happens to pass.
                    if not parsed.get("insufficient_content"):
                        problems = check_grounding(parsed, content[:MAX_CONTENT_CHARS])
                        if problems and all(p.startswith(_WORDING_PROBLEM) for p in problems):
                            # Wording only: the summary's content words are in
                            # the article. One re-ask of the same model, which
                            # may still decline; anything else it returns is
                            # checked again below.
                            log.warning("  %s: %s -- re-asking once without the input framing",
                                        model_name, "; ".join(problems))
                            retry, retry_usage, retry_model = call(
                                prompt + _WORDING_RETRY_INSTRUCTION, caller, api_key)
                            if retry is not None:
                                parsed, usage, used_model = retry, retry_usage, retry_model
                                problems = ([] if parsed.get("insufficient_content")
                                            else check_grounding(parsed, content[:MAX_CONTENT_CHARS]))
                        if problems:
                            log.warning("  %s: summary rejected by grounding check: %s",
                                        model_name, "; ".join(problems))
                            parsed = {"insufficient_content": True,
                                      "reason": "failed grounding check: " + "; ".join(problems)}
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
    """Load all articles from the JSONL data file (see jsonl_store)."""
    rows, _ = jsonl_store.read_rows(DATA_FILE)
    return rows


def save_articles(articles: list[dict], path: Path | None = None) -> None:
    """Rewrite all articles to a JSONL data file atomically.

    `path` uses a None sentinel rather than defaulting to DATA_FILE: a default
    argument binds once at def time, which is how write_session_heartbeat's
    output path stayed pinned through every monkeypatch.  Parameterised so
    backfill_themes.py can reuse this writer instead of growing its own -- it
    had a plain write_text(), and a failure mid-write left 151 of 4000 rows.
    """
    path = DATA_FILE if path is None else path
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(json.dumps(a, ensure_ascii=False) for a in articles) + "\n"
    tmp_path = path.with_suffix(".jsonl.tmp")
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(str(tmp_path), str(path))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _content_root() -> Path:
    return CONTENT_DIR.resolve()


def _resolve_content_path(article: dict) -> Path:
    """Resolve the on-disk content path from article metadata."""
    stored_path = str(article.get("content_path", "")).strip()
    if stored_path:
        content_path = Path(stored_path)
        if not content_path.is_absolute():
            content_path = BASE_DIR / content_path
        resolved_path = content_path.resolve()
        try:
            resolved_path.relative_to(_content_root())
        except ValueError as exc:
            raise ValueError(f"content_path escapes content dir: {stored_path}") from exc
        return resolved_path
    return CONTENT_DIR / f"{article['id']}.txt"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_SUMMARY_FIELDS = ("summary_en", "summary_zh", "key_takeaway_en", "key_takeaway_zh")


def _body_key(source_id: str, text: str) -> tuple[str, str]:
    """Identity of a stored body within its source, ignoring whitespace."""
    normalised = re.sub(r"\s+", " ", text).strip()
    return source_id, hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _published_bodies(articles: list[dict]) -> dict[tuple[str, str], dict]:
    """(source_id, body hash) -> the summarised article that already owns it.

    Stage 2 has stored one document under two titles (oaktree's memo links,
    GMO's shared download buttons) and one article under two URL spellings
    (brookfield, apollo, ares, man-group, rothschild, metlife, mfs) -- 14
    identical-body groups on 2026-09-14. A summary of such a body passes
    check_grounding, because it is faithful to the text; publishing it again
    is the fault. Only summarised articles count: a declined copy published
    nothing.
    """
    owners: dict[tuple[str, str], dict] = {}
    for a in articles:
        if not a.get("summarized"):
            continue
        try:
            text = _resolve_content_path(a).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        owners.setdefault(_body_key(a.get("source_id", ""), text), a)
    return owners


# A re-published document is rarely byte-identical: janus-henderson's "Charts
# for the beach 2026" came back 13 characters longer than the stored copy and
# was summarised a second time (2026-09-17). Similarity is measured as Jaccard
# over 8-character shingles, and ONLY between articles of the same source that
# carry the same title -- without that, ares' 1,489-char boilerplate-heavy
# pieces score 0.89 against a dozen unrelated ones, and gmo's quarterly
# forecasts (one template, different numbers) score 0.90 against each other.
#
# Measured on the store, same source + same title:
#   1.00 loomis monthly update, 1.00 janus chart deck  -> duplicates
#   0.79 franklin survey page still being filled in    -> kept
#   0.35 kkr, 0.33 gsam, 0.30 loomis, 0.18 gsam, 0.14 troweprice -> real issues
# so 0.85 sits far above every genuine issue seen and below both duplicates.
DUPLICATE_JACCARD = 0.85
NEAR_DUPLICATE_WATCH = 0.6          # logged and kept, so the borderline stays visible
SHINGLE = 8
MIN_COMPARABLE_CHARS = 400          # below this a ratio says nothing


def _shingles(text: str) -> set:
    compact = re.sub(r"[^0-9a-z]", "", (text or "").lower())[:20000]
    return {compact[i:i + SHINGLE] for i in range(max(len(compact) - SHINGLE + 1, 0))}


def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def published_index(articles: list[dict], bodies: dict[str, str] | None = None) -> dict:
    """What has already been published, for both duplicate checks.

    bodies is for tests; in production the text comes from each article's
    stored content file.
    """
    index = {"exact": {}, "by_title": {}}
    for a in articles:
        if not a.get("summarized"):
            continue
        if bodies is not None:
            text = bodies.get(a.get("id", ""))
            if text is None:
                continue
        else:
            try:
                text = _resolve_content_path(a).read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
        index["exact"].setdefault(_body_key(a.get("source_id", ""), text), a)
        key = (a.get("source_id", ""), _title_key(a.get("title", "")))
        index["by_title"].setdefault(key, []).append((a, text))
    return index


def duplicate_owner(index: dict, source_id: str, title: str, text: str) -> dict | None:
    """The already-published article this body repeats, or None."""
    owner = index["exact"].get(_body_key(source_id, text))
    if owner is not None:
        return owner
    if len(re.sub(r"\s+", " ", text or "").strip()) < MIN_COMPARABLE_CHARS:
        return None
    mine = _shingles(text)
    if not mine:
        return None
    for other, other_text in index["by_title"].get((source_id, _title_key(title)), []):
        theirs = _shingles(other_text)
        if not theirs:
            continue
        jaccard = len(mine & theirs) / len(mine | theirs)
        if jaccard >= DUPLICATE_JACCARD:
            return other
        if jaccard >= NEAR_DUPLICATE_WATCH:
            log.info("  near-duplicate kept (jaccard %.2f): %r vs already-published %s",
                     jaccard, title, other.get("id"))
    return None


def _record_insufficient(article: dict, result: dict) -> None:
    """Mark an article as not summarisable, and remove any older summary.

    The removal matters for re-queued articles (the 2026-09-13 backfill): an
    invented summary left in place would stay on the page. Themes go too --
    publish.py files an article under themes[0] whether or not it is summarised.
    """
    article["summarized"] = False
    article["analysis_status"] = INSUFFICIENT
    article["analysis_reason"] = result.get("reason") or ""
    # The countable form of the reason, shared with stage-2 failure labels.
    article["analysis_label"] = failure_labels.classify_analysis_decline(article["analysis_reason"])
    article["analysis_model"] = result.get("_model")
    article["analysis_checked_at"] = datetime.now(BJT).isoformat(timespec="seconds")
    article["analysis_code_version"] = CODE_VERSION
    for field in _SUMMARY_FIELDS:
        article.pop(field, None)
    article["themes"] = []
    article.pop("analysis_confidence", None)


def main() -> int:
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
    insufficient_count = 0
    published = published_index(articles)

    for a in pending:
        try:
            content_path = _resolve_content_path(a)
        except ValueError as e:
            log.warning("Invalid content path for %s: %s", a["id"], e)
            a["content_status"] = "failed"
            fail_count += 1
            continue
        if not content_path.exists():
            log.warning("Content file missing for %s: %s", a["id"], content_path)
            a["content_status"] = "failed"
            fail_count += 1
            continue

        content = content_path.read_text(encoding="utf-8")
        is_metadata = a.get("content_status") == "metadata_only"
        level = "metadata-only" if is_metadata else "full"
        log.info("Analyzing (%s): %s — %s", level, a.get("source_id", "?"), a.get("title", "?"))

        owner = duplicate_owner(published, a.get("source_id", ""), a.get("title", ""), content)
        if owner is not None and owner.get("id") != a["id"]:
            result = {"insufficient_content": True, "_model": None,
                      "reason": (f"same text as the already-summarised article "
                                 f"\"{owner.get('title', '')}\" ({owner.get('id')}); the page "
                                 f"served a document that belongs to another article, or "
                                 f"this article is stored twice")}
        elif is_metadata and is_title_only(content):
            result = {"insufficient_content": True, "_model": None,
                      "reason": "metadata holds only a title; nothing to summarise"}
        else:
            result = _analyze_with_fallback(
                content,
                api_keys,
                title=a.get("title", ""),
                source=a.get("source_id", ""),
                date=a.get("date", ""),
                metadata_only=is_metadata,
                article_id=a["id"],
            )

        if result is not None and result.get("insufficient_content"):
            _record_insufficient(a, result)
            insufficient_count += 1
            log.warning("  Not summarised (%s): %s", a["id"], result["reason"])
        elif result is not None:
            a["summary_en"] = result["summary_en"]
            a["summary_zh"] = result["summary_zh"]
            a["themes"] = result["themes"]
            a["key_takeaway_en"] = result["key_takeaway_en"]
            a["key_takeaway_zh"] = result["key_takeaway_zh"]
            a["summarized"] = True
            # Register it so a second copy later in the SAME run is caught too
            # (both halves: the exact hash and the same-title similarity list).
            published["exact"].setdefault(_body_key(a.get("source_id", ""), content), a)
            published["by_title"].setdefault(
                (a.get("source_id", ""), _title_key(a.get("title", ""))), []).append((a, content))
            a.pop("analysis_status", None)
            a.pop("analysis_reason", None)
            a.pop("analysis_label", None)
            a.pop("analysis_code_version", None)
            a["analysis_model"] = result["_model"]
            if is_metadata:
                a["analysis_confidence"] = "low"
            success_count += 1
            log.info("  Success (%s): %d themes", result["_model"], len(result["themes"]))
        else:
            log.error("  All models failed for %s", a["id"])
            fail_count += 1

    save_articles(articles)
    log.info("Analysis complete: %d ok, %d failed, %d not summarised (insufficient content)",
             success_count, fail_count, insufficient_count)

    print(f"\n{'='*60}")
    print(f"LLM Analysis — {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*60}")
    print(f"Pending: {len(pending)} | Success: {success_count} | Failed: {fail_count}"
          f" | Not summarised (insufficient content): {insufficient_count}")
    print()

    # Articles were waiting and not one was summarised: quota exhaustion, or
    # all three MODEL_CHAIN tiers down. Until 2026-09-11 main() returned None
    # here too, so the process exited 0 and run_pipeline.sh's
    # `if python3 analyze_articles.py` guard saw a clean run -- the same shape
    # fixed for stage 1 in 9f6e291.
    #
    # An empty pending list is the normal quiet case and stays silent, and a
    # partial failure is not an outage: those articles keep their unsummarised
    # state and are retried next run, and their cost is already visible in
    # logs/analyze-usage.jsonl.
    # A decline is a handled outcome: only "nothing answered at all" is an outage.
    if pending and success_count == 0 and insufficient_count == 0:
        log.error("TOTAL ANALYSIS OUTAGE: %d article(s) pending, none summarised",
                  len(pending))
        return 1
    return 0


if __name__ == "__main__":
    # sys.exit, not a bare call: main()'s return value was discarded, so the
    # process exited 0 however the run went.
    sys.exit(main() or 0)
