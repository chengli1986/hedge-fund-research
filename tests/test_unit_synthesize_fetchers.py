"""Test scaffolding for Fetcher Synthesis pipeline."""

from pathlib import Path

FETCH_ARTICLES = Path(__file__).resolve().parent.parent / "fetch_articles.py"
MARKER = "# FETCHER_SYNTHESIS_INSERTION_POINT"


def test_injection_marker_exists_exactly_once():
    """The marker comment must exist exactly once in fetch_articles.py."""
    text = FETCH_ARTICLES.read_text(encoding="utf-8")
    assert text.count(MARKER) == 1


def test_injection_marker_is_before_dispatcher():
    """The marker must appear before the Dispatcher comment block."""
    text = FETCH_ARTICLES.read_text(encoding="utf-8")
    marker_pos = text.find(MARKER)
    dispatcher_pos = text.find("# Dispatcher")
    assert marker_pos != -1, "Marker not found"
    assert dispatcher_pos != -1, "Dispatcher block not found"
    assert marker_pos < dispatcher_pos, "Marker must appear before Dispatcher"


# Tests for synthesize_fetchers.py
import json
import sys
from pathlib import Path
from unittest.mock import patch
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
    recent = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=recent)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert result == []


def test_list_targets_includes_stale_attempt():
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    candidates = [_make_candidate("alpha", "inaccessible", "HIGH", attempted_at=stale)]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert [t["id"] for t in result] == ["alpha"]


# ---------------------------------------------------------------------------
# Tests for auto-reject mechanism: count "failed" outcomes in history.jsonl
# and flip status inaccessible→rejected after MAX_SYNTHESIS_FAILURES attempts.
# ---------------------------------------------------------------------------


def _write_history(tmp_path, entries):
    p = tmp_path / "fetcher-synthesis-history.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return p


def test_count_failures_counts_only_matching_id_and_outcome(tmp_path):
    p = _write_history(tmp_path, [
        {"id": "alpha", "outcome": "failed"},
        {"id": "alpha", "outcome": "failed"},
        {"id": "alpha", "outcome": "success"},
        {"id": "_heartbeat", "outcome": "session_end"},
        {"id": "beta", "outcome": "failed"},
    ])
    assert synthesize_fetchers._count_failures("alpha", p) == 2
    assert synthesize_fetchers._count_failures("beta", p) == 1
    assert synthesize_fetchers._count_failures("gamma", p) == 0


def test_count_failures_returns_zero_for_missing_history(tmp_path):
    p = tmp_path / "nonexistent.jsonl"
    assert synthesize_fetchers._count_failures("alpha", p) == 0


def test_count_failures_skips_corrupt_lines(tmp_path):
    p = tmp_path / "fetcher-synthesis-history.jsonl"
    p.write_text(
        '{"id": "alpha", "outcome": "failed"}\n'
        'this is not json\n'
        '{"id": "alpha", "outcome": "failed"}\n'
    )
    assert synthesize_fetchers._count_failures("alpha", p) == 2


def test_auto_reject_flips_status_after_threshold(tmp_path):
    cf = tmp_path / "fund_candidates.json"
    cf.write_text(json.dumps([
        _make_candidate("alpha", "inaccessible", "HIGH"),
        _make_candidate("beta", "inaccessible", "HIGH"),
    ]) + "\n")
    hf = _write_history(tmp_path, [
        {"id": "alpha", "outcome": "failed"},
        {"id": "alpha", "outcome": "failed"},
        {"id": "alpha", "outcome": "failed"},
        {"id": "beta", "outcome": "failed"},
        {"id": "beta", "outcome": "failed"},
    ])
    flipped = synthesize_fetchers.auto_reject_exhausted_candidates(
        candidates_path=cf, history_path=hf, max_failures=3,
    )
    assert flipped == ["alpha"]
    after = json.loads(cf.read_text())
    by_id = {c["id"]: c for c in after}
    assert by_id["alpha"]["status"] == "rejected"
    assert by_id["beta"]["status"] == "inaccessible"


def test_auto_reject_preserves_original_notes(tmp_path):
    cf = tmp_path / "fund_candidates.json"
    cand = _make_candidate("alpha", "inaccessible", "HIGH")
    cand["notes"] = "Original diagnostic: 403 from EC2"
    cf.write_text(json.dumps([cand]) + "\n")
    hf = _write_history(tmp_path, [
        {"id": "alpha", "outcome": "failed"} for _ in range(3)
    ])
    synthesize_fetchers.auto_reject_exhausted_candidates(
        candidates_path=cf, history_path=hf, max_failures=3,
    )
    after = json.loads(cf.read_text())
    notes = after[0]["notes"]
    assert "Original diagnostic: 403 from EC2" in notes
    assert "Auto-rejected" in notes
    assert "3" in notes  # failure count surfaced


def test_auto_reject_does_not_write_when_no_flips(tmp_path):
    cf = tmp_path / "fund_candidates.json"
    original = json.dumps([_make_candidate("alpha", "inaccessible", "HIGH")]) + "\n"
    cf.write_text(original)
    mtime_before = cf.stat().st_mtime_ns
    hf = _write_history(tmp_path, [
        {"id": "alpha", "outcome": "failed"},
        {"id": "alpha", "outcome": "failed"},
    ])
    flipped = synthesize_fetchers.auto_reject_exhausted_candidates(
        candidates_path=cf, history_path=hf, max_failures=3,
    )
    assert flipped == []
    assert cf.read_text() == original
    assert cf.stat().st_mtime_ns == mtime_before


def test_auto_reject_only_flips_inaccessible(tmp_path):
    """Validated/promoted/rejected candidates should never be flipped, even
    if they have failure history (e.g. left over from a prior status)."""
    cf = tmp_path / "fund_candidates.json"
    cf.write_text(json.dumps([
        _make_candidate("alpha", "validated", "HIGH"),
        _make_candidate("beta", "promoted", "HIGH"),
        _make_candidate("gamma", "rejected", "HIGH"),
    ]) + "\n")
    hf = _write_history(tmp_path, [
        {"id": "alpha", "outcome": "failed"} for _ in range(5)
    ] + [
        {"id": "beta", "outcome": "failed"} for _ in range(5)
    ] + [
        {"id": "gamma", "outcome": "failed"} for _ in range(5)
    ])
    flipped = synthesize_fetchers.auto_reject_exhausted_candidates(
        candidates_path=cf, history_path=hf, max_failures=3,
    )
    assert flipped == []
