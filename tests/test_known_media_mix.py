"""A decided video-heavy source is reported, not warned (sources.json "known_media_mix").

2026-11-09: Fulcrum's three newest items were all Vimeo "Deeper Dive" /
"On the Horizon" pages under /insights/videos-podcasts/, each with a 196-208
char intro and no article text, so the content probe had nothing to read and
WARNed "content mix" (by design since ae3a16d). Only 3 of its 12 stored rows
have a body and the last written insight is 2026-10-21, so the warning would
have mailed every morning until the firm next wrote something. The user chose
to record it as a known state.

The flag only covers that warning: a FAIL (a selector, a 403, a fetch error)
alerts as usual, and so does a mix that is not all media_without_text.
"""
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("gfh_km", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(_spec)
sys.modules["gfh_km"] = gfh
_spec.loader.exec_module(gfh)

_ph_spec = importlib.util.spec_from_file_location("ph_km", REPO / "scripts" / "publish_health.py")
ph = importlib.util.module_from_spec(_ph_spec)
sys.modules["ph_km"] = ph
_ph_spec.loader.exec_module(ph)

NOTE = "Mostly Vimeo pages with a short intro; decided 2026-11-09: do not warn"
MIX = ("content mix: all top 3 articles are video/media pages (media_without_text), no full-text "
       "article to probe: #0=returned None (media_without_text: Fulcrum: extracted text too short (196 chars))")


def _result(status, reason=""):
    return {"status": status, "reason": reason, "articles_count": 10, "content_chars": 0,
            "most_recent_date": "2026-11-06", "elapsed_ms": 1}


def _sources(flag=True):
    return [{"id": "fulcrum-asset-management", **({"known_media_mix": NOTE} if flag else {})},
            {"id": "aqr"}]


def _streak(n=3):
    return {"sources": {"fulcrum-asset-management": {"status": "WARN", "consecutive_warns": n}}}


def test_the_media_mix_warning_is_known_not_warning():
    per = {"fulcrum-asset-management": _result("WARN", MIX), "aqr": _result("OK")}
    gfh.apply_known_media_mix(per, _sources())
    alerts = gfh.classify_alerts(per, _streak())
    assert alerts["warning"] == [] and alerts["recovered"] == []
    assert [sid for sid, _ in alerts["known"]] == ["fulcrum-asset-management"]
    assert not gfh.should_email(alerts, [])


def test_it_stays_a_warn_in_the_state():
    """Still a WARN night: the upstream grace and the fail streak are untouched."""
    per = {"fulcrum-asset-management": _result("WARN", MIX)}
    gfh.apply_known_media_mix(per, _sources())
    rec = gfh.merge_into_state(_streak(), per)["sources"]["fulcrum-asset-management"]
    assert rec["status"] == "WARN" and rec["consecutive_warns"] == 4 and rec["consecutive_fails"] == 0


def test_a_failure_still_alerts():
    per = {"fulcrum-asset-management": _result("FAIL", "all top 3 articles failed content probe: "
                                                       "#0=returned None (selector_miss: extraction took fallback)")}
    gfh.apply_known_media_mix(per, _sources())
    alerts = gfh.classify_alerts(per, {"sources": {}})
    assert [sid for sid, *_ in alerts["failing"]] == ["fulcrum-asset-management"]


def test_another_warning_still_alerts():
    per = {"fulcrum-asset-management": _result("WARN", "date parse failed for 3 of 10 articles")}
    gfh.apply_known_media_mix(per, _sources())
    alerts = gfh.classify_alerts(per, _streak())
    assert [sid for sid, *_ in alerts["warning"]] == ["fulcrum-asset-management"]


def test_without_the_flag_the_mix_warns():
    per = {"fulcrum-asset-management": _result("WARN", MIX)}
    gfh.apply_known_media_mix(per, _sources(flag=False))
    alerts = gfh.classify_alerts(per, _streak())
    assert [sid for sid, *_ in alerts["warning"]] == ["fulcrum-asset-management"]


def test_the_email_lists_it_with_the_decision():
    per = {"fulcrum-asset-management": _result("WARN", MIX + " <b>")}
    gfh.apply_known_media_mix(per, _sources())
    alerts = gfh.classify_alerts(per, _streak())
    html = gfh.render_html_email(per, alerts, _streak(), 1.0)
    section = html.split("KNOWN MEDIA MIX", 1)[1].split("HEALTHY", 1)[0]
    assert NOTE in section and "<b>" not in section


def test_the_health_page_shows_one_known_line_instead_of_a_probe_warning():
    row = {"id": "fulcrum-asset-management", "consecutive_zero": 0, "last_refusal": None,
           "not_inspected_hours": 1, "probe_status": "WARN", "probe_reason": MIX,
           "known_block": "", "known_media_mix": NOTE}
    assert [a["kind"] for a in ph._alerts([row], [], [], [])] == ["known_media_mix"]
    row["probe_reason"] = "date parse failed for 3 of 10 articles"
    assert [a["kind"] for a in ph._alerts([row], [], [], [])] == ["probe_warn"]


def test_every_known_media_mix_in_the_config_records_a_decision():
    sources = json.loads((REPO / "config" / "sources.json").read_text())
    sources = sources["sources"] if isinstance(sources, dict) else sources
    flagged = [s["id"] for s in sources if "known_media_mix" in s]
    assert flagged == ["fulcrum-asset-management"]
    for s in sources:
        if "known_media_mix" in s:
            assert re.search(r"Decided \d{4}-\d{2}-\d{2}", s["known_media_mix"]), s["id"]


def test_main_applies_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", tmp_path / "inspection_state.json")
    monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "gmia-fetcher-health.json")
    monkeypatch.setattr(gfh, "load_state", lambda *a, **k: _streak())
    monkeypatch.setattr(gfh, "load_sources", lambda: _sources())
    monkeypatch.setattr(gfh, "probe_source", lambda s: _result("WARN", MIX) if s["id"] == "fulcrum-asset-management"
                        else _result("OK"))
    monkeypatch.setattr(gfh, "store_damage", lambda *a, **k: 0)
    for name in ("pipeline_intake_anomalies", "recent_analysis_declines", "pipeline_zero_fetches"):
        monkeypatch.setattr(gfh, name, lambda *a, **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda *a, **k: None)
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda *a, **k: False)
    monkeypatch.setattr(gfh, "save_state", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(gfh, "should_email", lambda alerts, *a, **k: seen.update(alerts=alerts) or False)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
    assert gfh.main() == 0
    assert seen["alerts"]["warning"] == []
    assert [sid for sid, _ in seen["alerts"]["known"]] == ["fulcrum-asset-management"]
