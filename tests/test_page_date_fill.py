"""Stage 2 takes the publication date from the article page when the listing had none.

Stage-4 audit (2026-10-09), items 7/15: Capital Group's 25 rows have no date
(its listing shows none) and de-shaw's 10 have only a year (stored as
YYYY-01-01). Both sank to the bottom of the page and never counted as new.
The article pages carry the date: Capital Group, once rendered,
`<meta name="article-date" content="04-06-2026 12:00">` (day first:
30-09-2026 exists), de-shaw `article:published_time`. A plain download of a
Capital Group page is a 7KB shell without it, so the date is read where stage 2
already renders the page (user's call: option A).

Only a date that is empty or year-only is filled, a year-only one only within
its year, never later than a week after we first saw the row, and `date_raw`
is never touched (fetch_articles compares the next listing with it).
"""
import json
import sys

import pytest

import fetch_content as fc

CG_HTML = ('<html><head><meta name="article-date" data-type="date" content="04-06-2026 12:00">'
           '</head><body><div class="cmp-text"><p>' + "Capital Group body text. " * 40
           + "</p></div></body></html>")
DS_HTML = ('<html><head><meta property="article:published_time" content="2026-02-01"/></head>'
           '<body><div class="Blogs_body"><p>' + "D. E. Shaw body text. " * 40
           + "</p></div></body></html>")


@pytest.mark.parametrize("html,expected", [
    (CG_HTML, "2026-06-04"),
    ('<meta content="30-09-2026" name="article-date">', "2026-09-30"),     # either attribute order
    (DS_HTML, "2026-02-01"),
    ('<meta content="2025-08-27T00:00:00Z" property="article:published_time">', "2025-08-27"),
    ('<meta name="article-date" content="31-02-2026">', None),              # no such day
    ("<p>published 4 June 2026</p>", None),                                 # body text is not a date field
    ("", None),
])
def test_page_date(html, expected):
    assert fc.page_date(html) == expected


# ── the extractors note what the page said ────────────────────────────────────

class _Resp:
    status_code = 200
    headers = {"Content-Type": "text/html"}
    url = "https://www.deshaw.com/library/x"

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_de_shaw_extractor_reports_the_page_date(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: _Resp(DS_HTML))
    result, evidence = fc.fetch_with_evidence({"id": "ds1", "url": _Resp.url, "source_id": "de-shaw"},
                                              fc._fetch_content_de_shaw)
    assert result is not None and evidence["page_date"] == "2026-02-01"


def test_capital_group_extractor_reports_the_page_date(tmp_path, monkeypatch):
    import playwright.sync_api

    class _Page:
        def goto(self, *a, **k): pass
        def wait_for_timeout(self, *a): pass
        def content(self): return CG_HTML

    class _Browser:
        def new_context(self, **k): return self
        def new_page(self): return _Page()
        def close(self): pass

    class _PW:
        chromium = type("C", (), {"launch": staticmethod(lambda **k: _Browser())})
        def __enter__(self): return self
        def __exit__(self, *e): pass

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    result, evidence = fc.fetch_with_evidence(
        {"id": "cg1", "url": "https://www.capitalgroup.com/x.html", "source_id": "capital-group"},
        fc._fetch_content_capital_group)
    assert result is not None and evidence["page_date"] == "2026-06-04"


def test_evidence_never_carries_a_date_from_the_previous_article(tmp_path, monkeypatch):
    fc.note_page_date(DS_HTML)                               # left over from somewhere else
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    _, evidence = fc.fetch_with_evidence({"id": "x"}, lambda a: None)
    assert evidence["page_date"] is None


# ── main() fills only what it should ─────────────────────────────────────────

SEEN = "2026-06-10T03:48:29+08:00"


def _run(tmp_path, monkeypatch, row, page_day):
    content = tmp_path / "content"
    content.mkdir()
    data = tmp_path / "articles.jsonl"
    data.write_text(json.dumps(row) + "\n")

    def fetcher(article):
        if page_day:
            fc._page_dates.append(page_day)
        path = fc.CONTENT_DIR / f"{article['id']}.txt"
        fc._atomic_write(path, ("Body of the article. " * 40).encode("utf-8"))
        return (path, "ok")

    monkeypatch.setattr(fc, "DATA_FILE", data)
    monkeypatch.setattr(fc, "BASE_DIR", tmp_path)
    monkeypatch.setattr(fc, "CONTENT_DIR", content)
    monkeypatch.setitem(fc.CONTENT_FETCHERS, row["source_id"], fetcher)
    monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
    fc.main()
    return json.loads(data.read_text().splitlines()[0])


def _row(**kw):
    return dict({"id": "r1", "source_id": "capital-group", "title": "Outlook", "url": "https://x/1",
                 "date": None, "date_raw": "", "fetched_at": SEEN, "summarized": False}, **kw)


def test_an_undated_row_gets_the_page_date(tmp_path, monkeypatch):
    row = _run(tmp_path, monkeypatch, _row(), "2026-06-04")
    assert row["date"] == "2026-06-04" and row["date_basis"] == "page"
    assert row["date_raw"] == "" and row["date_listed"] is None
    assert row["content_status"] == "ok"


def test_a_year_only_row_gets_a_day_inside_its_year(tmp_path, monkeypatch):
    row = _run(tmp_path, monkeypatch,
               _row(source_id="de-shaw", date="2026-01-01", date_raw="2026"), "2026-02-01")
    assert (row["date"], row["date_basis"], row["date_listed"], row["date_raw"]) == \
        ("2026-02-01", "page", "2026-01-01", "2026")


@pytest.mark.parametrize("row,page_day,why", [
    (_row(source_id="de-shaw", date="2025-01-01", date_raw="2025"), "2026-02-01", "another year"),
    (_row(), "2026-06-20", "more than a week after we first saw it"),
    (_row(source_id="de-shaw", date="2026-05-12", date_raw="May 12, 2026"), "2026-05-11", "a listing day"),
    (_row(source_id="de-shaw", date="2026-01-01", date_raw="2026-01-01"), "2026-02-01", "a real 1 January"),
    (_row(date="2026-06-04", date_basis="page"), "2026-06-01", "already refined"),
    (_row(source_id="de-shaw", date="2026-01-01", date_raw="2026", date_basis="first_seen"), "2026-02-01",
     "already refined, year-only"),
    (_row(source_id="de-shaw", date="2026-03-05", date_raw="2026"), "2026-02-01",
     "a year-only label someone already gave a day"),
    (_row(), None, "the page had none"),
])
def test_other_rows_keep_their_date(tmp_path, monkeypatch, row, page_day, why):
    before = dict(row)
    after = _run(tmp_path, monkeypatch, row, page_day)
    for k in ("date", "date_raw", "date_basis", "date_listed"):
        assert after.get(k) == before.get(k), (why, k)
    assert after["content_status"] == "ok"


# ── the one-off backfill: propose, review, apply exactly that ────────────────

def _bf():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "backfill_page_dates", Path(__file__).resolve().parent.parent / "scripts" / "backfill_page_dates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_proposes_then_applies_only_the_reviewed_list(tmp_path, monkeypatch):
    bf = _bf()
    rows = [_row(id="cg1"), _row(id="cg2"),
            _row(id="ds1", source_id="de-shaw", date="2026-01-01", date_raw="2026"),
            _row(id="ds2", source_id="de-shaw", date="2026-03-05", date_raw="March 5, 2026"),
            _row(id="gmo", source_id="gmo")]
    data = tmp_path / "articles.jsonl"
    data.write_text("".join(json.dumps(r) + "\n" for r in rows))
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(fc, "CONTENT_DIR", content)
    pages = {"cg1": "2026-06-04", "cg2": None, "ds1": "2026-02-01"}
    asked = []

    def fetch(row, fetcher):
        asked.append(row["id"])
        (fc.CONTENT_DIR / f"{row['id']}.txt").write_text("re-fetched")   # must not reach content/
        return None, {"page_date": pages[row["id"]]}

    proposal = tmp_path / "proposal.json"
    found = bf.propose(data, proposal, "2026-10-10", fetch=fetch)
    assert asked == ["cg1", "cg2", "ds1"]
    assert list(content.iterdir()) == []
    assert {p["id"]: (p["new"] or {}).get("date") for p in found} == {"cg1": "2026-06-04", "cg2": None,
                                                                       "ds1": "2026-02-01"}
    assert data.read_text() == "".join(json.dumps(r) + "\n" for r in rows), "propose must not write"

    # ds1 got a date some other way after the list was made: leave it alone.
    changed = [dict(r, date="2026-02-03", date_basis="page") if r["id"] == "ds1" else r for r in rows]
    data.write_text("".join(json.dumps(r) + "\n" for r in changed))
    written, skipped = bf.apply(data, proposal)
    assert (written, skipped) == (1, 1)
    after = {r["id"]: r for r in map(json.loads, data.read_text().splitlines())}
    assert (after["cg1"]["date"], after["cg1"]["date_basis"]) == ("2026-06-04", "page")
    assert after["ds1"]["date"] == "2026-02-03"
    assert after["cg2"].get("date") is None and after["ds2"] == changed[3] and after["gmo"] == changed[4]
