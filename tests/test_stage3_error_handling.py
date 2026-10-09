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
    store([_art(i) for i in range(3)], bodies={f"a{i}": LATIN + f" Note {i}." * 3 for i in range(3)})
    api["script"] = [resp]
    assert aa.main() == 2
    assert len(api["calls"]) == 1
    assert not any(r.get("summarized") or r.get("analysis_status") for r in _rows().values())


def test_s2_quota_running_out_mid_run_keeps_what_was_done_and_alerts(store, api):
    # distinct bodies: identical ones would be declined as duplicates, with no call
    store([_art(i) for i in range(3)], bodies={f"a{i}": LATIN + f" Note {i}." * 3 for i in range(3)})
    api["script"] = [ok(json.dumps(SUMMARY)), http(429, "insufficient_quota")]
    assert aa.main() == 2
    assert [r.get("summarized", False) for r in _rows().values()] == [True, False, False]


def test_s2_a_plain_rate_limit_is_retried_not_fatal(store, api):
    store([_art(1)])
    api["script"] = [http(429, "rate_limit_exceeded"), ok(json.dumps(SUMMARY))]
    assert aa.main() == 0
    assert _rows()["a1"]["summarized"] is True


def test_s2_consecutive_unanswered_articles_stop_the_stage(store, monkeypatch):
    store([_art(i) for i in range(6)], bodies={f"a{i}": LATIN + f" Note {i}." * 3 for i in range(6)})
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
    bodies = {f"a{i}": LATIN + f" Note {i}." * 3 for i in range(4)}
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
