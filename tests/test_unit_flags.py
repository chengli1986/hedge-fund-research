"""Unit tests for country-flag rendering in publish.py Sources view."""

from publish import _FLAG_SVGS, _hq_to_flags, _build_sources_view


def test_single_us_state_code():
    assert _hq_to_flags("Westport, CT") == ["US"]
    assert _hq_to_flags("Newport Beach, CA") == ["US"]


def test_us_explicit_token():
    assert _hq_to_flags("New York, USA") == ["US"]
    assert _hq_to_flags("Los Angeles, California, USA") == ["US"]


def test_single_non_us_country():
    assert _hq_to_flags("London, UK") == ["GB"]
    assert _hq_to_flags("Edinburgh, UK") == ["GB"]
    assert _hq_to_flags("Paris, France") == ["FR"]
    assert _hq_to_flags("Rotterdam, Netherlands") == ["NL"]


def test_dual_hq_yields_two_flags_in_order():
    assert _hq_to_flags("Paris, France / Boston, MA") == ["FR", "US"]
    assert _hq_to_flags("London, UK / Denver, CO") == ["GB", "US"]


def test_unknown_country_gets_no_wrong_flag():
    # A country with no SVG yet must return no flag rather than a bogus US flag.
    assert _hq_to_flags("Milan, Italy") == []
    assert _hq_to_flags("") == []


def test_flag_svgs_are_collision_safe():
    # Inlined many times per page → must not carry id/url(#) references.
    for code, svg in _FLAG_SVGS.items():
        assert "id=" not in svg, f"{code} flag uses an id (collision risk)"
        assert "url(" not in svg, f"{code} flag references url(#...) (collision risk)"
        assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_build_sources_view_emits_flags():
    sources = {
        "man-group": {"name": "Man Group", "short_name": "MAN",
                      "strategy_tags": ["macro"], "url": "https://man.com",
                      "expected_hostname": "man.com", "description": "x"},
        "natixis-im": {"name": "Natixis IM", "short_name": "NATIXIS",
                       "strategy_tags": ["macro"], "url": "https://im.natixis.com",
                       "expected_hostname": "im.natixis.com", "description": "y"},
    }
    out = _build_sources_view(sources)
    assert out.count('class="sc-flags"') == 2
    # Man Group → 1 GB flag; Natixis (dual FR/US) → 2 flags ⇒ 3 flag svgs total.
    assert out.count('class="flag"') == 3
    assert 'aria-label="United Kingdom"' in out
    assert 'aria-label="France"' in out
