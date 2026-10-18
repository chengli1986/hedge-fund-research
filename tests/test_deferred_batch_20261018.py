"""Deferred items closed on 2026-10-18 (health mail wording, Robeco label,
stage-6 #6/#9/#11/C and the 15 minors). One section per item."""
import importlib.util
import json
import sys
from collections import Counter
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


gfh = _load("gfh_deferred", "scripts/gmia-fetcher-health.py")


# --- health mail: window headline vs store-wide tables ------------------------
def test_store_wide_tables_say_they_are_not_the_7_day_window():
    q = {"days": 7, "alerts": [],
         "intake": {"total": 96, "with_body": 95, "metadata_only": 0, "without_body": 1, "declined": 2},
         "content": Counter({("gsam", "body_rendered_client_side"): 7}),
         "declines": Counter({("ark-invest", "title_only"): 29, ("gmo", "wrong_document"): 3})}
    body = gfh._quality_section(q)
    assert "AI 拒绝摘要 <b>2</b>" in body                       # the window figure stays
    assert "全库 AI 拒绝摘要（按原因，共 32 篇，含历史，不限最近 7 天）" in body
    assert "全库目前未取得正文（按原因，共 7 篇" in body


# --- Robeco podcast page: "only a brief article introduction" -----------------
def test_an_article_introduction_is_a_teaser_not_other():
    import failure_labels as fl
    reason = ("The text is only a brief article introduction describing the upcoming US "
              "midterm elections ...; it contains no substantive analysis or conclusions.")
    assert fl.classify_analysis_decline(reason) == "teaser_only"
    # an introduction next to a disclaimer is still a disclaimer (earlier rule)
    assert fl.classify_analysis_decline(
        "a brief series introduction and legal disclaimer") == "disclaimer_only"


# --- stage 6 #6: the verdict on day 7, not day 8 ------------------------------
def test_the_trial_is_decided_on_day_7_after_seven_checks():
    tm = _load("tm_deferred", "gmia-trial-manager.py")
    assert tm.TRIAL_DAYS == 7
    assert not tm.trial_period_complete(5)          # day 6
    assert tm.trial_period_complete(6)              # day 7: day 1 ran at start
    src = (ROOT / "gmia-trial-manager.py").read_text()
    assert "if trial_period_complete(elapsed) and not active.get(\"auto_decided\")" in src
    # the day's quality sample comes before the verdict in the same pass
    assert src.index("if trial_day in SAMPLE_DAYS:") < src.index("if trial_period_complete(elapsed)")


# --- stage 6 C: the synthesis agent has its own clock --------------------------
def test_synthesis_agent_and_preflight_are_bounded_inside_cron_wrapper():
    import re
    w = (ROOT / "scripts" / "wrapper-fetcher-synthesis.sh").read_text()
    agent = int(re.search(r"^AGENT_TIMEOUT_S=(\d+)$", w, re.M).group(1))
    assert re.search(r'\| timeout --kill-after=30 "\$AGENT_TIMEOUT_S" "\$CLAUDE_BIN" --print', w)
    assert re.search(r'\| timeout --kill-after=10 120 "\$CLAUDE_BIN" --print --max-turns 1', w)
    # cron-wrapper --timeout 1200 must still leave room for backfill/heartbeat/mail
    assert 120 + 10 + agent + 30 <= 1200 - 120
    # the agent call comes before the steps that must survive it
    assert w.index('"$AGENT_TIMEOUT_S"') < w.index("backfill_failed_synthesis.py") \
        < w.index("write_session_heartbeat.py", w.index("backfill_failed_synthesis.py"))
