"""Unit tests for fund discovery seed pool and candidate state model."""

import json
import pytest
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"
SEED_FILE = CONFIG_DIR / "fund_seeds.json"
CANDIDATES_FILE = CONFIG_DIR / "fund_candidates.json"
SOURCES_FILE = CONFIG_DIR / "sources.json"

VALID_STATUSES = {
    "seed", "discovered", "screened", "validated",
    "watchlist", "rejected", "promoted",
}


# ---------------------------------------------------------------------------
# Seed file tests
# ---------------------------------------------------------------------------

class TestSeedFile:
    def test_seed_file_is_valid_json(self):
        """fund_seeds.json must be valid JSON."""
        text = SEED_FILE.read_text()
        seeds = json.loads(text)
        assert isinstance(seeds, list)

    def test_seeds_have_required_fields(self):
        """Every seed must have id, name, category, homepage."""
        seeds = json.loads(SEED_FILE.read_text())
        required = {"id", "name", "category", "homepage"}
        for seed in seeds:
            missing = required - set(seed.keys())
            assert not missing, f"Seed {seed.get('id', '?')} missing: {missing}"

    def test_seed_ids_are_unique(self):
        """Seed IDs must be unique."""
        seeds = json.loads(SEED_FILE.read_text())
        ids = [s["id"] for s in seeds]
        assert len(ids) == len(set(ids)), f"Duplicate IDs: {ids}"

    def test_no_overlap_with_production_sources(self):
        """Seed IDs must not overlap with production source IDs."""
        seeds = json.loads(SEED_FILE.read_text())
        sources = json.loads(SOURCES_FILE.read_text())
        seed_ids = {s["id"] for s in seeds}
        source_ids = {s["id"] for s in sources["sources"]}
        overlap = seed_ids & source_ids
        assert not overlap, f"Overlap with production: {overlap}"


# ---------------------------------------------------------------------------
# Candidate file tests
# ---------------------------------------------------------------------------

class TestCandidatesFile:
    def test_candidates_file_is_valid_json(self):
        """fund_candidates.json must be valid JSON."""
        text = CANDIDATES_FILE.read_text()
        candidates = json.loads(text)
        assert isinstance(candidates, list)

    def test_candidates_have_required_fields(self):
        """Every candidate must have id, name, status."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        required = {"id", "name", "status"}
        for c in candidates:
            missing = required - set(c.keys())
            assert not missing, f"Candidate {c.get('id', '?')} missing: {missing}"

    def test_candidate_statuses_are_valid(self):
        """Every candidate status must be in the valid set."""
        candidates = json.loads(CANDIDATES_FILE.read_text())
        for c in candidates:
            assert c["status"] in VALID_STATUSES, (
                f"Candidate {c['id']} has invalid status: {c['status']}"
            )

    def test_all_seeds_have_candidate_entry(self):
        """Every seed must have a corresponding candidate entry."""
        seeds = json.loads(SEED_FILE.read_text())
        candidates = json.loads(CANDIDATES_FILE.read_text())
        seed_ids = {s["id"] for s in seeds}
        candidate_ids = {c["id"] for c in candidates}
        missing = seed_ids - candidate_ids
        assert not missing, f"Seeds without candidate entry: {missing}"
