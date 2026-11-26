"""The validated-candidate URL probe uses requests, like production (2026-11-26).

Why: on 2026-11-25 and 11-26 the 04:30 health run exited 1 because the probe
of gcm-grosvenor (visitable, waiting for a trial slot) got HTTP 403, 1762
bytes: a "Checking your browser" page served to httpx. In one comparison of
that feed URL at that time, requests with the same headers got 200, 93,862
bytes of feed; production lists with requests. The probe now measures with
the production client and headers. Plan: ~/tmp/gmia-candidate-probe-plan.md
V0.4, PR-A.

Transport tests run the real requests stack (requests.get → Session →
redirects, netrc) over an offline adapter.
"""

import atexit
import base64
import contextlib
import copy
import email.message
import email.utils
import importlib.util
import io
import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter, HTTPAdapter
from urllib3.response import HTTPResponse

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@contextlib.contextmanager
def import_logs_in(log_dir: Path):
    """While importing fetch_articles / fetch_content, send the log files they
    open at import (logs/fetch.log, logs/fetch_content.log: mkdir + FileHandler
    on BASE_DIR/logs) to log_dir instead, so a clean checkout gets no logs/.
    Only the repo's logs/ directory is redirected; anything else is untouched."""
    repo_logs = REPO / "logs"
    real_handler, real_mkdir = logging.FileHandler, Path.mkdir

    class _Redirected(real_handler):
        def __init__(self, filename, *a, **k):
            path = Path(filename)
            if path.parent == repo_logs:
                filename = log_dir / path.name
            super().__init__(filename, *a, **k)

    def mkdir(self, *a, **k):
        if self == repo_logs:
            return None
        return real_mkdir(self, *a, **k)

    logging.FileHandler, Path.mkdir = _Redirected, mkdir
    try:
        yield
    finally:
        logging.FileHandler, Path.mkdir = real_handler, real_mkdir


# No-ops when an earlier test module already imported them.
IMPORT_LOG_DIR = Path(tempfile.mkdtemp(prefix="gmia-import-logs-"))
atexit.register(shutil.rmtree, IMPORT_LOG_DIR, True)
with import_logs_in(IMPORT_LOG_DIR):
    import fetch_articles  # noqa: E402
    import fetch_content  # noqa: E402,F401

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
    for p in adapter.sent:
        for k, v in fetch_articles.HEADERS.items():
            assert p.headers[k] == v
    assert all(k["timeout"] == gfh.CANDIDATE_PROBE_TIMEOUT_S == 15 for k in adapter.kwargs)


def test_probe_headers_equal_production_headers():
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


# "é" is 2 bytes in UTF-8: both bodies decode to 2,500 characters, so only a
# byte count tells 4,999 from 5,000 (the threshold).
@pytest.mark.parametrize("body,status", [
    ("é" * 2499 + "a", "WARN"),
    ("é" * 2500, "OK"),
], ids=["4999-bytes", "5000-bytes"])
def test_thin_body_threshold_counts_bytes_not_characters(transport, body, status):
    raw = body.encode("utf-8")
    assert len(body) == 2500 and len(raw) in (4999, 5000)
    transport(lambda req: _Route(200, raw, {"Content-Type": "text/html; charset=utf-8"}))
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert r["status"] == status, r
    assert r["content_size"] == len(raw)
    assert gfh.CANDIDATE_SHELL_HTML_THRESHOLD == 5000


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

def _basic(u, p):
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()


FUND_AUTH = _basic("fund", "fundpw")
CDN_AUTH = _basic("cdn", "cdnpw")


# One redirect chain per case: the origin host is in netrc, so hop 1 must carry
# its credentials -- otherwise "hop 2 has none" would prove nothing.
@pytest.mark.parametrize("target,target_auth", [
    ("https://unlisted.example.net/f", None),
    ("https://cdn.other-host.com/f", CDN_AUTH),
], ids=["target-not-in-netrc", "target-in-netrc"])
def test_netrc_credentials_across_a_cross_host_redirect(transport, monkeypatch, tmp_path,
                                                         target, target_auth):
    netrc = tmp_path / "netrc"
    netrc.write_text("machine www.example-fund.com login fund password fundpw\n"
                     "machine cdn.other-host.com login cdn password cdnpw\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    adapter = transport(lambda req: _Route(302, b"", {"Location": target})
                        if req.url == FEED else _Route(200, BIG))
    r = gfh.probe_candidate_url({"listing_url": FEED})
    assert [p.url for p in adapter.sent] == [FEED, target]
    assert adapter.sent[0].headers.get("Authorization") == FUND_AUTH
    assert adapter.sent[1].headers.get("Authorization") == target_auth
    assert r["status"] == "OK" and r["http_code"] == 200 and r["content_size"] == len(BIG)


# ── entry point: exit code, email, isolation from production sources ─────────

def _result(status="OK", reason="ok"):
    return {"status": status, "reason": reason, "articles_count": 10, "content_chars": 5000,
            "most_recent_date": "2026-11-25", "elapsed_ms": 5}


CANDIDATES = [{"id": "waiting", "status": "visitable", "listing_url": FEED},
              {"id": "in-trial", "status": "visitable", "listing_url": FEED + "t/"}]


def _isolate_main(monkeypatch, tmp_path, sources):
    """main() with every input but the probes stubbed and every write in tmp_path.

    Returns the record of what the entry point saw: each candidate it probed
    (id, URL, verdict) and each email it sent.
    """
    cand_file = tmp_path / "fund_candidates.json"
    cand_file.write_text(json.dumps(CANDIDATES))
    trial_file = tmp_path / "trial-state.json"
    trial_file.write_text(json.dumps({"active_trials": [{"id": "in-trial"}]}))
    monkeypatch.setattr(gfh, "CANDIDATES_FILE", cand_file)
    monkeypatch.setattr(gfh, "TRIAL_STATE_FILE", trial_file)
    monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "gmia-fetcher-health.json")
    monkeypatch.setattr(gfh, "LOGS_DIR", tmp_path)          # save_state mkdirs it
    monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(gfh, "load_sources", lambda: copy.deepcopy(sources))
    monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
    for name in ("pipeline_intake_anomalies", "recent_analysis_declines", "pipeline_zero_fetches",
                 "entrypoint_problems", "corrupt_state_backups"):
        monkeypatch.setattr(gfh, name, lambda *a, **k: [])
    monkeypatch.setattr(gfh, "content_orphans", lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    seen = {"candidates": [], "mail": []}
    monkeypatch.setattr(gfh, "send_email", lambda body, subject, **k: seen["mail"].append((subject, body)) or True)

    real_probe_candidate = gfh.probe_candidate_url

    def probe_candidate(c):
        r = real_probe_candidate(c)
        seen["candidates"].append((c["id"], gfh.candidate_probe_url(c), r["status"]))
        return r
    monkeypatch.setattr(gfh, "probe_candidate_url", probe_candidate)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", *argv])
        return gfh.main()
    return run, seen


@pytest.fixture
def main_env(monkeypatch, tmp_path):
    """main() with stubbed production probes (prod-a, prod-b)."""
    probes = {"prod-a": _result(), "prod-b": _result()}
    run, seen = _isolate_main(monkeypatch, tmp_path, [{"id": "prod-a"}, {"id": "prod-b"}])
    seen["probed_sources"] = []

    def probe_source(s):
        seen["probed_sources"].append(s["id"])
        return dict(probes[s["id"]])
    monkeypatch.setattr(gfh, "probe_source", probe_source)
    return {"run": run, "seen": seen, "probes": probes}


def _candidate_line(out, icon, code, size):
    return f"  {icon} {'waiting':30s} HTTP {code!s:>3s}  {size:>8d}b" in out


def test_main_candidate_fail_exits_1_and_mails_the_candidate(main_env, transport, capsys):
    adapter = transport(lambda req: _Route(404, b"gone"))
    assert main_env["run"]("--email", "--include-validated") == 1
    assert [p.url for p in adapter.sent] == [FEED]          # in-trial candidate not probed
    assert main_env["seen"]["candidates"] == [("waiting", FEED, "FAIL")]
    assert _candidate_line(capsys.readouterr().out, "✗", 404, 4)
    (subject, body), = main_env["seen"]["mail"]
    assert "waiting" in subject and "CANDIDATE URL FAILING" in body and "HTTP 404" in body


def test_main_candidate_warn_exits_0(main_env, transport, capsys):
    adapter = transport(lambda req: _Route(200, b"tiny"))
    assert main_env["run"]("--email", "--include-validated") == 0
    assert [p.url for p in adapter.sent] == [FEED]
    assert main_env["seen"]["candidates"] == [("waiting", FEED, "WARN")]
    assert _candidate_line(capsys.readouterr().out, "⚠", 200, 4)
    assert main_env["seen"]["mail"] == []


def test_main_candidate_ok_where_httpx_is_challenged_exits_0(main_env, transport, monkeypatch, capsys):
    import httpx
    monkeypatch.setattr(httpx, "Client", _ChallengedHttpx)
    adapter = transport(lambda req: _Route(200, BIG))
    assert main_env["run"]("--email", "--include-validated") == 0
    assert [p.url for p in adapter.sent] == [FEED]
    assert main_env["seen"]["candidates"] == [("waiting", FEED, "OK")]
    assert _candidate_line(capsys.readouterr().out, "✓", 200, len(BIG))
    assert main_env["seen"]["mail"] == []


def test_main_production_fail_still_exits_1(main_env, transport, capsys):
    adapter = transport(lambda req: _Route(200, BIG))
    main_env["probes"]["prod-b"] = _result("FAIL", "fetch_articles returned 0 articles")
    assert main_env["run"]("--email", "--include-validated") == 1
    # The candidate passed, so the exit code and the mail are the production failure's.
    assert [p.url for p in adapter.sent] == [FEED]
    assert main_env["seen"]["candidates"] == [("waiting", FEED, "OK")]
    assert _candidate_line(capsys.readouterr().out, "✓", 200, len(BIG))
    (subject, body), = main_env["seen"]["mail"]
    assert "1 FAILING (prod-b)" in subject and "candidate" not in subject
    assert "fetch_articles returned 0 articles" in body and "CANDIDATE URL FAILING" not in body


def test_main_without_include_validated_probes_no_candidate(main_env, transport):
    adapter = transport(lambda req: pytest.fail("candidate probed"))
    assert main_env["run"]("--email") == 0
    assert adapter.sent == [] and main_env["seen"]["candidates"] == []


def test_candidate_switch_does_not_touch_production_probes(main_env, transport):
    transport(lambda req: _Route(200, BIG))
    main_env["run"]("--include-validated", "--dry-run")
    with_candidates = list(main_env["seen"]["probed_sources"])
    main_env["seen"]["probed_sources"].clear()
    main_env["run"]("--dry-run")
    assert with_candidates == main_env["seen"]["probed_sources"] == ["prod-a", "prod-b"]


# ── production sources through the REAL probe_source ──────────────────────────
# Six production source ids (their real content fetchers; the probe-relevant
# fields copied verbatim from config/sources.json on 2026-11-26, so a config
# edit cannot change the test), served offline. Between them they reach every
# probe outcome main() reports: OK, stale WARN, zero-article WARN, content FAIL,
# known block, site down.

# One clock for the feeds and for the probe's article ages: 12:00 BJT, so a
# UTC feed date and the BJT "today" fall on the same calendar day.
REFERENCE_NOW = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return REFERENCE_NOW.astimezone(tz) if tz else REFERENCE_NOW.replace(tzinfo=None)


def freeze_clock(monkeypatch, module):
    """The health script's datetime.now() (article age, run stamps) = REFERENCE_NOW."""
    monkeypatch.setattr(module, "datetime", _FrozenDatetime)


def _rfc2822(days_ago):
    return email.utils.format_datetime(REFERENCE_NOW - timedelta(days=days_ago))


def _feed(links, days_ago):
    items = "".join(f"<item><title>Paper {i}</title><link>{u}</link>"
                    f"<pubDate>{_rfc2822(days_ago + i)}</pubDate><description>d</description></item>"
                    for i, u in enumerate(links))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>f</title>{items}</channel></rss>'.encode()


_PARA = "Rates, credit spreads and earnings revisions moved together this quarter. " * 12
RSS = {"Content-Type": "application/rss+xml; charset=UTF-8"}
HTML = {"Content-Type": "text/html; charset=UTF-8"}

PROD_SOURCES = json.loads(r'''
[
 {
  "id": "capital-fund-management",
  "url": "https://www.cfm.com/feed/",
  "frequency": "weekly",
  "expected_hostname": "cfm.com",
  "max_articles": 10,
  "listing_template": {
   "type": "rss_feed",
   "feed": "url"
  },
  "listing_template_active": true
 },
 {
  "id": "crescat-capital",
  "url": "https://www.crescat.net/category/quarterly-letter/crescat-updates/research-letter-performance-update-crescat-updates/feed/",
  "frequency": "monthly",
  "expected_hostname": "crescat.net",
  "listing_template": {
   "type": "rss_feed",
   "feed": "url",
   "sort": "date_desc"
  },
  "listing_template_active": true
 },
 {
  "id": "madison-investments",
  "url": "https://madisoninvestments.com/feed/",
  "frequency": "monthly",
  "expected_hostname": "madisoninvestments.com",
  "max_articles": 10,
  "listing_template": {
   "type": "rss_feed",
   "feed": "url",
   "sort": "date_desc"
  },
  "listing_template_active": true
 },
 {
  "id": "fulcrum-asset-management",
  "url": "https://fulcrumasset.com/feed/?post_type=insights",
  "frequency": "weekly",
  "expected_hostname": "fulcrumasset.com",
  "max_articles": 10,
  "listing_template": {
   "type": "rss_feed",
   "feed": "url",
   "path_prefix": "/insights/",
   "sort": "date_desc"
  },
  "listing_template_active": true,
  "known_media_mix": "Most items are Vimeo 'Deeper Dive' / 'On the Horizon' pages under /insights/videos-podcasts/ whose page holds only a 94-208 char intro; on 2026-11-09 the three newest were all such pages (3 of 12 stored rows have a body, last written insight 2026-10-21), so the content probe found nothing to read and WARNed 'content mix' every night. Decided 2026-11-09: report that warning as known, do not alert; any other failure still alerts."
 },
 {
  "id": "ark-invest",
  "url": "https://www.ark-invest.com/feed",
  "frequency": "weekly",
  "expected_hostname": "ark-invest.com",
  "listing_template": {
   "type": "rss_feed",
   "feed": "url",
   "sort": "date_desc",
   "categories": [
    "analyst research",
    "market commentary",
    "white papers",
    "market insights",
    "newsletters",
    "crypto reports",
    "bitcoin brainstorm",
    "valuation models",
    "the brainstorm",
    "for your innovation"
   ]
  },
  "listing_template_active": true,
  "known_block": "Article pages refuse this server: every www.ark-invest.com/articles|videos page returns 403 (blocked_by_bot_protection) since 2026-10-13, re-checked daily to 2026-10-16; the RSS feed and robots.txt still answer 200, so new articles are listed with title only. Decided 2026-10-16: keep the source as is, no bypass."
 },
 {
  "id": "bnp-paribas-am",
  "url": "https://www.bnpparibas-am.com/en-au/feed/",
  "frequency": "weekly",
  "expected_hostname": "bnpparibas-am.com",
  "max_articles": 10,
  "listing_template": {
   "type": "rss_feed",
   "feed": "rss_url"
  },
  "listing_template_active": true
 }
]
''')


CRESCAT_FEED = ("https://www.crescat.net/category/quarterly-letter/crescat-updates/"
                "research-letter-performance-update-crescat-updates/feed/")


def _prod_routes():
    cfm = [f"https://www.cfm.com/news-and-insights/paper-{i}/" for i in range(3)]
    crescat = [f"https://www.crescat.net/letter-{i}/" for i in range(3)]
    madison = [f"https://madisoninvestments.com/insights/note-{i}/" for i in range(3)]
    routes = {
        "https://www.cfm.com/feed/": _Route(200, _feed(cfm, 2), RSS),
        CRESCAT_FEED: _Route(200, _feed(crescat, 200), RSS),
        "https://madisoninvestments.com/feed/": _Route(200, _feed(madison, 3), RSS),
        # Every item is a video page outside the /insights/ prefix: 0 rows.
        "https://fulcrumasset.com/feed/?post_type=insights": _Route(
            200, _feed(["https://fulcrumasset.com/videos/v-1/"], 1), RSS),
        "https://www.ark-invest.com/feed": _Route(403, CHALLENGE, HTML),
        "https://www.bnpparibas-am.com/en-au/feed/": _Route(503, b"Scheduled maintenance", HTML),
    }
    for u in cfm:
        routes[u] = _Route(200, f'<html><body><div class="text-content"><p>{_PARA}</p></div>'
                                f'</body></html>'.encode(), HTML)
    for u in crescat:
        routes[u] = _Route(200, f'<html><body><article><div class="entry-content"><p>{_PARA}</p>'
                                f'</div></article></body></html>'.encode(), HTML)
    for u in madison:
        routes[u] = _Route(404, b"<html><title>Not found</title></html>", HTML)
    return routes


@pytest.fixture
def prod_world(monkeypatch, tmp_path, transport):
    """main() over the six sources above with the real probe_source; the
    waiting candidate is served too. Records each probe_source result and
    the per-source table main() reports."""
    run, seen = _isolate_main(monkeypatch, tmp_path, PROD_SOURCES)
    monkeypatch.setattr(gfh, "RETRY_SLEEP_S", 0)
    freeze_clock(monkeypatch, gfh)
    routes = _prod_routes()
    routes[FEED] = _Route(200, BIG, RSS)
    unknown = []

    def route(req):
        if req.url in routes:
            return routes[req.url]
        unknown.append(req.url)
        return _Route(599, b"unrouted")
    adapter = transport(route)
    seen["probe_source"], seen["reported"] = {}, []
    real_probe_source = gfh.probe_source

    def probe_source(s):
        r = real_probe_source(s)
        seen["probe_source"][s["id"]] = copy.deepcopy(r)
        return r
    monkeypatch.setattr(gfh, "probe_source", probe_source)
    real_report = gfh.print_console_report

    def report(per_source, *a, **k):
        seen["reported"].append(copy.deepcopy(per_source))
        return real_report(per_source, *a, **k)
    monkeypatch.setattr(gfh, "print_console_report", report)
    seen["alerts"], seen["next_state"] = [], []
    real_classify, real_merge = gfh.classify_alerts, gfh.merge_into_state

    def classify(per_source, prev_state):
        alerts = real_classify(per_source, prev_state)
        seen["alerts"].append({kind: sorted(row[0] for row in rows) for kind, rows in alerts.items()})
        return alerts

    def merge(prev_state, per_source):
        nxt = real_merge(prev_state, per_source)
        seen["next_state"].append(copy.deepcopy(nxt))
        return nxt
    monkeypatch.setattr(gfh, "classify_alerts", classify)
    monkeypatch.setattr(gfh, "merge_into_state", merge)
    yield {"run": run, "seen": seen, "adapter": adapter}
    assert unknown == [], f"requests outside the offline routes: {unknown}"


def _without_timing(per_source):
    return {sid: {k: v for k, v in r.items() if k != "elapsed_ms"} for sid, r in per_source.items()}


def _state_without_timing(state):
    """The state main() would write, minus this run's clock: elapsed times
    dropped, this run's timestamp replaced by a marker."""
    run = state["last_run"]
    return {sid: {k: ("<this run>" if v == run else v) for k, v in rec.items() if k != "last_elapsed_ms"}
            for sid, rec in state["sources"].items()}


def _production_requests(adapter, start=0):
    return [p.url for p in adapter.sent[start:] if not p.url.startswith(FEED)]


def _one_run(world, *argv):
    """One main() run with every observation cleared first; each observation
    point must have fired exactly once in THIS run, so nothing is carried over."""
    adapter, seen = world["adapter"], world["seen"]
    seen["reported"], seen["alerts"], seen["next_state"], seen["candidates"] = [], [], [], []
    seen["probe_source"] = {}
    mark = len(adapter.sent)
    assert world["run"](*argv) == 1
    assert len(seen["reported"]) == len(seen["alerts"]) == len(seen["next_state"]) == 1, \
        {k: len(seen[k]) for k in ("reported", "alerts", "next_state")}
    assert list(seen["probe_source"]) == [s["id"] for s in PROD_SOURCES]
    assert seen["next_state"][0]["last_run"] == "2026-11-26T12:00:00+08:00"   # the frozen clock
    return (_production_requests(adapter, mark), _without_timing(seen["probe_source"]),
            _without_timing(seen["reported"][0]), seen["alerts"][0],
            _state_without_timing(seen["next_state"][0]))


def test_real_production_probes_reach_every_outcome(prod_world):
    _, probed, reported, alerts, _ = _one_run(prod_world, "--dry-run")
    st = {sid: (r["status"], r["articles_count"]) for sid, r in probed.items()}
    assert st == {"capital-fund-management": ("OK", 3), "crescat-capital": ("WARN", 3),
                  "madison-investments": ("FAIL", 3), "fulcrum-asset-management": ("WARN", 0),
                  "ark-invest": ("FAIL", 0), "bnp-paribas-am": ("FAIL", 0)}
    assert probed["capital-fund-management"]["content_chars"] > 500
    crescat = probed["crescat-capital"]
    assert crescat["most_recent_age_days"] == 200
    assert crescat["reason"].startswith("stale: most recent article 200d old (frequency=monthly, threshold 90d)")
    assert probed["madison-investments"]["reason"].startswith("all top 3 articles failed content probe")
    assert probed["fulcrum-asset-management"]["reason"].startswith("fetch_articles returned 0 articles")
    assert "403" in probed["ark-invest"]["reason"] and probed["bnp-paribas-am"]["upstream_down"] == 503
    assert reported["ark-invest"]["status"] == "KNOWN" and reported["bnp-paribas-am"]["status"] == "UPSTREAM"
    assert alerts["failing"] == ["madison-investments"] and alerts["upstream"] == ["bnp-paribas-am"]


def test_candidate_switch_leaves_real_production_probes_identical(prod_world):
    """Same sources, same state, same responses, same clock: --include-validated
    changes nothing per source -- requests made, probe results, the reported
    table, the alert classification and the state that would be written."""
    seen = prod_world["seen"]
    on = _one_run(prod_world, "--include-validated", "--dry-run")
    assert seen["candidates"] == [("waiting", FEED, "OK")]
    off = _one_run(prod_world, "--dry-run")
    assert seen["candidates"] == []                               # not probed
    assert on == off
    # Listing, then content pages until one passes; fulcrum's 0 rows add the diagnostic GET.
    assert on[0] == ["https://www.cfm.com/feed/", "https://www.cfm.com/news-and-insights/paper-0/",
                     CRESCAT_FEED, "https://www.crescat.net/letter-0/",
                     "https://madisoninvestments.com/feed/",
                     *[f"https://madisoninvestments.com/insights/note-{i}/" for i in range(3)],
                     "https://fulcrumasset.com/feed/?post_type=insights",
                     "https://fulcrumasset.com/feed/?post_type=insights",
                     "https://www.ark-invest.com/feed", "https://www.bnpparibas-am.com/en-au/feed/"]
    assert on[1]["crescat-capital"]["most_recent_age_days"] == 200
    assert on[1]["crescat-capital"]["reason"].startswith("stale: most recent article 200d old")
    assert on[4].keys() == {s["id"] for s in PROD_SOURCES}
    assert on[3]["failing"] == ["madison-investments"] and on[3]["known"] == ["ark-invest"]
