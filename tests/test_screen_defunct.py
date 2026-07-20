"""Unit tests for defunct-source (cross-domain redirect) detection in
screen_fund_candidates.py — the guard that stops absorbed/shut-down funds
(angelogordon.com 301→tpg.com) from being screened in as live candidates."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screen_fund_candidates import _registrable_domain, detect_defunct_redirect


class _Redirect:
    """Minimal stand-in for an httpx redirect response in resp.history."""
    def __init__(self, status_code):
        self.status_code = status_code


# --- _registrable_domain ----------------------------------------------------
def test_registrable_domain_strips_www_and_keeps_last_two_labels():
    assert _registrable_domain("www.angelogordon.com") == "angelogordon.com"
    assert _registrable_domain("www.nuveen.com") == "nuveen.com"
    assert _registrable_domain("insights.example.co") == "example.co"


def test_registrable_domain_handles_empty():
    assert _registrable_domain("") == ""


# --- detect_defunct_redirect ------------------------------------------------
def test_permanent_cross_domain_redirect_is_defunct():
    reason = detect_defunct_redirect(
        "https://www.angelogordon.com/news-and-insights/insights",
        "https://www.tpg.com/",
        [_Redirect(301)],
    )
    assert reason and "angelogordon.com" in reason and "tpg.com" in reason


def test_308_permanent_cross_domain_is_defunct():
    assert detect_defunct_redirect(
        "https://old-brand.com/insights", "https://new-brand.com/",
        [_Redirect(308)],
    )


def test_same_domain_canonicalisation_not_defunct():
    # www -> non-www / http -> https on the SAME registrable domain: fine.
    assert detect_defunct_redirect(
        "https://www.pimco.com/insights", "https://pimco.com/en-us/insights",
        [_Redirect(301)],
    ) is None


def test_temporary_cross_domain_redirect_not_defunct():
    # 302/307 (login walls, geo hops) must NOT be flagged as defunct.
    assert detect_defunct_redirect(
        "https://www.fund.com/insights", "https://login.other.com/",
        [_Redirect(302)],
    ) is None


def test_no_redirect_not_defunct():
    assert detect_defunct_redirect(
        "https://www.fund.com/insights", "https://www.fund.com/insights", [],
    ) is None
