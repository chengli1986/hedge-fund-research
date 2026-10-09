"""A re-published document must not be summarised twice (finding C1).

Measured 2026-09-17, the morning after 3df7146 widened what counts as a new
issue at a reused URL. Of the issues it recovered, one was a real duplicate and
was caught -- loomis-sayles' monthly update, byte-identical. Another was not:
janus-henderson's "Charts for the beach 2026" came back 7,212 chars against the
stored 7,199, i.e. the same document with 13 characters more, and _body_key
hashes the whole text, so 13 characters made it a different article. It was
summarised again and the dashboard carried the piece twice.

The safety net this repo leaned on when widening ingestion ("a wrongly ingested
issue is refused publication as duplicate_body") only held for byte-identical
bodies. This is the near-duplicate half.

Thresholds come from the store, not from taste. Same source + same title pairs,
Jaccard over 8-character shingles:

    1.00  loomis-sayles   monthly update      真 duplicate (exact, already caught)
    1.00  janus-henderson Charts for the beach 真 duplicate (the one that slipped)
    0.79  franklin        Investment Survey    the same survey page still being
                                               filled in -- 20% more text, many
                                               new words; kept, not declined
    0.35  kkr             Wealth Playbook      genuine new edition
    0.33  gsam            Corporate Pension    genuine new issue
    0.30  loomis          Global Fixed Income  genuine new issue
    0.18  gsam            Market Monitor       genuine new issue
    0.14  troweprice      Weekly Update        genuine new issue

So DUPLICATE_JACCARD = 0.85 sits above every genuine issue observed (0.35) by a
wide margin and below both true duplicates. Pairs between 0.6 and 0.85 are
logged and kept, so the borderline is visible rather than guessed at again.

Why the title must match too: without it, ares' 1,489-char "Content is
Everywhere" scores 0.89 against a dozen longer, unrelated pieces (its text is
almost all shared boilerplate), and gmo's 7-Year Asset Class Forecasts for
February, April, 1Q and 2Q score 0.89-0.90 against each other -- one template,
different numbers, genuinely different issues.
"""
import json
import random

import pytest

import analyze_articles as aa
import text_identity

def _body(seed: int, sentences: int = 60) -> str:
    """Prose with a large shingle vocabulary, like a real 7,000-char article.

    Not one sentence repeated: a repeated line has ~120 distinct shingles, so a
    single added sentence moves Jaccard by 0.17 and the fixture would test the
    fixture rather than the rule. Janus' real pair -- 7,199 vs 7,212 chars --
    scored 1.00.
    """
    rng = random.Random(seed)
    words = [f"{w}{n}" for w in ("credit", "spread", "issuance", "duration", "carry", "sponsor",
                                 "leverage", "covenant", "refinancing", "dispersion", "liquidity",
                                 "valuation", "allocation", "drawdown", "maturity")
             for n in range(40)]
    return " ".join(" ".join(rng.sample(words, 12)) + "." for _ in range(sentences))


BASE = _body(1)


def _variant(changed: int) -> str:
    """BASE with its first `changed` sentences rewritten: 2 -> J 0.95, 5 -> 0.87."""
    alt = _body(9).split(". ")
    return ". ".join(alt[:changed] + BASE.split(". ")[changed:])


def _jaccard(a: str, b: str) -> float:
    mine, theirs = text_identity.shingles(a), text_identity.shingles(b)
    return len(mine & theirs) / len(mine | theirs)


def _art(i, title, source="janus-henderson", **kw):
    return dict({"id": f"a{i}", "source_id": source, "title": title, "url": f"https://x/{i}",
                 "date": "2026-09-01", "summarized": True, "summary_en": "s", "summary_zh": "s",
                 "key_takeaway_en": "k", "key_takeaway_zh": "k", "themes": ["Macro/Rates"],
                 "content_status": "ok"}, **kw)


class TestNearDuplicateDetection:
    def test_the_same_document_with_a_few_more_characters_is_a_duplicate(self):
        """janus: 7,199 chars stored, 7,212 fetched, same title."""
        published = aa.published_index([_art(1, "Charts for the beach 2026")], {"a1": BASE})
        owner = aa.duplicate_owner(published, "janus-henderson", "Charts for the beach 2026",
                                   BASE + " credit7 spread3 issuance9 duration2 carry5.")
        assert owner is not None and owner["id"] == "a1"

    def test_an_exact_copy_is_a_duplicate(self):
        published = aa.published_index([_art(1, "Monthly Update")], {"a1": BASE})
        assert aa.duplicate_owner(published, "janus-henderson", "Monthly Update", BASE) is not None

    def test_a_genuine_new_issue_of_the_same_series_is_not(self):
        """gsam Market Monitor: same title, J=0.18."""
        other = _body(2)
        published = aa.published_index([_art(1, "Market Monitor", source="gsam")], {"a1": BASE})
        assert aa.duplicate_owner(published, "gsam", "Market Monitor", other) is None

    def test_a_short_body_under_another_title_is_not_compared(self):
        """gmo's 7-Year Forecasts: one 1,360-char template, different numbers,
        0.80-0.82 against each other under different titles."""
        short = BASE[:2000]
        published = aa.published_index([_art(1, "GMO 7-Year Forecast: April", source="gmo")],
                                       {"a1": short})
        assert aa.duplicate_owner(published, "gmo", "GMO 7-Year Forecast: August",
                                  short + " credit7.") is None

    def test_a_retitled_copy_is_a_duplicate(self):
        """2026-10-09: Baillie Gifford retitled "EM: enough is not enough" (J 0.998)
        and it was summarised twice; brookfield, cohen-steers, wellington and
        principal carried the same body under two titles (0.95-0.996)."""
        published = aa.published_index([_art(1, "EM: enough is not enough", source="baillie-gifford")],
                                       {"a1": BASE})
        retitled = _variant(2)
        assert _jaccard(BASE, retitled) > 0.94
        owner = aa.duplicate_owner(published, "baillie-gifford",
                                   "Emerging markets: enough is not enough", retitled)
        assert owner is not None and owner["id"] == "a1"

    def test_a_retitled_body_needs_the_higher_bar(self):
        """Between DUPLICATE_JACCARD and RETITLED_JACCARD: a duplicate under the
        same title, not under another one."""
        near = _variant(5)
        assert aa.DUPLICATE_JACCARD < _jaccard(BASE, near) < text_identity.RETITLED_JACCARD
        published = aa.published_index([_art(1, "Credit Quarterly", source="brookfield")], {"a1": BASE})
        assert aa.duplicate_owner(published, "brookfield", "Credit Quarterly", near) is not None
        assert aa.duplicate_owner(published, "brookfield", "Credit Quarterly: New terms", near) is None

    def test_a_retitled_copy_later_in_the_same_run_is_caught(self):
        published = aa.published_index([], {})
        aa.register_published(published, _art(1, "Credit Quarterly: New normal?", source="brookfield"), BASE)
        assert aa.duplicate_owner(published, "brookfield", "Credit Quarterly: New terms",
                                  _variant(1)) is not None

    def test_cjk_articles_are_compared_on_their_own_text(self):
        """title_key and shingles kept only [0-9a-z]: every Japanese title had
        the key "" and two different Japanese bodies were compared on the
        English footer they share."""
        footer = " This material is for professional investors only. " * 12
        one = "日本株の見通しは企業統治改革と賃上げに支えられ、引き続き堅調である。" * 6 + footer
        two = "円相場は日銀の政策修正観測を背景に、ボラティリティが高まっている。" * 6 + footer
        published = aa.published_index([_art(1, "日本株の見通し", source="verdad")], {"a1": one})
        assert aa.duplicate_owner(published, "verdad", "円相場の展望", two) is None
        assert aa.duplicate_owner(published, "verdad", "日本株の見通し", two) is None
        assert aa.duplicate_owner(published, "verdad", "日本株の見通し", one + "追記。") is not None

    def test_empty_bodies_are_not_the_same_document(self):
        assert text_identity.same_document("", "", "T", "T") is False
        published = aa.published_index([_art(1, "T")], {"a1": ""})
        assert aa.duplicate_owner(published, "janus-henderson", "T", "") is None

    def test_another_source_with_the_same_title_is_not_compared(self):
        published = aa.published_index([_art(1, "Market Monitor", source="gsam")], {"a1": BASE})
        assert aa.duplicate_owner(published, "aqr", "Market Monitor", BASE) is None

    def test_a_document_still_being_filled_in_is_kept(self):
        """franklin's survey page: J=0.79, 20% more text, many new words."""
        grown = BASE + " " + _body(3, sentences=16)      # ~20% more text, many new words
        published = aa.published_index([_art(1, "Survey", source="franklin-templeton")], {"a1": BASE})
        assert aa.duplicate_owner(published, "franklin-templeton", "Survey", grown) is None

    def test_a_short_body_is_not_judged_by_similarity(self):
        """Two teasers of a few dozen characters score 0.92 on a ratio that
        means nothing at that length -- two different event notices would too.
        Stage 2 already refuses bodies this short for most sources; what
        reaches here is metadata_only, where a teaser is all there is."""
        short = "Quarterly outlook webinar registration is open."
        longer = "Quarterly outlook webinar registration is open now."
        assert len(aa._shingles(short) & aa._shingles(longer)) / \
            len(aa._shingles(short) | aa._shingles(longer)) > aa.DUPLICATE_JACCARD
        published = aa.published_index([_art(1, "Webinar")], {"a1": short})
        assert aa.duplicate_owner(published, "janus-henderson", "Webinar", longer) is None

    def test_the_threshold_and_watch_level_are_stated(self):
        assert 0.35 < aa.DUPLICATE_JACCARD <= 0.85
        assert aa.NEAR_DUPLICATE_WATCH < aa.DUPLICATE_JACCARD


class TestItRunsBeforeTheModel:
    ART = {"id": "new1", "source_id": "janus-henderson", "title": "Charts for the beach 2026",
           "url": "https://x/new", "date": "2026-09-10", "content_status": "ok", "summarized": False}

    def _run(self, tmp_path, monkeypatch, stored_body, new_body):
        data = tmp_path / "articles.jsonl"
        old = _art(1, "Charts for the beach 2026")
        data.write_text("\n".join(json.dumps(r) for r in (old, self.ART)) + "\n")
        content = tmp_path / "content"
        content.mkdir()
        (content / "a1.txt").write_text(stored_body)
        (content / "new1.txt").write_text(new_body)
        called = []
        monkeypatch.setattr(aa, "DATA_FILE", data)
        monkeypatch.setattr(aa, "CONTENT_DIR", content)
        monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
        monkeypatch.setattr(aa, "_analyze_with_fallback",
                            lambda *a, **k: called.append(1) or {"summary_en": "x", "summary_zh": "x",
                                                                 "key_takeaway_en": "k", "key_takeaway_zh": "k",
                                                                 "themes": [], "_model": "m", "_usage": {}})
        import sys
        monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
        aa.main()
        rows = {r["id"]: r for r in (json.loads(l) for l in data.read_text().splitlines())}
        return rows["new1"], called

    def test_a_near_duplicate_is_declined_without_a_model_call(self, tmp_path, monkeypatch):
        row, called = self._run(tmp_path, monkeypatch, BASE, BASE + " credit7 spread3 issuance9.")
        assert row["analysis_label"] == "duplicate_body"
        assert called == [], "paid for a summary of a body we already published"

    def test_a_genuine_new_issue_is_still_summarised(self, tmp_path, monkeypatch):
        other = _body(4)
        row, called = self._run(tmp_path, monkeypatch, BASE, other)
        assert row.get("summarized") is True and called == [1]


def test_a_second_copy_in_the_same_run_is_caught(tmp_path, monkeypatch):
    """Two copies arriving the same night: the first is summarised and must
    become the owner, or the second is paid for as well."""
    data = tmp_path / "articles.jsonl"
    rows = [dict(_art(1, "Quarterly Letter"), summarized=False, summary_en=None),
            dict(_art(2, "Quarterly Letter"), summarized=False, summary_en=None)]
    data.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    content = tmp_path / "content"
    content.mkdir()
    (content / "a1.txt").write_text(BASE)
    (content / "a2.txt").write_text(BASE + " credit7 spread3 issuance9.")
    calls = []
    monkeypatch.setattr(aa, "DATA_FILE", data)
    monkeypatch.setattr(aa, "CONTENT_DIR", content)
    monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
    monkeypatch.setattr(aa, "_analyze_with_fallback",
                        lambda *a, **k: calls.append(k.get("article_id")) or
                        {"summary_en": "x", "summary_zh": "x", "key_takeaway_en": "k",
                         "key_takeaway_zh": "k", "themes": [], "_model": "m", "_usage": {}})
    import sys
    monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
    aa.main()
    out = {r["id"]: r for r in (json.loads(l) for l in data.read_text().splitlines())}
    assert out["a1"]["summarized"] is True
    assert out["a2"]["analysis_label"] == "duplicate_body"
    assert calls == ["a1"], calls
