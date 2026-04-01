"""
Integration tests for the entrypoint discovery system.

Covers:
- End-to-end scoring pipeline (scoring all dimensions and combining)
- Config roundtrip (_write_entrypoints -> load_entrypoints)
- External domain rejection via extract_nav_links
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from entrypoint_scorer import (
    score_domain,
    score_final,
    score_gate,
    score_path,
    score_structure,
)
from discover_entrypoints import extract_nav_links, score_candidates, _write_entrypoints
import fetch_articles


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESEARCH_INDEX_HTML = """
<!DOCTYPE html>
<html>
<head><title>Research — Example Asset Management</title></head>
<body>
  <nav>
    <a href="/research">Research</a>
    <a href="/about">About</a>
  </nav>
  <main>
    <h1>Research &amp; Insights</h1>
    <div class="pagination">
      <a href="/research?page=2">Next</a>
    </div>
    <article>
      <h2><a href="/research/macro-outlook-2026">Macro Outlook 2026</a></h2>
      <time datetime="2026-03-15">Mar 2026</time>
      <span class="author">Jane Smith</span>
      <p>Our quarterly macro outlook covering global markets.</p>
      <a href="/research/macro-outlook-2026.pdf">Download report</a>
      <a href="/research/macro-outlook-2026">Read more</a>
    </article>
    <article>
      <h2><a href="/research/credit-perspectives-q1">Credit Perspectives Q1</a></h2>
      <time datetime="2026-02-10">Feb 2026</time>
      <span class="author byline">John Doe</span>
      <p>In-depth analysis of credit markets and spreads.</p>
      <a href="/research/credit-perspectives-q1.pdf">Download report</a>
      <a href="/research/credit-perspectives-q1">Read more</a>
    </article>
    <article>
      <h2><a href="/research/equity-letter-q4">Equity Letter Q4 2025</a></h2>
      <time datetime="2025-12-20">2025-12-20</time>
      <span class="byline">Research Team</span>
      <a href="/research/equity-letter-q4.pdf">Download report</a>
      <a href="/research/equity-letter-q4">Read more</a>
    </article>
  </main>
</body>
</html>
"""

ABOUT_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>About Us — Example Asset Management</title></head>
<body>
  <section class="hero">
    <h1>We manage capital for the world's leading institutions</h1>
    <a class="cta-button" href="/contact">Get in Touch</a>
  </section>
  <section class="overview">
    <p>Founded in 1998, we are a global investment manager.</p>
    <p>Our team of 300+ professionals serves clients in 40 countries.</p>
    <a class="btn-primary" href="/about/team">Meet the Team</a>
  </section>
  <section class="newsletter-signup">
    <h2>Stay Updated</h2>
    <form>
      <input type="email" placeholder="Your email" />
      <button type="submit">Subscribe</button>
    </form>
  </section>
  <footer>
    <a href="/careers">Careers</a>
    <a href="/contact">Contact</a>
  </footer>
</body>
</html>
"""

GATED_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Research — Restricted</title></head>
<body>
  <h1>Macro Outlook 2026</h1>
  <div class="gate-wall">
    <p>Subscribe to read the full report.</p>
    <p>Register to continue reading our premium research.</p>
    <p>For clients only: please log in to access this content.</p>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# TestEndToEndScoring
# ---------------------------------------------------------------------------

class TestEndToEndScoring:

    def test_research_index_scores_high(self):
        """A realistic research index page should score >= 0.6 overall."""
        url = "https://www.example.com/research"
        allowed_domains = ["example.com"]

        s_structure = score_structure(RESEARCH_INDEX_HTML)
        s_gate = score_gate(RESEARCH_INDEX_HTML)
        s_path = score_path(url)
        s_domain = score_domain(url, allowed_domains)
        final = score_final(s_domain, s_path, s_structure, s_gate)

        assert final >= 0.6, (
            f"Expected final >= 0.6 for research index, got {final:.4f} "
            f"(domain={s_domain}, path={s_path}, structure={s_structure}, gate={s_gate})"
        )

    def test_about_page_scores_low(self):
        """A marketing/about page should score < 0.5 overall."""
        url = "https://www.example.com/about"
        allowed_domains = ["example.com"]

        s_structure = score_structure(ABOUT_PAGE_HTML)
        s_gate = score_gate(ABOUT_PAGE_HTML)
        s_path = score_path(url)
        s_domain = score_domain(url, allowed_domains)
        final = score_final(s_domain, s_path, s_structure, s_gate)

        assert final < 0.5, (
            f"Expected final < 0.5 for about page, got {final:.4f} "
            f"(domain={s_domain}, path={s_path}, structure={s_structure}, gate={s_gate})"
        )

    def test_gated_page_penalized(self):
        """A page with subscription/registration barriers should have gate_penalty >= 0.3."""
        gate = score_gate(GATED_PAGE_HTML)

        assert gate >= 0.3, (
            f"Expected gate_penalty >= 0.3 for gated page, got {gate:.4f}"
        )


# ---------------------------------------------------------------------------
# TestEntrypointsConfigRoundtrip
# ---------------------------------------------------------------------------

class TestEntrypointsConfigRoundtrip:

    def test_write_and_load(self, tmp_path, monkeypatch):
        """Write entrypoints and load them back; source should appear with active=True."""
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text(json.dumps({"version": 1, "sources": {}}), encoding="utf-8")

        # Monkeypatch ENTRYPOINTS_FILE in both modules
        monkeypatch.setattr("discover_entrypoints.ENTRYPOINTS_FILE", ep_file)
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)

        candidates = [
            {
                "url": "https://www.example.com/research",
                "label": "Research",
                "domain_score": 1.0,
                "path_score": 1.0,
                "structure_score": 0.8,
                "gate_penalty": 0.0,
                "final_score": 0.92,
                "ai_classification": None,
            },
            {
                "url": "https://www.example.com/insights",
                "label": "Insights",
                "domain_score": 1.0,
                "path_score": 1.0,
                "structure_score": 0.75,
                "gate_penalty": 0.0,
                "final_score": 0.85,
                "ai_classification": None,
            },
        ]

        _write_entrypoints("test-source", candidates)

        entrypoints = fetch_articles.load_entrypoints()

        assert "test-source" in entrypoints["sources"], (
            "test-source not found in loaded entrypoints"
        )

        source_entry = entrypoints["sources"]["test-source"]
        eps = source_entry["entrypoints"]
        assert len(eps) >= 1, "Expected at least one entrypoint"

        # First entrypoint must be active
        assert eps[0]["active"] is True, (
            f"Expected first entrypoint to have active=True, got {eps[0]}"
        )
        assert eps[0]["url"] == "https://www.example.com/research"

        # Second entrypoint must not be active
        if len(eps) > 1:
            assert eps[1]["active"] is False

    def test_fallback_when_no_entrypoint(self, tmp_path, monkeypatch):
        """get_source_url returns source['url'] when the source has no entrypoints."""
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text(json.dumps({"version": 1, "sources": {}}), encoding="utf-8")

        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)

        source = {
            "id": "unknown-source",
            "url": "https://www.unknown-fund.com/research",
        }

        entrypoints = fetch_articles.load_entrypoints()
        result = fetch_articles.get_source_url(source, entrypoints)

        assert result == source["url"], (
            f"Expected fallback to source URL '{source['url']}', got '{result}'"
        )


# ---------------------------------------------------------------------------
# TestExternalDomainRejection
# ---------------------------------------------------------------------------

class TestExternalDomainRejection:

    def test_social_media_links_rejected(self):
        """extract_nav_links should filter out twitter/linkedin while keeping internal links."""
        html = """
        <!DOCTYPE html>
        <html>
        <body>
          <nav>
            <a href="https://twitter.com/examplefund">Follow on Twitter</a>
            <a href="https://www.linkedin.com/company/examplefund">LinkedIn</a>
            <a href="/research">Research</a>
            <a href="https://www.example.com/insights">Insights</a>
          </nav>
        </body>
        </html>
        """
        base_url = "https://www.example.com"
        allowed_domains = ["example.com"]

        links = extract_nav_links(html, base_url, allowed_domains=allowed_domains)
        urls = [link["url"] for link in links]

        # Social media links must be filtered out
        for url in urls:
            assert "twitter.com" not in url, f"twitter.com link should be rejected: {url}"
            assert "linkedin.com" not in url, f"linkedin.com link should be rejected: {url}"

        # Internal /research link must be kept
        assert any("/research" in url for url in urls), (
            f"Expected /research link to be kept, got: {urls}"
        )
