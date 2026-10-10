"""The re-summarising tool must never cost an article its summary."""
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "resummarize_long_articles.py"
spec = importlib.util.spec_from_file_location("resum", SCRIPT)
rs = importlib.util.module_from_spec(spec)
sys.modules["resum"] = rs
spec.loader.exec_module(rs)

OLD = {"summary_en": "old en", "summary_zh": "旧", "key_takeaway_en": "old k", "key_takeaway_zh": "旧要点",
       "themes": ["Macro/Rates"], "analysis_model": "gemini-2.5-pro"}
NEW = {"summary_en": "new en", "summary_zh": "新", "key_takeaway_en": "new k", "key_takeaway_zh": "新要点",
       "themes": ["Credit/Fixed Income"], "_model": "gpt-5.6-luna"}


def test_a_whole_new_summary_replaces_the_old_one():
    row = dict(OLD)
    assert rs.apply_result(row, dict(NEW)) == "replaced"
    assert row["summary_en"] == "new en" and row["analysis_model"] == "gpt-5.6-luna"
    assert row.get("analysis_resummarized_at")


def test_a_decline_keeps_the_old_summary():
    row = dict(OLD)
    out = rs.apply_result(row, {"insufficient_content": True, "reason": "failed grounding check: x"})
    assert out.startswith("kept") and row == OLD


def test_no_answer_keeps_the_old_summary():
    row = dict(OLD)
    assert rs.apply_result(row, None).startswith("kept") and row == OLD


def test_a_partial_answer_keeps_the_old_summary():
    row = dict(OLD)
    assert rs.apply_result(row, dict(NEW, summary_zh="")).startswith("kept") and row == OLD
    assert rs.apply_result(row, dict(NEW, themes=[])).startswith("kept") and row == OLD


# ---- Third review (2026-10-09) ----------------------------------------------

import json  # noqa: E402

import pytest  # noqa: E402

import analyze_articles as aa  # noqa: E402

LONG = "Credit markets " * 1200          # longer than OLD_LIMIT


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / "content").mkdir()
    rows = [dict(OLD, id=f"r{i}", source_id="s", title=f"T{i}", summarized=True, content_status="ok")
            for i in range(3)]
    for r in rows:
        (tmp_path / "content" / f"{r['id']}.txt").write_text(LONG, encoding="utf-8")
    path = tmp_path / "articles.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setattr(aa, "DATA_FILE", path)
    monkeypatch.setattr(aa, "BASE_DIR", tmp_path)
    monkeypatch.setattr(aa, "CONTENT_DIR", tmp_path / "content")
    monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
    return path


def _rows(path):
    return {r["id"]: r for r in (json.loads(l) for l in path.read_text().splitlines() if l)}


def test_an_interrupted_run_keeps_the_summaries_already_paid_for(store, tmp_path, monkeypatch):
    """Ctrl-C after the first answer: the report said "replaced" but the store kept the old one."""
    answers = iter([dict(NEW)])

    def analyze(*a, **k):
        r = next(answers, None)
        if r is None:
            raise KeyboardInterrupt
        return r
    monkeypatch.setattr(aa, "_analyze_with_fallback", analyze)
    with pytest.raises(KeyboardInterrupt):
        rs.main(["--backup", str(tmp_path / "bk")])
    assert _rows(store)["r0"]["summary_en"] == "new en"


def test_a_row_hidden_meanwhile_does_not_get_the_new_summary(store, tmp_path, monkeypatch):
    """The nightly run marked it duplicate_body while this tool was asking the model."""
    def analyze(*a, **k):
        rows = _rows(store)
        rows["r0"].update(summarized=False, analysis_label="duplicate_body")
        for f in rs.SUMMARY_FIELDS[:4]:
            rows["r0"].pop(f, None)
        rows["r0"]["themes"] = []
        store.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()))
        return dict(NEW)
    monkeypatch.setattr(rs, "candidates", lambda rows: [r for r in rows if r["id"] == "r0"])
    monkeypatch.setattr(aa, "_analyze_with_fallback", analyze)
    rs.main(["--backup", str(tmp_path / "bk")])
    r0 = _rows(store)["r0"]
    assert r0["summarized"] is False and "summary_en" not in r0 and r0["themes"] == []


def test_a_row_hidden_meanwhile_is_not_counted_as_replaced(store, tmp_path, monkeypatch, capsys):
    """Stage-3 health check Fd: flush skipped it, the report and the total still said replaced."""
    def analyze(*a, **k):
        rows = _rows(store)
        rows["r0"].update(summarized=False, analysis_label="duplicate_body")
        store.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows.values()))
        return dict(NEW)
    monkeypatch.setattr(rs, "candidates", lambda rows: [r for r in rows if r["id"] == "r0"])
    monkeypatch.setattr(aa, "_analyze_with_fallback", analyze)
    rs.main(["--backup", str(tmp_path / "bk")])
    assert "0 replaced" in capsys.readouterr().out
    report = [json.loads(l) for l in (tmp_path / "bk" / "report.jsonl").read_text().splitlines()]
    assert report[-1]["id"] == "r0" and report[-1]["outcome"] != "replaced"
