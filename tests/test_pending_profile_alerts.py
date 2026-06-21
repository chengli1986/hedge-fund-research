"""Tests for pending-profile age scanning + daily-summary alerting.

Without the urgent threshold, profile drafts written by auto-promote could sit
in pending_profiles/ forever — the AUM/founded/HQ stay missing on the public
dashboard with no one nudged to look.
"""

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "gmia-trial-manager.py"

spec = importlib.util.spec_from_file_location("gtm", SCRIPT)
gtm = importlib.util.module_from_spec(spec)
sys.modules["gtm"] = gtm
spec.loader.exec_module(gtm)


def _make_pending(dir_path: Path, fund_id: str, age_days: int,
                  validation: dict | None = None) -> Path:
    profile = dir_path / f"{fund_id}.json"
    profile.write_text(json.dumps({"id": fund_id, "founded": "1990",
                                   "aum": "$10B", "hq": "NYC, USA"}))
    # backdate mtime
    target = time.time() - age_days * 86400
    os.utime(profile, (target, target))
    if validation is not None:
        val_path = dir_path / f"{fund_id}.validation.json"
        val_path.write_text(json.dumps(validation))
    return profile


def test_scan_returns_empty_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", tmp_path / "does-not-exist")
    assert gtm._scan_pending_profiles("2026-05-08") == []


def test_scan_returns_empty_when_dir_empty(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    assert gtm._scan_pending_profiles("2026-05-08") == []


def test_scan_skips_validation_files(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "blackrock", 5)
    # Companion validation file should NOT show up as a separate profile
    (pending_dir / "blackrock.validation.json").write_text('{"ok": true}')
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    out = gtm._scan_pending_profiles(today)
    ids = [p["id"] for p in out]
    assert ids == ["blackrock"]


def test_scan_returns_age_in_days(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "fund-a", 0)
    _make_pending(pending_dir, "fund-b", 5)
    _make_pending(pending_dir, "fund-c", 12)
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    out = {p["id"]: p["age_days"] for p in gtm._scan_pending_profiles(today)}
    assert out["fund-a"] == 0
    assert out["fund-b"] == 5
    assert out["fund-c"] == 12


def test_scan_picks_up_validation_high_risk(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "shaky", 3,
                  validation={"ok": False, "high_risk": True, "issues": ["unknown AUM"]})
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    out = gtm._scan_pending_profiles(today)
    assert len(out) == 1
    assert out[0]["validation_ok"] is False
    assert out[0]["high_risk"] is True


def test_scan_handles_invalid_today_string(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "fund-a", 5)
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    # non-parsable date should not crash, just return []
    assert gtm._scan_pending_profiles("not-a-date") == []


def test_send_summary_does_not_skip_when_only_urgent_pending(monkeypatch, tmp_path):
    """Even on a quiet day (no trial events), urgent stale pending must trigger email."""
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "stale-fund", 10)  # >= URGENT_DAYS=7
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    # Hermetic: ignore the real auto-promote log (see fresh-pending test).
    monkeypatch.setattr(gtm, "AUTO_PROMOTE_LOG", tmp_path / "no-auto-promote.jsonl")

    # Capture the email send attempt
    sent = []
    monkeypatch.setattr(gtm, "load_env", lambda: {
        "SMTP_USER": "u", "SMTP_PASS": "p", "MAIL_TO": "x@y.com"})

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(gtm.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(gtm, "save_state", lambda s: None)

    # State with no trial activity at all
    today = datetime.now().strftime("%Y-%m-%d")
    state = {"history": [], "active_trials": []}

    gtm.send_daily_summary_email(state, today)

    assert len(sent) == 1, "urgent pending profile must surface even on quiet days"
    # MIMEText base64-encodes by default; walk parts and decode
    body = ""
    for part in sent[0].walk():
        if part.get_content_type() == "text/html":
            body = part.get_payload(decode=True).decode("utf-8")
    assert "stale-fund" in body
    assert "URGENT" in body and "10d" in body


def test_send_summary_skips_when_only_fresh_pending(monkeypatch, tmp_path):
    """If pending profiles exist but all are < 3d old, quiet day still skips."""
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "fresh-fund", 1)
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    # Isolate the real auto-promote log: a genuinely quiet day must not read the
    # production logs/auto-promote-history.jsonl (which may hold a promotion
    # dated today, e.g. 2026-06-21 rothschild-co-am) — that would spuriously
    # turn a "quiet day" into an eventful one and fail the skip assertion.
    monkeypatch.setattr(gtm, "AUTO_PROMOTE_LOG", tmp_path / "no-auto-promote.jsonl")

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(gtm.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(gtm, "load_env", lambda: {
        "SMTP_USER": "u", "SMTP_PASS": "p", "MAIL_TO": "x@y.com"})
    monkeypatch.setattr(gtm, "save_state", lambda s: None)

    state = {"history": [], "active_trials": []}
    today = datetime.now().strftime("%Y-%m-%d")

    gtm.send_daily_summary_email(state, today)

    assert sent == [], "quiet day with only fresh pending must NOT email"


def test_subject_flags_urgent_pending(monkeypatch, tmp_path):
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir()
    _make_pending(pending_dir, "stale-1", 10)
    _make_pending(pending_dir, "stale-2", 8)
    monkeypatch.setattr(gtm, "PENDING_PROFILES_DIR", pending_dir)
    # Hermetic: ignore the real auto-promote log (see fresh-pending test).
    monkeypatch.setattr(gtm, "AUTO_PROMOTE_LOG", tmp_path / "no-auto-promote.jsonl")

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): pass
        def send_message(self, msg):
            sent.append(msg)

    monkeypatch.setattr(gtm.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(gtm, "load_env", lambda: {
        "SMTP_USER": "u", "SMTP_PASS": "p", "MAIL_TO": "x@y.com"})
    monkeypatch.setattr(gtm, "save_state", lambda s: None)

    state = {"history": [], "active_trials": []}
    today = datetime.now().strftime("%Y-%m-%d")
    gtm.send_daily_summary_email(state, today)

    assert len(sent) == 1
    subject = sent[0]["Subject"]
    assert "2PP" in subject  # 2 pending profiles
