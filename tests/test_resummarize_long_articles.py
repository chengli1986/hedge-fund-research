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
