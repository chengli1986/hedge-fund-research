"""Stage 3 / 3b error and billing handling (stage-3 code audit, 2026-10-09).

Each test is one finding, reproduced against the code before it was fixed:

S1  a night with no working key, plus one article declined without a model
    call (duplicate / title-only), exited 0 -- no alert
S2  quota/auth errors did not stop the run (4 calls per article, every article),
    and a stalled network cost 8 minutes an article
S3  a refusal (HTTP 200, content null, usage present) was billed but not logged
S4  a summary with empty fields was accepted and published
S5  a re-ask answered with junk turned a wording-only problem into a decline
S6  one undecodable content file crashed the run, every night
S7  usage: null made the usage logger raise and threw a parsed summary away
S8  a missing content file skipped the batch-save count
N1  an invalid tag answer was retried with the identical prompt, every night
N2  a refusal crashed the whole tagging run, and its exit code read as the
    harmless "some left untagged"
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import requests

import analyze_articles as aa
import taxonomy

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tag_articles.py"
spec = importlib.util.spec_from_file_location("tag_articles", SCRIPT)
ta = importlib.util.module_from_spec(spec)
sys.modules["tag_articles"] = ta
spec.loader.exec_module(ta)

LATIN = ("The Federal Reserve kept interest rates unchanged while inflation expectations moderated "
         "across bond markets and equity valuations stretched further this quarter. ") * 20


def _distinct(i):
    """LATIN plus enough text of its own that the duplicate check (which also
    compares retitled bodies) sees a different article."""
    return LATIN + " ".join(f"item{i}x{k}" for k in range(400))


SUMMARY = {"summary_en": "The Federal Reserve kept interest rates unchanged while inflation expectations moderated.",
           "summary_zh": "美联储维持利率不变，通胀预期回落。", "themes": ["Macro/Rates"],
           "key_takeaway_en": "Rates unchanged; inflation expectations moderated across bond markets.",
           "key_takeaway_zh": "利率不变。"}
USAGE = {"prompt_tokens": 5000, "completion_tokens": 300, "total_tokens": 5300}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            r = requests.Response()
            r.status_code = self.status_code
            r._content = json.dumps(self._body).encode()
            raise requests.HTTPError(str(self.status_code), response=r)

    def json(self):
        return self._body


def ok(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": dict(USAGE)})


def refusal():
    return _Resp(200, {"choices": [{"message": {"content": None, "refusal": "I can't help with that."},
                                    "finish_reason": "stop"}], "usage": dict(USAGE)})


def http(status, code):
    return _Resp(status, {"error": {"code": code, "type": code}})


@pytest.fixture
def api(monkeypatch, tmp_path):
    """requests.post replaced by a script of responses; usage log in tmp."""
    monkeypatch.setattr(aa, "USAGE_LOG_FILE", tmp_path / "usage.jsonl")
    state = {"script": [], "calls": []}

    def post(url, headers=None, json=None, timeout=None):
        state["calls"].append(json["messages"][0]["content"])
        script = state["script"]
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(aa.requests, "post", post)
    state["usage_rows"] = lambda: ([json.loads(l) for l in (tmp_path / "usage.jsonl").read_text().splitlines()]
                                   if (tmp_path / "usage.jsonl").exists() else [])
    return state


@pytest.fixture
def store(tmp_path, monkeypatch):
    content = tmp_path / "content"
    content.mkdir()
    monkeypatch.setattr(aa, "CONTENT_DIR", content)
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "DATA_FILE", tmp_path / "articles.jsonl")
    monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
    monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])

    def make(rows, bodies=None):
        for r in rows:
            body = (bodies or {}).get(r["id"], LATIN)
            if body is not None:
                p = content / f"{r['id']}.txt"
                p.write_bytes(body) if isinstance(body, bytes) else p.write_text(body, encoding="utf-8")
        aa.DATA_FILE.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        return aa.DATA_FILE
    return make


def _art(i, **kw):
    r = {"id": f"a{i}", "source_id": "src", "title": f"Title {i}", "date": "2026-10-01", "content_status": "ok"}
    r.update(kw)
    return r


def _rows():
    return {r["id"]: r for r in (json.loads(l) for l in aa.DATA_FILE.read_text().splitlines() if l)}


# ---- S1 -------------------------------------------------------------------

def test_s1_a_dead_key_is_an_outage_even_when_a_title_only_article_was_declined(store, api):
    store([_art(1, content_status="metadata_only"), _art(2)], bodies={"a1": "Title: x"})
    api["script"] = [http(401, "invalid_api_key")]
    assert aa.main() != 0
    assert _rows()["a1"]["analysis_label"] == "title_only"       # the rule-made decline still happens


def test_s1_models_down_is_an_outage_even_when_a_title_only_article_was_declined(store, api):
    """Non-fatal errors (503) on every call: the counting rule itself, not the quota stop."""
    store([_art(1, content_status="metadata_only"), _art(2)], bodies={"a1": "Title: x"})
    api["script"] = [http(503, "server_error")]
    assert aa.main() == 1


def test_s1_a_night_where_only_rule_made_declines_happen_is_quiet(store, api):
    store([_art(1, content_status="metadata_only")], bodies={"a1": "Title: x"})
    api["script"] = [AssertionError("no model call expected")]
    assert aa.main() == 0


# ---- S2 -------------------------------------------------------------------

@pytest.mark.parametrize("resp", [http(401, "invalid_api_key"),
                                  http(429, "insufficient_quota")])
def test_s2_quota_or_auth_stops_the_run_at_the_first_call(store, api, resp):
    store([_art(i) for i in range(3)], bodies={f"a{i}": _distinct(i) for i in range(3)})
    api["script"] = [resp]
    assert aa.main() == 2
    assert len(api["calls"]) == 1
    assert not any(r.get("summarized") or r.get("analysis_status") for r in _rows().values())


def test_s2_quota_running_out_mid_run_keeps_what_was_done_and_alerts(store, api):
    # distinct bodies: identical ones would be declined as duplicates, with no call
    store([_art(i) for i in range(3)], bodies={f"a{i}": _distinct(i) for i in range(3)})
    api["script"] = [ok(json.dumps(SUMMARY)), http(429, "insufficient_quota")]
    assert aa.main() == 2
    assert [r.get("summarized", False) for r in _rows().values()] == [True, False, False]


def test_s2_a_plain_rate_limit_is_retried_not_fatal(store, api):
    store([_art(1)])
    api["script"] = [http(429, "rate_limit_exceeded"), ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    assert _rows()["a1"]["summarized"] is True


def test_s2_consecutive_unanswered_articles_stop_the_stage(store, monkeypatch):
    store([_art(i) for i in range(6)], bodies={f"a{i}": _distinct(i) for i in range(6)})
    seen = []
    monkeypatch.setattr(aa, "_analyze_with_fallback", lambda *a, **k: seen.append(k["article_id"]))
    assert aa.main() == 1
    assert len(seen) == aa.MAX_CONSECUTIVE_UNANSWERED


# ---- S3 -------------------------------------------------------------------

def test_s3_a_refusal_is_logged_as_a_billed_call(api):
    api["script"] = [refusal()]
    assert aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x") is None
    rows = api["usage_rows"]()
    assert len(rows) == len(api["calls"]) == 2 * aa.MAX_ATTEMPTS
    assert all(r["input_tokens"] == 5000 and r["parsed"] is False for r in rows)


# ---- S4 -------------------------------------------------------------------

@pytest.mark.parametrize("field", ["summary_en", "summary_zh", "key_takeaway_en", "key_takeaway_zh"])
@pytest.mark.parametrize("value", ["", "   ", None, 7])
def test_s4_an_empty_or_non_text_field_is_not_a_summary(field, value):
    assert aa._parse_llm_output(json.dumps(dict(SUMMARY, **{field: value}))) is None


# ---- S5 -------------------------------------------------------------------

def test_s5_a_junk_reask_is_retried_not_declined(api):
    wording = dict(SUMMARY, summary_en="The provided text says the Federal Reserve kept interest rates unchanged.")
    api["script"] = [ok(json.dumps(wording)), ok("not json"), ok(json.dumps(SUMMARY))]
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r is not None and not r.get("insufficient_content")
    assert r["summary_en"] == SUMMARY["summary_en"]


# ---- S6 -------------------------------------------------------------------

def test_s6_an_unreadable_body_is_sent_back_to_stage_2_and_the_run_goes_on(store, api):
    store([_art(1), _art(2)], bodies={"a1": b"\xff\xfe not utf-8 \x80"})
    api["script"] = [ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    rows = _rows()
    assert rows["a1"]["content_status"] == "failed" and rows["a2"]["summarized"] is True


# ---- S7 -------------------------------------------------------------------

def test_s7_usage_null_is_logged_as_unknown_and_never_raises(tmp_path):
    p = tmp_path / "u.jsonl"
    aa._append_usage_log("x", "gpt-4.1-mini", None, path=p, parsed=True)
    row = json.loads(p.read_text())
    assert row["input_tokens"] is None and row["parsed"] is True


# ---- S8 -------------------------------------------------------------------

def test_s8_a_missing_body_still_counts_towards_the_batch_save(store, monkeypatch):
    bodies = {f"a{i}": _distinct(i) for i in range(4)}
    store([_art(i) for i in range(5)], bodies=dict(bodies, a4=None))
    monkeypatch.setattr(aa, "_analyze_with_fallback", lambda *a, **k: dict(SUMMARY, _model="m", _usage={}))
    saves = []
    real = aa.save_articles
    monkeypatch.setattr(aa, "save_articles", lambda arts, path=None: (saves.append(1), real(arts, path)))
    aa.main()
    assert len(saves) == 2                      # after the 5th article, and the final one


# ---- N1 / N2 (Stage 3b) ---------------------------------------------------

TAG_BODY = ("Equity markets rallied as investors rotated into large technology names this quarter. "
            "Spending on artificial intelligence data centres by the hyperscalers kept accelerating through June. ") * 5
TAG_GOOD = {"article_type": "research", "regions": ["us"], "assets": ["equities"], "topics": ["ai_tech"], "methods": [],
            "evidence": {"equities": "Equity markets rallied as investors rotated into large technology names",
                         "ai_tech": "Spending on artificial intelligence data centres by the hyperscalers kept accelerating"}}
TOO_MANY = dict(TAG_GOOD, assets=["equities", "govt_bonds", "credit", "commodities"])


@pytest.fixture
def tagstore(tmp_path, monkeypatch):
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "CONTENT_DIR", tmp_path / "content")
    (tmp_path / "content").mkdir()
    path = tmp_path / "articles.jsonl"

    def make(rows):
        for r in rows:
            (tmp_path / "content" / f"{r['id']}.txt").write_text(TAG_BODY, encoding="utf-8")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return path
    return make


def _trow(i, **kw):
    r = {"id": f"t{i}", "source_id": "src", "title": f"T{i}", "summarized": True, "content_status": "ok"}
    r.update(kw)
    return r


def _tread(path):
    return {r["id"]: r for r in (json.loads(l) for l in path.read_text().splitlines() if l)}


def _trun(path, tmp_path, logged=None, **kw):
    return ta.run(path, "k", tmp_path / "bk", sleep=lambda s: None,
                  log_usage=(lambda i, m, u, parsed=None: logged.append((i, parsed))) if logged is not None
                  else (lambda *a, **k: None), **kw)


def test_n2_a_refusal_fails_that_article_only_and_is_billed(tagstore, tmp_path, api):
    path = tagstore([_trow(1), _trow(2)])
    api["script"] = [refusal()] * ta.ATTEMPTS + [ok(json.dumps(TAG_GOOD))]
    logged = []
    rc = _trun(path, tmp_path, logged, workers=1)
    rows = _tread(path)
    assert rc == 1
    assert "tags" not in rows["t1"] and rows["t2"]["tags"] == taxonomy.flatten(TAG_GOOD)
    assert [p for i, p in logged if i == "t1"] == [False] * ta.ATTEMPTS


def test_n2_an_unexpected_error_on_one_article_does_not_end_the_run(tagstore, tmp_path):
    path = tagstore([_trow(1), _trow(2)])

    def call(prompt, key, model):
        if "T1" in prompt:
            raise RuntimeError("boom")
        return json.dumps(TAG_GOOD), dict(USAGE), model
    assert _trun(path, tmp_path, workers=1, call=call) == 1
    assert "tags" in _tread(path)["t2"]


def test_n1_the_retry_says_what_was_wrong(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    prompts = []

    def call(prompt, key, model):
        prompts.append(prompt)
        return json.dumps(TOO_MANY if len(prompts) == 1 else TAG_GOOD), dict(USAGE), model
    assert _trun(path, tmp_path, call=call) == 0
    assert "assets: 4 not in 0-3" not in prompts[0]
    assert "assets: 4 not in 0-3" in prompts[1]


def test_n1_after_three_failed_nights_the_article_is_given_up_once_and_loudly(tagstore, tmp_path):
    path = tagstore([_trow(1)])

    def bad(prompt, key, model):
        return json.dumps(TOO_MANY), dict(USAGE), model
    assert [_trun(path, tmp_path, call=bad) for _ in range(ta.MAX_TAG_NIGHTS)] == [1, 1, ta.GAVE_UP_RC]
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS
    calls = []
    assert _trun(path, tmp_path, call=lambda *a, **k: calls.append(1)) == 0     # not asked again
    assert calls == []


def test_n1_a_success_clears_the_failure_count(tagstore, tmp_path):
    path = tagstore([_trow(1, tag_failures=2)])
    assert _trun(path, tmp_path, call=lambda p, k, model: (json.dumps(TAG_GOOD), dict(USAGE), model)) == 0
    assert "tag_failures" not in _tread(path)["t1"]


def test_n2_a_damaged_store_is_not_reported_as_some_left_untagged(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    with path.open("a") as f:
        f.write('{"id": "torn\n')
    assert _trun(path, tmp_path, call=lambda *a, **k: None) == ta.BROKEN_RC


def test_n2_a_crash_outside_the_loop_alerts_rather_than_warns(monkeypatch):
    def boom(argv=None):
        raise RuntimeError("store vanished")
    monkeypatch.setattr(ta, "main", boom)
    assert ta.entry() == ta.BROKEN_RC
    assert ta.BROKEN_RC not in (0, 1)          # run_pipeline.sh only logs 1


def test_n2_the_script_exits_through_entry():
    src = SCRIPT.read_text()
    assert "sys.exit(entry())" in src.split('if __name__ == "__main__":')[1]


# ---- Second review of the stage-3 code (2026-10-09, R2-R10) ---------------

def _empty_usage():
    return _Resp(200, {"choices": [{"message": {"content": json.dumps(TAG_GOOD)}}], "usage": None})


def test_r2_usage_null_does_not_lose_the_tagging_batch(tagstore, tmp_path, api):
    """S7 fixed the usage logger; the tagger's token tally still did usage.get()."""
    path = tagstore([_trow(1)])
    api["script"] = [_empty_usage()]
    assert _trun(path, tmp_path) == 0
    assert _tread(path)["t1"]["tags"] == taxonomy.flatten(TAG_GOOD)


def test_r2_call_openai_returns_a_dict_for_usage_null(api):
    api["script"] = [_empty_usage()]
    assert aa._call_openai("p", "k")[1] == {}


def _quota():
    http(429, "insufficient_quota").raise_for_status()


def test_r3_an_article_given_up_on_a_quota_night_is_still_announced(tagstore, tmp_path, capsys):
    path = tagstore([_trow(1, tag_failures=ta.MAX_TAG_NIGHTS - 1), _trow(2)])

    def call(prompt, key, model):
        if "Title: T2" in prompt:
            _quota()
        return json.dumps(TOO_MANY), dict(USAGE), model
    assert _trun(path, tmp_path, workers=1, call=call) == 2
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS
    assert "GAVE UP" in capsys.readouterr().out


@pytest.mark.parametrize("fail", [
    lambda: (_ for _ in ()).throw(requests.ConnectionError("reset")),
    lambda: (_ for _ in ()).throw(requests.Timeout("slow")),
    lambda: http(503, "server_error").raise_for_status(),
    lambda: http(429, "rate_limit_exceeded").raise_for_status(),
], ids=["network", "timeout", "http-503", "rate-limit"])
def test_r4_a_night_of_passing_faults_is_not_a_failed_night(tagstore, tmp_path, fail):
    """A fault that clears up by itself says nothing about the article."""
    path = tagstore([_trow(1, tag_failures=ta.MAX_TAG_NIGHTS - 1)])

    def call(prompt, key, model):
        fail()
    assert _trun(path, tmp_path, call=call) == 1
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS - 1


@pytest.mark.parametrize("fail", [
    lambda: http(400, "context_length_exceeded").raise_for_status(),
    lambda: (_ for _ in ()).throw(RuntimeError("local bug")),
], ids=["http-400", "local-exception"])
def test_r4_a_fault_that_comes_back_every_night_still_counts(tagstore, tmp_path, fail):
    """Not counting it would retry it silently forever, with exit 1 only logged."""
    path = tagstore([_trow(1, tag_failures=ta.MAX_TAG_NIGHTS - 1)])

    def call(prompt, key, model):
        fail()
    assert _trun(path, tmp_path, call=call) == ta.GAVE_UP_RC


def test_r4_a_bad_answer_then_a_timeout_is_a_failed_night(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    seq = iter([json.dumps(TOO_MANY)])

    def call(prompt, key, model):
        raw = next(seq, None)
        if raw is None:
            raise requests.Timeout("slow")
        return raw, dict(USAGE), model
    assert _trun(path, tmp_path, call=call) == 1
    assert _tread(path)["t1"]["tag_failures"] == 1


def test_r5_a_store_that_breaks_mid_run_stops_the_paid_calls(tagstore, tmp_path, monkeypatch):
    import threading
    import time
    path = tagstore([_trow(i) for i in range(8)])
    calls = []
    broke = threading.Event()

    def call(prompt, key, model):
        calls.append(1)
        if len(calls) > 1:              # in flight when the write fails, as on a real run
            broke.wait(5)
            time.sleep(0.2)
        return json.dumps(TAG_GOOD), dict(USAGE), model

    def broken(*a, **k):
        broke.set()
        raise RuntimeError("1 damaged row(s) in the store; stopping without writing")
    monkeypatch.setattr(ta, "SAVE_EVERY", 1)
    monkeypatch.setattr(ta, "flush", broken)
    with pytest.raises(RuntimeError):
        _trun(path, tmp_path, workers=1, call=call)
    assert len(calls) <= 2                       # the one saved, at most one already in flight


WORDING = dict(SUMMARY, summary_en="The provided text says the Federal Reserve kept interest rates unchanged.")


def test_r6_a_reask_that_errors_keeps_the_rejection_from_the_weaker_tier(monkeypatch, tmp_path):
    monkeypatch.setattr(aa, "USAGE_LOG_FILE", tmp_path / "usage.jsonl")
    models = []

    def post(url, headers=None, json=None, timeout=None):
        models.append(json["model"])
        if len(models) == 1:
            return ok(__import__("json").dumps(WORDING))
        raise requests.Timeout("slow")
    monkeypatch.setattr(aa.requests, "post", post)
    r = aa._analyze_with_fallback(LATIN, {"OPENAI_API_KEY": "k"}, article_id="x")
    assert r is not None and r["insufficient_content"] is True
    assert set(models) == {aa.MODEL_CHAIN[0]}


def test_r7_a_consecutive_stop_after_some_answers_does_not_say_none_answered(store, monkeypatch, caplog):
    store([_art(i) for i in range(5)], bodies={f"a{i}": _distinct(i) for i in range(5)})
    answers = iter([dict(SUMMARY, _model="m", _usage={})])
    monkeypatch.setattr(aa, "_analyze_with_fallback", lambda *a, **k: next(answers, None))
    with caplog.at_level("ERROR"):
        assert aa.main() == 1
    assert "none answered" not in caplog.text
    assert "1 answered" in caplog.text


@pytest.mark.parametrize("text,passage", [
    ("Credit spreads widened as the ﬁnancial conditions index tightened sharply over the quarter.",
     "the financial conditions index tightened sharply over the quarter"),
    ("Credit spreads widened as the financial conditions index tightened sharply over the quarter.",
     "the ﬁnancial conditions index tightened sharply over the quarter"),
    ("美联储维持利率不变，市场对降息的预期明显回落，美国国债收益率随之上行。",
     "市场对降息的预期明显回落，美国国债收益率随之上行"),
], ids=["ligature-in-text", "ligature-in-passage", "chinese"])
def test_r10_a_correct_quote_is_kept(text, passage):
    answer = dict(TAG_GOOD, assets=["govt_bonds"], topics=[], evidence={"govt_bonds": passage})
    kept, dropped = taxonomy.check_evidence(answer, text)
    assert kept["assets"] == ["govt_bonds"], dropped


def test_r10_a_two_character_chinese_quote_is_still_too_short():
    answer = dict(TAG_GOOD, assets=["govt_bonds"], topics=[], evidence={"govt_bonds": "国债"})
    kept, dropped = taxonomy.check_evidence(answer, "美联储维持利率不变，美国国债收益率上行。")
    assert kept["assets"] == [] and dropped == ["govt_bonds: passage too short"]


def test_r10_a_misquoted_passage_is_asked_for_once_more(tagstore, tmp_path):
    """A tag whose passage is not in the text gets one re-quote request, not a silent drop."""
    path = tagstore([_trow(1)])
    wrong = dict(TAG_GOOD, evidence=dict(TAG_GOOD["evidence"],
                                         ai_tech="Hyperscalers doubled their spending on AI chips this year"))
    prompts = []

    def call(prompt, key, model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps(wrong), dict(USAGE), model
        return json.dumps({"evidence": {"ai_tech": TAG_GOOD["evidence"]["ai_tech"]}}), dict(USAGE), model
    logged = []
    assert _trun(path, tmp_path, logged, call=call) == 0
    assert _tread(path)["t1"]["tags"] == taxonomy.flatten(TAG_GOOD)
    assert "ai_tech" in prompts[1] and len(prompts) == 2
    assert [p for _, p in logged] == [True, True]


def test_r10_a_requote_that_is_still_wrong_drops_the_tag(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    wrong = dict(TAG_GOOD, evidence=dict(TAG_GOOD["evidence"],
                                         ai_tech="Hyperscalers doubled their spending on AI chips this year"))
    replies = iter([json.dumps(wrong), json.dumps({"evidence": {"ai_tech": "still not a quote from this text at all"}})])
    assert _trun(path, tmp_path, call=lambda p, k, model: (next(replies), dict(USAGE), model)) == 0
    assert "ai_tech" not in _tread(path)["t1"]["tags"]


def _misquote_then(second):
    wrong = dict(TAG_GOOD, evidence=dict(TAG_GOOD["evidence"],
                                         ai_tech="Hyperscalers doubled their spending on AI chips this year"))
    n = []

    def call(prompt, key, model):
        n.append(1)
        if len(n) == 1:
            return json.dumps(wrong), dict(USAGE), model
        return second()
    return call


def test_r10_a_quota_error_on_the_requote_still_stops_the_run(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    assert _trun(path, tmp_path, call=_misquote_then(_quota)) == 2


def test_r10_a_refused_requote_is_billed_and_drops_the_tag(tagstore, tmp_path):
    path = tagstore([_trow(1)])

    def refuse():
        exc = aa.EmptyAnswer("no content")
        exc.usage, exc.model = dict(USAGE), "m"
        raise exc
    logged = []
    assert _trun(path, tmp_path, logged, call=_misquote_then(refuse)) == 0
    assert "ai_tech" not in _tread(path)["t1"]["tags"]
    assert [p for _, p in logged] == [True, False]


# ---- Third review (2026-10-09) ----------------------------------------------

def test_t3_a_local_request_error_counts_every_night(tagstore, tmp_path):
    """InvalidHeader (a key with a newline in it) is a RequestException that no
    retry can clear; labelled transient, it was retried silently forever."""
    path = tagstore([_trow(1, tag_failures=ta.MAX_TAG_NIGHTS - 1)])

    def call(prompt, key, model):
        raise requests.exceptions.InvalidHeader("Invalid leading whitespace in header value")
    assert _trun(path, tmp_path, call=call) == ta.GAVE_UP_RC


@pytest.mark.parametrize("later", [
    lambda: (_ for _ in ()).throw(requests.Timeout("slow")),
    lambda: http(503, "server_error").raise_for_status(),
], ids=["timeouts", "http-503"])
def test_t3_a_lasting_fault_is_not_washed_out_by_later_passing_ones(tagstore, tmp_path, later):
    path = tagstore([_trow(1)])
    faults = iter([lambda: http(400, "context_length_exceeded").raise_for_status()])

    def call(prompt, key, model):
        next(faults, later)()
    assert _trun(path, tmp_path, call=call) == 1
    assert _tread(path)["t1"]["tag_failures"] == 1


def test_t3_a_request_timeout_from_the_server_is_passing(tagstore, tmp_path):
    path = tagstore([_trow(1, tag_failures=ta.MAX_TAG_NIGHTS - 1)])

    def call(prompt, key, model):
        http(408, "timeout").raise_for_status()
    assert _trun(path, tmp_path, call=call) == 1
    assert _tread(path)["t1"]["tag_failures"] == ta.MAX_TAG_NIGHTS - 1


def test_t4_calls_in_flight_when_the_store_breaks_are_still_booked(tagstore, tmp_path, monkeypatch):
    import threading
    import time
    path = tagstore([_trow(i) for i in range(6)])
    calls = []
    broke = threading.Event()

    def call(prompt, key, model):
        calls.append(1)
        if len(calls) > 1:
            broke.wait(5)
            time.sleep(0.2)
        return json.dumps(TAG_GOOD), dict(USAGE), model

    def broken(*a, **k):
        broke.set()
        raise RuntimeError("1 damaged row(s) in the store; stopping without writing")
    monkeypatch.setattr(ta, "SAVE_EVERY", 1)
    monkeypatch.setattr(ta, "flush", broken)
    logged = []
    with pytest.raises(RuntimeError):
        _trun(path, tmp_path, logged, workers=3, call=call)
    assert len(logged) == len(calls)


def test_t5_a_quota_stop_on_the_requote_keeps_the_good_tags(tagstore, tmp_path):
    """The first answer was paid for and its other tags were fine."""
    path = tagstore([_trow(1)])
    logged = []
    assert _trun(path, tmp_path, logged, call=_misquote_then(_quota)) == 2
    tags = _tread(path)["t1"]["tags"]
    assert "equities" in tags and "ai_tech" not in tags
    assert [p for _, p in logged] == [True]


def test_t6_a_requoted_passage_already_cited_for_another_tag_does_not_rescue(tagstore, tmp_path):
    """Any sentence of the text would pass "is it in the document"; the one
    already quoted for another tag is the cheapest such rescue."""
    path = tagstore([_trow(1)])
    reused = {"evidence": {"ai_tech": TAG_GOOD["evidence"]["equities"]}}
    assert _trun(path, tmp_path, call=_misquote_then(lambda: (json.dumps(reused), dict(USAGE), "m"))) == 0
    assert "ai_tech" not in _tread(path)["t1"]["tags"]


def test_t6_the_requote_note_lets_the_model_withdraw_the_tag():
    assert "give null if there is none" in ta.REQUOTE_NOTE


def test_t9_a_requote_reply_cannot_change_the_evidence_of_other_tags(tagstore, tmp_path):
    path = tagstore([_trow(1)])
    reply = {"evidence": {"ai_tech": TAG_GOOD["evidence"]["ai_tech"],
                          "equities": "a passage that is nowhere in the document at all"}}
    path_report = tmp_path / "bk" / "report.jsonl"
    assert _trun(path, tmp_path, call=_misquote_then(lambda: (json.dumps(reply), dict(USAGE), "m"))) == 0
    assert _tread(path)["t1"]["tags"] == taxonomy.flatten(TAG_GOOD)
    ev = json.loads(path_report.read_text().splitlines()[-1])["evidence"]
    assert ev["equities"] == TAG_GOOD["evidence"]["equities"]
