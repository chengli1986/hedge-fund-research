# Merge fund_seeds.json into fund_candidates.json Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate `fund_seeds.json` by merging seeds into `fund_candidates.json` with a `source` field, so the pipeline has one unified fund pool instead of two overlapping files.

**Architecture:** Each candidate gets `"source": "manual"` (was a seed) or `"source": "auto_discovered"` (found by discovery agent). Seeds that haven't been discovered yet get `"status": "seed"`. `discover_fund_sites.py` now reads `status=seed` candidates instead of `fund_seeds.json`. The file `fund_seeds.json` is deleted after migration.

**Tech Stack:** Python 3.12, JSON, pytest

---

## File Map

| File | Action | Change |
|------|--------|--------|
| `config/fund_seeds.json` | Delete | Replaced by `source` field in candidates |
| `config/fund_candidates.json` | Modify | Add `source` to all entries; any seed not yet in candidates gets added with `status: "seed"` |
| `discover_fund_sites.py` | Modify | Read `status=seed` from candidates instead of `fund_seeds.json`; rename `load_seeds()` → `load_seed_candidates()` |
| `scripts/wrapper-candidate-discovery.sh` | Modify | Count `source=manual` for "Seeds" stat instead of `len(fund_seeds.json)` |
| `tests/test_unit_fund_discovery.py` | Modify | Remove `TestSeedFile` class + `test_all_seeds_have_candidate_entry`; add tests for `source` field |
| `candidate-discovery/program.md` | Modify | Update instruction from "add to fund_seeds.json" → "add to fund_candidates.json with source=manual, status=seed" |

---

## Task 1: Migrate config data

**Files:**
- Modify: `config/fund_candidates.json`
- Delete: `config/fund_seeds.json`

### What to do

All 18 seeds are already in `fund_candidates.json` (every seed was previously discovered). So no new entries need to be added — we just need to add `"source"` to every candidate.

- [ ] **Step 1: Add `source` field to all candidates**

Run this script:
```python
#!/usr/bin/env python3
import json
from pathlib import Path

seeds_path = Path("config/fund_seeds.json")
candidates_path = Path("config/fund_candidates.json")

seed_ids = {s["id"] for s in json.loads(seeds_path.read_text())}
candidates = json.loads(candidates_path.read_text())

for c in candidates:
    if "source" not in c:
        c["source"] = "manual" if c["id"] in seed_ids else "auto_discovered"

candidates_path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
print(f"Updated {len(candidates)} candidates")
print(f"  manual: {sum(1 for c in candidates if c['source'] == 'manual')}")
print(f"  auto_discovered: {sum(1 for c in candidates if c['source'] == 'auto_discovered')}")
```

Run: `python3 /tmp/migrate.py`
Expected output:
```
Updated 19 candidates
  manual: 18
  auto_discovered: 1
```

- [ ] **Step 2: Verify fund_candidates.json is valid JSON with source fields**

```bash
python3 -c "
import json
c = json.load(open('config/fund_candidates.json'))
assert all('source' in x for x in c), 'Missing source field'
assert sum(1 for x in c if x['source']=='manual') == 18
assert sum(1 for x in c if x['source']=='auto_discovered') == 1
print('OK:', len(c), 'candidates, all have source field')
"
```

- [ ] **Step 3: Delete fund_seeds.json**

```bash
git rm config/fund_seeds.json
```

- [ ] **Step 4: Commit**

```bash
git add config/fund_candidates.json
git commit -m "feat(seeds-merge): add source field to candidates, delete fund_seeds.json"
```

---

## Task 2: Update discover_fund_sites.py

**Files:**
- Modify: `discover_fund_sites.py`

The script currently loads seeds from `fund_seeds.json` via `load_seeds()`. It needs to instead load candidates with `status="seed"` or `source="manual"` that haven't been discovered yet.

But wait — in the new model, ALL seeds have already been discovered (they're all in candidates with statuses like `validated`, `inaccessible`, etc.). A new seed added by a human will have `status="seed"` and `source="manual"`. `discover_fund_sites.py` should process candidates with `status="seed"`.

- [ ] **Step 1: Remove SEED_FILE constant and load_seeds() function**

In `discover_fund_sites.py`, find and remove:
```python
SEED_FILE = BASE_DIR / "config" / "fund_seeds.json"
```

Remove the entire `load_seeds()` function (lines 161-173).

- [ ] **Step 2: Add load_seed_candidates() function**

After `load_candidates()` function, add:

```python
def load_seed_candidates(fund_id: Optional[str] = None) -> list[dict]:
    """Load candidates with status='seed' (not yet discovered).

    Args:
        fund_id: If provided, return only the candidate with this ID.

    Returns:
        List of seed candidate dicts.
    """
    candidates = load_candidates()
    seeds = [c for c in candidates if c.get("status") == "seed"]
    if fund_id:
        seeds = [s for s in seeds if s["id"] == fund_id]
    return seeds
```

- [ ] **Step 3: Update discover_one() to accept candidate dict**

`discover_one(seed)` currently reads `seed["homepage"]`. In candidates, the field is `homepage_url`. Update:

Find in `discover_one()`:
```python
    homepage = seed["homepage"]
```
Change to:
```python
    homepage = seed.get("homepage_url") or seed.get("homepage", "")
```

- [ ] **Step 4: Update main() to use load_seed_candidates()**

In `main()`, find:
```python
    seeds = load_seeds(fund_id=args.fund)
    if not seeds:
        log.error("No seeds found%s", f" for fund '{args.fund}'" if args.fund else "")
        return
```

Change to:
```python
    seeds = load_seed_candidates(fund_id=args.fund)
    if not seeds:
        log.info("No seed candidates found%s — all seeds already discovered",
                 f" for fund '{args.fund}'" if args.fund else "")
        return
```

- [ ] **Step 5: Update update_candidate() call to set source field**

In `main()`, find the `update_candidate()` call and add `source="manual"` — actually, `update_candidate()` doesn't have a `source` parameter. Instead, after calling `update_candidate()`, set source directly:

Find in `main()`:
```python
        if not args.dry_run:
            update_candidate(
                candidates,
                seed["id"],
                homepage_url=result["homepage_url"],
                research_url=research_url,
                rss_url=rss_url,
                official_domain=result["official_domain"],
                research_links=research_links,
                status=new_status,
            )
            updated_count += 1
```

Change to:
```python
        if not args.dry_run:
            update_candidate(
                candidates,
                seed["id"],
                homepage_url=result["homepage_url"],
                research_url=research_url,
                rss_url=rss_url,
                official_domain=result["official_domain"],
                research_links=research_links,
                status=new_status,
            )
            # Preserve source field from seed candidate
            for c in candidates:
                if c["id"] == seed["id"] and "source" not in c:
                    c["source"] = seed.get("source", "manual")
            updated_count += 1
```

- [ ] **Step 6: Verify syntax**

```bash
python3 -c "import discover_fund_sites; print('OK')"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add discover_fund_sites.py
git commit -m "feat(seeds-merge): update discover_fund_sites to read status=seed candidates"
```

---

## Task 3: Update wrapper-candidate-discovery.sh

**Files:**
- Modify: `scripts/wrapper-candidate-discovery.sh`

The email stats bar currently shows `Seeds N` by reading `fund_seeds.json`. After merge, count candidates where `source="manual"`.

- [ ] **Step 1: Find the seeds count line**

```bash
grep -n "fund_seeds\|len(seeds\|Seeds" scripts/wrapper-candidate-discovery.sh | head -10
```

- [ ] **Step 2: Replace the seeds loading and count**

Find the line that loads seeds (something like):
```python
seeds = json.loads((repo / "config/fund_seeds.json").read_text())
```

Remove that line entirely.

Find the line that counts seeds in the stats bar (something like):
```python
f'<span><strong>Seeds</strong>&nbsp;{len(seeds)}</span>'
```

Change to:
```python
seeds_count = sum(1 for c in candidates if c.get("source") == "manual")
```

And update the stats bar reference from `{len(seeds)}` to `{seeds_count}`.

- [ ] **Step 3: Verify Python syntax in the heredoc**

```bash
bash -n scripts/wrapper-candidate-discovery.sh && echo "Bash OK"
```

- [ ] **Step 4: Commit**

```bash
git add scripts/wrapper-candidate-discovery.sh
git commit -m "feat(seeds-merge): count source=manual candidates for Seeds stat in email"
```

---

## Task 4: Update tests

**Files:**
- Modify: `tests/test_unit_fund_discovery.py`

Remove `TestSeedFile` class (reads deleted `fund_seeds.json`) and `test_all_seeds_have_candidate_entry`. Add tests for `source` field.

- [ ] **Step 1: Remove SEED_FILE reference and TestSeedFile class**

In `tests/test_unit_fund_discovery.py`:

Remove line:
```python
SEED_FILE = CONFIG_DIR / "fund_seeds.json"
```

Remove the entire `TestSeedFile` class (4 tests: `test_seed_file_is_valid_json`, `test_seeds_have_required_fields`, `test_seed_ids_are_unique`, `test_no_overlap_with_production_sources`).

Remove `test_all_seeds_have_candidate_entry` from `TestCandidatesFile`.

- [ ] **Step 2: Add source field tests**

Add to `TestCandidatesFile`:

```python
    def test_candidates_have_source_field(self):
        """Every candidate must have a source field."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        valid_sources = {"manual", "auto_discovered"}
        for c in candidates:
            assert "source" in c, f"Candidate {c['id']} missing source field"
            assert c["source"] in valid_sources, (
                f"Candidate {c['id']} has invalid source: {c['source']}"
            )

    def test_manual_source_count(self):
        """There must be at least 10 manual (seed) candidates."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        manual_count = sum(1 for c in candidates if c["source"] == "manual")
        assert manual_count >= 10, f"Expected ≥10 manual candidates, got {manual_count}"
```

- [ ] **Step 3: Update load_seeds reference in discover_fund_sites tests**

In the same test file, find any test that calls `dfs.load_seeds()` and update to `dfs.load_seed_candidates()`.

- [ ] **Step 4: Run tests**

```bash
python3 -m pytest tests/test_unit_fund_discovery.py -v 2>&1 | tail -20
```

Expected: all tests pass (no `fund_seeds.json` references remaining).

- [ ] **Step 5: Run full test suite**

```bash
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Expected: same pass count as before (minus the 4 deleted TestSeedFile tests), no new failures.

- [ ] **Step 6: Commit**

```bash
git add tests/test_unit_fund_discovery.py
git commit -m "test(seeds-merge): remove TestSeedFile, add source field validation"
```

---

## Task 5: Update candidate-discovery/program.md

**Files:**
- Modify: `candidate-discovery/program.md`

- [ ] **Step 1: Find the instruction for adding new seeds**

```bash
grep -n "fund_seeds\|seed" candidate-discovery/program.md | head -20
```

- [ ] **Step 2: Update the instruction**

Find any instruction like:
> "Add to `config/fund_seeds.json`"

Change to:
> "Add to `config/fund_candidates.json` with `"source": "manual"` and `"status": "seed"`. Required fields: `id`, `name`, `homepage_url`, `source`, `status`, `strategy_tags`."

- [ ] **Step 3: Commit**

```bash
git add candidate-discovery/program.md
git commit -m "docs(seeds-merge): update program.md — add new funds to candidates with source=manual"
```

---

## Task 6: Final verification

- [ ] **Step 1: Confirm fund_seeds.json is gone**

```bash
ls config/fund_seeds.json 2>&1
```
Expected: `No such file or directory`

- [ ] **Step 2: Verify candidate counts**

```bash
python3 -c "
import json
from collections import Counter
c = json.load(open('config/fund_candidates.json'))
print('Total:', len(c))
print('By source:', dict(Counter(x['source'] for x in c)))
print('By status:', dict(Counter(x['status'] for x in c)))
"
```

Expected:
```
Total: 19
By source: {'manual': 18, 'auto_discovered': 1}
By status: {'inaccessible': 6, 'rejected': 2, 'validated': 8, 'watchlist': 3}
```

- [ ] **Step 3: Full test suite**

```bash
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -5
```

Expected: all tests pass (count will be slightly lower due to removed TestSeedFile).

- [ ] **Step 4: Git push**

```bash
git push origin main
```

---

## Self-Review

**Spec coverage:**
- ✅ `fund_seeds.json` deleted
- ✅ `source` field added to all candidates
- ✅ `discover_fund_sites.py` reads `status=seed` candidates
- ✅ `wrapper-candidate-discovery.sh` Seeds stat updated
- ✅ Tests updated
- ✅ `program.md` updated

**Placeholder check:** None found.

**Type consistency:** `load_seed_candidates()` returns `list[dict]` — same as old `load_seeds()`. `discover_one(seed)` still takes a dict; updated to read `homepage_url` field.

**Edge case:** If `load_seed_candidates()` returns empty (all seeds already discovered), `main()` logs a message and exits gracefully — correct behavior since there's nothing to discover.
