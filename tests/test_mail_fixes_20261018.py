"""Four reporting fixes from the 2026-10-18 mails.

1  fetcher-health exited 1 on "fulcrum-asset-management: candidate has no
   research_url" -- a template-first candidate has listing_url instead -- and
   the mail never named that failure.
2  the synthesis mail reported "5 of 7 targets have no result" when the
   session only ever plans the first two.
3  rbc-bluebay's api_json template keeps its date at author.date; _field read
   only top-level keys, so all 12 rows came back undated.
4  the weekly content audit reported ark-invest's 403s as "needs action"
   although the source carries a decided known_block.
"""
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gfh = _load("gfh_20261018", "scripts/gmia-fetcher-health.py")


# --- 1 ------------------------------------------------------------------------
class _Client:
    seen = []

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        _Client.seen.append(url)
        return type("R", (), {"status_code": 200, "content": b"x" * 20000})()


@pytest.mark.parametrize("cand,want", [
    ({"research_url": "https://a/r", "listing_url": "https://a/l"}, "https://a/r"),
    ({"listing_url": "https://a/l", "homepage_url": "https://a"}, "https://a/l"),
    ({"homepage_url": "https://a"}, "https://a"),
])
def test_candidate_probe_falls_back_to_listing_then_homepage(monkeypatch, cand, want):
    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    _Client.seen = []
    assert gfh.probe_candidate_url(cand)["status"] == "OK"
    assert _Client.seen == [want]


def test_a_failing_candidate_is_named_in_the_mail_and_subject():
    fails = [("fulcrum-asset-management", {"status": "FAIL", "reason": "HTTP 404"})]
    alerts = {"failing": [], "warning": [], "recovered": [], "healthy": [], "known": [], "lifted": []}
    assert gfh.should_email(alerts, [], candidate_fails=fails)
    assert "fulcrum-asset-management" in gfh.alerts_subject(alerts, candidate_fails=fails)
    body = gfh.render_html_email({}, alerts, {"sources": {}}, 1.0, candidate_fails=fails)
    assert "CANDIDATE URL FAILING" in body and "HTTP 404" in body


# --- 2 ------------------------------------------------------------------------
sss = _load("sss_20261018", "scripts/send_synthesis_summary.py")
NOW = datetime(2026, 10, 17, 18, 3, tzinfo=timezone.utc)
OK2 = [{"id": "fulcrum-asset-management", "outcome": "success", "name": "F"},
       {"id": "rbc-bluebay", "outcome": "success", "name": "R"}]


def test_both_planned_targets_done_is_not_reported_as_missing():
    htm = sss.build_html(OK2, 7, [], OK2, NOW, planned_ids=["fulcrum-asset-management", "rbc-bluebay"])
    assert "无结果记录" not in htm
    assert "另有 5 个目标排队" in htm and "本次计划 2" in htm


def test_a_planned_target_without_a_result_is_named():
    htm = sss.build_html(OK2[:1], 7, [], OK2[:1], NOW,
                         planned_ids=["fulcrum-asset-management", "rbc-bluebay"])
    assert "1 个无结果记录" in htm and "rbc-bluebay" in htm


def test_the_wrapper_passes_the_planned_ids_to_the_mail():
    w = (ROOT / "scripts" / "wrapper-fetcher-synthesis.sh").read_text()
    call = w[w.index("send_synthesis_summary.py"):w.index("summary email step failed")]
    assert '--planned-ids "$PLANNED_IDS"' in call


# --- 3 ------------------------------------------------------------------------
def test_a_dotted_field_follows_nested_objects():
    import listing_templates as lt
    rec = {"title": "T", "author": {"date": "October 14, 2026"}, "a.b": "flat"}
    assert lt._field(rec, "author.date") == "October 14, 2026"
    assert lt._field(rec, "a.b") == "flat"                  # a literal dotted key still wins
    assert lt._field(rec, ["author.missing", "title"]) == "T"


# --- 4 ------------------------------------------------------------------------
def _ark(label="blocked_by_bot_protection", detail="HTTP 403 at https://www.ark-invest.com/x",
         findings=None, sid="ark-invest"):
    return {"source_id": sid, "id": "a", "title": "t", "url": "u",
            "findings": findings or ["now_failing"], "label": label, "detail": detail}


SOURCES = [{"id": "ark-invest", "known_block": "Decided 2026-10-16: keep"}, {"id": "gmo"}]


def test_a_known_block_refusal_is_recorded_not_alerted():
    import content_audit as ca
    results = [_ark(), _ark(sid="gmo")]
    ca.mark_known_blocks(results, SOURCES)
    assert results[0]["findings"] == ["known_block"] and not ca.is_alert(results[0])
    assert ca.is_alert(results[1])                           # no known_block: still alerts
    body = ca.render_email(results, 2)
    assert "需要处理 <b>1</b>" in body and "已知拦截" in body


def test_another_failure_at_a_known_block_source_still_alerts():
    import content_audit as ca
    results = [_ark(label="selector_miss", detail="no article body"),
               _ark(findings=["now_failing", "extraction_drift"])]
    ca.mark_known_blocks(results, SOURCES)
    assert all(ca.is_alert(r) for r in results)


def test_the_audit_uses_the_health_probes_regex():
    import content_audit as ca
    assert ca._health_module().KNOWN_BLOCK_EVIDENCE.pattern == gfh.KNOWN_BLOCK_EVIDENCE.pattern
    src = (ROOT / "scripts" / "content_audit.py").read_text()
    assert "mark_known_blocks(results, sources)" in src
