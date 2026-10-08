"""The reclassification run must change tags and nothing else, and stop when the account can't pay."""
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

BODY = ("Equity markets rallied as investors rotated into large technology names this quarter. "
        "Spending on artificial intelligence data centres by the hyperscalers kept accelerating through June. ") * 5
GOOD = {"article_type": "research", "regions": ["us"], "assets": ["equities"], "topics": ["ai_tech"], "methods": [],
        "evidence": {"equities": "Equity markets rallied as investors rotated into large technology names",
                     "ai_tech": "Spending on artificial intelligence data centres by the hyperscalers kept accelerating"}}


def _row(i, **kw):
    r = {"id": f"id{i}", "source_id": "src", "title": f"T{i}", "date": "2026-10-01", "summarized": True,
         "summary_en": f"en {i}", "summary_zh": f"中 {i}", "themes": ["Macro/Rates"],
         "content_status": "ok", "content_path": f"content/id{i}.txt"}
    r.update(kw)
    return r


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "CONTENT_DIR", tmp_path / "content")
    (tmp_path / "content").mkdir()
    path = tmp_path / "articles.jsonl"

    def make(rows):
        for r in rows:
            (tmp_path / "content" / f"{r['id']}.txt").write_text(BODY, encoding="utf-8")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        return path
    return make


def _read(path):
    return {r["id"]: r for r in (json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l)}


def _http_error(status, code):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps({"error": {"code": code, "type": code}}).encode()
    return requests.HTTPError(f"{status}", response=resp)


def _caller(answers):
    """Fake _call_openai: each answer is a dict (returned as JSON), a str, or an exception."""
    calls = []

    def call(prompt, key, model):
        calls.append(prompt)
        a = answers[min(len(calls), len(answers)) - 1] if answers else GOOD
        if isinstance(a, Exception):
            raise a
        return (a if isinstance(a, str) else json.dumps(a)), {"prompt_tokens": 100, "completion_tokens": 10}, model
    call.calls = calls
    return call


def _run(path, tmp_path, call, **kw):
    return ta.run(path, "k", tmp_path / "bk", call=call, sleep=lambda s: None,
                  log_usage=lambda *a, **k: None, **kw)


def test_only_the_tag_fields_change(store, tmp_path):
    path = store([_row(1), _row(2)])
    before = _read(path)
    assert _run(path, tmp_path, _caller([GOOD])) == 0
    after = _read(path)
    for i, row in after.items():
        assert row["tags"] == ["research", "us", "equities", "ai_tech"]
        assert row["tags_model"] == ta.MODEL and row["tags_at"]
        assert {k: v for k, v in row.items() if k not in ta.TAG_FIELDS} == before[i]


def test_an_invalid_answer_is_retried_then_left_untagged(store, tmp_path):
    path = store([_row(1)])
    bad = dict(GOOD, regions=["global", "us"])
    call = _caller([bad])
    assert _run(path, tmp_path, call) == 1
    assert len(call.calls) == ta.ATTEMPTS
    assert "tags" not in _read(path)["id1"]


def test_a_near_miss_tag_id_is_not_guessed(store, tmp_path):
    path = store([_row(1)])
    call = _caller([dict(GOOD, topics=["AI"]), "Sure! {not json", GOOD])
    assert _run(path, tmp_path, call) == 0
    assert len(call.calls) == 3
    assert _read(path)["id1"]["tags"] == taxonomy.flatten(GOOD)


def test_running_out_of_credit_stops_the_run_and_keeps_what_was_done(store, tmp_path):
    path = store([_row(i) for i in range(6)])
    call = _caller([GOOD, GOOD, _http_error(429, "credit_balance_exhausted")])
    assert _run(path, tmp_path, call, workers=1) == 2
    assert len(call.calls) == 3          # no call after the fatal one
    after = _read(path)
    assert sum("tags" in r for r in after.values()) == 2


def test_a_plain_rate_limit_is_retried_not_fatal(store, tmp_path):
    path = store([_row(1)])
    call = _caller([_http_error(429, "rate_limit_exceeded"), GOOD])
    assert _run(path, tmp_path, call) == 0
    assert "tags" in _read(path)["id1"]


def test_a_second_run_skips_what_is_already_tagged(store, tmp_path):
    path = store([_row(1, tags=["research", "us"]), _row(2)])
    call = _caller([GOOD])
    assert _run(path, tmp_path, call) == 0
    assert len(call.calls) == 1
    assert _read(path)["id1"]["tags"] == ["research", "us"]


def test_writes_made_by_another_job_meanwhile_are_kept(store, tmp_path):
    path = store([_row(1), _row(2)])

    def call(prompt, key, model):
        rows = _read(path)
        rows["id2"]["summary_en"] = "rewritten by the nightly run"
        rows["new"] = _row(9, id="new")
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()), encoding="utf-8")
        return json.dumps(GOOD), {}, model
    assert _run(path, tmp_path, call, workers=1) == 0
    after = _read(path)
    assert after["id2"]["summary_en"] == "rewritten by the nightly run"
    assert "new" in after
    assert after["id1"]["tags"] and after["id2"]["tags"]


def test_an_empty_content_path_is_filled_and_a_set_one_is_left_alone(store, tmp_path):
    bare = _row(1)
    del bare["content_path"]           # the real 10 rows lack the key (not null)
    path = store([bare, _row(2, content_path="content/elsewhere.txt")])
    (tmp_path / "content" / "elsewhere.txt").write_text(BODY, encoding="utf-8")
    assert _run(path, tmp_path, _caller([GOOD])) == 0
    after = _read(path)
    assert after["id1"]["content_path"] == "content/id1.txt"
    assert after["id2"]["content_path"] == "content/elsewhere.txt"


def test_the_store_is_backed_up_before_the_first_write(store, tmp_path):
    path = store([_row(1)])
    original = path.read_bytes()
    _run(path, tmp_path, _caller([GOOD]))
    assert (tmp_path / "bk" / "articles.jsonl.before").read_bytes() == original


def test_a_real_run_needs_a_backup_directory(store, tmp_path):
    path = store([_row(1)])
    with pytest.raises(SystemExit):
        ta.run(path, "k", None, call=_caller([GOOD]), sleep=lambda s: None, log_usage=lambda *a, **k: None)
    assert "tags" not in _read(path)["id1"]


def test_retag_redoes_older_tags_and_resumes_without_redoing_new_ones(store, tmp_path):
    path = store([_row(1, tags=["research", "us"], tags_at="2025-12-01T20:00:00+08:00"),
                  _row(2, tags=["research", "us"], tags_at="2026-03-01T13:00:00+08:00"),
                  _row(3)])
    call = _caller([GOOD])
    assert _run(path, tmp_path, call, retag_before="2026-01-01T00:00:00+08:00") == 0
    after = _read(path)
    assert len(call.calls) == 2                                   # id1 (old) and id3 (untagged)
    assert after["id1"]["tags"] == taxonomy.flatten(GOOD)
    assert after["id2"]["tags"] == ["research", "us"]             # already re-done: kept
    again = _caller([GOOD])
    assert _run(path, tmp_path, again, retag_before="2026-01-01T00:00:00+08:00") == 0
    assert again.calls == []                                      # resume: nothing left


def test_without_retag_older_tags_are_left_alone(store, tmp_path):
    path = store([_row(1, tags=["research", "us"], tags_at="2025-12-01T20:00:00+08:00")])
    call = _caller([GOOD])
    assert _run(path, tmp_path, call) == 0
    assert call.calls == []


def test_a_tag_whose_passage_is_not_in_the_article_is_dropped(store, tmp_path):
    path = store([_row(1)])
    made_up = dict(GOOD, assets=["equities", "govt_bonds"],
                   evidence=dict(GOOD["evidence"], govt_bonds="Treasury yields fell sharply as investors bought duration"))
    assert _run(path, tmp_path, _caller([made_up])) == 0
    assert _read(path)["id1"]["tags"] == ["research", "us", "equities", "ai_tech"]
    report = [json.loads(l) for l in (tmp_path / "bk" / "report.jsonl").read_text().splitlines()]
    assert "govt_bonds: passage not in document" in report[0]["outcome"]
    assert set(report[0]["evidence"]) == {"equities", "ai_tech"}
