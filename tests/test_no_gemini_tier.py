"""The Gemini tier is gone (2026-09-21): MODEL_CHAIN is two OpenAI models.

Decision record. The Google key is being retired after a suspected leak. The
third tier existed as cross-provider insurance ("an OpenAI-wide outage still
leaves a tier"), but from the 2026-09-07 reorder to today it never ran once
outside probes (308 real analyses, all on luna), and a total outage is no
longer silent: main() exits non-zero when articles were pending and none was
summarised, and the articles stay pending for the next night. The user chose
to keep one provider rather than wire a second key in. This file pins that
choice so a future edit cannot quietly reintroduce a Google dependency.
"""
import inspect
from pathlib import Path

import analyze_articles as aa
import backfill_themes as bt

REPO = Path(__file__).resolve().parent.parent


def test_chain_is_exactly_two_openai_tiers():
    assert aa.MODEL_CHAIN == ["gpt-5.6-luna", "gpt-4.1-mini"]
    assert all(m in aa.OPENAI_MODELS for m in aa.MODEL_CHAIN)


def test_gemini_caller_and_accounting_are_gone():
    assert not hasattr(aa, "_call_gemini")
    assert not [m for m in aa._USAGE_FIELDS if m.startswith("gemini")]


def test_no_google_endpoint_or_key_in_stage3_sources():
    for rel in ("analyze_articles.py", "backfill_themes.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        for tok in ("generativelanguage", "GEMINI_API_KEY", "_call_gemini"):
            assert tok not in src, f"{rel} still mentions {tok}"


def test_a_google_key_alone_runs_nothing(monkeypatch):
    """Only a GEMINI key present: every tier is skipped, no caller is invoked."""
    def boom(*a, **k):
        raise AssertionError("an OpenAI tier ran without OPENAI_API_KEY")
    monkeypatch.setattr(aa, "_call_openai", boom)
    assert aa._analyze_with_fallback("body", {"GEMINI_API_KEY": "k"}, article_id="x") is None


def test_backfill_calls_openai_with_the_openai_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(aa, "_call_openai",
                        lambda prompt, api_key, model="gpt-4.1-mini": (seen.update(key=api_key, model=model), ("{}", {}, model))[1])
    bt._call_model("p", {"OPENAI_API_KEY": "ok", "GEMINI_API_KEY": "nope"})
    assert seen == {"key": "ok", "model": aa.MODEL_CHAIN[0]}
