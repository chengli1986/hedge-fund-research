"""Trial sampling fetches article pages with requests, like production (2026-11-26).

Why: gcm-grosvenor (visitable, waiting for a trial slot) serves a "Checking your
browser" challenge (HTTP 403, 1762 bytes) to httpx but the page itself to
requests, on the same URL with the same headers (measured 2026-11-25 22:16Z).
Production lists and fetches bodies with requests (listing_templates.py,
fetch_content.py), so a trial that sampled through httpx would score nothing
and end "inconclusive" for a client difference, not for content quality.

These tests drive the real ``requests.Session`` (redirects, cookie extraction,
netrc lookup) over an offline transport adapter, so they check what the
session actually sends, not what a stub claims.  Plan: ~/tmp/gmia-candidate-
probe-plan.md V0.4, PR-B.
"""

import email.message
import importlib.util
import io
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.response import HTTPResponse

_spec = importlib.util.spec_from_file_location(
    "trial_manager_rq",
    str(Path(__file__).resolve().parent.parent / "gmia-trial-manager.py"),
)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

HOST = "https://www.example-fund.com"
ARTICLE_URL = HOST + "/insights/2026/11/the-article/"
GATE_URL = HOST + "/advisor/public/authentication-0.htm?next=/insights/2026/11/the-article/"
BODY_SNIPPET = "Continuation vehicles expanded the opportunity set"
ARTICLE_HTML = (
    "<html><body><nav>menu</nav><article><p>" + BODY_SNIPPET + ". "
    + "Private equity secondaries research paragraph. " * 40
    + "</p></article></body></html>"
)
GATE_HTML = "<html><body>" + "Please sign in to continue. " * 60 + "</body></html>"
CHALLENGE_HTML = (
    "<!DOCTYPE html><title>Checking your browser...</title>"
    "<noscript><p>Javascript required</p></noscript>"
)


# ── offline transport ────────────────────────────────────────────────────────

class _Route:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = dict(headers or {})


class _OriginalResponse:
    """What requests reads Set-Cookie from (``raw._original_response.msg``)."""

    def __init__(self, msg):
        self.msg = msg

    def isclosed(self):
        return True

    def close(self):
        pass


class _FakeAdapter(BaseAdapter):
    """Answers from a routing function and records every PreparedRequest.

    Responses are built with requests' own HTTPAdapter.build_response over a
    urllib3 HTTPResponse, and ``_original_response.msg`` carries the headers,
    so the Session extracts Set-Cookie and follows Location itself.
    """

    def __init__(self, route):
        super().__init__()
        self.route = route
        self.sent: list[requests.PreparedRequest] = []
        self.kwargs: list[dict] = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        self.kwargs.append(kwargs)
        r = self.route(request)
        if isinstance(r, Exception):
            raise r
        msg = email.message.Message()
        for k, v in r.headers.items():
            for one in (v if isinstance(v, list) else [v]):
                msg[k] = one
        raw = HTTPResponse(
            body=io.BytesIO(r.body),
            headers=[(k, v) for k, v in msg.items()],
            status=r.status,
            preload_content=False,
            original_response=_OriginalResponse(msg),
        )
        return HTTPAdapter().build_response(request, raw)

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    """Install a routing function; every Session the module creates uses it."""
    holder = {}

    def install(route):
        adapter = _FakeAdapter(route)
        holder["adapter"] = adapter
        real_session = requests.Session
        created = []

        class _RoutedSession(real_session):
            def __init__(self):
                super().__init__()
                created.append(self)
                self.get_calls = 0
                self.mount("https://", adapter)
                self.mount("http://", adapter)

            def get(self, *a, **k):
                self.get_calls += 1
                return super().get(*a, **k)

        monkeypatch.setattr(tm.requests, "Session", _RoutedSession)
        adapter.sessions = created
        return adapter

    return install


def _ok(html=ARTICLE_HTML, ctype="text/html; charset=UTF-8"):
    return _Route(200, html, {"Content-Type": ctype})


# ── RED for the incident: httpx is challenged, requests gets the page ────────

def test_article_text_is_read_where_only_httpx_is_challenged(transport, monkeypatch):
    class _ChallengedHttpx:
        def __init__(self, *a, **k):
            self.cookies = {}

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def get(self, url, **k):
            return type("R", (), {"status_code": 403, "url": url,
                                  "text": CHALLENGE_HTML})()

    monkeypatch.setattr(tm.httpx, "Client", _ChallengedHttpx)
    transport(lambda req: _ok())
    text = tm._extract_article_text(ARTICLE_URL)
    assert text is not None and BODY_SNIPPET in text


# ── requests session behaviour (plan §5 PR-B rules 1-4, §6 item 5) ───────────

def test_plain_200_one_get_with_timeout_and_redirects(transport):
    adapter = transport(lambda req: _ok())
    resp = tm._get_with_auth_retry(ARTICLE_URL, timeout=17)
    assert resp is not None and resp.status_code == 200
    assert adapter.sessions[-1].get_calls == 1
    assert [k["timeout"] for k in adapter.kwargs] == [17]
    sent = adapter.sent[0]
    assert sent.headers["User-Agent"] == tm.HEADERS["User-Agent"]
    assert sent.headers["Accept-Language"] == tm.HEADERS["Accept-Language"]


def test_canonical_redirect_is_followed_inside_one_session_get(transport):
    def route(req):
        if req.url == ARTICLE_URL:
            return _Route(301, b"", {"Location": ARTICLE_URL.rstrip("/")})
        return _ok()
    adapter = transport(route)
    text = tm._extract_article_text(ARTICLE_URL)
    assert text is not None and BODY_SNIPPET in text
    # the adapter sees both hops, the helper issued one Session.get
    assert len(adapter.sent) == 2
    assert adapter.sessions[-1].get_calls == 1
    assert all(k["timeout"] == 20 for k in adapter.kwargs)


def test_cookie_gate_retries_once_with_the_extracted_cookie(transport):
    def route(req):
        if req.url.startswith(GATE_URL.split("?")[0]):
            return _Route(200, GATE_HTML, {"Content-Type": "text/html; charset=UTF-8",
                                           "Set-Cookie": "ADVISOR_CHECK=1; Path=/"})
        if "ADVISOR_CHECK=1" in (req.headers.get("Cookie") or ""):
            return _ok()
        return _Route(302, b"", {"Location": GATE_URL})
    adapter = transport(route)
    text = tm._extract_article_text(ARTICLE_URL, timeout=19)
    assert text is not None and BODY_SNIPPET in text
    session = adapter.sessions[-1]
    assert session.get_calls == 2
    second_article_request = [r for r in adapter.sent if r.url == ARTICLE_URL][-1]
    assert "ADVISOR_CHECK=1" in second_article_request.headers["Cookie"]
    assert all(k["timeout"] == 19 for k in adapter.kwargs)


def test_hard_gate_gives_none_and_is_not_a_js_shell(transport):
    def route(req):
        if req.url.startswith(GATE_URL.split("?")[0]):
            return _Route(200, GATE_HTML, {"Content-Type": "text/html",
                                           "Set-Cookie": "SESSION=1; Path=/"})
        return _Route(302, b"", {"Location": GATE_URL})
    adapter = transport(route)
    assert tm._extract_article_text(ARTICLE_URL) is None
    assert adapter.sessions[-1].get_calls == 2      # retried exactly once
    assert tm._is_likely_js_only(ARTICLE_URL) is False


def test_real_js_shell_still_detected(transport):
    shell = ("<html><head>" + "<script>var x=1;</script>" * 200
             + "</head><body><div id='root'></div></body></html>")
    adapter = transport(lambda req: _ok(shell))
    assert tm._is_likely_js_only(ARTICLE_URL) is True
    # both the probe and its inner _extract_article_text got the same 200 shell
    assert len(adapter.sent) == 2
    assert all(r.url == ARTICLE_URL for r in adapter.sent)


def test_same_url_403_returns_the_response_and_no_text(transport):
    transport(lambda req: _Route(403, CHALLENGE_HTML, {"Content-Type": "text/html"}))
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    assert resp is not None and resp.status_code == 403
    assert tm._extract_article_text(ARTICLE_URL) is None


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError("refused"),
    requests.exceptions.ReadTimeout("slow"),
])
def test_transport_errors_give_none(transport, exc):
    transport(lambda req: exc)
    assert tm._get_with_auth_retry(ARTICLE_URL) is None
    assert tm._extract_article_text(ARTICLE_URL) is None


def test_redirect_loop_gives_none(transport):
    transport(lambda req: _Route(302, b"", {"Location": req.url + "x"}))
    assert tm._get_with_auth_retry(ARTICLE_URL) is None


# ── encoding: decode like httpx 0.28 (plan §5 PR-B rule 5) ───────────────────

UTF8_TEXT = "café — naïve"


@pytest.mark.parametrize("ctype,expected", [
    ("text/html", UTF8_TEXT),
    ("text/xml", UTF8_TEXT),
    ("application/rss+xml", UTF8_TEXT),
    ("application/xml", UTF8_TEXT),
    (None, UTF8_TEXT),
    ("text/html; charset=", UTF8_TEXT),
    ("text/html; charset=no-such-codec", UTF8_TEXT),
    ("application/json", UTF8_TEXT),
    ("text/html; charset=UTF-8", UTF8_TEXT),
    ("text/html; charset=iso-8859-1", UTF8_TEXT.encode("utf-8").decode("iso-8859-1")),
    ('text/html; charset=iso-8859-1; x="a;charset=utf-8;b"',
     UTF8_TEXT.encode("utf-8").decode("iso-8859-1")),
])
def test_text_is_decoded_the_way_httpx_decodes_it(transport, ctype, expected):
    headers = {"Content-Type": ctype} if ctype is not None else {}
    transport(lambda req: _Route(200, UTF8_TEXT.encode("utf-8"), headers))
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    assert resp.text == expected


@pytest.mark.parametrize("ctype", ["text/html; charset=no-such-codec", "text/html; charset=",
                                   "text/html"])
def test_unusable_charset_sets_utf8_explicitly(transport, ctype):
    """requests' .text would fall back on its own for an unknown codec, but
    resp.encoding must still read utf-8, as httpx's .encoding does."""
    transport(lambda req: _Route(200, UTF8_TEXT.encode("utf-8"), {"Content-Type": ctype}))
    assert tm._get_with_auth_retry(ARTICLE_URL).encoding == "utf-8"


def test_encoding_rule_matches_httpx_on_the_same_bytes():
    """Same bytes and headers through httpx.Response and the new rule."""
    import httpx
    body = UTF8_TEXT.encode("utf-8")
    for ctype in ("text/html", "text/xml", "application/rss+xml", None,
                  "text/html; charset=iso-8859-1", "text/html; charset=bogus"):
        headers = {"Content-Type": ctype} if ctype else {}
        hx = httpx.Response(200, headers=headers, content=body)
        raw = HTTPResponse(body=io.BytesIO(body), headers=headers, status=200,
                           preload_content=False)
        req = requests.Request("GET", ARTICLE_URL).prepare()
        rq = HTTPAdapter().build_response(req, raw)
        tm._apply_charset_rule(rq)
        assert rq.text == hx.text, ctype


def test_old_and_new_paths_extract_the_same_body_from_the_same_bytes(transport, monkeypatch):
    """Regression for in-flight trials: identical response bytes → identical text."""
    html = ARTICLE_HTML.replace("research", "recherche — été")
    # old path: httpx response over the same bytes, through the extractor
    import httpx
    from bs4 import BeautifulSoup
    hx = httpx.Response(200, headers={"Content-Type": "text/html"}, content=html.encode("utf-8"))
    old_text = tm._extract_body_from_soup(BeautifulSoup(hx.text, "html.parser"))
    assert old_text is not None and BODY_SNIPPET in old_text and "été" in old_text
    transport(lambda req: _Route(200, html.encode("utf-8"), {"Content-Type": "text/html"}))
    new_text = tm._extract_article_text(ARTICLE_URL)
    assert new_text == old_text


# ── headers and environment auth (plan §5 PR-A/PR-B, §6 items 4/8) ────────────

def test_trial_headers_equal_production_headers():
    import fetch_articles
    assert tm.HEADERS == fetch_articles.HEADERS


def test_netrc_behaviour_on_redirects_is_recorded(transport, monkeypatch, tmp_path):
    """requests keeps trust_env (as production does), so a netrc entry is sent;
    a cross-host redirect drops it, and the target host gets only its own."""
    other = "https://cdn.other-host.com"
    netrc = tmp_path / "netrc"
    netrc.write_text(
        "machine www.example-fund.com login fund password fundpw\n"
        "machine cdn.other-host.com login cdn password cdnpw\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    import base64

    def basic(user, pw):
        return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    # 1) start host matches → its credentials
    adapter = transport(lambda req: _ok())
    tm._get_with_auth_retry(ARTICLE_URL)
    assert adapter.sent[0].headers.get("Authorization") == basic("fund", "fundpw")

    # 2) cross-host redirect to a host with no entry → credentials dropped
    netrc.write_text("machine www.example-fund.com login fund password fundpw\n")

    def to_unlisted(req):
        if req.url == ARTICLE_URL:
            return _Route(302, b"", {"Location": "https://unlisted.example.net/a"})
        return _ok()
    adapter = transport(to_unlisted)
    tm._get_with_auth_retry(ARTICLE_URL)
    assert adapter.sent[0].headers.get("Authorization") == basic("fund", "fundpw")
    assert "Authorization" not in adapter.sent[1].headers

    # 3) redirect to a host that has its own entry → only the target's
    netrc.write_text(
        "machine www.example-fund.com login fund password fundpw\n"
        "machine cdn.other-host.com login cdn password cdnpw\n")

    def to_cdn(req):
        if req.url == ARTICLE_URL:
            return _Route(302, b"", {"Location": other + "/a"})
        return _ok()
    adapter = transport(to_cdn)
    tm._get_with_auth_retry(ARTICLE_URL)
    assert adapter.sent[1].headers.get("Authorization") == basic("cdn", "cdnpw")


# ── end to end, offline: template links → requests bodies → Haiku → scores ───

def test_templated_trial_samples_three_bodies_through_requests(transport, monkeypatch):
    """Plan §6 item 7: three distinct URLs, each body reaches the Haiku prompt,
    and the saved scores are the model's own (not the zero fill for a
    dropped article)."""
    urls = [HOST + f"/insights/2026/11/piece-{i}/" for i in (1, 2, 3)]
    snippets = {u: f"Distinct finding number {i} on credit secondaries"
                for i, u in enumerate(urls, 1)}
    cand = {"id": "example-fund", "status": "visitable",
            "listing_template": {"type": "rss_feed", "feed": "listing_url"},
            "listing_url": HOST + "/feed/"}
    monkeypatch.setattr(tm, "load_candidates", lambda: [cand])

    class _Templates:
        @staticmethod
        def candidate_source(c):
            return c

        @staticmethod
        def fetch(source):
            return [{"url": u} for u in urls]

    monkeypatch.setattr(tm, "_listing_templates", lambda: _Templates)

    def route(req):
        html = ("<html><body><article><p>" + snippets[req.url] + ". "
                + "Long-form research paragraph. " * 40 + "</p></article></body></html>")
        return _Route(200, html, {"Content-Type": "text/html"})
    transport(route)

    prompts = []
    model_scores = {1: (0.9, 0.8, 1.0), 2: (0.6, 0.5, 0.9), 3: (0.7, 0.9, 0.8)}

    def fake_haiku(prompt, max_retries=1):
        prompts.append(prompt)
        return {"articles": [
            {"article_num": n, "relevance": r, "depth": d, "extractable": e,
             "notes": f"piece {n}"} for n, (r, d, e) in model_scores.items()]}
    monkeypatch.setattr(tm, "_call_haiku", fake_haiku)
    monkeypatch.setattr(tm, "_extract_article_text_playwright",
                        lambda url: pytest.fail("browser fallback used"))

    result = tm.sample_article_quality(cand["listing_url"], trial={"id": "example-fund"})

    assert result.get("error") is None
    scored = result["articles"]
    assert [a["url"] for a in scored] == urls            # three distinct URLs, in order
    assert len(prompts) == 1
    for u in urls:
        assert snippets[u] in prompts[0]                  # each body reached the model
    for n, a in enumerate(scored, 1):
        r, d, e = model_scores[n]
        assert (a["relevance"], a["depth"], a["extractable"]) == (r, d, e)
        assert a["overall"] == round(0.4 * r + 0.4 * d + 0.2 * e, 3) > 0
