import importlib.util, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("srs", REPO / "scripts" / "send_refresh_summary.py")
srs = importlib.util.module_from_spec(spec); sys.modules["srs"] = srs
spec.loader.exec_module(srs)


def test_render_lists_applied_and_flagged():
    html = srs.render_summary(applied=["apollo-global-management", "kkr"],
                              flagged=["gsam (gate failed)"], alert_only=False)
    assert "apollo-global-management" in html
    assert "kkr" in html
    assert "gsam (gate failed)" in html
    assert "<html" in html.lower()


def test_render_alert_only_banner():
    html = srs.render_summary(applied=["kkr"], flagged=[], alert_only=True)
    assert "ALERT-ONLY" in html.upper() or "告警模式" in html


def test_render_empty_is_noop_message():
    html = srs.render_summary(applied=[], flagged=[], alert_only=False)
    assert "no change" in html.lower() or "无变化" in html
