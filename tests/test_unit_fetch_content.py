"""Unit tests for fetch_content.py — validation helpers and atomic writes."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fetch_content import (
    _validate_pdf_response,
    _validate_json_response,
    _normalize_html,
    _check_min_content_length,
    _atomic_write,
    load_articles,
    save_articles,
    CONTENT_FETCHERS,
    _fetch_content_bridgewater,
    _extract_bridgewater_text,
    _fetch_content_principal_am,
    mark_content_failure,
    is_content_pending,
    MAX_CONTENT_ATTEMPTS,
)


# ---------------------------------------------------------------------------
# _validate_pdf_response
# ---------------------------------------------------------------------------

class TestValidatePdfResponse:
    def test_accepts_valid_pdf(self):
        assert _validate_pdf_response(200, "application/pdf", 5000) is True

    def test_rejects_html_content_type(self):
        assert _validate_pdf_response(200, "text/html", 5000) is False

    def test_rejects_too_small(self):
        assert _validate_pdf_response(200, "application/pdf", 1024) is False
        assert _validate_pdf_response(200, "application/pdf", 500) is False

    def test_rejects_non_200(self):
        assert _validate_pdf_response(404, "application/pdf", 5000) is False
        assert _validate_pdf_response(500, "application/pdf", 5000) is False

    def test_accepts_other_2xx(self):
        assert _validate_pdf_response(201, "application/pdf", 5000) is True

    def test_rejects_none_content_type(self):
        assert _validate_pdf_response(200, None, 5000) is False


# ---------------------------------------------------------------------------
# _validate_json_response
# ---------------------------------------------------------------------------

class TestValidateJsonResponse:
    def test_rejects_html_error_page(self):
        assert _validate_json_response("<html><body>Error</body></html>") is False

    def test_rejects_html_with_whitespace(self):
        assert _validate_json_response("  <html>") is False

    def test_accepts_valid_json(self):
        assert _validate_json_response('{"key": "value"}') is True
        assert _validate_json_response('[1, 2, 3]') is True

    def test_rejects_invalid_json(self):
        assert _validate_json_response("not json at all") is False


# ---------------------------------------------------------------------------
# _normalize_html
# ---------------------------------------------------------------------------

class TestNormalizeHtml:
    def test_strips_nav_footer(self):
        html = """
        <html>
        <nav>Navigation menu</nav>
        <header>Site header</header>
        <article>
            <p>Important article content here.</p>
        </article>
        <footer>Footer links</footer>
        </html>
        """
        text = _normalize_html(html, "article p")
        assert "Important article content" in text
        assert "Navigation menu" not in text
        assert "Footer links" not in text
        assert "Site header" not in text

    def test_preserves_article_body(self):
        html = """
        <div class="article-content">
            <p>First paragraph of the article.</p>
            <p>Second paragraph of the article.</p>
        </div>
        """
        text = _normalize_html(html, ".article-content p")
        assert "First paragraph" in text
        assert "Second paragraph" in text

    def test_fallback_selectors(self):
        html = """
        <main>
            <p>Content in main element.</p>
        </main>
        """
        # Primary selector won't match, should fall back to "main"
        text = _normalize_html(html, ".nonexistent-class p")
        assert "Content in main" in text

    def test_strips_script_and_style(self):
        html = """
        <article>
            <script>var x = 1;</script>
            <style>.foo { color: red; }</style>
            <p>Real content.</p>
        </article>
        """
        text = _normalize_html(html, "article p")
        assert "Real content" in text
        assert "var x" not in text
        assert "color: red" not in text


# ---------------------------------------------------------------------------
# _check_min_content_length
# ---------------------------------------------------------------------------

class TestCheckMinContentLength:
    def test_rejects_short_text(self):
        assert _check_min_content_length("short") is False
        assert _check_min_content_length("a" * 100) is False

    def test_accepts_long_text(self):
        assert _check_min_content_length("a" * 101) is True
        assert _check_min_content_length("a" * 500) is True

    def test_rejects_empty(self):
        assert _check_min_content_length("") is False


# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write(target, b"hello world")
        assert target.exists()
        assert target.read_bytes() == b"hello world"

    def test_no_tmp_left_behind(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write(target, b"data")
        # No .tmp file should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "subdir" / "deep" / "output.txt"
        _atomic_write(target, b"nested")
        assert target.exists()
        assert target.read_bytes() == b"nested"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "output.txt"
        target.write_bytes(b"old")
        _atomic_write(target, b"new")
        assert target.read_bytes() == b"new"


# ---------------------------------------------------------------------------
# Content status on failure
# ---------------------------------------------------------------------------

class TestContentStatusOnFailure:
    def test_content_status_set_to_failed_when_fetcher_returns_none(self, tmp_path):
        """When a fetcher returns None, the article's content_status should be 'failed'."""
        article = {
            "id": "test123",
            "source_id": "gmo",
            "source_name": "GMO",
            "title": "Test Article",
            "url": "https://www.gmo.com/test",
            "summarized": False,
        }

        # Simulate the main loop logic: fetcher returns None -> status = failed
        result = None  # simulating fetcher failure
        if result is not None:
            content_path, status = result
            article["content_path"] = str(content_path)
            article["content_status"] = status
        else:
            article["content_status"] = "failed"

        assert article["content_status"] == "failed"
        assert "content_path" not in article


class TestContentDeadLetterCap:
    def test_failure_increments_attempts_and_stays_failed_below_cap(self):
        a = {"id": "x", "title": "t"}
        for i in range(1, MAX_CONTENT_ATTEMPTS):
            status = mark_content_failure(a)
            assert a["content_attempts"] == i
            assert status == "failed"
            assert "content_permafailed_at" not in a

    def test_reaches_cap_retires_to_permafail(self):
        a = {"id": "x", "title": "t", "content_attempts": MAX_CONTENT_ATTEMPTS - 1}
        status = mark_content_failure(a)
        assert status == "permafail"
        assert a["content_status"] == "permafail"
        assert a["content_attempts"] == MAX_CONTENT_ATTEMPTS
        assert a.get("content_permafailed_at")  # stamped

    def test_permafail_and_terminal_statuses_drop_out_of_pending(self):
        src = next(iter(CONTENT_FETCHERS))  # a source that HAS a fetcher
        base = {"source_id": src, "summarized": False}
        # permafail / ok / metadata_only are terminal -> not pending
        for terminal in ("permafail", "ok", "metadata_only"):
            assert not is_content_pending({**base, "content_status": terminal})
        # a plain failure (below cap) is still pending -> gets retried
        assert is_content_pending({**base, "content_status": "failed"})
        # summarized or no-fetcher-source are not pending
        assert not is_content_pending({**base, "summarized": True})
        assert not is_content_pending({"source_id": "no-such-source", "summarized": False})

    def test_source_filter_limits_pending(self):
        src = next(iter(CONTENT_FETCHERS))
        a = {"source_id": src, "summarized": False}
        assert is_content_pending(a, source_filter=src)
        assert not is_content_pending(a, source_filter="other-source")


# ---------------------------------------------------------------------------
# CONTENT_FETCHERS registry
# ---------------------------------------------------------------------------

class TestContentFetchers:
    def test_bridgewater_included(self):
        assert "bridgewater" in CONTENT_FETCHERS

    def _load_source_ids(self):
        config_path = Path(__file__).resolve().parent.parent / "config" / "sources.json"
        config = json.loads(config_path.read_text())
        return {s["id"] for s in config["sources"]}

    def test_all_production_sources_have_content_fetcher(self):
        """Every source in config/sources.json must have a CONTENT_FETCHERS handler.

        Catches the class of bug where a new production source is added to
        sources.json but fetch_content.py's dispatcher is not updated,
        silently dropping all articles from that source at the content stage.
        """
        missing = sorted(self._load_source_ids() - set(CONTENT_FETCHERS))
        assert not missing, (
            f"Sources in sources.json without CONTENT_FETCHERS handler: {missing}. "
            f"Add a _fetch_content_<id> function and register it in CONTENT_FETCHERS."
        )

    def _load_trial_ids(self) -> set[str]:
        """IDs of candidates in active trial or visitable (pre-production) state.

        These are allowed to have CONTENT_FETCHERS handlers without being in
        sources.json — the handler is written ahead of trial so the health
        check and quality sampler can exercise it before promotion.
        """
        candidates_path = Path(__file__).resolve().parent.parent / "config" / "fund_candidates.json"
        candidates = json.loads(candidates_path.read_text())
        trial_statuses = {"visitable", "active_trial"}
        trial_ids: set[str] = {c["id"] for c in candidates if c.get("status") in trial_statuses}

        trial_state_path = Path(__file__).resolve().parent.parent / "config" / "trial-state.json"
        trial_state = json.loads(trial_state_path.read_text())
        for t in trial_state.get("active_trials", []):
            trial_ids.add(t["id"])

        return trial_ids

    def test_no_orphan_content_fetchers(self):
        """Every CONTENT_FETCHERS key must correspond to a source in sources.json.

        Catches stale handlers left after a source is removed from production.
        Trial-phase sources (visitable / active_trial) are exempt — their handlers
        are registered ahead of promotion so the health check can exercise them.
        """
        allowed = self._load_source_ids() | self._load_trial_ids()
        orphans = sorted(set(CONTENT_FETCHERS) - allowed)
        assert not orphans, (
            f"CONTENT_FETCHERS keys not in sources.json: {orphans}. "
            f"Remove stale handlers or re-add source to config."
        )

    def test_all_production_sources_have_fetch_articles_handler(self):
        """Every source in config/sources.json must have a FETCHERS handler.

        Parallel contract to CONTENT_FETCHERS — catches the same class of bug
        one pipeline stage earlier (fetch_articles vs fetch_content).
        """
        from fetch_articles import FETCHERS
        missing = sorted(self._load_source_ids() - set(FETCHERS))
        assert not missing, (
            f"Sources in sources.json without FETCHERS handler: {missing}. "
            f"Add a fetch_<id> function and register it in FETCHERS."
        )


# ---------------------------------------------------------------------------
# Bridgewater extraction
# ---------------------------------------------------------------------------

class TestBridgewaterExtraction:
    def test_extracts_article_body_and_ignores_gate_text(self, tmp_path, monkeypatch):
        html = """
        <html>
          <body>
            <nav>Navigation</nav>
            <header>Header</header>
            <div class="disclaimer">Disclaimer: Bridgewater Associates material.</div>
            <article class="article-body">
              <p>Bridgewater argues that markets are changing structurally.</p>
              <p>Portfolio resilience matters more as dispersion rises.</p>
              <p>Investors need better diversification and risk control.</p>
            </article>
            <footer>Footer</footer>
          </body>
        </html>
        """

        extracted = _extract_bridgewater_text(html)
        assert extracted is not None
        assert "markets are changing structurally" in extracted
        assert "Disclaimer" not in extracted
        assert "Navigation" not in extracted

        content_dir = tmp_path / "content"
        monkeypatch.setattr("fetch_content.CONTENT_DIR", content_dir)
        monkeypatch.setattr("fetch_content.BASE_DIR", tmp_path)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        article = {
            "id": "bw123",
            "url": "https://www.bridgewater.com/research-and-insights/test",
        }

        with patch("fetch_content.requests.get", return_value=mock_resp):
            result = _fetch_content_bridgewater(article)

        assert result is not None
        content_path, status = result
        assert status == "ok"
        assert content_path == content_dir / "bw123.txt"
        assert content_path.read_text(encoding="utf-8").startswith("Bridgewater argues")

    def test_rejects_gated_page_even_if_it_is_long(self, tmp_path, monkeypatch):
        html = """
        <html>
          <body>
            <div class="article-body">
              <p>Subscribe to read this article.</p>
              <p>Register to continue and accept all cookies.</p>
              <p>This content is available to Bridgewater clients only.</p>
              <p>Cookie preferences, privacy policy, and terms of use apply.</p>
            </div>
          </body>
        </html>
        """

        content_dir = tmp_path / "content"
        monkeypatch.setattr("fetch_content.CONTENT_DIR", content_dir)
        monkeypatch.setattr("fetch_content.BASE_DIR", tmp_path)

        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        article = {
            "id": "bw999",
            "url": "https://www.bridgewater.com/research-and-insights/gated",
        }

        with patch("fetch_content.requests.get", return_value=mock_resp):
            result = _fetch_content_bridgewater(article)

        assert result is None


class TestPrincipalAMFetcher:
    def test_fetch_content_principal_am_saves_body(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        html = "<html><body><main><p>%s</p></main></body></html>" % ("Principal CRE analysis. " * 30)
        resp = MagicMock(status_code=200, text=html); resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        out = fc._fetch_content_principal_am({"id": "principal-am-001", "url": "https://principalam.com/x"})
        assert out is not None
        path, status = out
        assert status == "ok" and path.exists()
        assert "Principal CRE analysis." in path.read_text()

    def test_fetch_content_principal_am_none_on_http_error(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        def boom(*a, **k): raise fc.requests.RequestException("500")
        monkeypatch.setattr(fc.requests, "get", boom)
        assert fc._fetch_content_principal_am({"id": "principal-am-002", "url": "https://x"}) is None

    def test_principal_am_registered(self):
        from fetch_content import CONTENT_FETCHERS
        assert "principal-am" in CONTENT_FETCHERS


class TestWellingtonFetcher:
    def test_uses_load_not_networkidle(self):
        # Wellington AEM pages have long-running analytics that never reach networkidle;
        # the fetcher must use wait_until="load" to avoid 30s timeouts.
        import inspect
        import fetch_content
        src = inspect.getsource(fetch_content._fetch_content_wellington)
        assert 'wait_until="load"' in src, (
            "_fetch_content_wellington must use wait_until='load', not 'networkidle'; "
            "Wellington pages have persistent analytics that never idle out."
        )
        assert 'wait_until="networkidle"' not in src


class TestPimcoContentFetcher:
    def test_uses_domcontentloaded_not_networkidle(self):
        # PIMCO's Coveo search engine keeps firing analytics/tracking beacons,
        # so the page never reaches networkidle (same root cause as the
        # fetch_articles.fetch_pimco listing fetcher fix, 2026-07-19); the
        # content-page fetcher has the identical bug and must use
        # wait_until="domcontentloaded" too.
        import inspect
        import fetch_content
        src = inspect.getsource(fetch_content._fetch_content_pimco)
        assert 'wait_until="domcontentloaded"' in src, (
            "_fetch_content_pimco must use wait_until='domcontentloaded', not "
            "'networkidle'; PIMCO pages have persistent Coveo analytics beacons "
            "that never idle out (2026-07-21 health-check failure: all 3 probed "
            "articles timed out at 30s)."
        )
        assert 'wait_until="networkidle"' not in src


class TestTrowepriceContentFetcher:
    def test_uses_domcontentloaded_not_networkidle(self):
        # Same root cause as the fetch_articles.fetch_troweprice listing fix
        # (2026-09-03): every troweprice.com page load fires ~75 CSP-violation
        # report beacons to api.public.troweprice.com and some never settle, so
        # networkidle is a coin flip.  The article page already hit this on
        # 2026-08-30 (attempt 1 timed out at 30s, attempt 2 recovered).
        import inspect
        import fetch_content
        src = inspect.getsource(fetch_content._fetch_content_troweprice)
        assert 'wait_until="domcontentloaded"' in src, (
            "_fetch_content_troweprice must use wait_until='domcontentloaded', "
            "not 'networkidle'; troweprice.com pages have hanging CSP-violation "
            "report beacons that never idle out."
        )
        assert 'wait_until="networkidle"' not in src


class TestCohenSteersPlaywrightFetcher:
    def test_fetch_content_cohen_steers_saves_body(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        html = "<html><body><article><p>%s</p></article></body></html>" % ("Cohen REIT insight. " * 30)
        monkeypatch.setattr("fetch_articles._get_playwright_page", lambda url, **kw: html)
        out = fc._fetch_content_cohen_steers({"id": "cohen-steers-001", "url": "https://cohenandsteers.com/x"})
        assert out is not None
        path, status = out
        assert status == "ok" and path.exists()
        assert "Cohen REIT insight." in path.read_text()

    def test_fetch_content_cohen_steers_none_on_browser_error(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        def boom(url, **kw): raise RuntimeError("browser crash")
        monkeypatch.setattr("fetch_articles._get_playwright_page", boom)
        assert fc._fetch_content_cohen_steers({"id": "cohen-steers-002", "url": "https://x"}) is None

    def test_cohen_steers_registered(self):
        from fetch_content import CONTENT_FETCHERS
        assert "cohen-steers" in CONTENT_FETCHERS


class TestMetLifeIMFetcher:
    """MetLife IM article bodies are SSR inside div.read-more-section.richtext.

    The same page also carries a long `terms-of-use-legalText` block (~11k chars
    of boilerplate on every article) — extracting it would swamp the summariser
    with identical legal text, so it must be stripped before the body is read.

    2026-08-03: the site moved to www.metlife.com/investments/en-us/ behind a
    disclaimer cookie gate, and the body container narrowed — a bare
    `div.richtext` also matches the page's empty search widget ("Sorry, we
    couldn't find any results matching"), which would otherwise lead the text.
    """

    BODY = "MIM sees a recovery scenario as inflation impulses persist. " * 12

    LEGAL = "This material is intended solely for informational purposes. " * 20
    # Some articles append the same block INSIDE the body container, marked only
    # by a leading "Disclosure" — 9.3k of 30.5k chars on the 2026-07-20 Global
    # Risks piece, i.e. a third of what the summariser would have read.
    INLINE_DISCLOSURE = (
        "DisclosureThis material is intended solely for Institutional Investors. " * 15
    )
    # The site-search widget renders empty on every article page, inside its own
    # div.richtext. It sorts before the body in document order.
    SEARCH_WIDGET = "Sorry, we couldn't find any results matching your search."

    URL = ("https://www.metlife.com/investments/en-us/insights/"
           "macro-strategy/summer-heat/")

    def _html(self) -> str:
        return (
            "<html><body>"
            "<nav><p>Insights</p></nav>"
            f'<div class="richtext body-b1"><p>{self.SEARCH_WIDGET}</p></div>'
            f'<div class="d-none read-more-section richtext body-b1">'
            f"<p>{self.BODY}</p><p>{self.INLINE_DISCLOSURE}</p></div>"
            f'<div class="terms-of-use-legalText font-body-2"><p>{self.LEGAL}</p></div>'
            "</body></html>"
        )

    def test_saves_body_without_legal_boilerplate_or_search_widget(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        resp = MagicMock(status_code=200, text=self._html())
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

        out = fc._fetch_content_metlife_im({"id": "metlife-im-001", "url": self.URL})

        assert out is not None
        path, status = out
        assert status == "ok" and path.exists()
        text = path.read_text()
        assert "MIM sees a recovery scenario" in text
        assert "informational purposes" not in text
        assert "Institutional Investors" not in text
        assert "couldn't find any results" not in text

    def test_sends_disclaimer_cookies(self, tmp_path, monkeypatch):
        """Regression guard for the 2026-08-03 outage — without both cookies
        the article URL 302s to the disclaimer interstitial."""
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        capture: dict = {}
        resp = MagicMock(status_code=200, text=self._html())
        resp.raise_for_status = lambda: None

        def _get(url, **kwargs):
            capture.update(kwargs)
            return resp

        monkeypatch.setattr(fc.requests, "get", _get)
        fc._fetch_content_metlife_im({"id": "metlife-im-004", "url": self.URL})

        assert capture["cookies"] == {"culture": "en-us",
                                      "disclaimer": "en-us/Institution"}

    def test_none_on_http_error(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)

        def boom(*a, **k):
            raise fc.requests.RequestException("500")

        monkeypatch.setattr(fc.requests, "get", boom)
        assert fc._fetch_content_metlife_im({
            "id": "metlife-im-002",
            "url": self.URL,
        }) is None

    def test_none_when_body_too_short(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        html = ('<html><body><div class="d-none read-more-section richtext">'
                "<p>Too short.</p></div></body></html>")
        resp = MagicMock(status_code=200, text=html)
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

        assert fc._fetch_content_metlife_im({
            "id": "metlife-im-003",
            "url": self.URL,
        }) is None

    def test_none_for_pdf_teaser_pages(self, tmp_path, monkeypatch):
        """The monthly Pension Funding Status pages have no read-more section —
        only a ~300-char blurb in <main> above a link to the real PDF. That is
        over MIN_CONTENT_LENGTH, so falling back to `main p` would store a
        teaser as if it were the research. It must return None instead.
        """
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        blurb = "U.S. corporate pension funded status declined in June 2026. " * 6
        assert len(blurb) > fc.MIN_CONTENT_LENGTH
        html = f"<html><body><main><p>{blurb}</p></main></body></html>"
        resp = MagicMock(status_code=200, text=html)
        resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

        assert fc._fetch_content_metlife_im({
            "id": "metlife-im-005",
            "url": ("https://www.metlife.com/investments/en-us/insights/"
                    "outlooks-and-market-updates/june-2026-pension-funding-status/"),
        }) is None

    def test_metlife_im_registered(self):
        from fetch_content import CONTENT_FETCHERS
        assert "metlife-im" in CONTENT_FETCHERS


class TestVerdadFetcher:
    """Verdad articles now land on mailchi.mp — table-based email HTML.

    Those pages carry no <p> tags at all, so the old "article p" selector fell
    through _normalize_html's fallbacks to whole-page get_text() and dragged in
    the Mailchimp chrome (language picker, "Past Issues", unsubscribe footer).
    The body lives in <td class="mcnTextContent">.
    """

    BODY = "High yield spreads have compressed structurally. " * 12
    CHROME = "Translate English العربية Afrikaans Subscribe Past Issues RSS"

    def _page(self) -> str:
        return (
            "<html><body>"
            f"<div id='awesomewrap'><a>{self.CHROME}</a></div>"
            "<table><tr>"
            f"<td class='mcnTextContent'>{self.BODY}</td>"
            "</tr></table>"
            "</body></html>"
        )

    def test_extracts_email_body(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        resp = MagicMock(status_code=200, text=self._page()); resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

        out = fc._fetch_content_verdad({
            "id": "verdad-capital-001",
            "url": "https://mailchi.mp/verdadcap/the-maturation-of-high-yield",
        })

        assert out is not None
        path, status = out
        assert status == "ok" and path.exists()
        text = path.read_text()
        assert "High yield spreads have compressed structurally." in text
        # Regression guard: Mailchimp chrome must stay out of the body.
        assert "Past Issues" not in text
        assert "Afrikaans" not in text

    def test_none_on_http_error(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        def boom(*a, **k): raise fc.requests.RequestException("500")
        monkeypatch.setattr(fc.requests, "get", boom)
        assert fc._fetch_content_verdad({"id": "verdad-capital-002", "url": "https://x"}) is None

    def test_none_when_body_too_short(self, tmp_path, monkeypatch):
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        html = "<html><body><td class='mcnTextContent'>Too short.</td></body></html>"
        resp = MagicMock(status_code=200, text=html); resp.raise_for_status = lambda: None
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        assert fc._fetch_content_verdad({"id": "verdad-capital-003", "url": "https://x"}) is None

    def test_verdad_registered(self):
        from fetch_content import CONTENT_FETCHERS
        assert "verdad-capital" in CONTENT_FETCHERS
