"""Custom paragraph loops must not fuse words across inline tags (2026-09-14).

c149894 fixed word-fusing in _normalize_html ("by75bpinSeptember"), but four
fetchers build their text with their own loop over <p> elements and called
p.get_text(strip=True) -- no separator -- so the fix never reached them.
Stored bodies with fused words at the time: de-shaw 9/9, robeco 22/58,
metlife-im 14/29, gsam 11/39.
"""
import ast
import inspect
from unittest.mock import MagicMock

import pytest

import fetch_content as fc

BODY = "<p>The Fed raised rates by<b>75bp</b>in<a href='#'>September</a>. " + "Filler sentence here. " * 8 + "</p>"

CASES = {
    "gsam": ("_fetch_content_gsam", f"<html><body><main>{BODY}</main></body></html>"),
    "robeco": ("_fetch_content_robeco", f"<html><body><main>{BODY}</main></body></html>"),
    "de-shaw": ("_fetch_content_de_shaw", f"<html><body><div class='Blogs_body'>{BODY}</div></body></html>"),
    "metlife-im": ("_fetch_content_metlife_im", f"<html><body><div class='read-more-section richtext'>{BODY}</div></body></html>"),
}


@pytest.mark.parametrize("sid", sorted(CASES))
def test_inline_tags_keep_their_spaces(sid, tmp_path, monkeypatch):
    name, html = CASES[sid]
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    resp = MagicMock(status_code=200, text=html)
    resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
    out = getattr(fc, name)({"id": f"{sid}-1", "url": "https://example.com/a"})
    assert out is not None
    text = out[0].read_text()
    assert "raised rates by 75bp in September." in text, text[:80]


def test_fetch_content_never_calls_get_text_without_a_separator():
    """Structural: a new custom loop must not reintroduce the bug. Every
    get_text call in fetch_content passes a separator (use _paragraph_text).
    The first version of this test only looked inside str.join(...), which
    metlife's append loop would have slipped past."""
    tree = ast.parse(inspect.getsource(fc))
    offenders = [node.lineno for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "get_text" and not node.args]
    assert offenders == [], f"get_text() without a separator at fetch_content.py lines {offenders}"
