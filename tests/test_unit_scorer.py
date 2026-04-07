"""Unit tests for entrypoint_scorer.py — 27 tests covering all five scoring functions."""

import json
import pytest
from entrypoint_scorer import (
    DEFAULT_SCORER_WEIGHTS,
    load_weights,
    score_domain,
    score_final,
    score_final_with_weights,
    score_gate,
    score_path,
    score_structure,
)


# ---------------------------------------------------------------------------
# TestScoreDomain  (7 tests)
# ---------------------------------------------------------------------------

class TestScoreDomain:
    ALLOWED = ["aqr.com", "bridgewater.com"]

    def test_exact_match(self):
        """Bare domain matches exactly."""
        assert score_domain("https://aqr.com/insights", self.ALLOWED) == 1.0

    def test_www_match(self):
        """www. prefix is treated as an exact match."""
        assert score_domain("https://www.aqr.com/insights", self.ALLOWED) == 1.0

    def test_bare_domain_in_list(self):
        """Second allowed domain also matches."""
        assert score_domain("https://www.bridgewater.com/research", self.ALLOWED) == 1.0

    def test_subdomain_match(self):
        """papers.aqr.com is a subdomain → 0.8."""
        assert score_domain("https://papers.aqr.com/article", self.ALLOWED) == 0.8

    def test_no_match(self):
        """Completely different domain → 0.0."""
        assert score_domain("https://www.example.com/page", self.ALLOWED) == 0.0

    def test_partial_name_rejected(self):
        """Domain that contains 'aqr' but isn't aqr.com is rejected."""
        assert score_domain("https://notaqr.com/page", self.ALLOWED) == 0.0

    def test_empty_url(self):
        """Empty string → 0.0, no crash."""
        assert score_domain("", self.ALLOWED) == 0.0

    def test_multiple_allowed(self):
        """Checks all allowed domains; first match wins."""
        allowed = ["man.com", "oaktreecapital.com", "gmo.com"]
        assert score_domain("https://www.gmo.com/perspectives", allowed) == 1.0


# ---------------------------------------------------------------------------
# TestScorePath  (7 tests)
# ---------------------------------------------------------------------------

class TestScorePath:
    def test_research_path(self):
        """/insights/research is strongly positive."""
        assert score_path("https://www.aqr.com/insights/research") > 0.7

    def test_about_page(self):
        """/about is a negative signal."""
        assert score_path("https://www.aqr.com/about") < 0.3

    def test_careers_page(self):
        """/careers is a negative signal."""
        assert score_path("https://www.bridgewater.com/careers") < 0.3

    def test_mixed_signals(self):
        """/research/team has both positive and negative tokens → middle range."""
        score = score_path("https://www.aqr.com/research/team")
        assert 0.3 <= score <= 0.7

    def test_neutral_path(self):
        """Path with no matching keywords → 0.5."""
        assert score_path("https://www.aqr.com/") == 0.5

    def test_multiple_positive(self):
        """/publications/reports/quarterly has many positive tokens → > 0.8."""
        assert score_path("https://www.gmo.com/publications/reports/quarterly") > 0.8

    def test_login_page(self):
        """/login is a negative signal."""
        assert score_path("https://www.bridgewater.com/login") < 0.3


# ---------------------------------------------------------------------------
# TestScoreStructure  (4 tests)
# ---------------------------------------------------------------------------

class TestScoreStructure:
    def test_research_index_page(self):
        """HTML with articles, time tags, PDF links, pagination → > 0.7."""
        html = """
        <html><body>
          <article><time datetime="2026-01-01">Jan 2026</time>
            <span class="author">John Smith</span>
            <a href="/report.pdf">Download Report</a>
          </article>
          <article><time datetime="2026-02-01">Feb 2026</time>
            <span class="byline">Jane Doe</span>
            <a href="/paper.pdf">Download Paper</a>
          </article>
          <a class="read-more" href="/more">Read more</a>
          <nav class="pagination"><a href="?page=2">Next</a></nav>
        </body></html>
        """
        assert score_structure(html) > 0.7

    def test_marketing_page(self):
        """Page dominated by subscribe forms and CTAs → < 0.4."""
        html = """
        <html><body>
          <form><input type="email" placeholder="Enter email">
            <button class="cta-button">Subscribe Now</button>
          </form>
          <div class="newsletter-signup">Sign up for our newsletter</div>
          <button class="btn-primary">Contact Us</button>
        </body></html>
        """
        assert score_structure(html) < 0.4

    def test_empty_html(self):
        """Empty string → 0.0."""
        assert score_structure("") == 0.0

    def test_pdf_hub(self):
        """Page with multiple PDF links scores > 0.5."""
        html = """
        <html><body>
          <ul>
            <li><a href="/q1-2026.pdf">Q1 2026 Report</a></li>
            <li><a href="/q2-2025.pdf">Q2 2025 Report</a></li>
            <li><a href="/annual-2025.pdf">Annual 2025 Report</a></li>
            <li><a href="/outlook-2026.pdf">2026 Outlook</a></li>
          </ul>
        </body></html>
        """
        assert score_structure(html) > 0.5


# ---------------------------------------------------------------------------
# TestScoreGate  (5 tests)
# ---------------------------------------------------------------------------

class TestScoreGate:
    def test_clean_page(self):
        """No gate or disclaimer markers → 0.0."""
        html = "<html><body><article><p>Open content here.</p></article></body></html>"
        assert score_gate(html) == 0.0

    def test_gated_content(self):
        """'subscribe to read' gate marker → penalty >= 0.15."""
        html = "<html><body><p>Subscribe to read the full article.</p></body></html>"
        assert score_gate(html) >= 0.15

    def test_cookie_disclaimer(self):
        """Cookie consent text → penalty >= 0.15."""
        html = "<html><body><div>Cookie preferences — manage cookies</div></body></html>"
        assert score_gate(html) >= 0.15

    def test_max_capped(self):
        """Many gate markers together → penalty capped at 1.0."""
        html = """
        subscribe to read register to continue log in to read
        sign up to read register to read for clients only
        cookie preferences privacy policy terms of use
        manage cookies accept all cookies
        """
        assert score_gate(html) == 1.0

    def test_empty_html(self):
        """Empty string → 0.0, no crash."""
        assert score_gate("") == 0.0


# ---------------------------------------------------------------------------
# TestScoreFinal  (4 tests)
# ---------------------------------------------------------------------------

class TestScoreFinal:
    def test_perfect_score(self):
        """All signals maxed out → final >= 0.8."""
        assert score_final(1.0, 1.0, 1.0, 0.0) >= 0.8

    def test_marketing_page(self):
        """Good domain but bad path/structure and gate → < 0.4."""
        assert score_final(1.0, 0.1, 0.1, 0.8) < 0.4

    def test_domain_reject(self):
        """Domain=0.0 with weak path/structure → overall score < 0.4."""
        # 0.0*0.2 + 0.2*0.3 + 0.2*0.3 + (1-0.8)*0.2 = 0.0 + 0.06 + 0.06 + 0.04 = 0.16
        assert score_final(0.0, 0.2, 0.2, 0.8) < 0.4

    def test_weights_sum_to_one(self):
        """Verify formula: domain*0.2 + path*0.3 + structure*0.3 + (1-gate)*0.2."""
        result = score_final(1.0, 1.0, 1.0, 0.0)
        expected = 1.0 * 0.2 + 1.0 * 0.3 + 1.0 * 0.3 + (1.0 - 0.0) * 0.2
        assert abs(result - expected) < 1e-9


# ---------------------------------------------------------------------------
# TestLoadWeights  (3 tests)
# ---------------------------------------------------------------------------

class TestLoadWeights:
    def test_load_weights_from_file(self, tmp_path):
        """Loads custom weights from a valid JSON file."""
        weights_file = tmp_path / "weights.json"
        custom = {"domain": 0.1, "path": 0.4, "structure": 0.4, "gate": 0.1}
        weights_file.write_text(json.dumps(custom))

        result = load_weights(str(weights_file))

        assert result == custom

    def test_load_weights_missing_file(self, tmp_path):
        """Missing file returns default weights without raising."""
        result = load_weights(str(tmp_path / "nonexistent.json"))

        assert result == DEFAULT_SCORER_WEIGHTS

    def test_load_weights_bad_sum(self, tmp_path):
        """Weights that don't sum to 1.0 (±0.01) return defaults."""
        weights_file = tmp_path / "bad_weights.json"
        bad = {"domain": 0.5, "path": 0.5, "structure": 0.5, "gate": 0.5}
        weights_file.write_text(json.dumps(bad))

        result = load_weights(str(weights_file))

        assert result == DEFAULT_SCORER_WEIGHTS


# ---------------------------------------------------------------------------
# TestScoreFinalWithWeights  (2 tests)
# ---------------------------------------------------------------------------

class TestScoreFinalWithWeights:
    def test_score_final_with_weights(self):
        """score_final_with_weights applies provided weights correctly."""
        weights = {"domain": 0.1, "path": 0.4, "structure": 0.4, "gate": 0.1}
        result = score_final_with_weights(1.0, 0.5, 0.5, 0.0, weights)
        expected = 1.0 * 0.1 + 0.5 * 0.4 + 0.5 * 0.4 + (1.0 - 0.0) * 0.1
        assert abs(result - expected) < 1e-9

    def test_score_final_unchanged(self):
        """Existing score_final still returns the same values as before refactor."""
        # Values computed against original hardcoded formula
        inputs = [
            (1.0, 1.0, 1.0, 0.0),
            (0.8, 0.5, 0.5, 0.3),
            (0.0, 0.2, 0.2, 0.8),
            (1.0, 0.1, 0.1, 0.8),
        ]
        for domain, path, structure, gate in inputs:
            expected = (
                domain * 0.2
                + path * 0.3
                + structure * 0.3
                + (1.0 - gate) * 0.2
            )
            assert abs(score_final(domain, path, structure, gate) - expected) < 1e-9
