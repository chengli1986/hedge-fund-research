"""Muzinich podcast pages are listed title-only (user 2026-11-01) and the
fetcher refuses them without a request. 2026-11-08, first ingest: the refusal
left no evidence, so "Stronger Together: The Power of Parallel Lending"
(/opinions/podcast-...) was filed as unknown -- code-dependent, retried daily,
and shown as unexplained in the health mail. The fetcher knows what the page
is and says so through a failure hint, as GMO's in-the-news pages do."""
import failure_labels as fl
import fetch_content as fc

PODCAST = ("https://www.muzinich.com/opinions/podcast-stronger-together-the-power-of-parallel-lending"
           "?utm_source=Insight&utm_medium=Website")


def test_a_podcast_page_is_media_without_text(monkeypatch):
    asked = []
    monkeypatch.setattr(fc.requests, "get", lambda url, **k: asked.append(url))
    article = {"id": "mz-podcast", "title": "Stronger Together", "url": PODCAST}
    result, evidence = fc.fetch_with_evidence(article, fc._fetch_content_muzinich)
    assert result is None
    assert asked == []
    assert fl.classify_content_failure(evidence)[0] == "media_without_text"
