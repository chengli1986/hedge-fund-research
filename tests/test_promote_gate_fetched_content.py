"""Auto-promote's Haiku gate scores what the source's own content fetcher keeps.

2026-11-01/02: CFM failed the gate twice (avg 0.43 < 0.5) because the generic
sampler's newest three included two podcast announcements that CFM's content
fetcher refuses (141 chars) -- items production lists title-only and never
summarises. The user decided (2026-11-01) that GMIA is text-first: podcasts
keep their title, their content is for a human to open. So the gate now runs
the Phase 3 content fetcher over the listing and scores the first three
articles it keeps with a full body.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gmia_trial_manager_gate",
                                               ROOT / "gmia-trial-manager.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

BODY = "Substantive macro research paragraph. " * 40   # ~1,500 chars


def _articles(*names):
    return [{"url": f"https://fund.example/{n}", "title": n} for n in names]


def _fetcher(bodies: dict):
    """Fake content fetcher: name -> body text, or None for a refusal."""
    def fetch(article, content_dir):
        name = article["url"].rsplit("/", 1)[1]
        body = bodies.get(name)
        if body is None:
            return {"ok": False, "error": "Content too short (141 chars)", "skipped": False}
        path = Path(content_dir) / f"{name}.txt"
        path.write_text(body, encoding="utf-8")
        return {"ok": True, "path": str(path), "chars": len(body), "skipped": False}
    return fetch


def _haiku_recorder(calls):
    def haiku(prompt):
        calls.append(prompt)
        n = prompt.count("--- Article ")
        return {"articles": [{"article_num": i, "relevance": 0.9, "depth": 0.8,
                              "extractable": 0.9, "notes": f"a{i}"} for i in range(1, n + 1)]}
    return haiku


def test_items_the_content_fetcher_refuses_are_not_scored(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    arts = _articles("rally", "pod-2", "pod-1", "outlook", "credit")
    fetch = _fetcher({"rally": BODY, "outlook": BODY, "credit": BODY})

    res = tm.sample_fetched_content(arts, fetch, tmp_path)

    assert [a["url"].rsplit("/", 1)[1] for a in res["articles"]] == ["rally", "outlook", "credit"]
    assert [u.rsplit("/", 1)[1] for u, _ in res["skipped"]] == ["pod-2", "pod-1"]
    assert res["avg_score"] >= 0.5 and res["error"] is None
    assert len(calls) == 1 and "pod-1" not in calls[0]


def test_a_body_the_fetcher_accepts_but_under_500_chars_is_not_scored(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    arts = _articles("teaser", "a", "b", "c")
    fetch = _fetcher({"teaser": "In this episode we discuss rates. " * 5, "a": BODY, "b": BODY, "c": BODY})

    res = tm.sample_fetched_content(arts, fetch, tmp_path)

    assert [a["url"].rsplit("/", 1)[1] for a in res["articles"]] == ["a", "b", "c"]
    assert res["skipped"][0][0].endswith("/teaser")


def test_stops_fetching_once_three_bodies_are_in_hand(monkeypatch, tmp_path):
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder([]))
    seen = []
    inner = _fetcher({n: BODY for n in "abcdef"})
    def fetch(article, content_dir):
        seen.append(article["url"])
        return inner(article, content_dir)

    tm.sample_fetched_content(_articles(*"abcdef"), fetch, tmp_path)

    assert len(seen) == 3


def test_fewer_than_three_full_text_articles_fails_without_asking_haiku(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    arts = _articles("pod-1", "a", "pod-2", "b")

    res = tm.sample_fetched_content(arts, _fetcher({"a": BODY, "b": BODY}), tmp_path)

    assert calls == []
    assert res["avg_score"] == 0.0 and res["sampled"] == 2
    assert "2 of 4" in res["error"]


def test_scored_text_is_capped_like_the_generic_sampler(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(tm, "_call_haiku", _haiku_recorder(calls))
    long_body = "x" * 9000

    tm.sample_fetched_content(_articles("a", "b", "c"), _fetcher(dict.fromkeys("abc", long_body)), tmp_path)

    assert "x" * 3001 not in calls[0]
