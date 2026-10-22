"""A site that is down for one night is reported, not failed (2026-10-22).

research-center.amundi.com, www.amundi.com and about.amundi.com all answered
503 "Scheduled maintenance is currently in progress" (Retry-After: 300) from
the 03:45 fetch to at least 10:40 BJT. The health check failed amundi, exited
1, cron-wrapper mailed and the liveness audit said BAIL: three mails for an
outage on the other side that needs nothing from us. mfs-investment-management
did the same on 2026-09-26 and was back the next night.

A listing step that still gets 502/503/504 after the retry is UPSTREAM on the
first night: listed in the email and on the health page, not counted in the
exit code. A second night in a row is FAIL as before. Anything else (403, 404,
a selector that finds nothing) fails on the first night as it always did.

The email also told the reader to roll the template back, which cannot help
when the whole site is down; that hint is now left off a 5xx.
"""
import importlib.util
import sys
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gfh_up", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(_spec)
sys.modules["gfh_up"] = gfh
_spec.loader.exec_module(gfh)

_ph_spec = importlib.util.spec_from_file_location("ph_up", REPO / "scripts" / "publish_health.py")
ph = importlib.util.module_from_spec(_ph_spec)
sys.modules["ph_up"] = ph
_ph_spec.loader.exec_module(ph)

URL = "https://research-center.amundi.com/rss.xml"
DOWN = ("fetch_articles raised HTTPError: 503 Server Error: Service Temporarily Unavailable "
        f"for url: {URL} (1 retry attempted)")


def _http_error(code):
    resp = requests.Response()
    resp.status_code = code
    resp.url = URL
    reason = {503: "Service Temporarily Unavailable", 403: "Forbidden"}.get(code, "Error")
    kind = "Server" if code >= 500 else "Client"
    return requests.HTTPError(f"{code} {kind} Error: {reason} for url: {URL}", response=resp)


def _result(status, reason="", upstream=None):
    r = {"status": status, "reason": reason, "articles_count": 0, "content_chars": 0,
         "most_recent_date": None, "elapsed_ms": 1}
    if upstream:
        r["upstream_down"] = upstream
    return r


def _prev(consecutive_fails, status="OK"):
    return {"sources": {"amundi": {"status": status, "consecutive_fails": consecutive_fails,
                                   "consecutive_warns": 0, "last_ok_at": "2026-10-21T04:34:52+08:00"}}}


# ── the probe: what counts as "the site is down" ─────────────────────────────

SOURCE = {"id": "amundi", "url": URL, "listing_template_active": True,
          "listing_template": {"type": "rss_feed"}}


def _probe_with(monkeypatch, exc):
    import fetch_articles
    import fetch_content

    def raiser(src):
        raise exc
    monkeypatch.setattr(fetch_articles, "listing_fetcher", lambda s: raiser)
    monkeypatch.setitem(fetch_content.CONTENT_FETCHERS, "amundi", lambda a: None)
    return gfh._probe_once(SOURCE)


def test_a_503_on_the_listing_marks_the_site_down_without_the_rollback_hint(monkeypatch):
    r = _probe_with(monkeypatch, _http_error(503))
    assert r["status"] == "FAIL" and r["upstream_down"] == 503
    assert "roll back" not in r["reason"]
    assert "503 Server Error" in r["reason"]


def test_a_403_keeps_the_hint_and_is_not_site_down(monkeypatch):
    r = _probe_with(monkeypatch, _http_error(403))
    assert r["status"] == "FAIL" and "upstream_down" not in r
    assert "roll back" in r["reason"]


def test_a_5xx_raised_without_a_response_is_recognised_by_its_message(monkeypatch):
    r = _probe_with(monkeypatch, RuntimeError("502 Server Error: Bad Gateway for url: x"))
    assert r["upstream_down"] == 502


def test_a_500_is_not_site_down(monkeypatch):
    """500 is the site's code failing, often on our request; it stays FAIL."""
    r = _probe_with(monkeypatch, _http_error(500))
    assert "upstream_down" not in r


# ── first night vs second night ──────────────────────────────────────────────

def test_first_night_is_upstream_and_not_failing():
    per = {"amundi": _result("FAIL", DOWN, upstream=503)}
    gfh.apply_upstream_grace(per, _prev(0))
    assert per["amundi"]["status"] == "UPSTREAM"
    alerts = gfh.classify_alerts(per, _prev(0))
    assert alerts["failing"] == [] and [sid for sid, _ in alerts["upstream"]] == ["amundi"]
    assert gfh.should_email(alerts, [])


def test_second_night_in_a_row_fails_as_before():
    state = gfh.merge_into_state(_prev(0), {"amundi": _result("UPSTREAM", DOWN, upstream=503)})
    rec = state["sources"]["amundi"]
    assert rec["consecutive_fails"] == 1 and rec["last_ok_at"].startswith("2026-10-21")
    per = {"amundi": _result("FAIL", DOWN, upstream=503)}
    gfh.apply_upstream_grace(per, state)
    assert per["amundi"]["status"] == "FAIL"
    assert [sid for sid, _, _ in gfh.classify_alerts(per, state)["failing"]] == ["amundi"]


def test_other_failures_get_no_grace():
    per = {"amundi": _result("FAIL", "fetch_articles raised HTTPError: 403 Client Error")}
    gfh.apply_upstream_grace(per, _prev(0))
    assert per["amundi"]["status"] == "FAIL"


def test_the_email_and_subject_say_so():
    per = {"amundi": _result("UPSTREAM", DOWN + " <b>", upstream=503)}
    alerts = gfh.classify_alerts(per, _prev(0))
    subject = gfh.alerts_subject(alerts)
    assert "amundi" in subject and "FAILING" not in subject
    html = gfh.render_html_email(per, alerts, {"sources": {}}, 1.0)
    assert "SITE TEMPORARILY DOWN" in html and "503 Server Error" in html
    assert "<b>" not in html.split("SITE TEMPORARILY DOWN", 1)[1].split("HEALTHY", 1)[0]
    assert "amundi" not in html.split("HEALTHY", 1)[1]


def test_the_health_page_shows_it():
    row = {"id": "amundi", "consecutive_zero": 1, "last_refusal": None, "not_inspected_hours": 1,
           "probe_status": "UPSTREAM", "probe_reason": DOWN, "known_block": ""}
    assert [a["kind"] for a in ph._alerts([row], [], [], [])] == ["probe_upstream"]


# ── the whole run: exit code ─────────────────────────────────────────────────

def _run_main(monkeypatch, tmp_path, prev):
    import json
    state = tmp_path / "gmia-fetcher-health.json"
    state.write_text(json.dumps(prev))
    monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(gfh, "STATE_FILE", state)
    monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "amundi"}, {"id": "aqr"}])
    monkeypatch.setattr(gfh, "probe_source", lambda s: _result("FAIL", DOWN, upstream=503)
                        if s["id"] == "amundi" else _result("OK"))
    monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
    for name in ("pipeline_intake_anomalies", "recent_analysis_declines", "pipeline_zero_fetches"):
        monkeypatch.setattr(gfh, name, lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "should_email", lambda *a, **k: False)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
    return gfh.main()


def test_main_exits_zero_on_the_first_night(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, _prev(0)) == 0


def test_main_exits_one_on_the_second_night(monkeypatch, tmp_path):
    assert _run_main(monkeypatch, tmp_path, _prev(1, status="UPSTREAM")) == 1
