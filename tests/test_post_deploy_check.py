"""The temporary post-deploy check (scripts/post_deploy_check.py), on tmp files only."""
import gzip
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("post_deploy_check", REPO / "scripts" / "post_deploy_check.py")
pdc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pdc)

NOW = datetime(2026, 10, 10, 20, 20, tzinfo=timezone.utc)          # 04:20 BJT on 10-11
TODAY = "2026-10-11"
OK_LOG = ("[2026-10-10T19:45:01Z] Pipeline starting\n"
          "  Dated from the article page: x -> 2026-10-02 (T)\n"
          "Pending: 7 | Success: 7 | Failed: 0 | Retired(permafail): 0 | re-tried after a code change: 7\n"
          "[2026-10-10T19:55:00Z] Pipeline complete — all stages OK\n")


def _rows():
    return [{"id": "p1", "source_id": "de-shaw", "url": "https://d/1", "date": "2026-02-01",
             "date_listed": "2026-01-01", "date_basis": "page"},
            {"id": "w1", "source_id": "wellington", "url": "https://w/1", "date": "2026-08-17",
             "date_listed": "2026-08-17", "date_basis": "listing"},
            {"id": "c1", "source_id": "capital-group", "url": "https://c/1", "date": None}]


def _page(tmp_path, built=TODAY, gz_same=True):
    html = f'<div class="stats" data-built="{built}"><b data-stat="total">3</b></div>'
    page = tmp_path / "p.html"
    page.write_text(html, encoding="utf-8")
    (tmp_path / "p.html.gz").write_bytes(gzip.compress((html if gz_same else html + "x").encode()))
    return page


def test_pipeline_ok_stale_and_failed():
    assert pdc.check_pipeline(OK_LOG, NOW)[1] == pdc.PASS
    assert pdc.check_pipeline(OK_LOG, NOW + timedelta(days=1))[1] == pdc.FAIL           # no run last night
    failed = OK_LOG.replace("Pipeline complete — all stages OK", "Pipeline FAILED — Stage4:precheck")
    st, detail = pdc.check_pipeline(failed, NOW)[1:]
    assert st == pdc.FAIL and "Stage4:precheck" in detail
    assert pdc.check_pipeline("", NOW)[1] == pdc.FAIL


def test_page_must_be_built_today_and_match_its_gz(tmp_path):
    assert pdc.check_page(_page(tmp_path), NOW)[1] == pdc.PASS
    assert pdc.check_page(_page(tmp_path, built="2026-10-10"), NOW)[1] == pdc.FAIL      # pre-check held it back
    assert pdc.check_page(_page(tmp_path, gz_same=False), NOW)[1] == pdc.FAIL


def test_data_changes_and_duplicates_fail():
    rows = _rows()
    base = pdc.make_baseline(rows)
    assert set(base["dates"]) == {"p1", "w1"}
    assert pdc.check_data(rows, base, "")[1] == pdc.PASS
    changed = [dict(r, date="2026-01-01") if r["id"] == "p1" else r for r in rows]
    assert pdc.check_data(changed, base, "")[1] == pdc.FAIL
    dup = rows + [{"id": "p2", "source_id": "de-shaw", "url": "https://d/1", "date": "2026-01-01"}]
    st, detail = pdc.check_data(dup, base, "")[1:]
    assert st == pdc.FAIL and "stored twice" in detail
    refused = "LISTING_WENT_BACKWARDS: de-shaw newest listed article is 2026-01-01"
    assert pdc.check_data(rows, base, refused)[1] == pdc.FAIL


def test_stage3_alarms_are_warnings_not_failures():
    rows = _rows() + [{"id": "a", "analysis_failures": 3}, {"id": "b", "analysis_unanswered_at": "2026-10-11"}]
    st, detail = pdc.check_stage3("GAVE UP after 3 failed nights", rows)[1:]
    assert st == pdc.WARN and "given up 1" in detail and "unanswered-marked 1" in detail
    assert pdc.check_stage3("", _rows())[1] == pdc.PASS


def test_stage2_reports_the_counts():
    detail = pdc.check_stage2(OK_LOG)[2]
    assert "dated from the page tonight: 1" in detail and "code change: 7" in detail


def test_ci_reads_the_latest_run():
    class R:
        def __init__(self, out):
            self.stdout = out
    ok = lambda *a, **k: R(json.dumps([{"status": "completed", "conclusion": "success", "headSha": "abcdef1"}]))
    bad = lambda *a, **k: R(json.dumps([{"status": "completed", "conclusion": "failure", "headSha": "abcdef1"}]))
    boom = lambda *a, **k: (_ for _ in ()).throw(OSError("no gh"))
    assert pdc.check_ci(ok)[1] == pdc.PASS
    assert pdc.check_ci(bad)[1] == pdc.FAIL
    assert pdc.check_ci(boom)[1] == pdc.WARN


def test_three_clean_nights_close_the_loop_and_a_fail_resets():
    clean = [("x", pdc.PASS, ""), ("y", pdc.WARN, "")]
    failing = [("x", pdc.FAIL, "")]
    s = {}
    s = pdc.advance(s, clean, "2026-10-11")
    s = pdc.advance(s, clean, "2026-10-11")                 # a rerun the same night does not count twice
    assert s["consecutive_clean"] == 1
    s = pdc.advance(s, failing, "2026-10-12")
    assert s["consecutive_clean"] == 0 and "closed" not in s
    for night in ("2026-10-13", "2026-10-14", "2026-10-15"):
        s = pdc.advance(s, clean, night)
    assert s["closed"] == "2026-10-15"
    assert "闭环结束" in pdc.render(clean, s, "2026-10-15")[0]


def test_run_checks_end_to_end_on_tmp_files(tmp_path):
    data = tmp_path / "a.jsonl"
    data.write_text("".join(json.dumps(r) + "\n" for r in _rows()))
    base = tmp_path / "base.json"
    base.write_text(json.dumps(pdc.make_baseline(_rows())))
    log = tmp_path / "gmia.log"
    log.write_text(OK_LOG)
    checks = pdc.run_checks(NOW, data=data, log=log, page=_page(tmp_path), baseline_path=base,
                            ci=lambda: ("CI", pdc.PASS, "ok"))
    assert [st for _, st, _ in checks] == [pdc.PASS] * 6, checks
