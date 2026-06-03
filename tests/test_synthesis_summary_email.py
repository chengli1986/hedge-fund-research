#!/usr/bin/env python3
"""Tests for scripts/send_synthesis_summary.py — the fetcher-synthesis digest email.

Covers: session filtering (since + heartbeat exclusion), HTML rendering for
success/failure/empty cases, rolling-rate math, and html.escape on free text.
SMTP itself is not exercised (send_email dry-run / env-gated).
"""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "send_synthesis_summary",
    str(Path(__file__).resolve().parent.parent / "scripts" / "send_synthesis_summary.py"),
)
sss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sss)

UTC = timezone.utc
NOW = datetime(2026, 6, 7, 18, 30, tzinfo=UTC)  # a Sunday synthesis run


def _hb(ts):
    return {"id": "_heartbeat", "outcome": "session_end", "timestamp": ts}


def _attempt(fid, name, outcome, attempted_at, **kw):
    d = {"id": fid, "name": name, "outcome": outcome, "attempted_at": attempted_at,
         "quality": kw.get("quality", "MEDIUM"), "research_url": kw.get("url", "")}
    if "commit" in kw:
        d["commit"] = kw["commit"]
    return d


# ── session_results filtering ─────────────────────────────────────────────────

def test_session_results_excludes_heartbeats_via_load(tmp_path):
    f = tmp_path / "hist.jsonl"
    f.write_text(
        json.dumps(_hb("2026-06-07T18:00:00+08:00")) + "\n"
        + json.dumps(_attempt("a", "A", "success", "2026-06-07T10:00:00Z")) + "\n",
        encoding="utf-8")
    entries = sss.load_history(f)
    assert len(entries) == 1
    assert entries[0]["id"] == "a"


def test_session_results_since_filter():
    entries = [
        _attempt("old", "Old", "success", "2026-06-01T10:00:00Z"),
        _attempt("new", "New", "success", "2026-06-07T10:00:00Z"),
    ]
    since = datetime(2026, 6, 7, 0, 0, tzinfo=UTC)
    res = sss.session_results(entries, since)
    assert [r["id"] for r in res] == ["new"]


def test_session_results_none_since_returns_all():
    entries = [_attempt("a", "A", "success", "2026-06-07T10:00:00Z")]
    assert len(sss.session_results(entries, None)) == 1


def test_session_results_excludes_unparseable_attempted_at_when_filtering():
    entries = [_attempt("bad", "Bad", "success", "not-a-date")]
    since = datetime(2026, 6, 7, 0, 0, tzinfo=UTC)
    assert sss.session_results(entries, since) == []


# ── rolling_success_rate ──────────────────────────────────────────────────────

def test_rolling_rate_window():
    entries = [
        _attempt("a", "A", "success", "2026-06-06T10:00:00Z"),  # in 30d
        _attempt("b", "B", "failed", "2026-06-05T10:00:00Z"),   # in 30d
        _attempt("c", "C", "success", "2026-04-01T10:00:00Z"),  # outside 30d
    ]
    succ, total = sss.rolling_success_rate(entries, 30, NOW)
    assert (succ, total) == (1, 2)


# ── build_html ────────────────────────────────────────────────────────────────

def _candidates():
    return [
        {"id": "acadian-asset", "name": "Acadian Asset Management",
         "status": "visitable", "quality": "HIGH",
         "research_url": "https://acadian.example/insights"},
        {"id": "cohen-steers", "name": "Cohen & Steers",
         "status": "inaccessible", "quality": "MEDIUM",
         "notes": "403 Forbidden confirmed; quality unverifiable."},
        {"id": "matthews-asia", "name": "Matthews Asia",
         "status": "promoted", "quality": "MEDIUM"},
    ]


def test_build_html_success_and_failure():
    results = [
        _attempt("acadian-asset", "Acadian Asset Management", "success",
                 "2026-06-07T10:00:00Z", quality="HIGH", commit="64c89297deadbeef"),
        _attempt("cohen-steers", "Cohen & Steers", "failed", "2026-06-07T10:05:00Z"),
    ]
    htm = sss.build_html(results, targets_count=3, candidates=_candidates(),
                         entries=results, now=NOW)
    assert "Acadian Asset Management" in htm
    assert "64c89297" in htm                      # short commit shown
    assert "重新 trial" in htm                     # visitable next-step text
    assert "403 Forbidden" in htm                 # failure reason from notes
    assert "1 成功" in htm and "1 失败" in htm
    assert "MIME" not in htm                       # body is pure HTML, no header leak


def test_build_html_promoted_next_step():
    results = [
        _attempt("matthews-asia", "Matthews Asia", "success",
                 "2026-06-07T10:00:00Z", commit="54882ed4"),
    ]
    htm = sss.build_html(results, 1, _candidates(), results, NOW)
    assert "直接进生产" in htm  # promoted → production wiring


def test_build_html_escapes_freetext():
    cands = [{"id": "x", "name": "Evil <script>", "status": "inaccessible",
              "notes": "bad <b>html</b> & stuff"}]
    results = [_attempt("x", "Evil <script>", "failed", "2026-06-07T10:00:00Z")]
    htm = sss.build_html(results, 1, cands, results, NOW)
    assert "<script>" not in htm
    assert "&lt;script&gt;" in htm
    assert "&amp; stuff" in htm


def test_build_html_empty_results_warns():
    htm = sss.build_html([], targets_count=2, candidates=_candidates(),
                         entries=[], now=NOW)
    assert "未记录任何结果" in htm
    assert "2 个目标" in htm


# ── main control flow ─────────────────────────────────────────────────────────

def test_main_skips_when_no_results_and_no_targets(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "hist.jsonl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(sss, "HISTORY_FILE", empty)
    rc = sss.main(["--targets-count", "0"])
    assert rc == 0
    assert "skipping email" in capsys.readouterr().out


def test_build_html_candidate_missing_falls_back_to_id():
    """A result whose id isn't in fund_candidates.json still renders (name from
    the jsonl entry, no crash)."""
    results = [_attempt("ghost-fund", "Ghost Fund", "success",
                        "2026-06-07T10:00:00Z", commit="abc12345")]
    htm = sss.build_html(results, 1, candidates=[], entries=results, now=NOW)
    assert "Ghost Fund" in htm
    assert "abc12345" in htm


def test_main_empty_results_with_targets_sends_warning(tmp_path, monkeypatch, capsys):
    """agent ran (targets>0) but recorded nothing → digest still goes out with
    the 'no results recorded' warning (the key ops-alert path), via main()."""
    hist = tmp_path / "hist.jsonl"
    hist.write_text("", encoding="utf-8")
    cands = tmp_path / "cands.json"
    cands.write_text(json.dumps(_candidates()), encoding="utf-8")
    monkeypatch.setattr(sss, "HISTORY_FILE", hist)
    monkeypatch.setattr(sss, "CANDIDATES_FILE", cands)
    rc = sss.main(["--since", "2026-06-07T00:00:00Z", "--targets-count", "2", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "未记录任何结果" in out
    assert "2 个目标" in out


def test_main_handles_missing_candidates_file(tmp_path, monkeypatch, capsys):
    """Missing fund_candidates.json must not crash — digest sends from history."""
    hist = tmp_path / "hist.jsonl"
    hist.write_text(
        json.dumps(_attempt("acadian-asset", "Acadian Asset Management", "success",
                            "2026-06-07T10:00:00Z", commit="64c89297")) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(sss, "HISTORY_FILE", hist)
    monkeypatch.setattr(sss, "CANDIDATES_FILE", tmp_path / "does-not-exist.json")
    rc = sss.main(["--since", "2026-06-07T00:00:00Z", "--targets-count", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cannot load candidates file" in out
    assert "Acadian Asset Management" in out


def test_main_dry_run_prints_html(tmp_path, monkeypatch, capsys):
    hist = tmp_path / "hist.jsonl"
    hist.write_text(
        json.dumps(_attempt("acadian-asset", "Acadian Asset Management", "success",
                            "2026-06-07T10:00:00Z", commit="64c89297")) + "\n",
        encoding="utf-8")
    cands = tmp_path / "cands.json"
    cands.write_text(json.dumps(_candidates()), encoding="utf-8")
    monkeypatch.setattr(sss, "HISTORY_FILE", hist)
    monkeypatch.setattr(sss, "CANDIDATES_FILE", cands)
    rc = sss.main(["--since", "2026-06-07T00:00:00Z", "--targets-count", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Acadian Asset Management" in out
    assert "GMIA Fetcher Synthesis" in out
