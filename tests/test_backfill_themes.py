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
        monkeypatch.setattr(bt, "_call_model",
                            lambda prompt, keys, aid="": (raw, {}, "gpt-5.6-luna"))
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


def _corpus(tmp_path, rows):
    f = tmp_path / "articles.jsonl"
    f.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return f


class TestRunPreservesTheCorpus:
    """run() rewrites the whole file, so it must put back everything it read.

    Caught by mutation on 2026-09-07: changing `for r in rows` to `for r in
    todo` in the write-back deletes every row the backfill did not touch -- on
    the real corpus, 764 of 1375 -- and the whole suite stayed green, because
    the only run() fixture was a ONE-ROW corpus where `rows` and `todo` are the
    same list. The distinction is invisible at n=1; these fixtures are mixed on
    purpose.
    """

    MIXED = [
        _article("keep-themed", themes=["AI/Tech"]),
        _article("todo-1"),
        _article("keep-unsummarised", summarized=False),
        _article("todo-2"),
        _article("keep-nosummary", summary=""),
    ]

    def _run(self, tmp_path, monkeypatch, classify=lambda a, keys: ["Macro/Rates"]):
        f = _corpus(tmp_path, self.MIXED)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "classify", classify)
        bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        return f, [json.loads(l) for l in f.read_text().splitlines()]

    def test_no_row_is_dropped_and_order_is_kept(self, tmp_path, monkeypatch):
        _, after = self._run(tmp_path, monkeypatch)
        assert len(after) == len(self.MIXED)
        assert [r["id"] for r in after] == [r["id"] for r in self.MIXED]

    def test_rows_outside_the_candidate_set_are_untouched(self, tmp_path, monkeypatch):
        _, after = self._run(tmp_path, monkeypatch)
        by_id = {r["id"]: r for r in after}
        for original in self.MIXED:
            if original["id"].startswith("keep-"):
                assert by_id[original["id"]] == original, (
                    f"{original['id']} was modified but is not a candidate")

    def test_only_candidates_gain_themes(self, tmp_path, monkeypatch):
        _, after = self._run(tmp_path, monkeypatch)
        by_id = {r["id"]: r for r in after}
        assert by_id["todo-1"]["themes"] == ["Macro/Rates"]
        assert by_id["todo-2"]["themes"] == ["Macro/Rates"]
        assert by_id["keep-themed"]["themes"] == ["AI/Tech"]


class TestWriteIsAtomic:
    """A failed write must leave the corpus as it was, not half of it.

    backfill_themes is the only thing that rewrites data/articles.jsonl, and it
    was the only writer not using the tmp+os.replace path that
    analyze_articles.save_articles already had. Measured under a write failure
    on a 4000-row corpus: write_text left 151 parseable rows and one corrupt
    line; tmp+replace left all 4000 intact.
    """

    def test_corpus_survives_a_failed_write(self, tmp_path, monkeypatch):
        rows = [_article("a"), _article("b")]
        f = _corpus(tmp_path, rows)
        before = f.read_bytes()
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "classify", lambda a, keys: ["Macro/Rates"])
        monkeypatch.setattr(bt.os, "replace",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC")))
        with pytest.raises(OSError):
            bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        assert f.read_bytes() == before, "a failed write damaged the corpus"


class TestOneBadReplyDoesNotAbortTheRun:
    """A chatty or malformed reply costs one article, not the whole run.

    The parse used to sit outside the try that guards the network call, so
    'Sure! Here is the JSON: {...' raised JSONDecodeError and aborted -- with
    611 articles that is every already-paid-for classification discarded, and
    no resume point.
    """

    @pytest.mark.parametrize("reply", [
        'Sure! Here is the JSON: {"themes": ["Macro/Rates',   # prose + truncated
        '["Macro/Rates"]',                                    # list, not object
        '',                                                   # empty
        'not json at all',
    ])
    def test_malformed_reply_yields_no_themes_and_does_not_raise(self, reply, monkeypatch):
        monkeypatch.setattr(bt, "_call_model",
                            lambda prompt, keys, aid="": (reply, {}, "gpt-5.6-luna"))
        assert bt.classify(_article("a"), {"OPENAI_API_KEY": "k"}) == []

    def test_one_bad_article_does_not_stop_the_others(self, tmp_path, monkeypatch):
        rows = [_article("a"), _article("bad"), _article("c")]
        f = _corpus(tmp_path, rows)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "_call_model",
                            lambda prompt, keys, aid="":
                            (("garbage" if aid == "bad" else '{"themes": ["Macro/Rates"]}'),
                             {}, "gpt-5.6-luna"))
        bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        after = {json.loads(l)["id"]: json.loads(l) for l in f.read_text().splitlines()}
        assert after["a"]["themes"] == ["Macro/Rates"]
        assert after["c"]["themes"] == ["Macro/Rates"]
        assert after["bad"]["themes"] == []

    def test_single_line_fenced_reply_is_understood(self, monkeypatch):
        # The analyzer's own parser handles this; the backfill's private fence
        # stripper did not, and a single-line fenced reply crashed the run.
        monkeypatch.setattr(bt, "_call_model",
                            lambda prompt, keys, aid="":
                            ('```json {"themes": ["Macro/Rates"]} ```', {}, "gpt-5.6-luna"))
        assert bt.classify(_article("a"), {"OPENAI_API_KEY": "k"}) == ["Macro/Rates"]


class TestCheckpointing:
    def test_results_reach_disk_before_the_run_ends(self, tmp_path, monkeypatch):
        """Checkpointing must be observable mid-run, not only via `finally`.

        The first version of this test asserted the state after a
        KeyboardInterrupt -- which `finally` flushes anyway, so removing
        checkpointing altogether still passed it (caught by mutation
        2026-09-07). The scenario checkpointing actually defends against is a
        hard crash that never runs `finally`, so the assertion has to be that
        work is already on disk while the run is still going.
        """
        rows = [_article(f"a{i}") for i in range(6)]
        f = _corpus(tmp_path, rows)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "CHECKPOINT_EVERY", 2)
        seen = {"n": 0, "on_disk": None}

        def classify(a, keys):
            seen["n"] += 1
            if seen["n"] == 5:
                on_disk = [json.loads(l) for l in f.read_text().splitlines()]
                seen["on_disk"] = sum(1 for r in on_disk if r["themes"])
            return ["Macro/Rates"]

        monkeypatch.setattr(bt, "classify", classify)
        bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        assert seen["on_disk"] == 4, (
            "nothing had reached disk four articles in — a hard crash, which "
            "skips `finally`, would lose every classification paid for so far")

    def test_partial_progress_survives_a_crash(self, tmp_path, monkeypatch):
        """A crash costs one batch, not the whole run."""
        rows = [_article(f"a{i}") for i in range(6)]
        f = _corpus(tmp_path, rows)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "CHECKPOINT_EVERY", 2)
        seen = {"n": 0}

        def classify(a, keys):
            seen["n"] += 1
            if seen["n"] == 5:
                raise KeyboardInterrupt("simulated crash")
            return ["Macro/Rates"]

        monkeypatch.setattr(bt, "classify", classify)
        with pytest.raises(KeyboardInterrupt):
            bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        after = [json.loads(l) for l in f.read_text().splitlines()]
        assert len(after) == 6, "checkpoint must not drop rows"
        assert sum(1 for r in after if r["themes"]) >= 4, (
            "work completed before the crash was not checkpointed")


class TestLimit:
    def test_limit_zero_processes_nothing(self, tmp_path, monkeypatch):
        # `if limit:` treated 0 as "no limit" and would have run all 611.
        f = _corpus(tmp_path, [_article("a")])
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "classify", lambda a, keys: ["Macro/Rates"])
        bt.run(dry_run=False, limit=0, api_keys={"OPENAI_API_KEY": "k"})
        assert json.loads(f.read_text())["themes"] == []


class TestUsageIsBookedHonestly:
    """`parsed` must record what happened, not a constant.

    It was hardcoded True at the call site, before anything had been parsed --
    so an unparseable reply was booked as a successful classification. The
    accounting exists to answer cost and quality questions; a field that can
    only ever say "fine" answers neither.
    """

    def test_unparseable_reply_is_booked_as_unparsed(self, tmp_path, monkeypatch):
        import analyze_articles as aa
        log = tmp_path / "usage.jsonl"
        monkeypatch.setattr(aa, "USAGE_LOG_FILE", log)
        monkeypatch.setattr(aa, "_call_openai",
                            lambda prompt, api_key, model="gpt-4.1-mini":
                            ("not json", {"prompt_tokens": 9, "completion_tokens": 3}, model))
        assert bt.classify(_article("a"), {"OPENAI_API_KEY": "k"}) == []
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        assert [r["parsed"] for r in rows] == [False]
        assert [r["article_id"] for r in rows] == ["a"]

    def test_good_reply_is_booked_as_parsed(self, tmp_path, monkeypatch):
        import analyze_articles as aa
        log = tmp_path / "usage.jsonl"
        monkeypatch.setattr(aa, "USAGE_LOG_FILE", log)
        monkeypatch.setattr(aa, "_call_openai",
                            lambda prompt, api_key, model="gpt-4.1-mini":
                            ('{"themes": ["Macro/Rates"]}',
                             {"prompt_tokens": 9, "completion_tokens": 3}, model))
        assert bt.classify(_article("a"), {"OPENAI_API_KEY": "k"}) == ["Macro/Rates"]
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        assert [r["parsed"] for r in rows] == [True]


class TestConcurrentWriteIsRefused:
    """The nightly pipeline rewrites the same file, and neither side locks it.

    `run()` reads the corpus, spends ~40 minutes classifying, then writes the
    whole thing back. `analyze_articles.save_articles` does the same at 03:45
    BJT. Whoever writes second silently discards the other's work -- the
    pipeline's new articles, or 611 classifications. Today's run missed that
    window by luck, not design.
    """

    def test_run_refuses_to_write_over_a_corpus_that_changed(self, tmp_path, monkeypatch):
        f = _corpus(tmp_path, [_article("a"), _article("b")])
        monkeypatch.setattr(bt, "DATA_FILE", f)

        added = {"done": False}

        def classify(a, keys):
            # Simulate the nightly pipeline appending to the corpus mid-run,
            # once, the way a real concurrent writer would.
            if not added["done"]:
                f.write_text(f.read_text() + json.dumps(_article("added")) + "\n")
                added["done"] = True
            return ["Macro/Rates"]

        monkeypatch.setattr(bt, "classify", classify)
        with pytest.raises(SystemExit, match="changed"):
            bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
        assert len([l for l in f.read_text().splitlines() if l.strip()]) == 3, (
            "the concurrent writer's row was lost")


class TestTheFinalFlushDoesNotHideACrash:
    """`finally` must not replace the exception that sent us there.

    run()'s finally calls _flush, which raises SystemExit when the corpus
    changed underneath the run. If the loop was already unwinding a real
    failure, that SystemExit replaces it and the operator is told "corpus
    changed" about a run that actually died of something else.
    """

    def test_the_original_exception_survives_a_refused_final_flush(self, tmp_path, monkeypatch):
        rows = [_article(f"a{i}") for i in range(4)]
        f = _corpus(tmp_path, rows)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "CHECKPOINT_EVERY", 99)     # nothing flushes mid-run
        seen = {"n": 0}

        def classify(a, keys):
            seen["n"] += 1
            if seen["n"] == 3:
                # The nightly pipeline appends while we are unwinding, so the
                # final flush will refuse.
                f.write_text(f.read_text() + json.dumps(_article("added")) + "\n")
                raise RuntimeError("the actual failure")
            return ["Macro/Rates"]

        monkeypatch.setattr(bt, "classify", classify)
        with pytest.raises(RuntimeError, match="the actual failure"):
            bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})

    def test_a_refused_flush_still_raises_when_nothing_else_failed(self, tmp_path, monkeypatch):
        # The refusal must stay loud when it is the only thing that went wrong.
        rows = [_article("a"), _article("b")]
        f = _corpus(tmp_path, rows)
        monkeypatch.setattr(bt, "DATA_FILE", f)
        monkeypatch.setattr(bt, "CHECKPOINT_EVERY", 99)
        added = {"done": False}

        def classify(a, keys):
            if not added["done"]:
                f.write_text(f.read_text() + json.dumps(_article("added")) + "\n")
                added["done"] = True
            return ["Macro/Rates"]

        monkeypatch.setattr(bt, "classify", classify)
        with pytest.raises(SystemExit, match="changed"):
            bt.run(dry_run=False, limit=None, api_keys={"OPENAI_API_KEY": "k"})
