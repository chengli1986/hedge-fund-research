"""Work already paid for must survive the next interruption (audit A3).

analyze_articles.py summarised every pending article and then wrote the store
once, after the loop. The pipeline runs under `cron-wrapper.sh --timeout 1800`,
so a night that overruns loses every summary generated that run -- each one
already charged to the API account -- and buys them again the next night.
Measured while auditing: recent runs take 265-472s against the 1800s budget, so
the margin is real today; a backlog after an outage is what eats it.

Saving after every article would rewrite a 1,700-row file per summary, so it
saves every SAVE_EVERY articles and once at the end.
"""
import json
import sys

import pytest

import analyze_articles as aa

RESULT = {"summary_en": "e", "summary_zh": "z", "themes": ["Macro/Rates"],
          "key_takeaway_en": "k", "key_takeaway_zh": "k", "_model": "m", "_usage": {}}


def _rows(n):
    return [{"id": f"p{i}", "source_id": "aqr", "title": f"T{i}", "url": f"https://x/{i}",
             "date": "2026-09-20", "content_status": "ok", "summarized": False} for i in range(n)]


def _setup(tmp_path, monkeypatch, n, on_call=None):
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps(r) + "\n" for r in _rows(n)))
    content = tmp_path / "content"
    content.mkdir()
    for i, r in enumerate(_rows(n)):
        # Distinct bodies: identical ones are declined as duplicate_body (which
        # is correct, and made the first version of this fixture summarise one
        # article out of seven).
        (content / f"{r['id']}.txt").write_text(
            f"Issue {i}: spreads on tranche {i} tightened while issuance recovered. " * 40)
    monkeypatch.setattr(aa, "DATA_FILE", data)
    monkeypatch.setattr(aa, "CONTENT_DIR", content)
    monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
    calls = {"n": 0}

    def fake(content_, *a, **k):
        calls["n"] += 1
        if on_call:
            on_call(calls["n"])
        return dict(RESULT)

    monkeypatch.setattr(aa, "_analyze_with_fallback", fake)
    monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
    return data, calls


def _summarised(data):
    return [r["id"] for r in (json.loads(l) for l in data.read_text().splitlines())
            if r.get("summarized")]


def test_summaries_survive_an_interruption_mid_run(tmp_path, monkeypatch):
    """The kill lands after SAVE_EVERY articles have been summarised."""
    def boom(n):
        if n > aa.SAVE_EVERY:
            raise KeyboardInterrupt("cron wrapper SIGTERM")

    data, _ = _setup(tmp_path, monkeypatch, aa.SAVE_EVERY * 2, on_call=boom)
    with pytest.raises(KeyboardInterrupt):
        aa.main()
    assert len(_summarised(data)) == aa.SAVE_EVERY, "a timeout threw away paid-for summaries"


def test_a_complete_run_saves_everything(tmp_path, monkeypatch):
    data, calls = _setup(tmp_path, monkeypatch, aa.SAVE_EVERY + 2)
    aa.main()
    assert len(_summarised(data)) == aa.SAVE_EVERY + 2
    assert calls["n"] == aa.SAVE_EVERY + 2


def test_a_short_run_still_saves(tmp_path, monkeypatch):
    data, _ = _setup(tmp_path, monkeypatch, 2)
    aa.main()
    assert len(_summarised(data)) == 2


def test_the_store_is_not_rewritten_once_per_article(tmp_path, monkeypatch):
    """A rewrite per summary would mean 1,700 rows written 20 times a night."""
    writes = {"n": 0}
    real = aa.save_articles
    monkeypatch.setattr(aa, "save_articles", lambda arts: writes.__setitem__("n", writes["n"] + 1) or real(arts))
    _setup(tmp_path, monkeypatch, aa.SAVE_EVERY * 2)
    aa.main()
    assert writes["n"] <= 3, f"saved {writes['n']} times for {aa.SAVE_EVERY * 2} articles"


def test_the_batch_size_is_stated():
    assert 1 < aa.SAVE_EVERY <= 10
