"""Tests for the --include-validated candidate probe pool.

Regression for 2026-07-02: cohen-steers entered a 7-day trial but its
fund_candidates.json status stays "visitable" until the trial verdict, so the
nightly liveness probe kept GET-ing its Cloudflare-403 site and failing the
whole health run (exit 1) every night of the trial. The probe pool must
exclude ids listed in trial-state.json active_trials.
"""

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gmia-fetcher-health.py"

spec = importlib.util.spec_from_file_location("gfh_pool", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_pool"] = gfh
spec.loader.exec_module(gfh)


CANDIDATES = [
    {"id": "waiting-fund", "status": "visitable", "research_url": "https://a.example/insights"},
    {"id": "cohen-steers", "status": "visitable", "research_url": "https://b.example/insights"},
    {"id": "done-fund", "status": "promoted", "research_url": "https://c.example/insights"},
]


def _write(tmp_path, candidates, trial_state):
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps(candidates))
    trial_file = tmp_path / "trial-state.json"
    if trial_state is not None:
        trial_file.write_text(json.dumps(trial_state))
    return cand_file, trial_file


def test_visitable_not_in_trial_is_probed(tmp_path, monkeypatch):
    cand_file, trial_file = _write(tmp_path, CANDIDATES, {"active_trials": []})
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    ids = {c["id"] for c in gfh.load_validated_candidates()}
    assert ids == {"waiting-fund", "cohen-steers"}


def test_active_trial_candidate_excluded(tmp_path, monkeypatch):
    cand_file, trial_file = _write(
        tmp_path, CANDIDATES,
        {"active_trials": [{"id": "cohen-steers", "start_date": "2026-07-02"}]})
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    ids = {c["id"] for c in gfh.load_validated_candidates()}
    assert ids == {"waiting-fund"}


def test_non_visitable_always_excluded(tmp_path, monkeypatch):
    cand_file, trial_file = _write(tmp_path, CANDIDATES, {"active_trials": []})
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    ids = {c["id"] for c in gfh.load_validated_candidates()}
    assert "done-fund" not in ids


def test_missing_trial_state_file_probes_all_visitable(tmp_path, monkeypatch):
    cand_file, trial_file = _write(tmp_path, CANDIDATES, None)
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    ids = {c["id"] for c in gfh.load_validated_candidates()}
    assert ids == {"waiting-fund", "cohen-steers"}


def test_missing_candidates_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", tmp_path / "trial-state.json")
    assert gfh.load_validated_candidates() == []
