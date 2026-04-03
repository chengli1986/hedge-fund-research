# Entrypoint Activation & Autoresearch Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate the dormant entrypoint validation/discovery system into a self-improving loop — entrypoints get more accurate over time, and the pipeline catches higher-quality market insights with less noise.

**Architecture:** Three phases, each independently deployable. Phase 1 wires existing code into the pipeline + cron. Phase 2 adds a quality metric (article yield) and feedback loop. Phase 3 fills the `_classify_with_ai` stub and adds an autoresearch program that tunes scorer weights against the yield metric. No new dependencies — uses existing requests, BeautifulSoup4, and the LLM models already configured in `analyze_articles.py`.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, pytest. LLM calls via existing `analyze_articles.py` patterns (Gemini 2.5 Pro / GPT-4.1 Mini fallback).

---

## Current State (what exists, what doesn't)

| Component | Status | Gap |
|-----------|--------|-----|
| `entrypoint_scorer.py` | Working, 27 tests | Weights hardcoded (0.2/0.3/0.3/0.2), not tunable |
| `validate_entrypoints.py` | Working, 5 tests | Never runs in pipeline or cron |
| `discover_entrypoints.py` | Working, 7 tests | Never runs; `_classify_with_ai` is stub (returns None) |
| `config/entrypoints.json` | 6 manual seeds, all `verified_by: manual` | Never auto-updated |
| `config/inspection_state.json` | Written by `fetch_articles.py` on every run | Anomaly alerts go to log only, no email |
| `fetch_articles.py` anomaly detection | Working, 4 tests | Alerts only logged, not acted on |
| Quality metric | Does not exist | No way to measure "is this entrypoint producing good articles?" |
| Autoresearch program | Does not exist | No `_classify_with_ai`, no weight tuning |

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `run_pipeline.sh` | Modify | Add validation pre-check (Phase 1) |
| `validate_entrypoints.py` | Modify | Add `--json` output, `--email` alert (Phase 1) |
| `config/scorer_weights.json` | Create | Tunable scorer weights, replaces hardcoded values (Phase 2) |
| `entrypoint_scorer.py` | Modify | Load weights from config file (Phase 2) |
| `evaluate_entrypoints.py` | Create | Quality metric: article yield per entrypoint (Phase 2) |
| `discover_entrypoints.py` | Modify | Fill `_classify_with_ai` with real LLM call (Phase 3) |
| `autoresearch/program.md` | Create | AR program for weight tuning (Phase 3) |
| `autoresearch/results.tsv` | Create | AR experiment log (Phase 3) |
| `tests/test_unit_evaluate.py` | Create | Tests for evaluate_entrypoints (Phase 2) |
| `tests/test_unit_classify.py` | Create | Tests for `_classify_with_ai` (Phase 3) |

---

## Phase 1: Wire Existing Code into Pipeline

**Outcome:** Validation runs weekly, discovery runs monthly, alerts go to email.

### Task 1: Add `--json` output to `validate_entrypoints.py`

**Files:**
- Modify: `validate_entrypoints.py` (main function)
- Test: `tests/test_unit_validate.py`

**Why:** Pipeline needs machine-readable output to decide whether to proceed or alert. Currently only prints human-readable text.

- [ ] **Step 1: Write failing test**

```python
# tests/test_unit_validate.py — add to existing file

def test_main_json_output(tmp_path, monkeypatch, capsys):
    """--json flag produces parseable JSON to stdout."""
    ep_file = tmp_path / "entrypoints.json"
    ep_file.write_text(json.dumps({
        "version": 1,
        "sources": {
            "test-fund": {
                "entrypoints": [{"url": "https://example.com/research", "active": True}],
                "rejected_pages": []
            }
        }
    }))
    src_file = tmp_path / "sources.json"
    src_file.write_text(json.dumps({
        "sources": [{"id": "test-fund", "expected_hostname": "example.com"}]
    }))
    monkeypatch.setattr("validate_entrypoints.ENTRYPOINTS_FILE", ep_file)
    monkeypatch.setattr("validate_entrypoints.SOURCES_FILE", src_file)

    with unittest.mock.patch("validate_entrypoints.validate_entrypoint") as mock_val:
        mock_val.return_value = {
            "url": "https://example.com/research",
            "status": "ok",
            "scores": {"final": 0.82},
            "error": None
        }
        from validate_entrypoints import main
        import sys
        monkeypatch.setattr(sys, "argv", ["validate_entrypoints", "--json"])
        main()

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["test-fund"][0]["status"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_validate.py::test_main_json_output -v`
Expected: FAIL — `--json` flag not recognized

- [ ] **Step 3: Implement `--json` flag**

In `validate_entrypoints.py`, add to the argparse section in `main()`:

```python
parser.add_argument("--json", action="store_true", help="Output results as JSON to stdout")
```

At the end of `main()`, after collecting all results, add:

```python
if args.json:
    import json as json_mod
    print(json_mod.dumps(all_results, indent=2))
    return
```

Where `all_results` is a dict built during the existing loop:

```python
all_results: dict[str, list[dict]] = {}
# Inside the per-source loop, after validate_source():
all_results[source_id] = results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_validate.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research
git add validate_entrypoints.py tests/test_unit_validate.py
git commit -m "feat(validate): add --json output for pipeline integration"
```

---

### Task 2: Add validation pre-check to `run_pipeline.sh`

**Files:**
- Modify: `run_pipeline.sh`

**Why:** Validation should run before fetch so broken entrypoints are detected early. Non-fatal — pipeline continues even if validation finds issues (the alert is what matters).

- [ ] **Step 1: Add validation step before Stage 1**

```bash
# Insert after the "Pipeline starting" echo, before Stage 1:

# Pre-check: validate entrypoints (non-fatal, weekly skip logic)
VALIDATE_INTERVAL_DAYS=7
LAST_VALIDATE_FILE="$HOME/hedge-fund-research/config/.last_validated"
should_validate=false
if [[ ! -f "$LAST_VALIDATE_FILE" ]]; then
  should_validate=true
elif [[ $(find "$LAST_VALIDATE_FILE" -mtime +${VALIDATE_INTERVAL_DAYS} 2>/dev/null) ]]; then
  should_validate=true
fi

if [[ "$should_validate" == "true" ]]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Running entrypoint validation"
  if python3 validate_entrypoints.py --json > /tmp/gmia-validate.json 2>/dev/null; then
    touch "$LAST_VALIDATE_FILE"
    # Check for any degraded/error results
    if python3 -c "
import json, sys
data = json.load(open('/tmp/gmia-validate.json'))
bad = [(s, r['url'], r['status']) for s, rs in data.items() for r in rs if r['status'] != 'ok']
if bad:
    for s, u, st in bad:
        print(f'WARN: {s} entrypoint {st}: {u}', file=sys.stderr)
    sys.exit(1)
" 2>&1; then
      echo "Entrypoint validation: all OK"
    else
      echo "WARN: Some entrypoints degraded — check /tmp/gmia-validate.json"
    fi
  else
    echo "WARN: Entrypoint validation failed (non-fatal)"
  fi
else
  echo "Skipping entrypoint validation (last run <${VALIDATE_INTERVAL_DAYS}d ago)"
fi
```

- [ ] **Step 2: Test manually**

```bash
cd ~/hedge-fund-research
rm -f config/.last_validated  # Force validation to run
bash run_pipeline.sh 2>&1 | head -20
# Should see "Running entrypoint validation" + "all OK"
# Then verify: ls -la config/.last_validated
```

- [ ] **Step 3: Run full test suite to verify no breakage**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/ -x -q`
Expected: 151 passed

- [ ] **Step 4: Commit**

```bash
cd ~/hedge-fund-research
git add run_pipeline.sh
git commit -m "feat(pipeline): add weekly entrypoint validation pre-check"
```

---

### Task 3: Add monthly discovery cron

**Files:**
- Modify: crontab

**Why:** Discovery should run less frequently than validation (it crawls homepages, heavier). Monthly is enough since hedge fund sites don't reorganize often.

- [ ] **Step 1: Add cron entry**

```bash
# Run on 1st of each month at 20:00 BJT (12:00 UTC), log-only (no --write)
crontab -l > /tmp/crontab.bak
echo '0 12 1 * * ~/cron-wrapper.sh --name gmia-discover --timeout 600 --lock -- python3 ~/hedge-fund-research/discover_entrypoints.py --all >> ~/logs/gmia-discover.log 2>&1' | crontab -
```

Note: no `--write` flag — discovery results go to log only. Human reviews before activating new entrypoints. This is deliberate: automatic discovery + automatic activation = risk of polluting entrypoints.json with bad URLs.

- [ ] **Step 2: Verify cron entry**

```bash
crontab -l | grep gmia-discover
```

- [ ] **Step 3: Test discovery manually**

```bash
cd ~/hedge-fund-research
python3 discover_entrypoints.py --source man-group 2>&1 | tail -20
# Should see candidate pages with scores
```

- [ ] **Step 4: Commit (just document the cron)**

```bash
cd ~/hedge-fund-research
echo "# Monthly entrypoint discovery: 1st of month, 20:00 BJT (12:00 UTC)" >> docs/cron-notes.md
git add docs/cron-notes.md
git commit -m "docs: document monthly entrypoint discovery cron"
```

---

## Phase 2: Quality Metric & Feedback Loop

**Outcome:** Each entrypoint gets a measurable "article yield" score. The pipeline knows which entrypoints produce good content and which are noise.

### Task 4: Create `evaluate_entrypoints.py` — Article Yield Metric

**Files:**
- Create: `evaluate_entrypoints.py`
- Create: `tests/test_unit_evaluate.py`

**Why:** Without a quality metric, we can't optimize. Article yield = (summarized articles with takeaway) / (total articles fetched) per entrypoint. This is the "quality score" for the autoresearch loop.

- [ ] **Step 1: Write failing test**

```python
# tests/test_unit_evaluate.py
import json
import pytest
from evaluate_entrypoints import compute_yield


def test_perfect_yield():
    """All articles summarized with takeaways → yield 1.0."""
    articles = [
        {"source_id": "man-group", "summarized": True, "key_takeaway_en": "Good insight"},
        {"source_id": "man-group", "summarized": True, "key_takeaway_en": "Another insight"},
    ]
    result = compute_yield(articles)
    assert result["man-group"]["yield"] == 1.0
    assert result["man-group"]["total"] == 2
    assert result["man-group"]["quality_articles"] == 2


def test_mixed_yield():
    """Some articles not summarized → yield < 1.0."""
    articles = [
        {"source_id": "test", "summarized": True, "key_takeaway_en": "Good"},
        {"source_id": "test", "summarized": True, "key_takeaway_en": ""},
        {"source_id": "test", "summarized": False},
    ]
    result = compute_yield(articles)
    assert result["test"]["yield"] == pytest.approx(1 / 3)
    assert result["test"]["quality_articles"] == 1


def test_empty_articles():
    """No articles → empty result."""
    assert compute_yield([]) == {}


def test_disclaimer_detected():
    """Article whose takeaway says 'disclaimer' or 'no substantive' counts as noise."""
    articles = [
        {"source_id": "bw", "summarized": True,
         "key_takeaway_en": "The document is a legal disclaimer and contains no substantive content."},
        {"source_id": "bw", "summarized": True,
         "key_takeaway_en": "Real insight about markets."},
    ]
    result = compute_yield(articles)
    assert result["bw"]["quality_articles"] == 1
    assert result["bw"]["noise_articles"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_evaluate.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `evaluate_entrypoints.py`**

```python
#!/usr/bin/env python3
"""
Hedge Fund Research — Entrypoint Quality Metric

Computes article yield per source: what fraction of fetched articles
are genuine, summarized market insights (not disclaimers, not noise).

Usage:
    python3 evaluate_entrypoints.py          # print per-source yield
    python3 evaluate_entrypoints.py --json   # machine-readable output
"""

import json
import re
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"

# Patterns that indicate noise rather than real market insight
_NOISE_PATTERNS = [
    re.compile(r"legal disclaimer", re.IGNORECASE),
    re.compile(r"no substantive.*(content|analysis|investment)", re.IGNORECASE),
    re.compile(r"cookie (preferences|policy)", re.IGNORECASE),
    re.compile(r"terms of (use|service)", re.IGNORECASE),
]


def _is_noise(article: dict) -> bool:
    """Check if an article's takeaway indicates noise, not insight."""
    takeaway = article.get("key_takeaway_en", "") or ""
    return any(p.search(takeaway) for p in _NOISE_PATTERNS)


def _is_quality(article: dict) -> bool:
    """A quality article is summarized, has a real takeaway, and isn't noise."""
    if not article.get("summarized"):
        return False
    takeaway = (article.get("key_takeaway_en") or "").strip()
    if len(takeaway) < 20:
        return False
    return not _is_noise(article)


def compute_yield(articles: list[dict]) -> dict[str, dict]:
    """Compute per-source article yield metrics.

    Returns dict keyed by source_id:
        {
            "total": int,
            "quality_articles": int,
            "noise_articles": int,
            "yield": float  # quality / total, or 0.0 if total == 0
        }
    """
    by_source: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_source[a.get("source_id", "unknown")].append(a)

    result: dict[str, dict] = {}
    for source_id, arts in sorted(by_source.items()):
        quality = sum(1 for a in arts if _is_quality(a))
        noise = sum(1 for a in arts if _is_noise(a))
        total = len(arts)
        result[source_id] = {
            "total": total,
            "quality_articles": quality,
            "noise_articles": noise,
            "yield": round(quality / total, 4) if total > 0 else 0.0,
        }
    return result


def load_articles() -> list[dict]:
    """Load all articles from JSONL."""
    articles = []
    if not DATA_FILE.exists():
        return articles
    with open(DATA_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Entrypoint quality metric")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    articles = load_articles()
    result = compute_yield(articles)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    # Human-readable output
    total_quality = 0
    total_all = 0
    for source_id, metrics in result.items():
        status = "OK" if metrics["yield"] >= 0.7 else "LOW" if metrics["yield"] >= 0.4 else "BAD"
        print(f"  [{status}] {source_id}: yield={metrics['yield']:.2f} "
              f"({metrics['quality_articles']}/{metrics['total']} quality, "
              f"{metrics['noise_articles']} noise)")
        total_quality += metrics["quality_articles"]
        total_all += metrics["total"]

    overall = round(total_quality / total_all, 4) if total_all > 0 else 0.0
    print(f"\nOverall yield: {overall:.4f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_evaluate.py -v`
Expected: All PASS

- [ ] **Step 5: Run against real data to establish baseline**

```bash
cd ~/hedge-fund-research && python3 evaluate_entrypoints.py
```

Record the output as the baseline yield. This number is what Phase 3 optimizes.

- [ ] **Step 6: Commit**

```bash
cd ~/hedge-fund-research
git add evaluate_entrypoints.py tests/test_unit_evaluate.py
git commit -m "feat: add entrypoint quality metric (article yield per source)"
```

---

### Task 5: Extract scorer weights to config file

**Files:**
- Create: `config/scorer_weights.json`
- Modify: `entrypoint_scorer.py`
- Modify: `tests/test_unit_scorer.py`

**Why:** Hardcoded weights can't be tuned. Extracting to JSON makes them tunable by the autoresearch loop without code changes.

- [ ] **Step 1: Write failing test**

```python
# tests/test_unit_scorer.py — add to existing file

def test_score_final_uses_custom_weights(tmp_path):
    """score_final respects loaded custom weights."""
    weights_file = tmp_path / "scorer_weights.json"
    weights_file.write_text(json.dumps({
        "domain": 0.1, "path": 0.4, "structure": 0.4, "gate": 0.1
    }))
    from entrypoint_scorer import load_weights, score_final_with_weights
    weights = load_weights(weights_file)
    # With these weights, path matters more
    # domain=1.0, path=0.9, structure=0.5, gate=0.0
    result = score_final_with_weights(1.0, 0.9, 0.5, 0.0, weights)
    expected = 1.0 * 0.1 + 0.9 * 0.4 + 0.5 * 0.4 + 1.0 * 0.1  # 0.1 + 0.36 + 0.2 + 0.1 = 0.76
    assert result == pytest.approx(expected, abs=0.001)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py::test_score_final_uses_custom_weights -v`
Expected: FAIL — `load_weights` not found

- [ ] **Step 3: Create config file and modify scorer**

Create `config/scorer_weights.json`:

```json
{
    "domain": 0.2,
    "path": 0.3,
    "structure": 0.3,
    "gate": 0.2
}
```

Add to `entrypoint_scorer.py`:

```python
import json
from pathlib import Path

_DEFAULT_WEIGHTS = {"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}


def load_weights(path: Path | None = None) -> dict[str, float]:
    """Load scorer weights from JSON file. Falls back to defaults."""
    if path is None:
        path = Path(__file__).resolve().parent / "config" / "scorer_weights.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                weights = json.load(f)
            # Validate: all keys present, sum to 1.0 ± 0.01
            for key in _DEFAULT_WEIGHTS:
                if key not in weights:
                    return dict(_DEFAULT_WEIGHTS)
            if abs(sum(weights.values()) - 1.0) > 0.01:
                return dict(_DEFAULT_WEIGHTS)
            return weights
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULT_WEIGHTS)
    return dict(_DEFAULT_WEIGHTS)


def score_final_with_weights(
    domain: float, path: float, structure: float,
    gate_penalty: float, weights: dict[str, float]
) -> float:
    """Combine scores using provided weights."""
    return (
        domain * weights["domain"]
        + path * weights["path"]
        + structure * weights["structure"]
        + (1.0 - gate_penalty) * weights["gate"]
    )
```

Keep the existing `score_final()` function unchanged (backwards-compatible), but have it call the new one internally:

```python
def score_final(domain: float, path: float, structure: float, gate_penalty: float) -> float:
    """Combine individual scores into single final score."""
    return score_final_with_weights(domain, path, structure, gate_penalty, _DEFAULT_WEIGHTS)
```

- [ ] **Step 4: Run full test suite**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/ -x -q`
Expected: All 151+ tests PASS (existing tests unaffected since `score_final` behavior unchanged)

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research
git add config/scorer_weights.json entrypoint_scorer.py tests/test_unit_scorer.py
git commit -m "feat(scorer): extract weights to config for tunability"
```

---

## Phase 3: Autoresearch Integration

**Outcome:** LLM classifies candidate pages. An autoresearch program tunes scorer weights to maximize article yield.

### Task 6: Fill `_classify_with_ai` stub

**Files:**
- Modify: `discover_entrypoints.py`
- Create: `tests/test_unit_classify.py`

**Why:** The rule-based scorer works for obvious cases (paths with "research", "insights") but misses pages with unusual URL structures. LLM classification catches what rules miss. The stub has been waiting for this since the original entrypoint plan.

- [ ] **Step 1: Write failing test**

```python
# tests/test_unit_classify.py
import json
import pytest
from unittest.mock import patch, MagicMock
from discover_entrypoints import _classify_with_ai


def test_classify_research_page():
    """LLM classifies a research index page correctly."""
    html = """
    <html><body>
    <h1>Research & Insights</h1>
    <article><h2>Market Outlook Q1 2026</h2><time>2026-03-01</time></article>
    <article><h2>AI Capex Analysis</h2><time>2026-02-15</time></article>
    </body></html>
    """
    with patch("discover_entrypoints._call_llm") as mock_llm:
        mock_llm.return_value = {
            "is_research_index": True,
            "confidence": 0.92,
            "reasoning": "Contains dated research articles with analytical titles"
        }
        result = _classify_with_ai("https://example.com/research", html)

    assert result is not None
    assert result["is_research_index"] is True
    assert result["confidence"] >= 0.8


def test_classify_marketing_page():
    """LLM classifies a marketing page correctly."""
    html = "<html><body><h1>About Us</h1><p>We are a hedge fund.</p></body></html>"
    with patch("discover_entrypoints._call_llm") as mock_llm:
        mock_llm.return_value = {
            "is_research_index": False,
            "confidence": 0.95,
            "reasoning": "Corporate about page with no research content"
        }
        result = _classify_with_ai("https://example.com/about", html)

    assert result is not None
    assert result["is_research_index"] is False


def test_classify_llm_failure_returns_none():
    """LLM failure returns None (graceful degradation)."""
    with patch("discover_entrypoints._call_llm", side_effect=Exception("API error")):
        result = _classify_with_ai("https://example.com/research", "<html></html>")
    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_classify.py -v`
Expected: FAIL — `_call_llm` not found

- [ ] **Step 3: Implement LLM classification**

In `discover_entrypoints.py`, replace the `_classify_with_ai` stub:

```python
import os
import urllib.request


def _call_llm(prompt: str) -> dict:
    """Call LLM API for classification. Uses Gemini 2.5 Pro via OpenAI-compatible endpoint."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")

    payload = json.dumps({
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
    })
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def _classify_with_ai(url: str, html: str) -> dict | None:
    """Use LLM to classify whether a page is a research index.

    Returns {"is_research_index": bool, "confidence": float, "reasoning": str}
    or None on failure.
    """
    # Truncate HTML to avoid excessive token usage
    html_sample = html[:4000] if html else ""
    prompt = f"""Analyze this webpage and determine if it is a research index page
(a page that lists multiple research articles, market commentary, or investment insights).

URL: {url}
HTML (truncated):
{html_sample}

Respond in JSON:
{{"is_research_index": true/false, "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""

    try:
        return _call_llm(prompt)
    except Exception as e:
        log.warning("LLM classification failed for %s: %s", url, e)
        return None
```

Update `score_candidates` to use the AI classification in the scoring:

```python
def score_candidates(candidates, allowed_domains, page_html_map):
    # ... existing code ...
    for c in candidates:
        # ... existing scoring ...
        ai_result = _classify_with_ai(c["url"], page_html_map.get(c["url"], ""))

        # AI classification boosts or penalizes final score
        ai_adjustment = 0.0
        if ai_result is not None:
            if ai_result.get("is_research_index") and ai_result.get("confidence", 0) >= 0.8:
                ai_adjustment = 0.1  # Boost confirmed research pages
            elif not ai_result.get("is_research_index") and ai_result.get("confidence", 0) >= 0.8:
                ai_adjustment = -0.1  # Penalize confirmed non-research pages

        scored.append({
            "url": c["url"],
            "label": c.get("label", ""),
            "domain_score": round(d, 4),
            "path_score": round(p, 4),
            "structure_score": round(s, 4),
            "gate_penalty": round(g, 4),
            "final_score": round(f + ai_adjustment, 4),
            "ai_classification": ai_result,
        })
    # ... sort and return ...
```

- [ ] **Step 4: Run tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_classify.py tests/test_unit_discover.py -v`
Expected: All PASS (existing discover tests use mock, unaffected)

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research
git add discover_entrypoints.py tests/test_unit_classify.py
git commit -m "feat(discover): fill _classify_with_ai stub with Gemini 2.5 Pro LLM classification"
```

---

### Task 7: Create autoresearch program

**Files:**
- Create: `autoresearch/program.md`
- Create: `autoresearch/results.tsv`

**Why:** Following the proven pattern from global-news. The AR agent tunes `config/scorer_weights.json` to maximize the quality metric from `evaluate_entrypoints.py`. Each experiment: adjust weights → re-score existing entrypoints → measure yield → keep/revert.

- [ ] **Step 1: Create program.md**

```markdown
# GMIA Entrypoint Autoresearch: Scorer Weight Optimization

## Goal
Maximize the **article yield** — the fraction of fetched articles that are
genuine, summarized market insights (not disclaimers, not noise).

## The ONE file you can edit
`config/scorer_weights.json` — the four scorer weights.

## The metric
Run: `cd ~/hedge-fund-research && python3 evaluate_entrypoints.py`
Read the last line: `Overall yield: 0.XXXX`
Higher is better.

## Rules
1. **NEVER edit** any file except `config/scorer_weights.json`
2. **NEVER edit** evaluate_entrypoints.py, entrypoint_scorer.py, or any other code
3. Before EACH experiment: `cd ~/hedge-fund-research && git add -A && git commit -m "experiment: <description>"`
4. Run: `python3 evaluate_entrypoints.py` and read the yield
5. If yield **improved**: keep the commit, log to autoresearch/results.tsv
6. If yield **worsened or stayed the same**: `git reset --hard HEAD~1`
7. Log EVERY experiment (even failures)
8. **NEVER STOP** — keep running experiments until told to stop

## results.tsv format
Append one line per experiment (tab-separated):
```
commit_hash	yield	status	description
```

## Constraints
- scorer_weights.json must remain valid JSON
- All 4 keys must be present: domain, path, structure, gate
- All values must be in [0.05, 0.6]
- Values must sum to 1.0 (tolerance ±0.01)

## Experiment ideas (try in this order)
1. Increase structure weight (0.3 → 0.4), decrease domain (0.2 → 0.1)
2. Increase path weight (0.3 → 0.35), decrease gate (0.2 → 0.15)
3. Equal weights (0.25/0.25/0.25/0.25)
4. Structure-dominant (0.1/0.2/0.5/0.2)
5. Path-dominant (0.1/0.5/0.2/0.2)
```

- [ ] **Step 2: Create results.tsv with header**

```
commit_hash	yield	status	description
```

- [ ] **Step 3: Commit**

```bash
cd ~/hedge-fund-research
git add autoresearch/program.md autoresearch/results.tsv
git commit -m "feat: add autoresearch program for scorer weight optimization"
```

- [ ] **Step 4: Optionally add AR cron (like global-news)**

```bash
# Weekly AR session: Sundays 20:00 BJT (12:00 UTC)
# Uses claude CLI with --print flag for non-interactive execution
0 12 * * 0 ~/cron-wrapper.sh --name gmia-autoresearch --timeout 2700 --lock -- /usr/local/bin/claude --print -p "Read ~/hedge-fund-research/autoresearch/program.md and follow its instructions exactly. Run 5 experiments." >> ~/logs/gmia-autoresearch.log 2>&1
```

---

## Verification Checklist

After all phases are complete, verify:

- [ ] `python3 -m pytest tests/ -x -q` — all tests pass
- [ ] `python3 validate_entrypoints.py --json` — produces valid JSON
- [ ] `python3 evaluate_entrypoints.py` — shows per-source yield
- [ ] `bash run_pipeline.sh` — runs validation pre-check, then 4 stages
- [ ] `python3 discover_entrypoints.py --source man-group` — shows candidates with AI classification
- [ ] `crontab -l | grep gmia` — shows daily pipeline, nightly tests, monthly discover
- [ ] `config/scorer_weights.json` — exists, valid, sums to 1.0

## What This Does NOT Do (deliberate exclusions)

1. **No automatic entrypoint activation** — discovery finds candidates, human approves. This prevents polluting `entrypoints.json` with bad URLs.
2. **No new source addition** — the 6 sources are fixed. Discovery optimizes URLs within existing sources, not adds new funds.
3. **No parallel entrypoint racing** — one active entrypoint per source. We could add A/B testing later, but YAGNI for now.
4. **No LLM calls during validation** — validation is pure HTTP + scoring. LLM is only used during discovery (monthly). This keeps daily pipeline costs zero.
