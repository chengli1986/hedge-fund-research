"""Backfilling themes onto articles that were summarised before the prompt fix.

611 of 1331 summarised articles carry `themes: []` because neither prompt ever
showed the model the allowlist it would be filtered against (fixed 2026-09-07 in
`c061639`).  Those articles are already summarised well; only the classification
is missing, so the backfill re-derives themes from the stored summary and writes
back nothing else.  Rewriting 611 published summaries wholesale would be a far
larger change than the defect warrants.
"""
import json

import pytest

import backfill_themes as bt


def _article(aid, themes=None, summarized=True, summary="A summary about rates."):
    return {"id": aid, "title": f"T{aid}", "source_id": "src", "date": "2026-09-01",
            "summary_en": summary, "summary_zh": "摘要", "themes": themes if themes is not None else [],
            "key_takeaway_en": "k", "key_takeaway_zh": "要", "summarized": summarized,
            "analysis_model": "gemini-2.5-pro"}


class TestSelection:
    def test_picks_summarised_articles_with_no_themes(self):
        rows = [_article("a"), _article("b", themes=["AI/Tech"]),
                _article("c", summarized=False)]
        assert [a["id"] for a in bt.select_articles(rows)] == ["a"]

    def test_skips_articles_with_no_stored_summary(self):
        # Nothing to classify from; re-reading content is out of scope here.
        rows = [_article("a", summary=""), _article("b")]
        assert [a["id"] for a in bt.select_articles(rows)] == ["b"]


class TestWriteBack:
    def test_only_the_themes_field_changes(self, monkeypatch):
        before = _article("a")
        original = json.loads(json.dumps(before))
        monkeypatch.setattr(bt, "classify", lambda a, keys: ["Macro/Rates"])
        after = bt.apply(before, {"OPENAI_API_KEY": "k"})
        assert after["themes"] == ["Macro/Rates"]
        for k in original:
            if k != "themes":
                assert after[k] == original[k], f"backfill modified {k}"

    def test_empty_classification_does_not_wipe_existing_themes(self, monkeypatch):
        """A model that returns nothing usable must not overwrite anything.

        The first version of this test started from an article whose themes
        were already [] and compared the returned dict to the same object it
        had passed in -- so "write the empty list back anyway" passed it.  A
        mutation caught that on 2026-09-07: the assertion has to start from
        themes that would visibly be destroyed, and compare against a copy.
        """
        before = _article("a", themes=["Macro/Rates"])
        original = json.loads(json.dumps(before))
        monkeypatch.setattr(bt, "classify", lambda a, keys: [])
        after = bt.apply(before, {"OPENAI_API_KEY": "k"})
        assert after == original

    def test_empty_classification_leaves_an_unthemed_article_alone(self, monkeypatch):
        before = _article("a")
        original = json.loads(json.dumps(before))
        monkeypatch.setattr(bt, "classify", lambda a, keys: [])
        after = bt.apply(before, {"OPENAI_API_KEY": "k"})
        assert after == original

    def test_classification_is_filtered_through_the_allowlist(self, monkeypatch):
        import analyze_articles as aa
        raw = '{"themes": ["Macro/Rates", "Totally Invented Label"]}'
        monkeypatch.setattr(bt, "_call_model", lambda prompt, keys, aid="": raw)
        got = bt.classify(_article("a"), {"OPENAI_API_KEY": "k"})
        assert got == ["Macro/Rates"]
        assert all(t in aa.VALID_THEMES for t in got)


class TestPromptSharesOneAllowlist:
    def test_backfill_prompt_reuses_the_analyzer_instruction(self):
        # Same abstraction, not a second copy: a copy drifts the moment
        # VALID_THEMES changes, and the drift is silent.
        import analyze_articles as aa
        assert aa._THEME_INSTRUCTION in bt.BACKFILL_PROMPT


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        f = tmp_path / "articles.jsonl"
        f.write_text(json.dumps(_article("a"), ensure_ascii=False) + "\n")
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "classify", lambda a, keys: ["Macro/Rates"])
        sig = f.stat().st_size, f.stat().st_mtime_ns
        bt.run(dry_run=True, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        assert (f.stat().st_size, f.stat().st_mtime_ns) == sig

    def test_real_run_backs_up_before_writing(self, tmp_path, monkeypatch):
        f = tmp_path / "articles.jsonl"
        f.write_text(json.dumps(_article("a"), ensure_ascii=False) + "\n")
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "classify", lambda a, keys: ["Macro/Rates"])
        bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        backups = list(tmp_path.glob("articles.jsonl.bak-*"))
        assert backups, "no backup written before mutating the corpus"
        assert json.loads(backups[0].read_text())["themes"] == []
        assert json.loads(f.read_text())["themes"] == ["Macro/Rates"]
