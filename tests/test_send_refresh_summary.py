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


# ── 10-10: the summary says what changed, not only which fund ────────────────

CHANGE = {"field": "aum", "old": "~$758B", "new": "~$800B", "reason": "Q2 10-Q",
          "source": "https://www.sec.gov/kkr-10q"}


def test_applied_funds_show_field_old_new_reason_and_source():
    html = srs.render_summary(applied=["kkr"], flagged=[], alert_only=False,
                              details={"kkr": [CHANGE]}, names={"kkr": "KKR & Co."})
    assert "KKR &amp; Co." in html and "aum" in html
    assert "~$758B" in html and "~$800B" in html and "→" in html
    assert "Q2 10-Q" in html and '<a href="https://www.sec.gov/kkr-10q"' in html


def test_values_are_escaped_and_only_web_sources_become_links():
    bad = dict(CHANGE, new="<img src=x onerror=1>", source="javascript:alert(1)")
    html = srs.render_summary(applied=["kkr"], flagged=[], alert_only=False, details={"kkr": [bad]})
    assert "<img" not in html and "&lt;img" in html
    assert 'href="javascript' not in html and "javascript:alert(1)" in html


def test_details_come_from_the_archived_draft_or_the_pending_one(tmp_path):
    import json
    (tmp_path / "applied").mkdir()
    (tmp_path / "applied" / "kkr.refresh.json").write_text(json.dumps({"change_log": [CHANGE]}))
    (tmp_path / "gsam.refresh.json").write_text(json.dumps({"change_log": [dict(CHANGE, new="~$4.1T")]}))
    got = srs.load_details(["kkr", "gsam", "missing"], tmp_path)
    assert got["kkr"][0]["new"] == "~$800B" and got["gsam"][0]["new"] == "~$4.1T" and "missing" not in got


def test_main_renders_details_and_names_from_the_repo_files(tmp_path, monkeypatch):
    import json
    (tmp_path / "applied").mkdir()
    (tmp_path / "applied" / "kkr.refresh.json").write_text(json.dumps({"change_log": [CHANGE]}))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": [{"id": "kkr", "name": "KKR"}]}))
    captured = {}
    monkeypatch.setattr(srs, "send", lambda subject, body: captured.update(body=body) or True)
    monkeypatch.setattr(sys, "argv", ["send_refresh_summary.py", "--applied", "kkr", "--alert-only", "0",
                                      "--drafts-dir", str(tmp_path), "--sources", str(sources)])
    assert srs.main() == 0
    assert "KKR" in captured["body"] and "~$800B" in captured["body"]


# ── long text: mark only what changed ────────────────────────────────────────

OLD_DESC = ("Acadian 是一家总部位于波士顿的系统化量化投资管理公司，成立于 1986 年，"
            "专注于全球股票与另类策略，管理规模约 $178B，客户以养老金和主权基金为主。")
NEW_DESC = OLD_DESC.replace("$178B", "$233B")


def _desc_html(old=OLD_DESC, new=NEW_DESC):
    return srs.render_summary(applied=["acadian"], flagged=[], alert_only=False,
                              details={"acadian": [dict(CHANGE, field="desc_zh", old=old, new=new)]})


def test_a_long_text_change_marks_only_the_changed_words():
    html = _desc_html()
    assert "<del" in html and ">$178B</del>" in html and ">$233B</ins>" in html
    assert html.count("客户以养老金") == 1                    # the paragraph is not printed twice
    assert "→" not in html.split("desc_zh", 1)[1].split("理由", 1)[0]


def test_the_unchanged_stretch_far_from_the_change_is_elided():
    html = _desc_html()
    assert "…" in html and "总部位于波士顿" not in html          # 40+ chars before the change
    assert "管理规模约 " in html                                   # its immediate context stays


def test_the_marked_diff_is_escaped():
    html = _desc_html(old=OLD_DESC + "<b>旧</b>", new=NEW_DESC + "<img src=x onerror=1>")
    import re
    assert "<img" not in html and "<b>旧" not in html
    tags = r"</?(?:del|ins)[^>]*>"
    as_new = re.sub(tags, "", re.sub(r"<del[^>]*>.*?</del>", "", html))    # reading only the new text
    as_old = re.sub(tags, "", re.sub(r"<ins[^>]*>.*?</ins>", "", html))
    assert "&lt;img src=x onerror=1&gt;" in as_new and "&lt;b&gt;旧&lt;/b&gt;" in as_old


def test_a_short_value_still_reads_old_arrow_new():
    html = srs.render_summary(applied=["kkr"], flagged=[], alert_only=False, details={"kkr": [CHANGE]})
    assert "~$758B → ~$800B" in html and "<del" not in html
