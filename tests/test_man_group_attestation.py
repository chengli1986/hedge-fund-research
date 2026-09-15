"""Man Group: the insights listing now sits behind a role attestation (2026-09-15).

From 2026-09-15 https://www.man.com/insights answers 200 with a "Confirm your
region / country / Select your role" form (form#mangroup-digital-attestation-
form, POST /insights) and no article cards: 0 links with requests and with
headless Chromium, so the listing fetched 0 articles. The choice is kept in
the server-side Drupal session, not in a cookie that could be set statically
as metlife's disclaimer cookie is, so the form is submitted in a session --
Americas / United States (237) / Institutional Investor (2), the same
declaration GMIA already makes for metlife, approved by the owner -- and the
listing is read in that session. Article pages still serve their body without
it (div.digital-article, checked live), so only the listing does this.
"""
import http.client
import types
from urllib.parse import parse_qs

import requests

import fetch_articles as fa

SOURCE = {"id": "man-group", "url": "https://www.man.com/insights", "max_articles": 10}

GATE = """<html><body>
<form class="mangroup-digital-attestation-form" action="/insights" method="post" id="mangroup-digital-attestation-form">
  <input type="hidden" name="regions" value="AM">
  <select name="country_AM"><option value=""></option><option value="237" selected="selected">United States</option></select>
  <select name="investor_type"><option value="" selected="selected"></option><option value="2">Institutional Investor</option>
    <option value="28">Financial Professional</option></select>
  <input type="hidden" name="selected_country" value="237">
  <input type="hidden" name="form_build_id" value="form-abc123">
  <input type="hidden" name="form_id" value="mangroup_digital_attestation_form">
  <button name="op" value="Accept" type="submit">Accept</button>
</form></body></html>"""

LISTING = """<html><body><div class="card"><a href="/insights/views-from-the-floor-2026-9-Sept">
  <div class="text-blue">Article</div><div class="bg-primary"><span>Views From the Floor</span><span>Sep 2026</span></div>
  <h5>The Mispriced Debt Powering the AI Boom</h5><span class="fs-6">Summary.</span></a></div></body></html>"""


def _site(monkeypatch, gated=True):
    """A fake man.com: the listing is gated until the session has attested."""
    state = {"posts": []}

    def send(self, request, **kw):
        r = requests.Response()
        r.request = request
        r.url = request.url
        r.headers["Content-Type"] = "text/html"
        attested = "attested=1" in (request.headers.get("Cookie") or "")
        if request.method == "POST":
            state["posts"].append(parse_qs(request.body if isinstance(request.body, str) else request.body.decode()))
            r.status_code = 200
            # requests stores cookies from raw._original_response.msg; a bare
            # Response header is ignored, so the fake session would never attest.
            msg = http.client.HTTPMessage()
            msg["Set-Cookie"] = "attested=1; Path=/"
            r.raw = types.SimpleNamespace(_original_response=types.SimpleNamespace(msg=msg))
            r._content = LISTING.encode()
        else:
            r.status_code = 200
            r._content = (LISTING if (attested or not gated) else GATE).encode()
        return r

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return state


def test_a_gated_listing_is_attested_and_read(monkeypatch):
    state = _site(monkeypatch, gated=True)
    articles = fa.fetch_man_group(SOURCE)
    assert [a["title"] for a in articles] == ["The Mispriced Debt Powering the AI Boom"]
    assert len(state["posts"]) == 1
    posted = state["posts"][0]
    assert posted["investor_type"] == ["2"] and posted["country_AM"] == ["237"] and posted["regions"] == ["AM"]
    assert posted["op"] == ["Accept"]
    assert posted["form_build_id"] == ["form-abc123"], "the form's own hidden fields must be sent back"


def test_an_ungated_listing_is_not_attested(monkeypatch):
    state = _site(monkeypatch, gated=False)
    articles = fa.fetch_man_group(SOURCE)
    assert len(articles) == 1 and state["posts"] == []


def test_a_gate_that_does_not_open_yields_nothing_rather_than_the_form(monkeypatch):
    def send(self, request, **kw):
        r = requests.Response(); r.request = request; r.url = request.url
        r.status_code = 200; r.headers["Content-Type"] = "text/html"; r._content = GATE.encode()
        return r
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    assert fa.fetch_man_group(SOURCE) == []
