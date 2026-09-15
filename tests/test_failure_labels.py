"""Failure labels: one taxonomy for why an article has no body or no summary.

Until 2026-09-15 every content failure was the same status ("failed" /
"permafail") and every stage-3 decline a free-text reason, so a video page,
a deleted page, a dead selector and a Cloudflare block were indistinguishable
and nothing could be counted. The cases below are the evidence actually seen
while fixing sources this week.
"""
import pytest

import failure_labels as fl


def ev(**kw):
    base = {"exception": None, "messages": [], "responses": [], "extraction_paths": []}
    base.update(kw)
    return base


def resp(status=200, url="https://site.com/insights/a-piece/", final=None, challenge=False, media=False,
         content_type="text/html"):
    return {"status": status, "url": url, "final_url": final or url, "content_type": content_type,
            "challenge": challenge, "media_player": media}


class TestContentFailure:
    def test_cloudflare_challenge_is_blocked(self):
        # ARK articles: 403 "Just a moment..." from requests and headless Chromium
        e = ev(responses=[resp(403, "https://www.ark-invest.com/articles/analyst-research/x", challenge=True)])
        assert fl.classify_content_failure(e)[0] == "blocked_by_bot_protection"

    def test_a_challenge_served_with_200_is_blocked(self):
        # ARK "Stock Stories": 200 "Enable JavaScript and cookies to continue"
        e = ev(responses=[resp(200, challenge=True)])
        assert fl.classify_content_failure(e)[0] == "blocked_by_bot_protection"

    def test_404_is_page_gone(self):
        # lazard Behind the Headlines Dec-Jan issues; a metlife slug
        assert fl.classify_content_failure(ev(responses=[resp(404)]))[0] == "page_gone"

    @pytest.mark.parametrize("final", [
        "https://www.metlife.com/investments/en-us/",            # metlife: redirected to the site home
        "https://www.metlife.com/investments/en-us/insights/",   # ...or to the insights index
    ])
    def test_redirect_to_home_or_index_is_page_gone(self, final):
        url = "https://www.metlife.com/investments/en-us/insights/investment-perspectives/japan-equities/"
        label, detail = fl.classify_content_failure(ev(responses=[resp(200, url, final)]))
        assert label == "page_gone" and final in detail

    def test_a_cross_domain_redirect_to_the_new_site_home_is_page_gone(self):
        # metlife: pinebridge.com article -> 308/301 x3 -> metlife.com/investments/en-us/
        url = "https://www.pinebridge.com/en/insights/japans-reflation-experiment-boj-hikes"
        label, _ = fl.classify_content_failure(ev(responses=[resp(200, url, "https://www.metlife.com/investments/en-us/")]))
        assert label == "page_gone"

    @pytest.mark.parametrize("url,final", [
        # research-affiliates -> syzygy: same article under the new domain
        ("https://www.researchaffiliates.com/publications/articles/1122-moat-or-mirage",
         "https://www.syzygyassetmanagement.com/insights/publications/articles/1122-moat-or-mirage"),
        ("https://www.researchaffiliates.com/publications/articles/1118-growth-without-price-distortion",
         "https://www.rafi.com/research/publications/articles/1118-growth-without-price-distortion"),
    ])
    def test_a_domain_move_to_the_same_article_is_not_page_gone(self, url, final):
        label, _ = fl.classify_content_failure(ev(responses=[resp(200, url, final)], extraction_paths=["primary"]))
        assert label != "page_gone"

    def test_a_redirect_to_another_article_is_not_page_gone(self):
        url = "https://www.apollo.com/insights/the-view-from-apollo/2026/07/hybrid"
        final = "https://www.apollo.com/insights/podcast/the-allocation/2026/07/hybrid"
        label, _ = fl.classify_content_failure(ev(responses=[resp(200, url, final)], extraction_paths=["primary"]))
        assert label != "page_gone"

    def test_off_primary_extraction_is_selector_miss(self):
        e = ev(responses=[resp(200)], extraction_paths=["fallback:main"])
        assert fl.classify_content_failure(e)[0] == "selector_miss"

    def test_a_short_page_with_a_media_player_is_media_without_text(self):
        # matthews/apollo/robeco video and podcast pages
        e = ev(responses=[resp(200, media=True)], extraction_paths=["primary"],
               messages=["Matthews Asia: extracted text too short (324 chars, min 500)"])
        assert fl.classify_content_failure(e)[0] == "media_without_text"

    def test_short_text_without_a_player_is_body_too_short(self):
        e = ev(responses=[resp(200)], extraction_paths=["primary"], messages=["X: extracted text too short (80 chars)"])
        assert fl.classify_content_failure(e)[0] == "body_too_short"

    @pytest.mark.parametrize("msg", [
        "Cambridge: Playwright fetch failed: Page.goto: Timeout 30000ms exceeded.",
        "GMO: failed to fetch article page: HTTPSConnectionPool(host='x'): Read timed out.",
    ])
    def test_timeouts_and_errors_are_fetch_error(self, msg):
        assert fl.classify_content_failure(ev(messages=[msg]))[0] == "fetch_error"

    def test_an_exception_is_fetch_error_with_its_text(self):
        label, detail = fl.classify_content_failure(ev(exception="ValueError: boom"))
        assert label == "fetch_error" and "boom" in detail

    def test_5xx_is_fetch_error(self):
        assert fl.classify_content_failure(ev(responses=[resp(502)]))[0] == "fetch_error"

    def test_an_explicit_hint_wins(self):
        # troweprice soft 404; a linked PDF that is not this article
        e = ev(responses=[resp(200)], extraction_paths=["primary"], hints=[("pdf_not_usable", "title mismatch")])
        assert fl.classify_content_failure(e) == ("pdf_not_usable", "title mismatch")

    def test_every_label_returned_is_defined(self):
        cases = [ev(), ev(responses=[resp(403, challenge=True)]), ev(responses=[resp(404)]),
                 ev(extraction_paths=["whole-page"]), ev(responses=[resp(200, media=True)])]
        for e in cases:
            assert fl.classify_content_failure(e)[0] in fl.CONTENT_FAILURE_LABELS


def test_a_client_rendered_page_hint_is_its_own_label():
    # gsam: body rendered in the browser, no API summary to stand in
    e = ev(responses=[resp(200)], hints=[("body_rendered_client_side", "no body in HTML, API summary empty")])
    assert fl.classify_content_failure(e)[0] == "body_rendered_client_side"
    assert "body_rendered_client_side" in fl.CONTENT_FAILURE_LABELS


class TestResponseFacts:
    @pytest.mark.parametrize("html,expected", [
        ("<title>Just a moment...</title>", True),
        ("<p>Enable JavaScript and cookies to continue</p>", True),
        ("<title>Attention Required! | Cloudflare</title>", True),
        ("<p>Cockroaches in the coal mine</p>", False),
    ])
    def test_challenge_markers(self, html, expected):
        assert fl.looks_like_challenge(html) is expected

    @pytest.mark.parametrize("html,expected", [
        ('<a class="item" data-video="6403596113112">', True),
        ('<video-js class="vjs-tech">', True),
        ('<iframe src="https://players.brightcove.net/x">', True),
        ('<div class="podcast-player">', True),
        ('<p>We discuss video games as an industry.</p>', False),
        ('"brightcoveVideoId":"6390000000112"', True),                                     # apollo episode pages
        ('<iframe src="https://html5-player.libsyn.com/embed/episode/id/41306325/">', True), # robeco podcast
        ('<a href="https://www.youtube.com/c/robecoassetmanagement">YouTube</a>', False),   # footer link on every page
    ])
    def test_media_player_markers(self, html, expected):
        assert fl.has_media_player(html) is expected


class TestAnalysisDecline:
    @pytest.mark.parametrize("reason,label", [
        ("metadata holds only a title; nothing to summarise", "title_only"),
        ('same text as the already-summarised article "Part 1: What Barbarians" (4a); the page served', "duplicate_body"),
        ("failed grounding check: coverage 0.11 < 0.25", "grounding_failed"),
        ("The text consists of a brief series introduction and legal disclaimer; it does not include", "disclaimer_only"),
        ("The provided text consists solely of legal, educational-use, investment-risk, and disclosure disclaimers", "disclaimer_only"),
        ("The text is primarily website navigation, publication metadata, a registration/login wall", "navigation_or_login_wall"),
        ("The text consists only of repeated chart source notes and an 'For illustrative purposes only'", "chart_notes_only"),
        ("The text is a brief video description and scheduling note; it does not provide", "teaser_only"),
        ("The text is only a promotional link to another white paper", "teaser_only"),
        ("The text is an important health coverage tax document and GMO contact information", "wrong_document"),
        ("The text is a broker-dealer relationship summary and regulatory disclosure", "wrong_document"),
        ("the page has no article text and its download button serves a different, earlier paper", "wrong_document"),
        ("something else entirely", "other_declined"),
    ])
    def test_reason_to_label(self, reason, label):
        assert fl.classify_analysis_decline(reason) == label

    def test_every_label_is_defined(self):
        assert set(fl.ANALYSIS_DECLINE_LABELS) >= {
            "title_only", "duplicate_body", "grounding_failed", "disclaimer_only",
            "navigation_or_login_wall", "chart_notes_only", "teaser_only", "wrong_document", "other_declined"}
