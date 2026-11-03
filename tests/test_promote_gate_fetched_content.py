"""Auto-promote's Haiku gate scores what the source's own content fetcher keeps.

2026-11-01/02: CFM failed the gate twice (avg 0.43 < 0.5) because the generic
sampler's newest three included two podcast announcements that CFM's content
fetcher refuses (141 chars) -- items production lists title-only and never
summarises. The user decided (2026-11-01) that GMIA is text-first: podcasts
keep their title, their content is for a human to open. So the gate now runs
the Phase 3 content fetcher over the listing and scores the first three
articles it keeps with a full body.

2026-11-03: the first version (41a6a2b) called the fetcher as
``fetcher(article, dir) -> {"ok", "path", "chars"}`` -- a shape no fetcher in
fetch_content has -- and these tests used a fake of that same shape, so they
passed while the 11-03 auto-promote session had to write an adapter to run the
real ``_fetch_content_cfm``. The fakes below now follow the production contract
(``fetcher(article) -> (path, status) | None``, body written to
``fetch_content.CONTENT_DIR``), and one test drives the real CFM fetcher.
"""
import importlib.util
from pathlib import Path

import pytest

import fetch_content as fc

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gmia_trial_manager_gate",
                                               ROOT / "gmia-trial-manager.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

BODY = "Substantive macro research paragraph. " * 40   # ~1,500 chars


@pytest.fixture(autouse=True)
def stand_in_content_dir(monkeypatch, tmp_path):
    """Point the "production" CONTENT_DIR at a temp dir for every test, so a gate
    that stops isolating fails a test instead of writing into the real content/
    (2026-11-03: a mutation run with the isolation removed left 11 test bodies
    there)."""
    prod = tmp_path / "production-content"
    prod.mkdir()
    monkeypatch.setattr(fc, "CONTENT_DIR", prod)
    return prod


def _articles(*names):
    return [{"id": f"id-{n}", "url": f"https://fund.example/{n}", "title": n} for n in names]


def _fetcher(bodies: dict, status: str = "ok"):
    """Fake with the production contract: name -> body text, or None for a refusal.
    Writes where every real extractor writes: fetch_content.CONTENT_DIR at call time."""
    def fetch(article):
        name = article["url"].rsplit("/", 1)[1]
        body = bodies.get(name)
        if body is None:
            return None
        path = fc.CONTENT_DIR / f"{article['id']}.txt"
        path.write_text(body, encoding="utf-8")
        return (path, status)
    return fetch


def _haiku_recorder(calls):
    def haiku(prompt):
        calls.append(prompt)
        n = prompt.count("--- Article ")
        return {"articles": [{"article_num": i, "relevance": 0.9, "depth": 0.8,
                              "extractable": 0.9, "notes": f"a{i}"} for i in range(1, n + 1)]}
    return haiku


def test_items_the_content_fetcher_refuses_are_not_scored(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    arts = _articles("rally", "pod-2", "pod-1", "outlook", "credit")
    fetch = _fetcher({"rally": BODY, "outlook": BODY, "credit": BODY})

    res = tm.sample_fetched_content(arts, fetch)

    assert [a["url"].rsplit("/", 1)[1] for a in res["articles"]] == ["rally", "outlook", "credit"]
    assert [u.rsplit("/", 1)[1] for u, _ in res["skipped"]] == ["pod-2", "pod-1"]
    assert res["avg_score"] >= 0.5 and res["error"] is None
    assert len(calls) == 1 and "pod-1" not in calls[0]


def test_real_cfm_fetcher_runs_unadapted(monkeypatch):
    """The production extractor goes straight in: podcast stub refused, disclaimer
    block dropped, articles scored."""
    article_html = ("<html><body>"
                    + "<div class='text-content'><p>" + BODY + "</p></div>"
                    + "<div class='text-content'><p>Disclaimer: past performance is not a guide.</p></div>"
                    + "</body></html>")
    podcast_html = ("<html><body><div class='text-content'><p>Philip Seager speaks on the "
                    "Most Important Thing podcast. Listen here.</p></div></body></html>")

    class _Resp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    monkeypatch.setattr(fc.requests, "get",
                        lambda url, **kw: _Resp(podcast_html if "podcast" in url else article_html))
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    arts = [{"id": f"cfm-{n}", "url": f"https://www.cfm.com/{n}/", "title": n}
            for n in ("post-midterm-rally", "seager-podcast", "midterms", "liquidity")]

    res = tm.sample_fetched_content(arts, fc._fetch_content_cfm)

    assert res["error"] is None and res["sampled"] == 3
    assert [u for u, _ in res["skipped"]] == ["https://www.cfm.com/seager-podcast/"]
    assert "Disclaimer" not in calls[0]


def test_bodies_go_to_a_scratch_dir_never_the_production_content_dir(monkeypatch, stand_in_content_dir):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    prod_dir = stand_in_content_dir
    written = []
    inner = _fetcher(dict.fromkeys("abc", BODY))

    def fetch(article):
        res = inner(article)
        written.append(res[0])
        return res

    tm.sample_fetched_content(_articles(*"abc"), fetch)

    assert fc.CONTENT_DIR == prod_dir
    assert written and all(prod_dir not in p.parents for p in written)
    assert not any(p.exists() for p in written)          # scratch dir is gone afterwards
    assert list(prod_dir.iterdir()) == []


def test_metadata_only_and_crashing_fetches_are_skipped(monkeypatch):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    ok = _fetcher(dict.fromkeys("abc", BODY))
    meta = _fetcher({"m": BODY}, status="metadata_only")

    def fetch(article):
        name = article["url"].rsplit("/", 1)[1]
        if name == "boom":
            raise RuntimeError("parser blew up")
        return meta(article) if name == "m" else ok(article)

    res = tm.sample_fetched_content(_articles("m", "boom", "a", "b", "c"), fetch)

    assert res["error"] is None
    assert [u.rsplit("/", 1)[1] for u, _ in res["skipped"]] == ["m", "boom"]
    assert "metadata_only" in res["skipped"][0][1] and "RuntimeError" in res["skipped"][1][1]


def test_a_body_the_fetcher_accepts_but_under_500_chars_is_not_scored(monkeypatch):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    arts = _articles("teaser", "a", "b", "c")
    fetch = _fetcher({"teaser": "In this episode we discuss rates. " * 5, "a": BODY, "b": BODY, "c": BODY})

    res = tm.sample_fetched_content(arts, fetch)

    assert [a["url"].rsplit("/", 1)[1] for a in res["articles"]] == ["a", "b", "c"]
    assert res["skipped"][0][0].endswith("/teaser")


def test_stops_fetching_once_three_bodies_are_in_hand(monkeypatch):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    seen = []
    inner = _fetcher({n: BODY for n in "abcdef"})

    def fetch(article):
        seen.append(article["url"])
        return inner(article)

    tm.sample_fetched_content(_articles(*"abcdef"), fetch)

    assert len(seen) == 3


def test_fewer_than_three_full_text_articles_fails_without_asking_haiku(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    arts = _articles("pod-1", "a", "pod-2", "b")

    res = tm.sample_fetched_content(arts, _fetcher({"a": BODY, "b": BODY}))

    assert calls == []
    assert res["avg_score"] == 0.0 and res["sampled"] == 2
    assert "2 of 4" in res["error"]


def test_scored_text_is_capped_like_the_generic_sampler(monkeypatch):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    long_body = "x" * 9000

    tm.sample_fetched_content(_articles("a", "b", "c"), _fetcher(dict.fromkeys("abc", long_body)))

    assert "x" * 3001 not in calls[0]
