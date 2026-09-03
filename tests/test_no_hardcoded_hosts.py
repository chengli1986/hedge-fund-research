"""Fleet-wide guard: no fetcher may hardcode a site host.

What it prevents
----------------
2026-08-21: Research Affiliates renamed to Syzygy Asset Management.
``config/sources.json`` was pointed at the new host and the fetcher still
returned **0 articles, with no error** — every relative href was rebuilt on the
hardcoded old host by ``urljoin("https://www.researchaffiliates.com", href)``
and then silently dropped by ``_validate_hostname``.  A domain move is meant to
be a config edit; a hardcoded host turns it into an outage that surfaces days
later as "0 篇" in a staleness report.

Only that one fetcher was converted at the time.  The 2026-09-03 sweep found
the same shape in 24 of 40 listing fetchers and 2 content fetchers; they now all
derive the base from the URL they were handed (``fetch_articles._site_base``).
This test fails if anyone reintroduces a literal.

Why AST and not a regex over the source text
--------------------------------------------
Docstrings legitimately quote URLs (``fetch_researchaffiliates`` documents both
the old and the new host), and so do comments.  Parsing to an AST and skipping
the docstring node means only literals that actually reach the runtime are
inspected; comments never enter the tree at all.
"""

import ast
import inspect
import re
import textwrap

import pytest

from fetch_articles import FETCHERS
from fetch_content import CONTENT_FETCHERS

# (module, function, exact literal) -> reason.
#
# Keyed on all three on purpose.  Exempting a whole *function* would also excuse
# any real hardcoded host that later appears in it — fetch_goehring_rozencwajg is
# exactly that trap: it holds one legitimate spec constant next to two site URLs
# that must follow the config.  A genuine exception is a host that is NOT the
# source's own site and therefore cannot be derived from the URL we were handed.
ALLOWED_LITERALS: dict[tuple[str, str, str], str] = {
    ("fetch_articles", "fetch_goehring_rozencwajg",
     "http://www.sitemaps.org/schemas/sitemap/0.9"):
        "XML namespace URI from the sitemaps.org spec, not a host we fetch. "
        "It is a fixed string in the sitemap format; deriving it from the "
        "source URL would break parsing.",
    ("fetch_articles", "fetch_principal_am", "https://"):
        "f-string scheme prefix for the third-party Coveo search API "
        "(f\"https://{org}.org.coveo.com/...\"). The host is coveo.com, not "
        "principalam.com, so it cannot come from source[\"url\"]; the org id it "
        "is built from is already read from the page.",
}

_URL_LITERAL = re.compile(r"https?://", re.I)


def _runtime_url_literals(fn) -> list[str]:
    """URL string literals that actually execute inside ``fn``.

    Docstrings are dropped; comments are absent from the AST by construction.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    func = tree.body[0]
    body = func.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # docstring

    found = []
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if _URL_LITERAL.search(node.value):
                    found.append(node.value)
    return found


def _targets():
    for sid, fn in sorted(FETCHERS.items()):
        yield "fetch_articles", sid, fn
    for sid, fn in sorted(CONTENT_FETCHERS.items()):
        yield "fetch_content", sid, fn


@pytest.mark.parametrize(
    "module,source_id,fn",
    [pytest.param(m, s, f, id=f"{m}:{s}") for m, s, f in _targets()],
)
def test_fetcher_does_not_hardcode_a_host(module, source_id, fn):
    literals = [lit for lit in _runtime_url_literals(fn)
                if (module, fn.__name__, lit) not in ALLOWED_LITERALS]
    assert not literals, (
        f"{module}.{fn.__name__} (source {source_id!r}) hardcodes {literals!r}.\n"
        "Derive the base from the URL you were handed instead:\n"
        "    base_url = _site_base(source[\"url\"])      # listing fetchers\n"
        "    base_url = _site_base(article[\"url\"])     # content fetchers\n"
        "A hardcoded host makes a domain rename return 0 articles silently "
        "(research-affiliates, 2026-08-21). If this literal is a genuine "
        "exception (third-party API, a CDN on a different host), add it to "
        "ALLOWED_LITERAL_HOSTS with a reason."
    )


def test_guard_actually_detects_a_hardcoded_host():
    """The guard must fail on a fetcher that hardcodes — not just pass vacuously.

    Without this, deleting the assertion above (or an AST walk that silently
    stopped finding anything) would leave 80 green tests and no coverage.
    """
    from urllib.parse import urljoin

    def _bad_fetcher(source):
        """Docstring mentioning https://example.com must NOT count."""
        return urljoin("https://www.hardcoded-example.com", "/a")

    def _good_fetcher(source):
        """Docstring mentioning https://example.com must NOT count."""
        from fetch_articles import _site_base
        return urljoin(_site_base(source["url"]), "/a")

    assert _runtime_url_literals(_bad_fetcher) == ["https://www.hardcoded-example.com"]
    assert _runtime_url_literals(_good_fetcher) == []


def test_allowlist_entries_are_still_live():
    """Every allowlist entry must still match a literal that is really there.

    Without this, an entry outlives the code it excused and quietly widens the
    exemption for whoever edits that function next.
    """
    import fetch_articles
    import fetch_content
    modules = {"fetch_articles": fetch_articles, "fetch_content": fetch_content}
    stale = []
    for (mod_name, fn_name, literal), _reason in ALLOWED_LITERALS.items():
        fn = getattr(modules[mod_name], fn_name, None)
        if fn is None or literal not in _runtime_url_literals(fn):
            stale.append((mod_name, fn_name, literal))
    assert not stale, (
        f"allowlist entries no longer match any live literal: {stale}. "
        "Delete them — a stale exemption silently covers future hardcoding."
    )
