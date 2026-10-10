"""Stage-3 health check follow-ups (2026-10-10), each reproduced before the fix.

F1  an article every model fails on in the same way was retried every night,
    paid every night, forever; three such at the front stopped the stage
F2  tagging calls were booked in the stage-3 usage log (1,890 / 4,237 rows on
    10-07 / 10-08), and importing analyze_articles sent every importer's log
    lines to analyze_articles.log
F3  refine_dates replaced a precise listing day with a fallback (7 Wellington
    rows: the visible label is "August 2026", the datetime attribute the day)
F4  Ctrl-C during a tag run kept every queued call going and saved nothing
F5  two timeouts on the wording re-ask turned an article into grounding_failed
F6  --retag-before never gave up on a row that kept failing
Fd  resummarize reported a row it did not write as "replaced" (in
    test_resummarize_long_articles.py)
Fe  a passage with no letters passed the evidence check
"""
import importlib.util
import json
import json as _json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

import analyze_articles as aa
import publish
import taxonomy
from test_stage3_error_handling import (LATIN, SUMMARY, TAG_BODY, TAG_GOOD, TOO_MANY, USAGE, _art,  # noqa: F401
                                        _distinct, _rows, _tread, _trow, api, http, ok, refusal, store, ta,
                                        tagstore)

REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rd = _load("refine_dates_f", "scripts/refine_dates.py")


# ── F1: a give-up cap for articles that fail the same way every night ─────────

def _with_good_neighbour(rows, night):
    """Add a fresh article that will be summarised tonight: a night on which no
    article is answered at all is the service's fault and is not counted."""
    good = _art(100 + night)
    path = aa.DATA_FILE
    current = [json.loads(l) for l in path.read_text().splitlines() if l] if path.exists() else rows
    (aa.CONTENT_DIR / f"{good['id']}.txt").write_text(_distinct(100 + night), encoding="utf-8")
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in current + [good]))


def test_f1_a_deterministic_failure_is_given_up_after_max_nights(store, api, capsys):
    store([_art(1)], bodies={"a1": _distinct(1)})
    outs = []
    for night in range(aa.MAX_ANALYSIS_NIGHTS):
        _with_good_neighbour(None, night)
        # night 1: a1 is first in the store; after that it has failed, so it goes last
        api["script"] = ([http(400, "invalid_prompt")] * (2 * aa.MAX_ATTEMPTS) + [ok(json.dumps(SUMMARY))]
                         if night == 0 else [ok(json.dumps(SUMMARY)), http(400, "invalid_prompt")])
        outs.append((aa.main(), capsys.readouterr().out))
    assert _rows()["a1"]["analysis_failures"] == aa.MAX_ANALYSIS_NIGHTS
    assert "GAVE UP" in outs[-1][1] and all("GAVE UP" not in o for _, o in outs[:-1])
    assert outs[-1][0] == aa.GAVE_UP_RC and all(rc == 0 for rc, _ in outs[:-1])
    assert aa._should_analyze(_rows()["a1"]) is False


def test_f1_the_give_up_night_alerts_even_when_others_were_summarised(store, api, capsys):
    store([_art(1, analysis_failures=aa.MAX_ANALYSIS_NIGHTS - 1), _art(2)],
          bodies={"a1": _distinct(1), "a2": _distinct(2)})
    api["script"] = [ok(json.dumps(SUMMARY)), http(400, "invalid_prompt")]
    assert aa.main() == aa.GAVE_UP_RC
    assert "GAVE UP" in capsys.readouterr().out and _rows()["a2"]["summarized"] is True


@pytest.mark.parametrize("bad", [ok("not json"), http(400, "invalid_prompt")])
def test_f1_unparsable_answers_and_4xx_count(store, api, bad):
    store([_art(1), _art(2)], bodies={"a1": _distinct(1), "a2": _distinct(2)})
    api["script"] = [ok(json.dumps(SUMMARY)), bad]          # a1 answered (it is first), a2 fails
    aa.main()
    assert _rows()["a2"]["analysis_failures"] == 1


def test_f1_a_night_nobody_was_answered_counts_nothing_and_only_marks(store, api, capsys):
    """A model retired or a parameter rejected fails every request the same
    way: nothing counts against the articles, however many nights it lasts."""
    store([_art(i) for i in range(3)], bodies={f"a{i}": _distinct(i) for i in range(3)})
    api["script"] = [http(404, "model_not_found")]
    rcs = [aa.main() for _ in range(aa.MAX_ANALYSIS_NIGHTS + 2)]
    rows = _rows().values()
    assert not any("analysis_failures" in r for r in rows)
    assert all(r.get("analysis_unanswered_at") for r in rows)
    assert rcs[0] == 1, "the first such night is an outage alert"
    assert "GAVE UP" not in capsys.readouterr().out


def test_f1_a_quiet_night_that_only_re_asks_known_bad_rows_is_not_an_outage(store, api, caplog):
    """No new articles (09-14, 09-21, 10-05) and a row that failed before:
    "TOTAL ANALYSIS OUTAGE" every such night was a false alarm."""
    store([_art(1, analysis_failures=1)])
    api["script"] = [ok("not json")]
    with caplog.at_level("ERROR"):
        assert aa.main() == 0
    assert "OUTAGE" not in caplog.text and "STOPPED" not in caplog.text
    assert _rows()["a1"]["analysis_failures"] == 1          # not judged tonight


def test_f1_three_bad_articles_at_the_front_no_longer_block_new_ones(store, monkeypatch, capsys, tmp_path):
    """From zero failures, in store order: night 1 they stop the stage before
    the new article (nobody answered: marked, not counted); from night 2 they
    go last, a fresh article is asked first and answered, and they are judged
    and finally given up -- with exit 3, not hidden behind a stop's exit 1."""
    monkeypatch.setattr(aa, "USAGE_LOG_FILE", tmp_path / "usage.jsonl")
    monkeypatch.setattr(aa.requests, "post", lambda url, headers=None, json=None, timeout=None:
                        ok("not json") if "BADMARK" in json["messages"][0]["content"] else ok(_json.dumps(SUMMARY)))
    store([_art(i) for i in range(3)], bodies={f"a{i}": _distinct(i) + " BADMARK" for i in range(3)})
    summarised, rcs = [], []
    for night in range(aa.MAX_ANALYSIS_NIGHTS + 1):
        _with_good_neighbour(None, night)
        rcs.append(aa.main())
        summarised.append({r["id"] for r in _rows().values() if r.get("summarized")})
    rows = _rows()
    assert "a100" not in summarised[0] and "a100" in summarised[1], summarised
    assert all(rows[f"a{i}"]["analysis_failures"] == aa.MAX_ANALYSIS_NIGHTS for i in range(3))
    assert rcs[-1] == aa.GAVE_UP_RC and "GAVE UP" in capsys.readouterr().out


def test_f1_an_article_alone_is_judged_only_on_nights_another_one_is_answered(store, api, capsys):
    store([_art(1)])
    api["script"] = [ok("not json")]
    for _ in range(aa.MAX_ANALYSIS_NIGHTS + 1):
        aa.main()
    assert "analysis_failures" not in _rows()["a1"]                 # alone: never proven
    for night in range(aa.MAX_ANALYSIS_NIGHTS):
        _with_good_neighbour(None, night)
        api["script"] = [ok(json.dumps(SUMMARY)), ok("not json")]
        rc = aa.main()
    assert rc == aa.GAVE_UP_RC and "GAVE UP" in capsys.readouterr().out


def test_f1_a_refusal_counts_when_the_service_is_proven(store, api):
    """A refusal is an answer (HTTP 200, no text), counted like junk."""
    store([_art(1), _art(2)], bodies={"a1": _distinct(1), "a2": _distinct(2)})
    api["script"] = [ok(json.dumps(SUMMARY)), refusal()]
    aa.main()
    assert _rows()["a2"]["analysis_failures"] == 1


def test_f1_a_proxy_error_page_is_a_passing_fault(store, api):
    """A 200 whose body is not JSON: requests raises its JSONDecodeError (a
    RequestException), which counted against the article."""
    store([_art(1), _art(2)], bodies={"a1": _distinct(1), "a2": _distinct(2)})

    class HtmlPage:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise requests.exceptions.JSONDecodeError("Expecting value", "<html>", 0)
    api["script"] = [ok(json.dumps(SUMMARY)), HtmlPage()]
    aa.main()
    assert "analysis_failures" not in _rows()["a2"]


def test_f1_a_batch_save_never_carries_a_count_the_night_may_take_back(store, api, monkeypatch):
    # Title-only rows are declined without a model call, so the 3-in-a-row stop
    # does not end the night before the batch save after the 5th article.
    title_only = {1, 2, 4}
    rows = [_art(i, content_status="metadata_only") if i in title_only else _art(i) for i in range(6)]
    store(rows, bodies={f"a{i}": ("Title: x" if i in title_only else _distinct(i)) for i in range(6)})
    api["script"] = [http(404, "model_not_found")]
    saved = []
    real = aa.save_articles
    monkeypatch.setattr(aa, "save_articles",
                        lambda arts, path=None: (saved.append([a.get("analysis_failures") for a in arts]),
                                                 real(arts, path)))
    aa.main()
    assert len(saved) >= 2, "the batch save after the 5th article did not happen"
    # Counts are written once the night is judged: in the final save only.
    assert all(v is None for snap in saved[:-1] for v in snap), saved


def test_f1_articles_that_failed_before_go_to_the_back_of_the_queue(store, api):
    rows = [_art(i, analysis_failures=1) for i in range(3)] + [_art(9)]
    store(rows, bodies={f"a{i}": _distinct(i) for i in (0, 1, 2, 9)})
    api["script"] = [ok(json.dumps(SUMMARY)), http(400, "invalid_prompt")]
    aa.main()
    assert _rows()["a9"]["summarized"] is True


@pytest.mark.parametrize("script", [[http(503, "server_error")], [requests.Timeout("slow")],
                                    [http(429, "rate_limit_exceeded")]])
def test_f1_a_night_of_passing_faults_does_not_count(store, api, script):
    store([_art(1)])
    api["script"] = script
    aa.main()
    assert "analysis_failures" not in _rows()["a1"]


def test_f1_a_success_clears_the_count(store, api):
    store([_art(1, analysis_failures=2, analysis_unanswered_at="2026-10-09")])
    api["script"] = [ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    row = _rows()["a1"]
    assert row["summarized"] is True and "analysis_failures" not in row and "analysis_unanswered_at" not in row


def test_f1_given_up_articles_no_longer_block_the_ones_behind(store, api):
    rows = [_art(i, analysis_failures=aa.MAX_ANALYSIS_NIGHTS) for i in range(3)] + [_art(9)]
    store(rows, bodies={f"a{i}": _distinct(i) for i in (0, 1, 2, 9)})
    api["script"] = [ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    assert _rows()["a9"]["summarized"] is True and len(api["calls"]) == 1


# ── F5: a re-ask that never got an answer is not a rejection ─────────────────

WORDING = dict(SUMMARY, summary_en="The provided text says the Federal Reserve kept interest rates unchanged.")


def test_f5_two_timed_out_reasks_leave_the_article_pending(api):
    api["script"] = [ok(json.dumps(WORDING)), requests.Timeout("slow"),
                     ok(json.dumps(WORDING)), requests.Timeout("slow")]
    faults = []
    assert aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x", faults=faults) is None
    assert len(api["calls"]) == 4, "the weaker tier must not be asked"
    assert faults and all(f == aa.PASSING for f in faults)


@pytest.mark.parametrize("second", [requests.Timeout("slow"), http(503, "server_error")])
def test_f5_a_timed_out_reask_then_a_failed_attempt_is_not_a_rejection(api, second):
    """The re-ask times out, then the next attempt's first call fails too: no
    re-ask ever answered, so nothing was judged."""
    api["script"] = [ok(json.dumps(WORDING)), requests.Timeout("slow"), second, second]
    assert aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x") is None


def test_f5_an_earlier_answered_reask_does_not_judge_a_later_rejection(api):
    """Attempt 1's re-ask came back (junk), attempt 2's re-ask timed out: the
    rejection that stands is attempt 2's, and nobody answered it."""
    api["script"] = [ok(json.dumps(WORDING)), ok("not json"),
                     ok(json.dumps(WORDING)), requests.Timeout("slow")]
    assert aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x") is None


def test_f5_reasks_that_came_back_as_junk_still_end_in_a_rejection(api):
    """Answered, if badly: the rejection was judged and stands (audit S5)."""
    api["script"] = [ok(json.dumps(WORDING)), ok("not json"), ok(json.dumps(WORDING)), ok("not json")]
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r is not None and r["_label"] == aa.RULE_MADE_DECLINE


def test_f5_a_reask_that_answers_badly_is_still_a_rejection(api):
    api["script"] = [ok(json.dumps(WORDING)), ok(json.dumps(WORDING))]
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r["insufficient_content"] and r["_label"] == aa.RULE_MADE_DECLINE


# ── F2: each stage keeps its own books and its own log ───────────────────────

def test_f2_summary_rows_say_which_stage_booked_them(tmp_path):
    p = tmp_path / "u.jsonl"
    aa._append_usage_log("x", "gpt-5.6-luna", dict(USAGE), path=p, parsed=True)
    assert json.loads(p.read_text())["stage"] == "summary"


def test_f2_tagging_books_its_calls_in_its_own_log(tagstore, tmp_path, monkeypatch):
    summary_log, tag_log = tmp_path / "summary.jsonl", tmp_path / "tag.jsonl"
    monkeypatch.setattr(aa, "USAGE_LOG_FILE", summary_log)
    monkeypatch.setattr(aa, "TAG_USAGE_LOG_FILE", tag_log)
    path = tagstore([_trow(1)])
    rc = ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None,
                call=lambda p, k, model: (json.dumps(TAG_GOOD), dict(USAGE), model))
    assert rc == 0
    assert not summary_log.exists()
    rows = [json.loads(l) for l in tag_log.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["stage"] == "tag" and rows[0]["article_id"] == "t1"


@pytest.mark.parametrize("load", ["import analyze_articles",
                                  "runpy.run_path('scripts/tag_articles.py', run_name='imported')",
                                  "runpy.run_path('scripts/refine_dates.py', run_name='imported')"])
def test_f2_importing_does_not_hijack_the_log(load):
    """Only a summarising run writes analyze_articles.log (refine_dates still logs to
    fetch.log through fetch_articles: stage 1b is stage-1 work)."""
    code = ("import logging, runpy, sys; sys.path[:0] = ['.', 'scripts']; " + load + "; "
            "print(any(getattr(h, 'baseFilename', '').endswith('analyze_articles.log') "
            "for h in logging.getLogger().handlers))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:]
    assert r.stdout.strip().splitlines()[-1] == "False"


def test_f2_the_model_switch_check_reads_summary_rows_only():
    text = (REPO / "scripts" / "check-model-switch.sh").read_text(encoding="utf-8")
    assert 'r.get("stage", "summary") == "summary"' in text


# ── F3: a precise listing day is not "month-only" ────────────────────────────

def _drow(date, raw="August 2026", **kw):
    return dict({"id": "d1", "source_id": "wellington", "url": "https://x/1", "date": date,
                 "date_raw": raw, "fetched_at": "2026-09-15T04:00:00+08:00"}, **kw)


@pytest.mark.parametrize("date,expected", [("2026-08-17", False), ("2026-08-31", True), ("2026-08-01", True)])
def test_f3_only_a_month_boundary_date_is_refined(date, expected):
    assert bool(rd.candidates([_drow(date)])) is expected


def test_f3_a_day_from_the_listing_is_shown_as_a_day():
    row = _drow("2026-08-17", date_listed="2026-08-17", date_basis="listing")
    assert publish._display_date(row) == "2026-08-17"


# ── F4: an interrupted tag run stops spending and keeps what it has ──────────

def test_f4_ctrl_c_cancels_the_queue_and_saves_the_finished(tagstore, tmp_path):
    path = tagstore([_trow(i) for i in range(30)])
    calls = []

    def call(prompt, key, model):
        calls.append(1)
        time.sleep(0.02)                 # a real call takes time; the queue must not drain at once
        return json.dumps(TAG_GOOD), dict(USAGE), model

    seen, at_interrupt = [], []

    def log_usage(*a, **k):
        seen.append(1)
        if len(seen) == 5:
            at_interrupt.append(len(calls))
            raise KeyboardInterrupt

    workers = 2
    with pytest.raises(KeyboardInterrupt):
        ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, workers=workers, call=call, log_usage=log_usage)
    # The workers may have run ahead before the interrupt (the fake call is
    # instant); after it, at most the ones already in flight may finish.
    assert len(calls) <= at_interrupt[0] + workers, (at_interrupt, len(calls))
    assert len(calls) < 30
    tagged = [r for r in _tread(path).values() if r.get("tags")]
    # Every call that came back is paid for: its tags must be saved, the ones in
    # flight at the interrupt included (pre-merge review: they were dropped) --
    # all but the one being recorded when Ctrl-C landed, which is not recorded
    # twice (re-review: that booked its calls twice).
    assert len(tagged) == len(calls) - 1, (len(tagged), len(calls))


def test_f4_a_give_up_reached_before_the_interrupt_is_still_announced(tagstore, tmp_path, capsys):
    path = tagstore([_trow(i, tag_failures=ta.MAX_TAG_NIGHTS - 1) for i in range(3)])

    def bad(prompt, key, model):
        time.sleep(0.01)
        return json.dumps(TOO_MANY), dict(USAGE), model
    seen = []

    def log_usage(*a, **k):
        seen.append(1)
        if len(seen) == ta.ATTEMPTS * 2:          # after the second article's calls
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, workers=1, call=bad, log_usage=log_usage)
    gave_up = [r["id"] for r in _tread(path).values() if r.get("tag_failures") == ta.MAX_TAG_NIGHTS]
    assert gave_up and "GAVE UP" in capsys.readouterr().out


# ── F6: re-tagging gives up too ──────────────────────────────────────────────

def test_f6_retag_skips_a_row_that_has_failed_max_nights(tagstore):
    rows = [_trow(1, tags=["research"], tags_at="2026-10-01T00:00:00+08:00", tag_failures=ta.MAX_TAG_NIGHTS),
            _trow(2, tags=["research"], tags_at="2026-10-01T00:00:00+08:00")]
    tagstore(rows)                       # bodies exist: only the failure count can exclude t1
    assert [r["id"] for r in ta.candidates(rows, retag_before="2026-10-09T00:00:00+08:00")] == ["t2"]


def test_f6_retag_failures_are_given_up_once(tagstore, tmp_path):
    path = tagstore([_trow(1, tags=["research"], tags_at="2026-10-01T00:00:00+08:00")])

    def bad(prompt, key, model):
        return json.dumps(TOO_MANY), dict(USAGE), model
    rcs = [ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, call=bad, log_usage=lambda *a, **k: None,
                  retag_before="2026-10-09T00:00:00+08:00") for _ in range(ta.MAX_TAG_NIGHTS + 2)]
    assert rcs == [1, 1, ta.GAVE_UP_RC, 0, 0]
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS


# ── Fe: a passage needs letters ──────────────────────────────────────────────

def test_fe_a_passage_without_letters_is_not_evidence():
    answer = dict(TAG_GOOD, evidence=dict(TAG_GOOD["evidence"], equities="_ _ _ _ _ _ _ _ _ _"))
    kept, dropped = taxonomy.check_evidence(answer, TAG_BODY)
    assert "equities" not in kept["assets"] and any(d.startswith("equities") for d in dropped)


def test_f4_a_failing_report_write_still_saves_what_is_tagged(tagstore, tmp_path):
    """record() raising used to be retried by the cleanup, raise again, and skip
    the save of every article already tagged."""
    path = tagstore([_trow(1), _trow(2)])
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "report.jsonl").mkdir()                     # every report write fails
    with pytest.raises(OSError):
        ta.run(path, "k", bk, sleep=lambda s: None, workers=1, log_usage=lambda *a, **k: None,
               call=lambda p, k, model: (json.dumps(TAG_GOOD), dict(USAGE), model))
    assert any(r.get("tags") for r in _tread(path).values())


def test_f4_an_interrupt_while_recording_books_nothing_twice(tagstore, tmp_path):
    path = tagstore([_trow(1), _trow(2)])
    booked = []

    def log_usage(article_id, *a, **k):
        booked.append(article_id)
        if len(booked) == 1:
            raise KeyboardInterrupt

    def call(p, k, model):
        time.sleep(0.01)
        return json.dumps(TAG_GOOD), dict(USAGE), model
    with pytest.raises(KeyboardInterrupt):
        ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None, workers=1, call=call, log_usage=log_usage)
    assert len(booked) == len(set(booked)), booked


def test_f5_a_refused_reask_is_an_answer_like_junk(api):
    api["script"] = [ok(json.dumps(WORDING)), refusal(), requests.Timeout("slow"), requests.Timeout("slow")]
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r is not None and r["_label"] == aa.RULE_MADE_DECLINE


def test_f1_a_several_night_outage_keeps_alerting_without_new_articles(store, api):
    """Rows only MARKED on an outage night are not proven bad: the next
    outage night must alert again, new articles or not."""
    store([_art(i) for i in range(2)], bodies={f"a{i}": _distinct(i) for i in range(2)})
    api["script"] = [http(400, "invalid_request_error")]
    assert [aa.main() for _ in range(3)] == [1, 1, 1]
