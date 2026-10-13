"""A decided refusal is reported, not alerted (sources.json "known_block").

2026-10-13: www.ares.com started answering this server with Cloudflare's
"Sorry, you have been blocked" for every page, robots.txt included, and an
Aliyun server got the same; another Cloudflare site answered both. The user
chose to keep the source as it is, without a bypass. Without a way to record
that decision the health check failed, cron-wrapper mailed and the liveness
audit said BAIL every morning, for a state nobody was going to change.

The flag only covers the refusal it was set for: any other failure still
alerts, and a probe that passes again says the flag can be removed.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gfh_kb", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(_spec)
sys.modules["gfh_kb"] = gfh
_spec.loader.exec_module(gfh)

_ph_spec = importlib.util.spec_from_file_location("ph_kb", REPO / "scripts" / "publish_health.py")
ph = importlib.util.module_from_spec(_ph_spec)
sys.modules["ph_kb"] = ph
_ph_spec.loader.exec_module(ph)

NOTE = "Cloudflare blocks this server; decided 2026-10-13: keep as is"
BLOCKED = "fetch_articles raised HTTPError: 403 Client Error: Forbidden for url: https://www.ares.com/sitemap.xml"


def _result(status, reason=""):
    return {"status": status, "reason": reason, "articles_count": 0, "content_chars": 0,
            "most_recent_date": None, "elapsed_ms": 1}


def _sources(flag=True):
    return [{"id": "ares-management", **({"known_block": NOTE} if flag else {})},
            {"id": "aqr"}]


def test_the_refusal_it_was_set_for_becomes_known():
    per = {"ares-management": _result("FAIL", BLOCKED), "aqr": _result("OK")}
    gfh.apply_known_blocks(per, _sources())
    assert per["ares-management"]["status"] == "KNOWN"
    assert per["aqr"]["status"] == "OK"


def test_any_other_failure_still_alerts():
    per = {"ares-management": _result("FAIL", "fetch_articles raised AttributeError: 'NoneType'")}
    gfh.apply_known_blocks(per, _sources())
    assert per["ares-management"]["status"] == "FAIL"


def test_without_the_flag_a_403_is_a_failure():
    per = {"ares-management": _result("FAIL", BLOCKED)}
    gfh.apply_known_blocks(per, _sources(flag=False))
    assert per["ares-management"]["status"] == "FAIL"


def test_known_is_neither_failing_nor_recovered():
    per = {"ares-management": _result("KNOWN", BLOCKED)}
    prev = {"sources": {"ares-management": {"status": "FAIL", "consecutive_fails": 1}}}
    alerts = gfh.classify_alerts(per, prev)
    assert alerts["failing"] == [] and alerts["recovered"] == []
    assert [sid for sid, _ in alerts["known"]] == ["ares-management"]
    assert not gfh.should_email(alerts, [])          # on its own, nothing to send


def test_known_still_counts_as_a_failed_probe_in_the_state():
    prev = {"sources": {"ares-management": {"status": "FAIL", "consecutive_fails": 1}}}
    rec = gfh.merge_into_state(prev, {"ares-management": _result("KNOWN", BLOCKED)})["sources"]["ares-management"]
    assert rec["status"] == "KNOWN" and rec["consecutive_fails"] == 2 and rec["consecutive_warns"] == 0


def test_a_passing_probe_says_the_flag_can_go():
    per = {"ares-management": _result("OK")}
    gfh.apply_known_blocks(per, _sources())
    alerts = gfh.classify_alerts(per, {"sources": {}})
    assert [sid for sid, _ in alerts["lifted"]] == ["ares-management"]
    assert gfh.should_email(alerts, [])
    html = gfh.render_html_email(per, alerts, {"sources": {}}, 1.0)
    assert "KNOWN BLOCK LIFTED" in html and "known_block" in html


def test_the_email_lists_known_blocks_apart_from_healthy():
    per = {"ares-management": _result("FAIL", BLOCKED + " <b>")}
    gfh.apply_known_blocks(per, _sources())
    alerts = gfh.classify_alerts(per, {"sources": {}})
    html = gfh.render_html_email(per, alerts, {"sources": {}}, 1.0)
    assert "KNOWN BLOCKED" in html and NOTE in html
    assert "<b>" not in html.split("KNOWN BLOCKED", 1)[1].split("HEALTHY", 1)[0]


def test_main_exits_zero_and_drops_the_zero_fetch_line(monkeypatch, tmp_path):
    monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "gmia-fetcher-health.json")
    monkeypatch.setattr(gfh, "load_sources", lambda: _sources())
    monkeypatch.setattr(gfh, "probe_source", lambda s: _result("FAIL", BLOCKED) if s["id"] == "ares-management"
                        else _result("OK"))
    for name in ("store_damage",):
        monkeypatch.setattr(gfh, name, lambda *a, **k: 0)
    for name in ("pipeline_intake_anomalies", "recent_analysis_declines"):
        monkeypatch.setattr(gfh, name, lambda *a, **k: [])
    monkeypatch.setattr(gfh, "pipeline_zero_fetches",
                        lambda *a, **k: [("ares-management", {"last_article_count": 0})])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(gfh, "should_email", lambda alerts, zero, *a, **k: seen.update(zero=zero) or False)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
    assert gfh.main() == 0
    assert seen["zero"] == []


def test_the_health_page_shows_one_known_line_instead_of_two_alerts():
    row = {"id": "ares-management", "consecutive_zero": 3, "last_refusal": None,
           "not_inspected_hours": 1, "probe_status": "KNOWN", "probe_reason": BLOCKED,
           "known_block": NOTE}
    alerts = ph._alerts([row], [], [], [])
    assert [a["kind"] for a in alerts] == ["known_block"]


def test_the_ares_decision_is_recorded_in_the_config():
    sources = json.loads((REPO / "config" / "sources.json").read_text())
    sources = sources["sources"] if isinstance(sources, dict) else sources
    ares = next(s for s in sources if s["id"] == "ares-management")
    assert "no bypass" in ares["known_block"]
    assert gfh.KNOWN_BLOCK_EVIDENCE.search(BLOCKED)
