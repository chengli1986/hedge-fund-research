"""The validated-candidate URL probe uses requests, like production (2026-11-26).

Why: on 2026-11-25 and 11-26 the 04:30 health run exited 1 because the probe
of gcm-grosvenor (visitable, waiting for a trial slot) got HTTP 403, 1762
bytes: a "Checking your browser" page served to httpx. requests, with the same
URL and headers, gets the feed (200, 93,862 bytes) — and production lists and
fetches with requests. The probe now measures with the production client and
headers. Plan: ~/tmp/gmia-candidate-probe-plan.md V0.4, PR-A.

Transport tests run the real requests stack (requests.get → Session →
redirects, netrc) over an offline adapter.
"""

import base64
import email.message
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.response import HTTPResponse

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gfh_probe_rq", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(_spec)
sys.modules["gfh_probe_rq"] = gfh
_spec.loader.exec_module(gfh)

FEED = "https://www.example-fund.com/feed/"
BIG = b"<rss>" + b"<item>research</item>" * 1000 + b"</rss>"
CHALLENGE = b"<!DOCTYPE html><title>Checking your browser...</title>"


# ── offline transport for the real requests stack ─────────────────────────────

class _Route:
    def __init__(self, status=200, body=b"", headers=None):
        self.status, self.body, self.headers = status, body, dict(headers or {})


class _OriginalResponse:
    def __init__(self, msg):
        self.msg = msg

    def isclosed(self):
        return True

    def close(self):
        pass


class _FakeAdapter(BaseAdapter):
    def __init__(self, route):
        super().__init__()
        self.route, self.sent, self.kwargs = route, [], []

    def send(self, request, **kwargs):
        self.sent.append(request)
        self.kwargs.append(kwargs)
        r = self.route(request)
        if isinstance(r, Exception):
            raise r
        msg = email.message.Message()
        for k, v in r.headers.items():
            msg[k] = v
        raw = HTTPResponse(body=io.BytesIO(r.body), headers=list(msg.items()), status=r.status,
                           preload_content=False, original_response=_OriginalResponse(msg))
        return HTTPAdapter().build_response(request, raw)

    def close(self):
        pass


@pytest.fixture
def transport(monkeypatch):
    def install(route):
        adapter = _FakeAdapter(route)
        real = requests.sessions.Session

        class _Routed(real):
            def __init__(self):
                super().__init__()
                self.mount("https://", adapter)
                self.mount("http://", adapter)

        monkeypatch.setattr(requests.sessions, "Session", _Routed)
        return adapter
    return install


class _ChallengedHttpx:
    """httpx as gcm-grosvenor answered it on 2026-11-25."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *e):
        return False

    def get(self, url, **k):
        return type("R", (), {"status_code": 403, "content": CHALLENGE, "url": url})()


# ── the incident ─────────────────────────────────────────────────────────────

def test_candidate_challenged_only_for_httpx_is_ok(transport, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", _ChallengedHttpx)
    transport(lambda req: _Route(200, BIG, {"Content-Type": "application/rss+xml; charset=UTF-8"}))
    r = gfh.probe_candidate_url({"id": "gcm-grosvenor", "listing_url": FEED})
    assert r["status"] == "OK", r
    assert r["http_code"] == 200 and r["content_size"] == len(BIG)


# ── what is sent ─────────────────────────────────────────────────────────────

def test_probe_sends_production_headers_timeout_and_follows_redirects(transport):
    def route(req):
        if req.url == FEED:
            return _Route(301, b"", {"Location": FEED + "rss2/"})
        return _Route(200, BIG)
    adapter = transport(route)
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert r["status"] == "OK"
    assert [p.url for p in adapter.sent] == [FEED, FEED + "rss2/"]
    import fetch_articles
    for p in adapter.sent:
        for k, v in fetch_articles.HEADERS.items():
            assert p.headers[k] == v
    assert all(k["timeout"] == gfh.CANDIDATE_PROBE_TIMEOUT_S == 15 for k in adapter.kwargs)


def test_probe_headers_equal_production_headers():
    import fetch_articles
    assert gfh.CANDIDATE_PROBE_HEADERS == fetch_articles.HEADERS
    assert not hasattr(gfh, "CANDIDATE_PROBE_UA")


@pytest.mark.parametrize("cand,want", [
    ({"research_url": "https://a.example/r", "listing_url": "https://a.example/l"}, "https://a.example/r"),
    ({"listing_url": "https://a.example/l", "homepage_url": "https://a.example/"}, "https://a.example/l"),
    ({"homepage_url": "https://a.example/"}, "https://a.example/"),
])
def test_url_priority_unchanged(transport, cand, want):
    adapter = transport(lambda req: _Route(200, BIG))
    assert gfh.probe_candidate_url(cand)["status"] == "OK"
    assert [p.url for p in adapter.sent] == [want]


# ── verdicts (unchanged semantics) ───────────────────────────────────────────

def test_thin_body_is_warn(transport):
    transport(lambda req: _Route(200, b"<html>tiny</html>"))
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert r["status"] == "WARN" and "thin body" in r["reason"]


@pytest.mark.parametrize("code", [403, 404, 500])
def test_http_error_is_fail_with_code(transport, code):
    transport(lambda req: _Route(code, CHALLENGE))
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert r["status"] == "FAIL" and r["http_code"] == code and r["reason"] == f"HTTP {code}"


@pytest.mark.parametrize("exc,name", [
    (requests.exceptions.ReadTimeout("slow"), "ReadTimeout"),
    (requests.exceptions.ConnectionError("refused"), "ConnectionError"),
])
def test_transport_error_is_fail_naming_the_exception(transport, exc, name):
    transport(lambda req: exc)
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert r["status"] == "FAIL" and r["http_code"] is None and r["reason"].startswith(name)


def test_no_url_is_fail_without_a_request(transport):
    adapter = transport(lambda req: pytest.fail("requested"))
    r = gfh.probe_candidate_url({"id": "x"})
    assert r["status"] == "FAIL" and adapter.sent == []


# ── environment auth (requests keeps trust_env, as production does) ───────────

def test_netrc_behaviour_on_redirects_is_recorded(transport, monkeypatch, tmp_path):
    netrc = tmp_path / "netrc"
    monkeypatch.setenv("NETRC", str(netrc))

    def basic(u, p):
        return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

    netrc.write_text("machine www.example-fund.com login fund password fundpw\n"
                     "machine cdn.other-host.com login cdn password cdnpw\n")
    netrc.chmod(0o600)
    adapter = transport(lambda req: _Route(200, BIG))
    gfh.probe_candidate_url({"listing_url": FEED})
    assert adapter.sent[0].headers.get("Authorization") == basic("fund", "fundpw")

    netrc.write_text("machine www.example-fund.com login fund password fundpw\n")
    adapter = transport(lambda req: _Route(302, b"", {"Location": "https://unlisted.example.net/f"})
                        if req.url == FEED else _Route(200, BIG))
    gfh.probe_candidate_url({"listing_url": FEED})
    assert "Authorization" not in adapter.sent[1].headers

    netrc.write_text("machine www.example-fund.com login fund password fundpw\n"
                     "machine cdn.other-host.com login cdn password cdnpw\n")
    adapter = transport(lambda req: _Route(302, b"", {"Location": "https://cdn.other-host.com/f"})
                        if req.url == FEED else _Route(200, BIG))
    gfh.probe_candidate_url({"listing_url": FEED})
    assert adapter.sent[1].headers.get("Authorization") == basic("cdn", "cdnpw")


# ── entry point: exit code, email, isolation from production sources ─────────

def _result(status="OK", reason="ok"):
    return {"status": status, "reason": reason, "articles_count": 10, "content_chars": 5000,
            "most_recent_date": "2026-11-25", "elapsed_ms": 5}


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    """main() with every input stubbed and every write isolated to tmp_path."""
    sources = [{"id": "prod-a"}, {"id": "prod-b"}]
    probes = {"prod-a": _result(), "prod-b": _result()}
    cands = [{"id": "waiting", "status": "visitable", "listing_url": FEED},
             {"id": "in-trial", "status": "visitable", "listing_url": FEED + "t/"}]
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps(cands))
    trial_file = tmp_path / "trial-state.json"
    trial_file.write_text(json.dumps({"active_trials": [{"id": "in-trial"}]}))
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "gmia-fetcher-health.json")
    monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(gfh, "load_sources", lambda: sources)
    seen = {"probed_sources": [], "mail": []}

    def probe_source(s):
        seen["probed_sources"].append(s["id"])
        return dict(probes[s["id"]])
    monkeypatch.setattr(gfh, "probe_source", probe_source)
    monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
    for name in ("pipeline_intake_anomalies", "recent_analysis_declines", "pipeline_zero_fetches",
                 "entrypoint_problems", "corrupt_state_backups"):
        monkeypatch.setattr(gfh, name, lambda *a, **k: [])
    monkeypatch.setattr(gfh, "content_orphans", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "send_email", lambda body, subject, **k: seen["mail"].append((subject, body)) or True)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", *argv])
        return gfh.main()
    return {"run": run, "seen": seen, "probes": probes}


def test_main_candidate_fail_exits_1_and_mails_the_candidate(main_env, transport):
    adapter = transport(lambda req: _Route(404, b"gone"))
    assert main_env["run"]("--email", "--include-validated") == 1
    assert [p.url for p in adapter.sent] == [FEED]          # in-trial candidate not probed
    (subject, body), = main_env["seen"]["mail"]
    assert "waiting" in subject and "CANDIDATE URL FAILING" in body and "HTTP 404" in body


def test_main_candidate_warn_exits_0(main_env, transport):
    transport(lambda req: _Route(200, b"tiny"))
    assert main_env["run"]("--email", "--include-validated") == 0


def test_main_candidate_ok_where_httpx_is_challenged_exits_0(main_env, transport, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "Client", _ChallengedHttpx)
    transport(lambda req: _Route(200, BIG))
    assert main_env["run"]("--email", "--include-validated") == 0
    assert main_env["seen"]["mail"] == []


def test_main_production_fail_still_exits_1(main_env, transport):
    transport(lambda req: _Route(200, BIG))
    main_env["probes"]["prod-b"] = _result("FAIL", "fetch_articles returned 0 articles")
    assert main_env["run"]("--email", "--include-validated") == 1


def test_main_without_include_validated_probes_no_candidate(main_env, transport):
    adapter = transport(lambda req: pytest.fail("candidate probed"))
    assert main_env["run"]("--email") == 0
    assert adapter.sent == []


def test_candidate_switch_does_not_touch_production_probes(main_env, transport):
    transport(lambda req: _Route(200, BIG))
    main_env["run"]("--include-validated", "--dry-run")
    with_candidates = list(main_env["seen"]["probed_sources"])
    main_env["seen"]["probed_sources"].clear()
    main_env["run"]("--dry-run")
    assert with_candidates == main_env["seen"]["probed_sources"] == ["prod-a", "prod-b"]
