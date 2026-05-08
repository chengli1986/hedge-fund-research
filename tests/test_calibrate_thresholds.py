"""Tests for threshold-calibration helpers.

The calibration script is meant to be advisory (writes a JSON report; doesn't
self-modify constants), but the percentile + recommendation logic is the load-
bearing part — if it returns junk recommendations, humans get bad advice.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "calibrate_thresholds.py"

spec = importlib.util.spec_from_file_location("ct", SCRIPT)
ct = importlib.util.module_from_spec(spec)
sys.modules["ct"] = ct
spec.loader.exec_module(ct)


# ── _percentile ────────────────────────────────────────────────────────────────

def test_percentile_empty_returns_none():
    assert ct._percentile([], 0.5) is None


def test_percentile_single_value():
    assert ct._percentile([42], 0.5) == 42


def test_percentile_p50_is_median():
    assert ct._percentile([1, 2, 3, 4, 5], 0.50) == 3
    assert ct._percentile([1, 2, 3, 4], 0.50) == 2.5


def test_percentile_p95():
    values = list(range(100))  # 0..99
    p95 = ct._percentile(values, 0.95)
    # 99-element span × 0.95 = 94.05 → between index 94 and 95
    assert p95 is not None
    assert 94 <= p95 <= 95


def test_percentile_p5():
    values = list(range(100))
    p5 = ct._percentile(values, 0.05)
    assert p5 is not None
    assert 4 <= p5 <= 5


# ── _summarize ────────────────────────────────────────────────────────────────

def test_summarize_empty():
    s = ct._summarize("test", [])
    assert s["n"] == 0
    assert s["summary"] is None


def test_summarize_returns_full_distribution():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    s = ct._summarize("body sizes", values)
    assert s["n"] == 10
    assert s["min"] == 10
    assert s["max"] == 100
    assert s["mean"] == 55.0
    assert s["p50"] == 55
    assert s["p5"] is not None and s["p5"] < s["p50"]
    assert s["p95"] is not None and s["p95"] > s["p50"]


# ── _recommend_shell_threshold ────────────────────────────────────────────────

def test_recommend_shell_threshold_with_no_data():
    r = ct._recommend_shell_threshold([])
    assert r["current"] == 5000
    assert r["recommended"] is None
    assert "no body-size samples" in r["rationale"]


def test_recommend_shell_threshold_keeps_2500_floor():
    """Tiny p5 must not drop threshold below 2500 — would false-positive everything."""
    sizes = [3000, 3000, 3000, 3000]  # p5/2 = 1500 → clamps to 2500
    r = ct._recommend_shell_threshold(sizes)
    assert r["recommended"] == 2500


def test_recommend_shell_threshold_uses_p5_when_above_floor():
    """Healthy distribution: p5/2 well above 2500."""
    sizes = [50_000, 60_000, 70_000, 80_000, 90_000, 100_000, 110_000]
    r = ct._recommend_shell_threshold(sizes)
    # p5 ≈ 53000, /2 ≈ 26500
    assert r["recommended"] is not None
    assert r["recommended"] > 2500


def test_recommend_shell_threshold_handles_real_world_franklin_case():
    """If franklin (2367) had been included in a healthy sample, p5/2 should
    still keep us safe — but franklin is the outlier, so test what happens
    if we exclude it."""
    sizes = [10000, 20000, 30000, 40000, 50000, 60000, 70000, 80000, 90000, 100000]
    r = ct._recommend_shell_threshold(sizes)
    # p5 ≈ 14500 → recommend 7250 → > 2500, < 10000
    assert r["recommended"] is not None
    assert 5000 <= r["recommended"] < 15000


# ── _recommend_stale_threshold ────────────────────────────────────────────────

def test_recommend_stale_threshold_no_data():
    r = ct._recommend_stale_threshold([])
    assert r["recommended"] is None


def test_recommend_stale_threshold_uses_p95_with_safety_margin():
    """p95 × 1.5 → enough margin for cadence variance without false alarms."""
    gaps = [7, 14, 14, 21, 28, 30, 30, 35, 60]  # p95 ≈ 60 (last value)
    r = ct._recommend_stale_threshold(gaps)
    assert r["recommended"] is not None
    # 60 × 1.5 = 90
    assert r["recommended"] >= 60
    assert r["n_observed"] == 9


def test_recommend_stale_threshold_consistent_with_quarterly_reality():
    """Quarterly publishers (Verdad, AQR ~quarterly) routinely show 60-110d gaps.
    Threshold should NOT be tighter than what they actually publish."""
    quarterly_gaps = [60, 75, 90, 110, 120, 95, 88]
    r = ct._recommend_stale_threshold(quarterly_gaps)
    assert r["recommended"] is not None
    # Must be at least the largest natural gap
    assert r["recommended"] >= 120
