"""The daily health email carries a content-quality section (2026-09-15).

failure_stats.py counted failures by source and label, but only on demand.
The email now shows the last week's intake, what is currently missing a body
or a summary and why (with the Chinese label descriptions), and -- as an
alert that sends the email -- any source that shows an alertable label for
the first time (selector_miss, blocked_by_bot_protection, pdf_not_usable;
wrong_document, duplicate_body, grounding_failed).

--test-email ADDRESS runs the real check, writes no state, and always sends
the email to that address only, subject prefixed "[测试]".
"""
import importlib.util
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gfh_quality", REPO / "scripts" / "gmia-fetcher-health.py")
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_quality"] = gfh
spec.loader.exec_module(gfh)

NO_ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}
QUALITY = {
    "days": 7,
    "intake": {"total": 42, "with_body": 38, "metadata_only": 2, "without_body": 4, "declined": 3},
    "content": Counter({("matthews-asia", "media_without_text"): 12, ("gsam", "body_rendered_client_side"): 7}),
    "declines": Counter({("ark-invest", "title_only"): 28, ("gmo", "wrong_document"): 3}),
    "alerts": [("content", "aqr", "selector_miss"), ("analysis", "gmo", "wrong_document")],
}


def test_the_section_renders_intake_tables_and_alerts():
    html = gfh.render_html_email({}, NO_ALERTS, {"sources": {}}, 1.0, quality=QUALITY)
    assert "正文获取质量" in html
    assert "42" in html and "38" in html
    assert "视频或播客页" in html and "matthews-asia" in html          # label description + source
    assert "只有标题" in html and "ark-invest" in html
    assert "aqr" in html and "抓取规则未匹配" in html                  # the alert, described


def test_quality_alerts_send_the_email_and_name_it_in_the_subject():
    assert gfh.should_email(NO_ALERTS, [], False, declines=[], quality=QUALITY) is True
    quiet = dict(QUALITY, alerts=[])
    assert gfh.should_email(NO_ALERTS, [], False, declines=[], quality=quiet) is False
    subject = gfh.alerts_subject(NO_ALERTS, [], False, declines=[], quality=QUALITY)
    assert "all OK" not in subject and "2" in subject


def test_the_section_is_omitted_when_quality_is_unavailable():
    html = gfh.render_html_email({}, NO_ALERTS, {"sources": {}}, 1.0, quality=None)
    assert "正文获取质量" not in html


def test_send_email_can_address_a_test_recipient(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, msg): sent["to"] = msg["To"]; sent["subject"] = msg["Subject"]

    monkeypatch.setattr(gfh, "load_env", lambda: {"SMTP_USER": "u", "SMTP_PASS": "p", "MAIL_TO": "owner@example.com"})
    monkeypatch.setattr(gfh.smtplib, "SMTP_SSL", FakeSMTP)
    assert gfh.send_email("<html/>", "s", to="tester@example.com") is True
    assert sent["to"] == "tester@example.com"


def test_test_email_sends_to_that_address_and_writes_no_state(monkeypatch):
    calls = {}
    monkeypatch.setattr(gfh, "load_sources", lambda: [])
    monkeypatch.setattr(gfh, "pipeline_zero_fetches", lambda: [])
    monkeypatch.setattr(gfh, "pipeline_did_not_run", lambda: False)
    monkeypatch.setattr(gfh, "recent_analysis_declines", lambda **k: [])
    monkeypatch.setattr(gfh, "load_quality", lambda days=7, since=None: dict(QUALITY, alerts=[]))
    monkeypatch.setattr(gfh, "load_state", lambda: {"last_run": None, "sources": {}})
    monkeypatch.setattr(gfh, "save_state", lambda s: calls.setdefault("saved", True))
    monkeypatch.setattr(gfh, "send_email", lambda html, subject, to=None: calls.update(to=to, subject=subject, html=html) or True)
    monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--test-email", "tester@example.com"])
    gfh.main()
    assert calls.get("to") == "tester@example.com"
    assert calls["subject"].startswith("[测试]")
    assert "正文获取质量" in calls["html"]
    assert "saved" not in calls, "a test email run wrote the health state"


def test_main_loads_and_passes_quality():
    import inspect
    src = inspect.getsource(gfh.main)
    assert "load_quality(since=" in src and src.count("quality=quality") >= 3
