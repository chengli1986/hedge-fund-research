"""A body already summarised for another article of the same source is not
summarised again (2026-09-14).

Fourteen groups of identical stored bodies were found across the corpus. Two
kinds, and both put wrong or repeated text on the page:

  different articles, one document -- oaktree "Cockroaches in the Coal Mine"
  and "What's Going on in Private Credit?" (both "What Does the Market
  Know?"), gmo's two 60/40 pieces (both an Oct-2025 paper), rothschild's
  September monthly (August's text), amundi "Bond yields on the rise" /
  "...rise again". Each summary faithfully describes its text, so
  check_grounding passes it; the fault is that the text belongs to another
  title.

  one article stored twice under URL variants -- brookfield, apollo x2,
  ares (case), man-group (%20 vs -), rothschild, metlife, mfs: two
  identical summaries on the page.

Stage 3 cannot tell which title the text really belongs to, but it can
refuse to publish the same text twice: the second article is recorded as
insufficient_content with a reason naming the first, which the daily
health email then reports by source.
"""
import json
import sys

import analyze_articles as aa

BODY = "Memo to: Oaktree Clients. From: Howard Marks. Re: What Does the Market Know? " * 20


def _run(tmp_path, monkeypatch, articles, bodies):
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in articles))
    content = tmp_path / "content"
    content.mkdir()
    for aid, text in bodies.items():
        (content / f"{aid}.txt").write_text(text)
    calls = []

    def fake(content_, *a, **k):
        calls.append(k.get("article_id"))
        return {"summary_en": "s", "summary_zh": "摘", "themes": ["Credit/Fixed Income"],
                "key_takeaway_en": "k", "key_takeaway_zh": "要", "_model": "gpt-5.6-luna", "_usage": {}}

    monkeypatch.setattr(aa, "DATA_FILE", data)
    monkeypatch.setattr(aa, "CONTENT_DIR", content)
    monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
    monkeypatch.setattr(aa, "_analyze_with_fallback", fake)
    monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
    rc = aa.main()
    return rc, {r["id"]: r for r in (json.loads(l) for l in data.read_text().splitlines())}, calls


def _art(aid, sid="oaktree", title=None, **kw):
    return dict({"id": aid, "source_id": sid, "title": title or aid, "url": f"https://x/{aid}",
                 "date": "2026-04-09", "content_status": "ok", "summarized": False}, **kw)


def test_a_body_already_summarised_for_another_article_is_declined(tmp_path, monkeypatch):
    done = _art("cockroaches", title="Cockroaches in the Coal Mine", summarized=True, summary_en="s")
    new = _art("private-credit", title="What's Going on in Private Credit?")
    rc, rows, calls = _run(tmp_path, monkeypatch, [done, new], {"cockroaches": BODY, "private-credit": BODY})
    assert calls == [], "the same text was sent for summarisation a second time"
    row = rows["private-credit"]
    assert row["summarized"] is False and row["analysis_status"] == "insufficient_content"
    assert "Cockroaches in the Coal Mine" in row["analysis_reason"]
    assert rc in (0, None)


def test_whitespace_differences_do_not_hide_a_duplicate(tmp_path, monkeypatch):
    done = _art("a", summarized=True)
    new = _art("b")
    _, rows, calls = _run(tmp_path, monkeypatch, [done, new], {"a": BODY, "b": BODY.replace(" ", "  \n")})
    assert calls == [] and rows["b"]["analysis_status"] == "insufficient_content"


def test_two_pending_copies_in_one_run_summarise_only_the_first(tmp_path, monkeypatch):
    _, rows, calls = _run(tmp_path, monkeypatch, [_art("a"), _art("b")], {"a": BODY, "b": BODY})
    assert calls == ["a"]
    assert rows["a"]["summarized"] is True
    assert rows["b"]["analysis_status"] == "insufficient_content"


def test_the_same_text_at_another_source_is_not_a_duplicate(tmp_path, monkeypatch):
    done = _art("a", sid="oaktree", summarized=True)
    new = _art("b", sid="gmo")
    _, rows, calls = _run(tmp_path, monkeypatch, [done, new], {"a": BODY, "b": BODY})
    assert calls == ["b"]


def test_a_declined_article_does_not_block_a_later_one(tmp_path, monkeypatch):
    """Only a published summary counts: a declined copy published nothing."""
    declined = _art("a", analysis_status="insufficient_content", analysis_reason="r")
    new = _art("b")
    _, rows, calls = _run(tmp_path, monkeypatch, [declined, new], {"a": BODY, "b": BODY})
    assert calls == ["b"]


def test_different_bodies_are_analysed(tmp_path, monkeypatch):
    done = _art("a", summarized=True)
    _, rows, calls = _run(tmp_path, monkeypatch, [done, _art("b")], {"a": BODY, "b": BODY + " Addendum."})
    assert calls == ["b"]
