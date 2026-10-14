"""Audit of the three GMIA jobs outside stages 1-6 (2026-10-14).

L1  the liveness mail failing (or the script crashing) was invisible: the cron
    job runs without cron-wrapper alerts and the exit code was 1 either way.
L2  one incident was mailed every day until it could change (10-11 -> 10-18).
L3  only five stages were watched; a job that stopped running, or whose cron
    line vanished, never showed.
N1  the nightly regression test ran FETCHERS[id], not what production runs,
    for the 19 templated sources.
N2  its GMO API test sent no Referer, got 500 and skipped as an "upstream
    outage" every night for months.
C1  content_audit --mark-gone rewrote articles.jsonl without the store's lock
    or merge, dropping a row appended while it fetched.
"""
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import gmia_liveness_audit as la  # noqa: E402

NOW = datetime(2026, 10, 14, 21, 2, tzinfo=timezone.utc)


def _run(job, ts, exit_code=0):
    return {"ts": ts.isoformat(), "job": job, "exit": exit_code}


# --- L1: exit codes ----------------------------------------------------------
@pytest.fixture
def liveness(tmp_path, monkeypatch):
    """main() with one open problem, a temp state file and a fake mailer."""
    monkeypatch.setattr(la, "ALERT_STATE", tmp_path / "state.json")
    monkeypatch.setattr(la, "run_audit", lambda now=None: [
        la._verdict("daily-fetch", la.STALE, "newest article 5.0d old")])
    monkeypatch.setattr(la, "_load_env_file", lambda path: None)
    sent = {"ok": True, "calls": 0}
    fake = type(sys)("send_synthesis_summary")

    def send_email(body, subject):
        sent["calls"] += 1
        return sent["ok"]
    fake.send_email = send_email
    monkeypatch.setitem(sys.modules, "send_synthesis_summary", fake)
    return sent


def test_mail_that_did_not_go_out_exits_2_and_keeps_it_due(liveness):
    liveness["ok"] = False
    assert la.main(["--email"]) == 2
    assert not la.ALERT_STATE.exists()          # retried tomorrow
    liveness["ok"] = True
    assert la.main(["--email"]) == 0
    assert liveness["calls"] == 2


def test_problem_mailed_exits_0(liveness):
    assert la.main(["--email"]) == 0
    assert "daily-fetch:STALE" in json.loads(la.ALERT_STATE.read_text())


def test_crash_is_not_swallowed(liveness, monkeypatch):
    def boom(now=None):
        raise OSError("disk")
    monkeypatch.setattr(la, "run_audit", boom)
    with pytest.raises(OSError):
        la.main(["--email"])


def test_run_by_hand_still_exits_1_on_a_problem(liveness):
    assert la.main([]) == 1
    assert liveness["calls"] == 0


# --- L2: mail once, remind weekly ------------------------------------------
def test_problem_is_mailed_once_then_reminded_after_remind_days(liveness):
    assert la.main(["--email"]) == 0 and liveness["calls"] == 1
    assert la.main(["--email"]) == 0 and liveness["calls"] == 1    # same problem, next run
    p = [{"key": "daily-fetch:STALE"}]
    state = json.loads(la.ALERT_STATE.read_text())
    later = la.parse_iso(state["daily-fetch:STALE"]["last_alerted"]) + timedelta(days=la.REMIND_DAYS)
    due, new = la.plan_alert(p, state, later)
    assert due == ["daily-fetch:STALE"]
    assert new["daily-fetch:STALE"]["first_alerted"] == state["daily-fetch:STALE"]["first_alerted"]


def test_cleared_problem_that_returns_is_new():
    state = {"x:STALE": {"first_alerted": NOW.isoformat(), "last_alerted": NOW.isoformat()}}
    _, cleared = la.plan_alert([], state, NOW + timedelta(hours=1))
    assert cleared == {}
    due, _ = la.plan_alert([{"key": "x:STALE"}], cleared, NOW + timedelta(days=1))
    assert due == ["x:STALE"]


def test_next_failed_synthesis_session_is_a_new_problem(tmp_path, monkeypatch):
    hist = tmp_path / "h.jsonl"
    monkeypatch.setattr(la, "SYNTHESIS_HISTORY", hist)
    monkeypatch.setattr(la, "_read_log_tail", lambda name: "")
    keys = []
    for ts in ("2026-10-10T18:00:00+00:00", "2026-10-17T18:00:00+00:00"):
        hist.write_text(json.dumps({"id": "_heartbeat", "timestamp": ts, "agent_exit": 1}) + "\n")
        v = la.check_synthesis(NOW + timedelta(days=4 if ts.startswith("2026-10-17") else 0))
        assert v["status"] == la.BAIL
        keys.append(v["key"])
    assert keys[0] != keys[1]


def test_each_failed_run_of_a_job_is_its_own_problem():
    a = la.check_ops(NOW, [_run("gmia-fetcher-health", NOW - timedelta(days=1), 1)])
    b = la.check_ops(NOW, [_run("gmia-fetcher-health", NOW - timedelta(hours=2), 1)])
    assert a[0]["key"] != b[0]["key"]


def test_mail_marks_problems_already_reported():
    v = la._verdict("x", la.STALE, "d")
    state = {v["key"]: {"first_alerted": "2026-10-11T00:00:00+00:00"}}
    assert "已报过，首次 2026-10-11" in la.render_html([v], state, due=[])
    assert "已报过" not in la.render_html([v], state, due=[v["key"]])


# --- L3: every job ran ------------------------------------------------------
CRONTAB = "\n".join(
    [f"0 1 * * * ~/cron-wrapper.sh --name {j} -- true" for j in la.EXPECTED_JOBS]
    + ["2 21 * * * ~/cron-wrapper.sh --name gmia-liveness-audit -- x",
       "#0 1 * * * ~/cron-wrapper.sh --name gmia-commented-out -- x"])


def _all_ran(now=NOW):
    return [_run(j, now - timedelta(hours=1)) for j in la.EXPECTED_JOBS]


def test_all_jobs_recent_and_listed_is_clean():
    assert la.check_jobs_ran(NOW, _all_ran(), CRONTAB) == []


def test_job_that_stopped_running_is_reported():
    rows = [r for r in _all_ran() if r["job"] != "gmia-health-page"]
    rows.append(_run("gmia-health-page", NOW - timedelta(days=2)))
    rows.append({"ts": NOW.isoformat(), "job": "gmia-health-page", "event": "alert"})
    out = la.check_jobs_ran(NOW, rows, CRONTAB)
    assert [v["stage"] for v in out] == ["ran:gmia-health-page"]


def test_job_never_seen_is_reported():
    rows = [r for r in _all_ran() if r["job"] != "gmia-census"]
    out = la.check_jobs_ran(NOW, rows, CRONTAB)
    assert out[0]["detail"] == "no run on record"


def test_monthly_job_within_a_31_day_month_is_fine():
    rows = [r for r in _all_ran() if r["job"] != "gmia-census"]
    rows.append(_run("gmia-census", NOW - timedelta(days=31, hours=1)))
    assert la.check_jobs_ran(NOW, rows, CRONTAB) == []


def test_cron_line_removed_and_unwatched_job_are_both_reported():
    tab = CRONTAB.replace("--name gmia-ab-gate", "--name gmia-brand-new")
    out = {v["stage"]: v["detail"] for v in la.check_jobs_ran(NOW, _all_ran(), tab)}
    assert out["ran:gmia-ab-gate"] == "not in the crontab any more"
    assert "not watched" in out["ran:gmia-brand-new"]
    assert "ran:gmia-commented-out" not in out


def test_unreadable_crontab_is_said_not_skipped_silently():
    out = la.check_jobs_ran(NOW, _all_ran(), None)
    assert [v["stage"] for v in out] == ["crontab"]


def test_ops_history_includes_rotations(tmp_path, monkeypatch):
    monkeypatch.setattr(la, "LOG_DIR", tmp_path)
    (tmp_path / "ops-status.jsonl").write_text(json.dumps(_run("a", NOW)) + "\n")
    with gzip.open(tmp_path / "ops-status.jsonl.1.gz", "wt") as f:
        f.write(json.dumps(_run("b", NOW - timedelta(days=20))) + "\nnot json\n")
    assert {r["job"] for r in la._read_ops_history()} == {"a", "b"}


# --- N1 / N2: nightly regression test ---------------------------------------
def test_nightly_regression_fetches_through_listing_fetcher(monkeypatch):
    import fetch_articles
    import test_regression as tr
    seen = []

    def fake_listing_fetcher(src):
        seen.append(src["id"])
        return lambda s: [{"title": "t", "url": "https://x/", "date": "2026-10-01"}]
    monkeypatch.setattr(fetch_articles, "listing_fetcher", fake_listing_fetcher)
    tr._fetch_all_sources()
    config = json.loads((ROOT / "config" / "sources.json").read_text())
    assert seen == [s["id"] for s in config["sources"]]


def test_gmo_api_test_sends_the_referer_production_sends(monkeypatch):
    import requests
    import test_regression as tr

    class Resp:
        def __init__(self, status, text="", data=None):
            self.status_code, self.text, self._data = status, text, data

        def json(self):
            return self._data

    def fake_get(url, **kw):
        if "data-endpoint" not in url and "api" not in url:
            return Resp(200, '<section class="article-grid" data-endpoint="/api/x?uid=1"></section>')
        # The real API: 500 without a Referer (measured 2026-10-14).
        if not (kw.get("headers") or {}).get("Referer"):
            return Resp(500)
        return Resp(200, data={"listing": []})
    monkeypatch.setattr(requests, "get", fake_get)
    try:
        tr.TestApiHealth().test_gmo_api_returns_json()
    except pytest.skip.Exception as exc:          # a skip here is the bug, not a pass
        pytest.fail(f"the GMO test skipped: {exc}")


# --- C1: mark-gone goes through the store -----------------------------------
def test_mark_gone_keeps_a_row_appended_while_it_fetched(tmp_path, monkeypatch):
    import content_audit as ca
    import fetch_content as fc
    import jsonl_store as js
    p = tmp_path / "articles.jsonl"
    p.write_text(json.dumps({"id": "a1", "source_id": "x", "url": "u1"}) + "\n{torn\n")

    def fetch(row, fetcher):
        js.append_rows(p, [{"id": "NEW", "source_id": "y", "url": "u2"}])
        return None, {"extraction_paths": []}, ""
    monkeypatch.setattr(ca, "_fetch_to_temp", fetch)
    monkeypatch.setattr(fc, "content_fetcher_for", lambda r: None)
    monkeypatch.setattr(ca.failure_labels, "classify_content_failure", lambda ev: ("page_gone", "404"))
    assert ca.mark_gone(["a1"], data_file=p) == 0
    lines = p.read_text().splitlines()
    rows = [json.loads(l) for l in lines if l.startswith("{\"")]
    assert {r["id"]: r.get("url_status") for r in rows} == {"a1": "gone", "NEW": None}
    assert "{torn" in lines                       # damaged line kept as evidence
