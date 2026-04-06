"""Unit tests for evaluate_entrypoints.py — ranking precision metric."""

import pytest
from evaluate_entrypoints import compute_ranking_precision, rescore_entry


def _make_entry(domain=0.9, path=0.8, structure=0.7, gate=0.1, final=None):
    """Create an entrypoint dict with component scores."""
    entry = {
        "url": "https://example.com/insights",
        "domain_score": domain,
        "path_score": path,
        "structure_score": structure,
        "gate_penalty": gate,
        "final_score": final or 0.0,
    }
    return entry


class TestRescoreEntry:
    def test_rescore_with_weights(self) -> None:
        """Re-scoring with known weights produces correct final score."""
        entry = _make_entry(domain=1.0, path=1.0, structure=0.8, gate=0.1)
        weights = {"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}
        # expected: 1.0*0.2 + 1.0*0.3 + 0.8*0.3 + (1-0.1)*0.2 = 0.2+0.3+0.24+0.18 = 0.92
        result = rescore_entry(entry, weights)
        assert result == pytest.approx(0.92, abs=0.01)

    def test_rescore_missing_components_fallback(self) -> None:
        """Missing component scores → falls back to stored final_score."""
        entry = {"url": "https://example.com", "final_score": 0.75}
        weights = {"domain": 0.25, "path": 0.25, "structure": 0.25, "gate": 0.25}
        result = rescore_entry(entry, weights)
        assert result == 0.75


class TestComputeRankingPrecision:
    def test_perfect_separation(self) -> None:
        """All good URLs above threshold, all bad below → precision 1.0."""
        data = {"sources": {"fund_a": {
            "entrypoints": [_make_entry(domain=1.0, path=1.0, structure=0.8, gate=0.1)],
            "rejected_pages": [{"url": "https://example.com/careers",
                                "domain_score": 1.0, "path_score": 0.0,
                                "structure_score": 0.1, "gate_penalty": 0.5,
                                "final_score": 0.3, "label": "bad"}],
        }}}
        weights = {"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}
        result = compute_ranking_precision(data, weights)
        assert result["overall_precision"] == 1.0
        assert result["overall_reject_rate"] == 1.0
        assert result["overall"] == pytest.approx(1.0)

    def test_no_bad_urls(self) -> None:
        """No rejected pages → reject_rate 0.0 (no bad data to measure)."""
        data = {"sources": {"fund_a": {
            "entrypoints": [_make_entry(domain=1.0, path=1.0, structure=0.8, gate=0.1)],
            "rejected_pages": [],
        }}}
        weights = {"domain": 0.25, "path": 0.25, "structure": 0.25, "gate": 0.25}
        result = compute_ranking_precision(data, weights)
        assert result["overall_precision"] == 1.0
        assert result["overall_reject_rate"] == 0.0

    def test_good_url_below_threshold(self) -> None:
        """Good URL with low scores falls below threshold → precision < 1.0."""
        data = {"sources": {"fund_a": {
            "entrypoints": [
                _make_entry(domain=1.0, path=1.0, structure=0.8, gate=0.1),  # high
                _make_entry(domain=0.0, path=0.0, structure=0.1, gate=0.5),  # low
            ],
            "rejected_pages": [],
        }}}
        weights = {"domain": 0.25, "path": 0.25, "structure": 0.25, "gate": 0.25}
        result = compute_ranking_precision(data, weights)
        assert result["overall_precision"] == 0.5  # 1 of 2 good URLs above threshold

    def test_empty_data(self) -> None:
        """Empty sources → 0 totals."""
        data = {"sources": {}}
        weights = {"domain": 0.25, "path": 0.25, "structure": 0.25, "gate": 0.25}
        result = compute_ranking_precision(data, weights)
        assert result["total_good"] == 0
        assert result["total_bad"] == 0
