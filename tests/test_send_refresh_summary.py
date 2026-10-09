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


# Stage-4 audit finding 18: all 7 monthly runs logged "SMTP env missing; skip".
# The wrapper never sourced ~/.stock-monitor.env, and that file has no
# SMTP_HOST anyway (every other GMIA mail goes to smtp.163.com directly).

class _FakeSMTP:
    sent: list = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, pw):
        pass

    def sendmail(self, frm, to, msg):
        _FakeSMTP.sent.append((self.host, self.port, frm, to))


def test_send_works_without_smtp_host(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(srs.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("SMTP_USER", "bot@163.com")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setenv("MAIL_TO", "me@example.com")
    assert srs.send("s", "<p>x</p>") is True
    assert _FakeSMTP.sent == [("smtp.163.com", 465, "bot@163.com", ["me@example.com"])]


def test_unsent_summary_is_a_nonzero_exit(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "argv", ["send_refresh_summary.py", "--applied", "kkr", "--alert-only", "0"])
    assert srs.main() != 0
