"""Blue Owl "Executive Perspectives" pages keep their substance in an
executive-summary component, not in the wysiwyg body (2026-09-25).

The Anaplan piece (2026-09-23) stored 846 chars: the one-sentence intro plus
the third-party disclaimer. The three "key takeaways" paragraphs that carry
the actual content sit in `section.paragraph--type--insights-executive-summary`,
which `.insights-content-wysiwyg p` never matched, so stage 3 correctly
refused to summarise a disclaimer. Verified against the live page on
2026-09-25: the widened selector yields 2860 chars with all three takeaways,
keeps every line of the long-form mid-year-focus piece and changes nothing on
a page without the component.
"""
from unittest.mock import MagicMock

import fetch_content as fc

INTRO = "Erik Bissonnette joins Charlie Gottdiener to discuss how AI is reshaping enterprise software."
TAKEAWAYS = [
    "Deterministic accuracy is critical for CFOs: the calculation engine delivers auditable answers.",
    "Competitive positioning in an AI world: replicating the platform in-house would be extremely difficult.",
    "Evolving software budgets: early conversations centered around build versus buy decisions.",
]
DISCLAIMER = "This material contains information from third party sources which Blue Owl has not verified."


def _page() -> str:
    items = "".join(
        f'<div class="es-item flex"><div><div><p><span><strong>{t}</strong></span></p></div></div></div>'
        for t in TAKEAWAYS)
    return (
        '<html><body><div class="article__main"><div class="article__content">'
        '<section class="paragraph paragraph--type--insights-content-wysiwyg insights-content-wysiwyg">'
        f'<div class="is-wysiwyg"><p>{INTRO}</p></div></section>'
        '<section class="paragraph paragraph--type--insights-executive-summary">'
        f'<div class="es-items">{items}</div></section>'
        '<section class="paragraph paragraph--type--insights-content-wysiwyg insights-content-wysiwyg">'
        f'<div class="is-wysiwyg"><p>{DISCLAIMER}</p></div></section>'
        '</div></div></body></html>'
    )


def test_executive_summary_takeaways_are_part_of_the_body(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    resp = MagicMock(status_code=200, text=_page())
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)

    out = fc._fetch_content_blue_owl_capital(
        {"id": "blue-owl-anaplan", "url": "https://www.blueowl.com/insights/anaplan"})

    assert out is not None
    text = out[0].read_text()
    for takeaway in TAKEAWAYS:
        assert takeaway in text
    # Document order survives: intro, then the takeaways, then the disclaimer.
    assert text.index(INTRO) < text.index(TAKEAWAYS[0]) < text.index(DISCLAIMER)
