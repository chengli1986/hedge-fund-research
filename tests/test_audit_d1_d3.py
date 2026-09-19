"""Three findings from the 2026-09-19 code-review backlog audit, verified by hand.

D1 — analyze_articles._call_gemini indexed data["candidates"][0] unguarded.
     Gemini answers a safety-filtered request with {"candidates": [],
     "promptFeedback": {"blockReason": "SAFETY"}}; that raised IndexError,
     which the model chain logged as a generic "error (attempt N): list index
     out of range" and retried. The block reason never reached the log.

D2 — jsonl_store.read_rows decoded the whole file strictly. One invalid byte
     (a write torn inside a multi-byte character; ensure_ascii=False writes
     Chinese text raw) raised UnicodeDecodeError before a single line was
     read, so every caller lost every row -- the opposite of the per-line
     recovery the store exists to provide (B8).

D3 — scripts/gmia-fetcher-health.py load_state() parsed the state file with
     no handling and save_state() wrote it in place. A truncated file (host
     crash mid-write) aborted the whole health check on every later run: no
     FAIL/WARN email until someone deleted the file by hand. Same family as
     B4 (fetch_articles, fixed 2026-09-17): keep the evidence, say so, run.
"""
import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest

import analyze_articles as aa
import jsonl_store

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gfh", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(_spec)
sys.modules["gfh"] = gfh
_spec.loader.exec_module(gfh)


class TestD1GeminiSafetyBlock:
    def test_an_empty_candidates_list_names_the_block_reason(self, monkeypatch):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"candidates": [],
                        "promptFeedback": {"blockReason": "SAFETY"},
                        "usageMetadata": {"promptTokenCount": 12}}

        monkeypatch.setattr(aa.requests, "post", lambda url, **kw: R())
        with pytest.raises(ValueError, match="SAFETY"):
            aa._call_gemini("p", "k", model="gemini-2.5-flash")

    def test_a_reply_with_no_candidates_key_is_also_a_clear_error(self, monkeypatch):
        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}

        monkeypatch.setattr(aa.requests, "post", lambda url, **kw: R())
        with pytest.raises(ValueError, match="PROHIBITED_CONTENT"):
            aa._call_gemini("p", "k", model="gemini-2.5-flash")


class TestD2InvalidByteInTheStore:
    def test_one_undecodable_line_does_not_take_the_others_with_it(self, tmp_path, caplog):
        f = tmp_path / "a.jsonl"
        good1 = json.dumps({"id": "a1", "title": "第一篇"}, ensure_ascii=False).encode()
        torn = '{"id": "a2", "title": "第'.encode() + b"\xe4\xb8" + b'"}'   # cut inside 二
        good3 = json.dumps({"id": "a3", "title": "第三篇"}, ensure_ascii=False).encode()
        f.write_bytes(good1 + b"\n" + torn + b"\n" + good3 + b"\n")
        with caplog.at_level(logging.ERROR):
            rows, damaged = jsonl_store.read_rows(f)
        assert [r["id"] for r in rows] == ["a1", "a3"]
        assert damaged == 1
        assert any("line 2" in r.getMessage() for r in caplog.records), \
            "the undecodable row must be named by line, like any other damaged row"


class TestD3HealthStateFile:
    @pytest.fixture
    def state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "LOGS_DIR", tmp_path)
        f = tmp_path / "gmia-fetcher-health.json"
        monkeypatch.setattr(gfh, "STATE_FILE", f)
        return f

    def test_a_truncated_state_file_does_not_abort_the_health_check(self, state, caplog):
        state.write_text('{"last_run": "2026-09-18T20:30:00+08:00", "sources": {"a": {"st')
        with caplog.at_level(logging.ERROR):
            loaded = gfh.load_state()
        assert loaded == {"last_run": None, "sources": {}}
        assert any("STATE FILE UNREADABLE" in r.getMessage() for r in caplog.records)

    def test_the_unreadable_file_is_kept_aside_as_evidence(self, state):
        state.write_text('{"sources": {"a": {"st')
        gfh.load_state()
        copies = list(state.parent.glob(state.name + ".corrupt-*"))
        assert len(copies) == 1
        assert copies[0].read_text() == '{"sources": {"a": {"st'

    def test_a_save_that_dies_midway_leaves_the_old_file_intact(self, state, monkeypatch):
        state.write_text('{"last_run": "old", "sources": {}}\n')

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(gfh.os, "replace", boom)
        with pytest.raises(OSError):
            gfh.save_state({"last_run": "new", "sources": {}})
        assert json.loads(state.read_text())["last_run"] == "old"
        assert not list(state.parent.glob("*.tmp")), "a failed save must not litter a temp file"

    def test_a_save_that_completes_replaces_the_file(self, state):
        gfh.save_state({"last_run": "new", "sources": {"a": 1}})
        assert json.loads(state.read_text()) == {"last_run": "new", "sources": {"a": 1}}


class TestD3ItReachesAHuman:
    def test_main_reports_the_health_checks_own_state_backups(self, tmp_path, monkeypatch):
        """A log line is not a destination: the copy load_state keeps must
        show up in the health email like the inspection_state copies do."""
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "gmia-fetcher-health.json")
        (tmp_path / "gmia-fetcher-health.json.corrupt-20260919-0230").write_text("{")
        seen = {}
        monkeypatch.setattr(gfh, "load_sources", lambda: [])
        monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
        monkeypatch.setattr(gfh, "pipeline_intake_anomalies", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "pipeline_zero_fetches", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "recent_analysis_declines", lambda *a, **k: [])
        monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
        monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
        monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
        monkeypatch.setattr(gfh, "should_email", lambda *a, **k: seen.update(k) or False)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py"])
        gfh.main()
        assert seen.get("corrupt_state") == ["gmia-fetcher-health.json.corrupt-20260919-0230"]
