"""A new issue at a reused URL waits for its page to change (2026-10-06).

gsam's "Corporate Pension Monthly" lives at one URL. On 2026-10-02 the
listing showed a new issue, stage 2 fetched the page, and the page still
carried the September issue's text -- the site updated its listing date
days before the article. Stage 3 saw a body identical to the published
09-02 issue, hid it as duplicate_body, and nothing ever asked again; by
10-06 the page held the real new issue ("In September ... 114.2%"), which
the store never got.

Stage 2 now holds such a body back as page_not_updated and re-fetches the
next night, for up to PAGE_UPDATE_WAIT_NIGHTS nights; after that it stores
the body and stage 3 decides, as before. "The same" is text_identity's
definition, which stage 3's duplicate check also uses.
"""
import json
import sys

import pytest

import failure_labels as fl
import fetch_content as fc
import text_identity

SRC = "gsam"
URL = "https://am.gs.com/insights/article/corporate-pension-monthly"
AUGUST = ("In August, our estimate of the aggregate corporate defined benefit funded "
          "status increased to 112.2%, up from 111.8% in July. ") * 8
SEPTEMBER = ("In September, our estimate of the aggregate corporate defined benefit "
             "funded status increased to 114.2%, up from 112.2% in August. ") * 8


def _run(tmp_path, monkeypatch, new_row, fetched_text, prev_text=AUGUST, prev_url=URL):
    content = tmp_path / "content"
    content.mkdir()
    prev = {"id": "prev0902", "source_id": SRC, "title": "Corporate Pension Monthly", "url": prev_url,
            "date": "2026-09-02", "content_status": "ok", "summarized": True,
            "content_path": "content/prev0902.txt"}
    (content / "prev0902.txt").write_text(prev_text, encoding="utf-8")
    data = tmp_path / "articles.jsonl"
    data.write_text(json.dumps(prev) + "\n" + json.dumps(new_row) + "\n")

    def fetcher(article):
        path = fc.CONTENT_DIR / f"{article['id']}.txt"
        fc._atomic_write(path, fetched_text.encode("utf-8"))
        return (path, "ok")

    monkeypatch.setattr(fc, "DATA_FILE", data)
    monkeypatch.setattr(fc, "BASE_DIR", tmp_path)       # content_path is stored relative to it
    monkeypatch.setattr(fc, "CONTENT_DIR", content)
    monkeypatch.setitem(fc.CONTENT_FETCHERS, SRC, fetcher)
    monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
    fc.main()
    rows = {r["id"]: r for r in (json.loads(l) for l in data.read_text().splitlines())}
    return rows["new1002"], content


def _new(**kw):
    return dict({"id": "new1002", "source_id": SRC, "title": "Corporate Pension Monthly", "url": URL,
                 "date": "2026-10-02", "summarized": False}, **kw)


def test_the_previous_issues_text_is_held_back_and_retried(tmp_path, monkeypatch):
    row, content = _run(tmp_path, monkeypatch, _new(), AUGUST)
    assert row["content_status"] == "failed"
    assert row["content_failure"]["label"] == "page_not_updated"
    assert row.get("content_retry_after"), "it must be scheduled for another try"
    assert not (content / "new1002.txt").exists(), "the stale body must not be kept"


def test_a_page_that_has_changed_is_stored(tmp_path, monkeypatch):
    row, content = _run(tmp_path, monkeypatch, _new(), SEPTEMBER)
    assert row["content_status"] == "ok"
    assert (content / "new1002.txt").read_text() == SEPTEMBER


def test_a_near_identical_page_under_the_same_title_also_waits(tmp_path, monkeypatch):
    """Stage 3 would hide this one too (Jaccard >= 0.85, same title)."""
    row, _ = _run(tmp_path, monkeypatch, _new(), AUGUST + " Updated 2 October.")
    assert row["content_failure"]["label"] == "page_not_updated"


def test_a_retitled_listing_whose_page_still_shows_the_last_issue_waits(tmp_path, monkeypatch):
    """The listing renamed the issue but the page still carries the old text."""
    row, _ = _run(tmp_path, monkeypatch, _new(title="Corporate Pension Monthly: October"),
                  AUGUST * 4 + " Oct.", prev_text=AUGUST * 4)
    assert row["content_failure"]["label"] == "page_not_updated"


def test_the_same_text_at_another_url_is_left_to_stage_3(tmp_path, monkeypatch):
    """Two URLs with one body is stage 3's duplicate case, not a stale page."""
    row, _ = _run(tmp_path, monkeypatch, _new(), AUGUST, prev_url=URL + "-archive")
    assert row["content_status"] == "ok"


def test_after_the_wait_the_body_is_stored_and_stage_3_decides(tmp_path, monkeypatch):
    waited = _new(content_status="failed", content_attempts=fl.PAGE_UPDATE_WAIT_NIGHTS,
                  content_failure={"label": "page_not_updated", "detail": "d",
                                   "streak": fl.PAGE_UPDATE_WAIT_NIGHTS})
    row, content = _run(tmp_path, monkeypatch, waited, AUGUST)
    assert row["content_status"] == "ok"
    assert (content / "new1002.txt").exists()


def test_the_policy_outlasts_the_wait():
    """The label must never retire to permafail before the wait is over."""
    assert fl.RETRY_POLICY["page_not_updated"]["max_attempts"] > fl.PAGE_UPDATE_WAIT_NIGHTS
    assert fl.RETRY_POLICY["page_not_updated"]["code_dependent"] is False


@pytest.mark.parametrize("text,other,title,other_title", [
    (AUGUST, AUGUST, "A", "B"),                          # exact: titles do not matter
    (AUGUST, "  " + AUGUST.replace(". ", ".\n"), "A", "B"),   # whitespace only
    (AUGUST + " x", AUGUST, "T", "T"),                   # near, same title
    (AUGUST + " x", AUGUST, "T", "U"),                   # near, different title
    (SEPTEMBER, AUGUST, "T", "T"),                       # different issue
    ("short text", "short text!", "T", "T"),             # too short to compare
    (AUGUST * 4 + " x", AUGUST * 4, "T", "U"),           # near, retitled, long enough
    ("", "", "T", "T"),                                  # nothing to compare
    ("日本株の見通し。" * 80, "円相場の展望。" * 80, "", ""),   # CJK text is compared on itself
])
def test_same_document_agrees_with_stage_3s_duplicate_check(text, other, title, other_title):
    """One definition, two callers: the pair verdict must match duplicate_owner."""
    import analyze_articles as aa
    other_row = {"id": "o1", "source_id": SRC, "title": other_title, "summarized": True}
    index = aa.published_index([other_row], bodies={"o1": other})
    stage3 = aa.duplicate_owner(index, SRC, title, text) is not None
    assert text_identity.same_document(text, other, title, other_title) is stage3
