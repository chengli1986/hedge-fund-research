"""Trial sampling fetches article pages with requests, like production (2026-11-26).

Why: gcm-grosvenor (visitable, waiting for a trial slot) serves a "Checking your
browser" challenge (HTTP 403, 1762 bytes) to httpx but the page itself to
requests, on the same URL with the same headers (measured once, 2026-11-25
22:16Z).  Production lists and fetches bodies with requests (listing_templates.py,
fetch_content.py).  If that difference held, a trial sampling through httpx
would score nothing, and -- only if the quantity and freshness gates passed
and fewer than 3 valid samples were collected over the whole trial -- end
"inconclusive" (watchlist) for a client difference, not for content quality.

These tests drive the real ``requests.Session`` (redirects, cookie extraction,
netrc lookup) over an offline transport adapter, so they check what the
session actually sends, not what a stub claims.  The last section runs the
daily entry cmd_run() end to end (valid rss_feed template, tmp state files,
fake Haiku only) and reads the saved trial-state back.  Not covered here: the
real responses of the candidates in trial (needs an online fetch).  Plan: ~/tmp/gmia-candidate-
probe-plan.md V0.4, PR-B.
"""

import email.message
import importlib.util
import io
import json
import re
import sys
from datetime import datetime, timedelta
from email.utils import format_datetime
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

_REAL_SESSION = requests.sessions.Session

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
    """Install a routing function; every Session the module creates uses it.

    Each Session records the raw kwargs its ``get`` was called with (before
    requests fills in defaults) and whether it was closed.  With
    ``module_get=True`` the module-level ``requests.get`` -- what
    listing_templates.fetch uses for an RSS feed -- is routed too, by
    replacing ``requests.sessions.Session``, which ``requests.api`` looks up
    on every call.
    """
    holder = {}

    def install(route, module_get=False):
        adapter = _FakeAdapter(route)
        holder["adapter"] = adapter
        real_session = _REAL_SESSION
        created = []

        class _RoutedSession(real_session):
            def __init__(self):
                super().__init__()
                created.append(self)
                self.get_calls = 0
                self.get_kwargs: list[dict] = []
                self.closed = 0
                self.mount("https://", adapter)
                self.mount("http://", adapter)

            def get(self, *a, **k):
                self.get_calls += 1
                self.get_kwargs.append(dict(k))
                return super().get(*a, **k)

            def close(self):
                self.closed += 1
                super().close()

        monkeypatch.setattr(tm.requests, "Session", _RoutedSession)
        if module_get:
            monkeypatch.setattr(requests.sessions, "Session", _RoutedSession)
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


# ── Session.get kwargs, retry condition, close (plan §5 PR-B rules 1-2) ──────
# requests follows redirects by default, so these read the kwargs as passed to
# Session.get: a GET that dropped ``allow_redirects=True`` would still follow
# the redirect and every behavioural test above would stay green.

def _gate_route(set_cookie: bool):
    def route(req):
        if req.url.startswith(GATE_URL.split("?")[0]):
            headers = {"Content-Type": "text/html; charset=UTF-8"}
            if set_cookie:
                headers["Set-Cookie"] = "ADVISOR_CHECK=1; Path=/"
            return _Route(200, GATE_HTML, headers)
        if "ADVISOR_CHECK=1" in (req.headers.get("Cookie") or ""):
            return _ok()
        return _Route(302, b"", {"Location": GATE_URL})
    return route


def test_every_session_get_passes_timeout_and_allow_redirects_explicitly(transport):
    # plain 200: one GET
    adapter = transport(lambda req: _ok())
    tm._get_with_auth_retry(ARTICLE_URL, timeout=13)
    assert adapter.sessions[-1].get_kwargs == [{"timeout": 13, "allow_redirects": True}]
    # cookie gate: the first GET and the retry
    adapter = transport(_gate_route(set_cookie=True))
    resp = tm._get_with_auth_retry(ARTICLE_URL, timeout=14)
    assert resp.status_code == 200 and BODY_SNIPPET in resp.text
    assert adapter.sessions[-1].get_kwargs == [
        {"timeout": 14, "allow_redirects": True},
        {"timeout": 14, "allow_redirects": True},
    ]


def test_gate_url_without_a_cookie_is_not_retried(transport):
    """Retry needs both halves of ``gate and session.cookies``: a gate that set
    no cookie has nothing to retry with."""
    adapter = transport(_gate_route(set_cookie=False))
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    session = adapter.sessions[-1]
    assert tm._is_auth_gate_url(ARTICLE_URL, str(resp.url))     # it did land on the gate
    assert not session.cookies
    assert session.get_calls == 1


def test_cookie_without_a_gate_is_not_retried(transport):
    """A site that sets a cookie on an ordinary page (or a canonical redirect)
    is not retried: the cookie alone is not the condition."""
    def route(req):
        if req.url == ARTICLE_URL:
            return _Route(301, b"", {"Location": ARTICLE_URL.rstrip("/"),
                                     "Set-Cookie": "visitor=abc; Path=/"})
        return _Route(200, ARTICLE_HTML, {"Content-Type": "text/html; charset=UTF-8",
                                          "Set-Cookie": "consent=1; Path=/"})
    adapter = transport(route)
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    session = adapter.sessions[-1]
    assert resp.status_code == 200
    assert not tm._is_auth_gate_url(ARTICLE_URL, str(resp.url))
    assert {c.name for c in session.cookies} == {"visitor", "consent"}
    assert session.get_calls == 1


def test_session_is_closed_after_success(transport):
    adapter = transport(lambda req: _ok())
    assert tm._get_with_auth_retry(ARTICLE_URL) is not None
    assert [s.closed for s in adapter.sessions] == [1]


def test_session_is_closed_when_the_adapter_raises(transport):
    adapter = transport(lambda req: requests.exceptions.ConnectionError("refused"))
    assert tm._get_with_auth_retry(ARTICLE_URL) is None
    assert [s.closed for s in adapter.sessions] == [1]


# ── encoding: charset selected by httpx 0.28's rule (plan §5 PR-B rule 5) ────
# The guarantee is the charset selection rule.  For the UTF-8 and single-byte
# bodies below the decoded text is also equal; that is not true of every
# codec (see the utf-16 boundary test at the end of this section).

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
def test_charset_is_selected_by_the_httpx_rule(transport, ctype, expected):
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


def test_encoding_rule_matches_httpx_on_the_same_utf8_bytes():
    """Same UTF-8 bytes and headers through httpx.Response and the new rule."""
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


@pytest.mark.skipif(sys.byteorder != "little", reason="records the little-endian result only")
def test_utf16_without_bom_differs_httpx_raises_requests_decodes(transport):
    """Boundary of the guarantee: same charset chosen, different decoding.

    ``charset=utf-16`` over UTF-16LE bytes without a BOM: httpx 0.28.1's
    incremental decoder raises UnicodeError on ``.text`` (so the old helper's
    caller would have returned None), while requests 2.31 decodes the body in
    the machine's byte order and the article text comes out.  Recorded, not
    endorsed: if either library changes this, the test says so.
    """
    import httpx
    html = ARTICLE_HTML.replace("research", "recherche — été")
    body = html.encode("utf-16-le")
    headers = {"Content-Type": "text/html; charset=utf-16"}
    hx = httpx.Response(200, headers=headers, content=body)
    with pytest.raises(UnicodeError):
        hx.text
    transport(lambda req: _Route(200, body, headers))
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    assert resp.encoding == "utf-16"
    assert resp.text == html
    text = tm._extract_article_text(ARTICLE_URL)
    assert text is not None and BODY_SNIPPET in text and "été" in text


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


# ── daily entry, offline: cmd_run → RSS template → requests bodies → Haiku ──
# Plan §6 item 7 and the §5 mid-trial rules, run through cmd_run() (the cron
# entry), not through sample_article_quality(): the listing is a real, valid
# rss_feed template served by the offline transport, every body is fetched by
# the requests helper, only the Haiku call is faked, and the assertions read
# back the trial-state file cmd_run wrote.
#
# One frozen clock: every datetime.now() in the trial manager (cmd_run's
# today and elapsed days, the freshness check) and every date the fixtures
# build read the same instant, mid-morning BJT, so a run that straddles BJT
# midnight cannot see two different days.

NOW = datetime(2026, 11, 26, 10, 0, 0, tzinfo=tm.BJT)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


FEED_URL = HOST + "/feed/"
CAND_ID = "example-fund"
RSS_SPEC = {"type": "rss_feed", "feed": "url"}     # listing_url holds the feed


def _day(offset: int) -> str:
    """The BJT date ``offset`` days before the frozen NOW."""
    return (NOW.date() - timedelta(days=offset)).strftime("%Y-%m-%d")


def _art_url(n: int) -> str:
    return HOST + f"/insights/2026/11/piece-{n}/"


def _snippet(n: int) -> str:
    return f"Distinct finding number {n} on credit secondaries"


def _art_page(n: int) -> str:
    return ("<html><body><nav>menu</nav><article><p>" + _snippet(n) + ". "
            + "Long-form research paragraph. " * 40 + "</p></article></body></html>")


def _feed_xml(nums) -> str:
    pub = format_datetime(NOW - timedelta(days=1))
    items = "".join(f"<item><title>Piece {n}</title><link>{_art_url(n)}</link>"
                    f"<pubDate>{pub}</pubDate></item>" for n in nums)
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>Example Fund</title>{items}</channel></rss>")


def _old_sample(start_offset: int, day: int, nums, score: float) -> dict:
    """A saved sample for trial ``day`` of a trial that started ``start_offset``
    days before NOW; its date is derived from that start (day 1 = start)."""
    assert 1 <= day <= start_offset + 1
    arts = [{"url": _art_url(n), "relevance": score, "depth": score,
             "extractable": score, "overall": score, "notes": f"old {n}"} for n in nums]
    return {"sampled": len(arts), "articles": arts,
            "avg_score": score if arts else 0.0,
            "error": None if arts else "No new (un-sampled) article links available",
            "js_only_count": 0, "js_only_checked": 0, "day": day,
            "date": _day(start_offset - (day - 1))}


def _active_trial(start_offset: int, check_offsets, samples) -> dict:
    return {
        "id": CAND_ID, "name": "Example Fund",
        "research_url": HOST + "/insights/", "homepage_url": HOST,
        "fit_score": 0.9, "quality": "HIGH", "topics": None,
        "start_date": _day(start_offset), "end_date": None,
        "daily_checks": {_day(o): {"accessible": True, "article_count": 9, "date_count": 9,
                                   "newest_date": _day(o + 1), "article_urls": [],
                                   "error": None, "fetcher_used": True}
                         for o in check_offsets},
        "auto_decided": False, "outcome": None,
        "quality_samples": samples,
    }


@pytest.fixture
def daily_run(tmp_path, monkeypatch, transport):
    """cmd_run() against tmp state files, the offline transport and a fake Haiku.

    Returns run(feed_nums, state, scores) -> dict with the saved state, the
    prompts sent to Haiku, the article GETs and the mails that would go out.
    ``scores`` maps article_num -> (relevance, depth, extractable); the fake
    answers in descending article_num order, so a positional mapping would
    attach the wrong score.
    """
    import listing_templates
    listing_templates.validate_spec(RSS_SPEC)             # a spec production accepts
    candidate = {"id": CAND_ID, "name": "Example Fund", "status": "visitable",
                 "quality": "HIGH", "fit_score": 0.9,
                 "research_url": HOST + "/insights/", "homepage_url": HOST,
                 "listing_url": FEED_URL, "listing_template": RSS_SPEC}
    cands = tmp_path / "fund_candidates.json"
    cands.write_text(json.dumps([candidate]))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": []}))
    state_file = tmp_path / "trial-state.json"
    monkeypatch.setattr(tm, "CANDIDATES_FILE", cands)
    monkeypatch.setattr(tm, "SOURCES_FILE", sources)
    monkeypatch.setattr(tm, "TRIAL_STATE_FILE", state_file)
    monkeypatch.setattr(tm, "ENV_FILE", tmp_path / "nonexistent.env")
    monkeypatch.setattr(tm, "ANTHROPIC_KEY_FILE", tmp_path / "nonexistent-key.env")
    monkeypatch.setattr(tm, "AUTO_PROMOTE_LOG", tmp_path / "auto-promote-history.jsonl")
    monkeypatch.setattr(tm, "PENDING_PROFILES_DIR", tmp_path / "pending_profiles")
    assert CAND_ID not in tm._load_fetchers()              # template path, no fetcher
    monkeypatch.setattr(tm, "datetime", _FrozenDatetime)

    mails = {"trial": [], "summary": []}
    monkeypatch.setattr(tm, "send_trial_email",
                        lambda trial, passed, *a, **k: mails["trial"].append((trial["id"], passed)))
    monkeypatch.setattr(tm, "send_daily_summary_email",
                        lambda state, today: mails["summary"].append(today))
    monkeypatch.setattr(tm.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP used"))
    monkeypatch.setattr(tm.smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("SMTP_SSL used"))
    monkeypatch.setattr(tm, "_extract_article_text_playwright",
                        lambda url: pytest.fail("browser fallback used"))

    def run(feed_nums, state, scores):
        state_file.write_text(json.dumps(state))
        by_url = {_art_url(n): n for n in feed_nums}

        def route(req):
            if req.url == FEED_URL:
                return _Route(200, _feed_xml(feed_nums),
                              {"Content-Type": "application/rss+xml; charset=UTF-8"})
            if req.url in by_url:
                return _Route(200, _art_page(by_url[req.url]), {"Content-Type": "text/html"})
            return _Route(404, b"not found", {"Content-Type": "text/plain"})
        adapter = transport(route, module_get=True)

        prompts = []

        def fake_haiku(prompt, max_retries=1):
            prompts.append(prompt)
            return {"articles": [
                {"article_num": n, "relevance": r, "depth": d, "extractable": e,
                 "notes": f"piece {n}"} for n, (r, d, e) in sorted(scores.items(), reverse=True)]}
        monkeypatch.setattr(tm, "_call_haiku", fake_haiku)

        saves = []
        real_save = tm.save_state

        def recording_save(st):
            saves.append(json.loads(json.dumps(st)))
            real_save(st)
        monkeypatch.setattr(tm, "save_state", recording_save)

        tm.cmd_run()
        return {"state": json.loads(state_file.read_text()),
                "saves": saves,
                "prompts": prompts,
                "article_gets": [r.url for r in adapter.sent if r.url != FEED_URL],
                "feed_gets": sum(1 for r in adapter.sent if r.url == FEED_URL),
                "candidates": json.loads(cands.read_text()),
                "mails": mails}

    return run


_HEADER_RE = re.compile(r"--- Article (\d+) \(URL: (.*?)\) ---")


def _assert_prompt_articles(prompt: str, nums_and_urls) -> None:
    """The prompt holds exactly these article headers, in this order, and each
    block carries its own body and no other article's."""
    assert _HEADER_RE.findall(prompt) == [(str(n), u) for n, u in nums_and_urls]
    for n, u in nums_and_urls:
        block = _article_block(prompt, n, u)
        body = int(u.rstrip("/").rsplit("-", 1)[1])          # piece-<k>
        assert _snippet(body) in block
        assert all(_snippet(k) not in block for k in range(1, 13) if k != body)


def _assert_old_urls_absent(prompt: str, old_nums) -> None:
    """Already-scored articles appear nowhere in the prompt: not as a header,
    not in the preamble, not as a link or body text inside another block."""
    for n in old_nums:
        assert _art_url(n) not in prompt
        assert _snippet(n) not in prompt


def _assert_saved_scores(sample: dict, urls, scores: dict) -> None:
    """The saved articles carry the model's own scores, matched by article
    number to URL (not the zero fill for an article the model dropped)."""
    assert [a["url"] for a in sample["articles"]] == list(urls)
    overall = []
    for n, (a, url) in enumerate(zip(sample["articles"], urls), 1):
        r, d, e = scores[n]
        assert (a["url"], a["relevance"], a["depth"], a["extractable"]) == (url, r, d, e)
        assert a["overall"] == round(0.4 * r + 0.4 * d + 0.2 * e, 3) > 0
        overall.append(a["overall"])
    assert sample["avg_score"] == round(sum(overall) / len(overall), 3)


def _article_block(prompt: str, n: int, url: str) -> str:
    """The text between Article n's header and the next one."""
    start = prompt.index(f"--- Article {n} (URL: {url}) ---")
    nxt = prompt.find("--- Article ", start + 1)
    return prompt[start:nxt if nxt != -1 else len(prompt)]


def test_daily_run_starts_a_templated_trial_and_saves_three_model_scores(daily_run):
    """Plan §6 item 7: from cmd_run, three distinct URLs; each body reaches the
    Haiku prompt under its own article number; the trial-state file holds the
    model's scores for those numbers, not the zero fill for a dropped article."""
    urls = [_art_url(n) for n in (1, 2, 3)]
    scores = {1: (0.9, 0.8, 1.0), 2: (0.6, 0.5, 0.9), 3: (0.7, 0.9, 0.8)}
    out = daily_run([1, 2, 3], {"active_trials": [], "history": []}, scores)

    today = _day(0)
    trials = out["state"]["active_trials"]
    assert [t["id"] for t in trials] == [CAND_ID]
    trial = trials[0]
    assert trial["start_date"] == today
    check = trial["daily_checks"][today]
    assert check["accessible"] and check["article_count"] == 3 and check["fetcher_used"]

    assert out["feed_gets"] == 2                          # count, then sampling links
    assert out["article_gets"] == urls                    # each body once, via requests
    assert len(out["prompts"]) == 1
    _assert_prompt_articles(out["prompts"][0], list(enumerate(urls, 1)))

    samples = trial["quality_samples"]
    assert len(samples) == 1
    sample = samples[0]
    assert (sample["day"], sample["date"], sample["error"], sample["sampled"]) == (1, today, None, 3)
    assert [a["url"] for a in sample["articles"]] == urls
    for n, a in enumerate(sample["articles"], 1):
        r, d, e = scores[n]
        assert (a["relevance"], a["depth"], a["extractable"], a["notes"]) == (r, d, e, f"piece {n}")
        assert a["overall"] == round(0.4 * r + 0.4 * d + 0.2 * e, 3) > 0
    overall = [round(0.4 * r + 0.4 * d + 0.2 * e, 3) for r, d, e in scores.values()]
    assert sample["avg_score"] == round(sum(overall) / 3, 3)
    assert out["mails"] == {"trial": [], "summary": [today]}


def test_resumed_trial_excludes_old_urls_and_keeps_old_samples(daily_run):
    """Plan §5 mid-trial switch: the excluded URLs come from the saved
    trial-state, the earlier samples stay as they were, and today's sample is
    appended for new URLs only."""
    old = [_old_sample(2, 1, (1, 2, 3), 0.5), _old_sample(2, 2, (4, 5, 6), 0.6)]
    state = {"active_trials": [_active_trial(2, (2, 1), old)], "history": []}
    out = daily_run(list(range(1, 10)), json.loads(json.dumps(state)),
                    {1: (0.8, 0.8, 0.8), 2: (0.8, 0.8, 0.8), 3: (0.8, 0.8, 0.8)})

    trial = out["state"]["active_trials"][0]
    assert trial["daily_checks"][_day(0)]["article_count"] == 9
    assert trial["quality_samples"][:2] == old             # untouched
    new = trial["quality_samples"][2]
    assert (new["day"], new["date"], new["error"]) == (3, _day(0), None)
    assert [a["url"] for a in new["articles"]] == [_art_url(n) for n in (7, 8, 9)]
    assert out["article_gets"] == [_art_url(n) for n in (7, 8, 9)]   # 1-6 never fetched
    assert len(out["prompts"]) == 1
    _assert_prompt_articles(out["prompts"][0], [(i, _art_url(n)) for i, n in enumerate((7, 8, 9), 1)])
    _assert_old_urls_absent(out["prompts"][0], range(1, 7))
    _assert_saved_scores(new, [_art_url(n) for n in (7, 8, 9)],
                         {1: (0.8, 0.8, 0.8), 2: (0.8, 0.8, 0.8), 3: (0.8, 0.8, 0.8)})
    assert not trial["auto_decided"] and out["state"]["history"] == []


def test_resumed_trial_skips_a_day_already_sampled(daily_run):
    """Same trial day already recorded (a second run that day): no fetch, no
    Haiku call, the saved samples and checks unchanged."""
    old = [_old_sample(2, 1, (1, 2, 3), 0.5), _old_sample(2, 2, (4, 5, 6), 0.6),
           _old_sample(2, 3, (7, 8, 9), 0.7)]
    assert old[2]["date"] == _day(0)
    seeded = _active_trial(2, (2, 1, 0), old)
    state = {"active_trials": [seeded], "history": []}
    out = daily_run(list(range(1, 13)), json.loads(json.dumps(state)),
                    {1: (0.8, 0.8, 0.8)})

    trial = out["state"]["active_trials"][0]
    assert trial["quality_samples"] == old
    assert trial["daily_checks"] == seeded["daily_checks"]
    assert out["prompts"] == []
    assert out["article_gets"] == [] and out["feed_gets"] == 0


def test_resumed_trial_interrupted_after_todays_count_samples_once(daily_run):
    """Interrupted between cmd_run's two saves: today's daily_check is on disk,
    today's quality sample is not.  The rerun reuses the saved count (no
    listing fetch for counting, the seeded record unchanged), samples only
    URLs not scored before, and writes today's sample to disk exactly once."""
    old = [_old_sample(2, 1, (1, 2, 3), 0.5), _old_sample(2, 2, (4, 5, 6), 0.6)]
    seeded = _active_trial(2, (2, 1, 0), old)
    seeded["daily_checks"][_day(0)]["article_count"] = 7     # marker: not what the feed lists
    state = {"active_trials": [seeded], "history": []}
    out = daily_run(list(range(1, 10)), json.loads(json.dumps(state)),
                    {1: (0.8, 0.7, 0.9), 2: (0.6, 0.6, 0.6), 3: (0.9, 0.9, 0.9)})

    trial = out["state"]["active_trials"][0]
    assert trial["daily_checks"] == seeded["daily_checks"]   # count reused, not redone
    assert out["feed_gets"] == 1                             # the sampling listing only
    assert trial["quality_samples"][:2] == old
    assert len(trial["quality_samples"]) == 3
    new = trial["quality_samples"][2]
    assert (new["day"], new["date"], new["error"], new["sampled"]) == (3, _day(0), None, 3)
    assert [a["url"] for a in new["articles"]] == [_art_url(n) for n in (7, 8, 9)]
    assert out["article_gets"] == [_art_url(n) for n in (7, 8, 9)]
    assert len(out["prompts"]) == 1
    _assert_prompt_articles(out["prompts"][0], [(i, _art_url(n)) for i, n in enumerate((7, 8, 9), 1)])
    _assert_old_urls_absent(out["prompts"][0], range(1, 7))
    # the scores on disk are the model's, URL by URL: (0.78 + 0.60 + 0.90) / 3
    _assert_saved_scores(new, [_art_url(n) for n in (7, 8, 9)],
                         {1: (0.8, 0.7, 0.9), 2: (0.6, 0.6, 0.6), 3: (0.9, 0.9, 0.9)})
    assert new["avg_score"] == 0.76
    # today's sample reaches disk in one save, and no save carries it twice
    per_save = [[s["day"] for s in st["active_trials"][0]["quality_samples"]]
                for st in out["saves"]]
    assert per_save == [[1, 2, 3]]


def test_verdict_combines_old_and_new_scores(daily_run):
    """Day 7: the verdict averages the saved scores from earlier days with
    today's.  Old alone (0.30) would fail quality, new alone would average
    0.90; only the combined 3.9/7 = 0.557 is right."""
    old = ([_old_sample(6, 1, (1, 2), 0.3), _old_sample(6, 2, (3, 4), 0.3)]
           + [_old_sample(6, d, (), 0.0) for d in (3, 4, 5, 6)])
    state = {"active_trials": [_active_trial(6, (6, 5, 4, 3, 2, 1), old)], "history": []}
    out = daily_run(list(range(1, 8)), json.loads(json.dumps(state)),
                    {1: (0.9, 0.9, 0.9), 2: (0.9, 0.9, 0.9), 3: (0.9, 0.9, 0.9)})

    assert out["state"]["active_trials"] == []
    decided = out["state"]["history"][-1]
    assert decided["id"] == CAND_ID and decided["end_date"] == _day(0)
    assert decided["quality_samples"][:6] == old
    new = decided["quality_samples"][6]
    assert new["day"] == 7
    assert [a["url"] for a in new["articles"]] == [_art_url(n) for n in (5, 6, 7)]
    assert decided["avg_quality_score"] == round(3.9 / 7, 3)
    assert decided["outcome"] == "pass"
    assert out["candidates"][0]["status"] == "promoted"
    assert out["mails"]["trial"] == [(CAND_ID, True)]
