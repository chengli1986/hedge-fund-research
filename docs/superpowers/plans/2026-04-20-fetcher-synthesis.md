# Fetcher Synthesis Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated pipeline where a Claude Code agent tries multiple scraping strategies for each `inaccessible` fund, generates a working `fetch_*` function, injects it into `fetch_articles.py`, and promotes the fund to `validated` status — all without human intervention.

**Architecture:** A Python helper script (`synthesize_fetchers.py`) lists inaccessible candidates with skip logic and outputs a target list. A Claude Code agent reads `fetcher-synthesis/program.md`, inspects each target site via Playwright, writes and tests a fetcher function, injects it into `fetch_articles.py`, and updates `fund_candidates.json`. A Bash wrapper (`scripts/wrapper-fetcher-synthesis.sh`) invokes the agent on a weekly cron schedule.

**Tech Stack:** Python 3.12, Playwright (chromium), BeautifulSoup4, Claude Code CLI (`claude --print`), existing `_get_playwright_page()` helper in `fetch_articles.py`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `synthesize_fetchers.py` | Create | List inaccessible targets with skip logic; skip-field helpers |
| `fetcher-synthesis/program.md` | Create | Agent instructions: inspect → write → test → inject → commit |
| `scripts/wrapper-fetcher-synthesis.sh` | Create | Weekly cron wrapper; invokes `claude --print` with program.md |
| `fetch_articles.py` | Modify | Add `# FETCHER_SYNTHESIS_INSERTION_POINT` marker comment before dispatcher block |
| `tests/test_unit_synthesize_fetchers.py` | Create | Unit tests for candidate filtering, skip logic, injection marker |

---

## Task 1: Add injection marker to `fetch_articles.py` + tests

The agent needs a reliable, stable anchor point to insert new fetch functions. We add a single-line marker comment between the last hand-written fetcher and the dispatcher block.

**Files:**
- Modify: `fetch_articles.py:1148` (line before `# ---...Dispatcher...---`)
- Create: `tests/test_unit_synthesize_fetchers.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_unit_synthesize_fetchers.py
import subprocess, sys

def test_injection_marker_exists_in_fetch_articles():
    """The marker comment must exist exactly once in fetch_articles.py."""
    text = open("fetch_articles.py").read()
    assert text.count("# FETCHER_SYNTHESIS_INSERTION_POINT") == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /home/ubuntu/hedge-fund-research
python3 -m pytest tests/test_unit_synthesize_fetchers.py::test_injection_marker_exists_in_fetch_articles -v
```

Expected: FAIL with `AssertionError` (marker not yet present)

- [ ] **Step 3: Add marker to `fetch_articles.py`**

Find line 1148 (the `# ---...Dispatcher...---` line). Insert ONE line immediately before it:

```python
# FETCHER_SYNTHESIS_INSERTION_POINT — auto-generated fetchers inserted above this line


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
```

- [ ] **Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_unit_synthesize_fetchers.py::test_injection_marker_exists_in_fetch_articles -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add fetch_articles.py tests/test_unit_synthesize_fetchers.py
git commit -m "feat: add injection marker + test scaffold for fetcher synthesis"
```

---

## Task 2: `synthesize_fetchers.py` — candidate lister with skip logic

This script lists inaccessible funds that need a synthesis attempt. The agent reads its JSON output to know which funds to process.

**Files:**
- Create: `synthesize_fetchers.py`
- Modify: `tests/test_unit_synthesize_fetchers.py`

Skip logic:
- Exclude quality == `"LOW"` (not worth scraping — retail/HR content)
- Exclude if already in `FETCHERS` dict (fetcher already written)
- Exclude if `synthesis_attempted_at` is within 7 days (avoid retry storm)

Output: JSON array written to stdout, each item: `{"id", "name", "homepage_url", "research_url", "notes"}`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_unit_synthesize_fetchers.py
import json, sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
import synthesize_fetchers


def _make_candidate(id_, status, quality, attempted_at=None):
    c = {"id": id_, "name": id_, "status": status, "quality": quality,
         "homepage_url": f"https://{id_}.com", "research_url": None, "notes": ""}
    if attempted_at:
        c["synthesis_attempted_at"] = attempted_at
    return c


def test_list_targets_returns_only_inaccessible():
    candidates = [
        _make_candidate("alpha", "inaccessible", "HIGH"),
        _make_candidate("beta",  "validated",    "HIGH"),
        _make_candidate("gamma", "rejected",     "HIGH"),
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]


def test_list_targets_excludes_low_quality():
    candidates = [
        _make_candidate("alpha", "inaccessible", "HIGH"),
        _make_candidate("beta",  "inaccessible", "LOW"),
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]


def test_list_targets_excludes_already_has_fetcher():
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH")]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value={"alpha"}):
        result = synthesize_fetchers.list_targets()
    assert result == []


def test_list_targets_excludes_recently_attempted():
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=recent)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert result == []


def test_list_targets_includes_stale_attempt():
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=stale)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_unit_synthesize_fetchers.py -k "test_list_targets" -v
```

Expected: 5 FAILs (ImportError — module not yet created)

- [ ] **Step 3: Implement `synthesize_fetchers.py`**

```python
#!/usr/bin/env python3
"""Helper for fetcher synthesis pipeline.

Lists inaccessible fund candidates that need a new fetcher, applying skip logic.
Outputs a JSON array to stdout. Used by fetcher-synthesis/program.md agent.
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
FETCH_ARTICLES = BASE_DIR / "fetch_articles.py"
SKIP_WINDOW_DAYS = 7


def load_candidates() -> list[dict]:
    data = json.loads(CANDIDATES_FILE.read_text())
    return data if isinstance(data, list) else data.get("candidates", [])


def load_fetcher_ids() -> set[str]:
    """Parse FETCHERS dict keys from fetch_articles.py (no import needed)."""
    text = FETCH_ARTICLES.read_text()
    start = text.find("FETCHERS = {")
    if start == -1:
        return set()
    block = text[start:text.find("}", start) + 1]
    return {
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"')
    }


def list_targets() -> list[dict]:
    """Return inaccessible candidates that need a synthesis attempt."""
    candidates = load_candidates()
    fetcher_ids = load_fetcher_ids()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SKIP_WINDOW_DAYS)

    targets = []
    for c in candidates:
        if c.get("status") != "inaccessible":
            continue
        if c.get("quality") == "LOW":
            continue
        if c["id"] in fetcher_ids:
            continue
        last = c.get("synthesis_attempted_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt > cutoff:
                    continue
            except ValueError:
                pass
        targets.append({
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "homepage_url": c.get("homepage_url", ""),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "notes": c.get("notes", ""),
        })
    return targets


def main() -> None:
    targets = list_targets()
    json.dump(targets, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_unit_synthesize_fetchers.py -k "test_list_targets" -v
```

Expected: 5 PASS

- [ ] **Step 5: Smoke-test with live data**

```bash
python3 synthesize_fetchers.py | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} targets:'); [print(f'  {c[\"id\"]}') for c in d]"
```

Expected: lists 5 inaccessible funds (KKR, Fidelity excluded as LOW, BlackRock, NB, Carlyle, Schroders)
Note: Fidelity (LOW) excluded → expect 5 targets

- [ ] **Step 6: Commit**

```bash
git add synthesize_fetchers.py tests/test_unit_synthesize_fetchers.py
git commit -m "feat: synthesize_fetchers.py — inaccessible candidate lister with skip logic"
```

---

## Task 3: `fetcher-synthesis/program.md` — agent instructions

The agent reads this file and autonomously inspects, writes, tests, and injects a fetcher for each target. Max 2 targets per session (Playwright is slow).

**Files:**
- Create: `fetcher-synthesis/program.md`

- [ ] **Step 1: Write `fetcher-synthesis/program.md`**

```markdown
# GMIA Fetcher Synthesis — Agent Program

## Goal

You are a Python scraping engineer. Your job: for each `inaccessible` fund in GMIA's
candidate list, try multiple fetching strategies, write a working `fetch_<id>()` function
that follows the existing pattern, inject it into `fetch_articles.py`, and promote the fund
to `validated` status. If all strategies fail, record the attempt and move on.

## Setup

```bash
cd /home/ubuntu/hedge-fund-research
python3 synthesize_fetchers.py
```

Read the JSON output. This is your **target list**. Process at most **2 funds per session**
(pick highest quality first — HIGH before MEDIUM). If the list is empty, output "No targets"
and exit.

## Per-fund workflow

For each target fund, work through these phases in order. Stop at the first success.

### Phase 1 — Inspect the page

```bash
cd /home/ubuntu/hedge-fund-research
python3 - << 'EOF'
from playwright.sync_api import sync_playwright
import sys

url = "REPLACE_WITH_RESEARCH_URL"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    page.goto(url, wait_until="networkidle", timeout=30000)
    print(page.content()[:8000])
    browser.close()
EOF
```

Scan the HTML output. Look for:
- Article card containers (`article`, `div[class*=card]`, `li[class*=item]`, `a[class*=insight]`)
- Title elements (`h2`, `h3`, `h4`, `a`)
- Date elements (`time[datetime]`, `span[class*=date]`, `p[class*=date]`)
- Signs of JS hydration: minimal HTML, `data-react`, `__NEXT_DATA__`, `ng-*`

If HTML is mostly empty (<500 chars of meaningful content) → the site is heavily JS-rendered.
Try waiting longer:

```bash
python3 - << 'EOF'
from playwright.sync_api import sync_playwright
url = "REPLACE_WITH_RESEARCH_URL"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    page.goto(url, timeout=30000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    print(page.content()[:8000])
    browser.close()
EOF
```

### Phase 2 — Write the fetcher function

Based on what you found, write a `fetch_<fund_id>` function following the pattern in
`fetch_articles.py`. All functions must:
- Accept `source: dict` as the only parameter
- Return `list[dict]` where each dict has: `title` (str), `url` (str), `date` (str|None), `date_raw` (str)
- Use `_get_playwright_page(source["url"], wait_selector="<selector>")` for JS-rendered pages
- Use `requests.get(url, headers=HEADERS, timeout=20)` for SSR pages
- Call `parse_date()` for date strings
- Call `_validate_hostname(url, expected_host)` on each URL
- Return `articles[:source.get("max_articles", 10)]`

**Reference pattern** (copy `fetch_blackstone` as template):
```python
def fetch_FUNDID(source: dict) -> list[dict]:
    """Fetch articles from FUNDNAME (Playwright — CSR).

    Structure: DESCRIBE_HTML_STRUCTURE
    """
    base_url = "https://www.DOMAIN.com"
    html = _get_playwright_page(source["url"], wait_selector="SELECTOR")
    soup = BeautifulSoup(html, "html.parser")
    expected_host = source.get("expected_hostname", "DOMAIN.com")

    articles = []
    for card in soup.select("CARD_SELECTOR"):
        link_el = card.select_one("TITLE_A_SELECTOR")
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _validate_hostname(url, expected_host):
            continue
        title = link_el.get_text(strip=True)
        if not title:
            continue
        time_el = card.select_one("time[datetime]")
        date_raw = ""
        parsed_date = None
        if time_el:
            dt_attr = time_el.get("datetime", "")
            date_raw = time_el.get_text(strip=True) or dt_attr
            parsed_date = parse_date(dt_attr) or parse_date(date_raw)
        else:
            date_el = card.select_one("DATE_SELECTOR")
            if date_el:
                date_raw = date_el.get_text(strip=True)
                parsed_date = parse_date(date_raw)
        articles.append({"title": title, "url": url, "date": parsed_date, "date_raw": date_raw})
    return articles[:source.get("max_articles", 10)]
```

### Phase 3 — Test the fetcher

Save the function to a temp file and test it in isolation:

```bash
python3 - << 'EOF'
import sys
sys.path.insert(0, "/home/ubuntu/hedge-fund-research")

# Paste your fetch function definition here, then:
from fetch_articles import _get_playwright_page, parse_date, _validate_hostname, HEADERS
from bs4 import BeautifulSoup
from urllib.parse import urljoin

def fetch_FUNDID(source):
    # YOUR IMPLEMENTATION HERE
    pass

source = {"url": "RESEARCH_URL", "max_articles": 10}
results = fetch_FUNDID(source)
print(f"Found {len(results)} articles")
for a in results[:3]:
    print(f"  {a['date'] or 'n/a':10s}  {a['title'][:70]}")
EOF
```

**Pass criteria:** returns ≥ 3 articles. If fewer, try a different selector or strategy.

If Playwright strategy fails (0 articles after 2 attempts), try:
- **RSS discovery**: `python3 -c "import requests; r=requests.get('URL/feed', timeout=10); print(r.status_code, r.text[:500])"`
- **JSON API sniff**: inspect page source for `fetch(` or `axios.get(` calls, try the API URL directly

If all strategies return 0 articles → skip this fund (mark attempted, move to next).

### Phase 4 — Inject into `fetch_articles.py`

Only inject if Phase 3 returned ≥ 3 articles.

**Step A**: Read `fetch_articles.py` to understand current structure.

**Step B**: Insert the function body immediately BEFORE this exact marker line:
```
# FETCHER_SYNTHESIS_INSERTION_POINT — auto-generated fetchers inserted above this line
```
Use the Edit tool. The new function goes between the last existing fetcher and the marker.

**Step C**: Add the fund to the `FETCHERS` dict. Find the line:
```python
    "pimco": fetch_pimco,
```
And add immediately after:
```python
    "FUNDID": fetch_FUNDID,
```

**Step D**: Verify the injection didn't break the file:
```bash
cd /home/ubuntu/hedge-fund-research
python3 -c "import fetch_articles; print('OK — FETCHERS:', list(fetch_articles.FETCHERS.keys()))"
```

Expected: prints OK with FUNDID in the list.

**Step E**: Run the unit tests:
```bash
python3 -m pytest tests/ -q --timeout=30 2>&1 | tail -5
```

Expected: all tests pass (or only live/nightly deselected).

### Phase 5 — Update `fund_candidates.json`

```python
import json
from datetime import datetime, timezone
from pathlib import Path

f = Path("config/fund_candidates.json")
data = json.loads(f.read_text())
candidates = data if isinstance(data, list) else data.get("candidates", [])

now = datetime.now(timezone.utc).isoformat()
for c in candidates:
    if c["id"] == "FUNDID":
        # SUCCESS path:
        c["status"] = "validated"
        c["synthesis_attempted_at"] = now
        c["synthesis_outcome"] = "success"
        c["notes"] = c.get("notes", "").replace("needs Playwright", "Playwright fetcher auto-generated")
        break

out = candidates if isinstance(data, list) else {**data, "candidates": candidates}
f.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
print("Updated", "FUNDID", "→ validated")
```

If the fund FAILED all strategies, set instead:
```python
        c["synthesis_attempted_at"] = now
        c["synthesis_outcome"] = "failed"
        # status stays "inaccessible"
```

### Phase 6 — Commit

```bash
cd /home/ubuntu/hedge-fund-research
git add fetch_articles.py config/fund_candidates.json
git commit -m "feat(fetcher): auto-synthesize fetcher for FUNDID"
git push
```

## Session output

After processing all targets (max 2), print a summary:
```
Fetcher Synthesis Session — YYYY-MM-DD
  FUNDID1: SUCCESS — 8 articles fetched, promoted to validated
  FUNDID2: FAILED  — 0 articles after Playwright + RSS attempts
```

## Rules

- **NEVER** touch `config/sources.json` or `config/entrypoints.json`
- **NEVER** modify any existing `fetch_*` function (only add new ones)
- **Max 2 funds per session**
- If pytest fails after injection, revert the injection with `git checkout fetch_articles.py` and mark the fund as failed
- Always run `python3 -c "import fetch_articles"` before committing to verify no syntax errors
```

- [ ] **Step 2: Verify the file is readable**

```bash
cat /home/ubuntu/hedge-fund-research/fetcher-synthesis/program.md | wc -l
```

Expected: ≥ 100 lines

- [ ] **Step 3: Commit**

```bash
git add fetcher-synthesis/program.md
git commit -m "feat: fetcher-synthesis agent program.md"
```

---

## Task 4: `scripts/wrapper-fetcher-synthesis.sh` — weekly cron wrapper

**Files:**
- Create: `scripts/wrapper-fetcher-synthesis.sh`

- [ ] **Step 1: Write the wrapper**

```bash
#!/bin/bash
# Weekly wrapper for GMIA fetcher synthesis
# Invokes Claude Code agent to auto-generate fetchers for inaccessible funds
# Schedule: weekly Sunday 02:00 BJT (18:00 UTC Saturday)

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

echo "$LOG_PREFIX Starting fetcher synthesis session..."

# Check if there are any targets before invoking the agent
TARGET_COUNT=$(cd "$REPO_DIR" && python3 synthesize_fetchers.py | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d))")
if [ "$TARGET_COUNT" -eq 0 ]; then
    echo "$LOG_PREFIX No inaccessible targets to process. Exiting."
    exit 0
fi
echo "$LOG_PREFIX Found $TARGET_COUNT target(s) to process."

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
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

PROGRAM_MD="$REPO_DIR/fetcher-synthesis/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 20 minutes for this session
- Process at most 2 funds
- Always run pytest after injecting a fetcher; revert if tests fail
- Commit and push after each successful injection
"

echo "$LOG_PREFIX Invoking Claude Code agent..."
echo "$PROMPT" | claude --print \
    --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
    --max-turns 60 \
    2>&1

EXIT_CODE=$?
echo "$LOG_PREFIX Agent exited with code $EXIT_CODE"
exit $EXIT_CODE
```

- [ ] **Step 2: Make executable**

```bash
chmod +x /home/ubuntu/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```

- [ ] **Step 3: Dry-run (target list only, no agent)**

```bash
cd /home/ubuntu/hedge-fund-research
python3 synthesize_fetchers.py
```

Expected: JSON array with 5 inaccessible targets (Fidelity excluded)

- [ ] **Step 4: Commit**

```bash
git add scripts/wrapper-fetcher-synthesis.sh
git commit -m "feat: wrapper-fetcher-synthesis.sh weekly cron wrapper"
```

---

## Task 5: Wire up cron + run-once smoke test

**Files:**
- No file changes — cron registration only

- [ ] **Step 1: Register in crontab**

Run `crontab -e` and add (Sunday 02:00 BJT = Saturday 18:00 UTC):

```cron
0 18 * * 6 ~/cron-wrapper.sh gmia-fetcher-synthesis 20m bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```

- [ ] **Step 2: Verify cron entry**

```bash
crontab -l | grep fetcher-synthesis
```

Expected: the line above is present

- [ ] **Step 3: Run once manually to test the full flow (targets only, no commit)**

```bash
cd /home/ubuntu/hedge-fund-research
python3 synthesize_fetchers.py
```

Verify the 5 expected targets appear (KKR, BlackRock, Neuberger Berman, Carlyle, Schroders). No Fidelity (LOW quality).

- [ ] **Step 4: Final pytest**

```bash
python3 -m pytest tests/ -q 2>&1 | tail -5
```

Expected: 243+ passed, 15 deselected (or better)

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Agent tries multiple strategies (Playwright → RSS → JSON API) — Phase 1–3 of program.md
- ✅ Successful fetch → injects into fetch_articles.py (Phase 4)
- ✅ Promotes to validated (Phase 5)
- ✅ Failed → keeps inaccessible, records `synthesis_attempted_at` (Phase 5 failure path)
- ✅ Skip logic: LOW quality, already has fetcher, recently attempted (synthesize_fetchers.py)
- ✅ Max 2 per session (program.md + wrapper constraint)
- ✅ Tests don't regress (Phase 4 Step E + Task 4 Step 4)
- ✅ Weekly cron (Task 5)

**Placeholder scan:** All steps have concrete code or commands. No "TBD" or "add error handling" placeholders.

**Type consistency:** `list_targets()` returns `list[dict]` with keys `id, name, homepage_url, research_url, notes` — consistent with usage in program.md.
