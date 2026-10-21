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


# --- minor: synthesis commits what its own post-agent steps changed -----------
def test_synthesis_commits_the_backfill_marks(tmp_path):
    import subprocess
    import test_wrapper_synthesis_preflight as wsp
    (tmp_path / "s").mkdir()
    sandbox = wsp._sandbox(tmp_path / "s")
    remote = tmp_path / "remote.git"
    git = lambda *a, cwd=sandbox: subprocess.run(["git", *a], cwd=cwd, check=True,
                                                 capture_output=True, text=True).stdout
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    git("remote", "add", "origin", str(remote))
    git("add", "config/fund_candidates.json"); git("commit", "-qm", "seed")
    git("push", "-q", "-u", "origin", "main")
    wsp._run(sandbox, preflight_fail=False)               # agent skips -> backfill marks it
    assert "synthesis: record session outcomes" in git("log", "-1", "--format=%s")
    assert git("status", "--porcelain", "config/fund_candidates.json") == ""
    assert git("log", "-1", "--format=%s", "main", cwd=remote).startswith("synthesis: record")


# --- minor: candidate/trial files are written atomically ----------------------
def test_write_json_atomic_replaces_whole_and_keeps_mode(tmp_path, monkeypatch):
    import os
    import status_util as su
    p = tmp_path / "c.json"
    p.write_text("[]")
    p.chmod(0o640)
    su.write_json_atomic(p, [{"id": "a"}])
    assert json.loads(p.read_text()) == [{"id": "a"}] and (p.stat().st_mode & 0o777) == 0o640

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(su.json, "dump", boom)
    with pytest.raises(OSError):
        su.write_json_atomic(p, [{"id": "b"}])
    assert json.loads(p.read_text()) == [{"id": "a"}]       # old file intact
    assert [f.name for f in tmp_path.iterdir()] == ["c.json"]  # no temp left


WRITERS = ["scripts/backfill_failed_synthesis.py", "synthesize_fetchers.py"]


@pytest.mark.parametrize("rel", WRITERS)
def test_candidate_writers_go_through_the_atomic_helper(rel):
    src = (ROOT / rel).read_text()
    assert "write_json_atomic(" in src
    assert ".write_text(json.dumps" not in src


# --- minor: the promotion probe checks the gate on measured numbers -----------
def _probe_with(monkeypatch, stdout, rc=0):
    import subprocess as sp
    import verify_promotions as vp
    monkeypatch.setattr(vp.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a[0], rc, stdout=stdout, stderr=""))
    return vp.check_probe({"id": "new-src"})


def test_promotion_probe_fails_below_the_gate_the_message_claims(monkeypatch):
    assert _probe_with(monkeypatch, "  new-src                   1 articles · 150 chars · 9ms\n")[0] == "fail"
    assert _probe_with(monkeypatch, "  new-src                   10 articles · 900 chars · 9ms\n")[0] == "ok"
    assert _probe_with(monkeypatch, "nothing parseable\n")[0] == "error"
    assert _probe_with(monkeypatch, "", rc=1)[0] == "fail"


def test_validator_and_probe_share_one_gate():
    import promote_today as pt
    src = (ROOT / "scripts" / "validate_promote_commit_msg.py").read_text()
    assert "from promote_today import MIN_ARTICLES, MIN_CONTENT_CHARS" in src
    assert "MIN_ARTICLES = " not in src
    assert (pt.MIN_ARTICLES, pt.MIN_CONTENT_CHARS) == (3, 500)


# --- minor: auto-reject with notes stored as null -----------------------------
def test_auto_reject_survives_null_notes(tmp_path):
    import synthesize_fetchers as sf
    cands = tmp_path / "c.json"
    cands.write_text(json.dumps([{"id": "x", "status": "inaccessible", "notes": None}]))
    hist = tmp_path / "h.jsonl"
    hist.write_text("".join(json.dumps({"id": "x", "outcome": "failed"}) + "\n" for _ in range(3)))
    assert sf.auto_reject_exhausted_candidates(cands, hist, max_failures=3) == ["x"]
    c = json.loads(cands.read_text())[0]
    assert c["status"] == "rejected" and c["notes"].startswith("Auto-rejected ")


# --- minor: a promotion reverted twice in a week is parked --------------------
def _promote_repo(tmp_path, monkeypatch, prior_reverts):
    import subprocess
    import verify_promotions as vp
    monkeypatch.chdir(tmp_path)
    git = lambda *a: subprocess.run(["git", *a], check=True, capture_output=True, text=True).stdout
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "config").mkdir(); (tmp_path / "logs").mkdir()
    (tmp_path / "config" / "fund_candidates.json").write_text(json.dumps([{"id": "x", "status": "promoted"}]))
    (tmp_path / "config" / "sources.json").write_text('{"sources": []}')
    git("add", "-A"); git("commit", "-qm", "base")
    (tmp_path / "config" / "sources.json").write_text('{"sources": [{"id": "x"}]}')
    git("commit", "-qam", "promote x")
    sha = git("rev-parse", "HEAD").strip()
    today = vp.today_bjt()
    (tmp_path / "logs" / "auto-promote-history.jsonl").write_text("".join(
        json.dumps({"date": today, "id": "x", "outcome": "auto_reverted"}) + "\n"
        for _ in range(prior_reverts)))
    _, problem = vp.revert({"id": "x", "date": today, "commit": sha}, "auto_reverted", "probe FAIL", "probe")
    c = json.loads((tmp_path / "config" / "fund_candidates.json").read_text())[0]
    return c, git("log", "-1", "--format=%s"), problem


def test_first_revert_keeps_the_retry(tmp_path, monkeypatch):
    c, last, _ = _promote_repo(tmp_path, monkeypatch, prior_reverts=0)
    assert c["status"] == "promoted" and last.startswith("Revert")


def test_second_revert_in_a_week_parks_it(tmp_path, monkeypatch):
    c, last, problem = _promote_repo(tmp_path, monkeypatch, prior_reverts=1)
    assert c["status"] == "watchlist" and "set back to promoted to retry" in c["notes"]
    assert last.startswith("auto-promote: park x")
    assert "push failed" in (problem or "")          # no remote here; still reported


# --- discovery wrapper: #11 timeouts, timestamps, budget, trigger, report ----
DISC = (ROOT / "scripts" / "wrapper-candidate-discovery.sh").read_text()


def test_every_discovery_step_fits_inside_the_job_timeout():
    import re
    v = {k: int(re.search(rf"^{k}=(\d+)", DISC, re.M).group(1))
         for k in ("JOB_TIMEOUT_S", "PHASE1_TIMEOUT_S", "POST_RESERVE_S", "AGENT_MAX_S")}
    assert v["JOB_TIMEOUT_S"] == 960
    # the agent gets what is left (<= AGENT_MAX_S, >= 60): even a Phase 1 that
    # used its whole timeout leaves the post-agent steps their reserve
    assert v["PHASE1_TIMEOUT_S"] + 10 + 60 + 30 + v["POST_RESERVE_S"] <= v["JOB_TIMEOUT_S"]
    assert v["AGENT_MAX_S"] <= v["JOB_TIMEOUT_S"] - v["POST_RESERVE_S"] - 30
    assert 'timeout --kill-after=30 "$AGENT_TIMEOUT_S" "$CLAUDE_BIN"' in DISC
    assert "AGENT_TIMEOUT_S=$(( JOB_TIMEOUT_S - SECONDS - POST_RESERVE_S - 30 ))" in DISC


def test_each_log_line_gets_its_own_timestamp():
    assert "$LOG_PREFIX" not in DISC and "ts() {" in DISC
    synth = (ROOT / "scripts" / "wrapper-fetcher-synthesis.sh").read_text()
    assert "$LOG_PREFIX" not in synth and "ts() {" in synth


def test_one_budget_for_the_agent():
    assert "max 5 WebSearch" not in DISC and "max 10 WebFetch" not in DISC
    program = (ROOT / "candidate-discovery" / "program.md").read_text()
    assert "Maximum 3 WebSearch" in program and "Maximum 8 WebFetch" in program


def test_the_trigger_counts_with_synthesis_own_rule_and_fails_closed():
    assert "sf.list_priority_targets()" in DISC and "from fetch_articles import FETCHERS" not in DISC
    assert "could not count synthesis_priority targets, not triggering" in DISC


def test_list_priority_targets_skips_fetchers_templates_and_recent_attempts():
    import synthesize_fetchers as sf
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cands = [{"id": "a", "synthesis_priority": True},
             {"id": "b", "synthesis_priority": True},                       # has a fetcher
             {"id": "c", "synthesis_priority": True, "listing_template": {"type": "x"}},
             {"id": "d", "synthesis_priority": True, "synthesis_attempted_at": now.isoformat()},
             {"id": "e"}]
    got = sf.list_priority_targets(cands, {"b"}, now - timedelta(days=7))
    assert [t["id"] for t in got] == ["a"]


def test_a_triggered_synthesis_goes_through_cron_wrapper():
    assert ('nohup "$CRON_WRAPPER" --name gmia-fetcher-synthesis --timeout 1200 --lock --' in DISC)


def _report(tmp_path, cands, trial_queue_ids):
    """Run the wrapper's report code against a temp repo, SMTP unset."""
    import os
    import re
    import subprocess
    code = re.search(r"python3 << 'PYEOF'\n(.*?)\nPYEOF", DISC, re.S).group(1)
    code = code.replace('    print("WARNING: SMTP not configured, report not sent")',
                        '    open(os.environ["OUT_HTML"], "w").write(html)\n'
                        '    print("WARNING: SMTP not configured, report not sent")')
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fund_candidates.json").write_text(json.dumps(cands))
    (tmp_path / "config" / "sources.json").write_text('{"sources": []}')
    (tmp_path / "gmia-trial-manager.py").write_text(
        f"def get_trial_queue(state):\n    import json\n"
        f"    return [c for c in json.load(open('{tmp_path}/config/fund_candidates.json'))"
        f" if c['id'] in {trial_queue_ids!r}]\n")
    for f in ("status_util.py", "detect_stalled_candidates.py"):
        (tmp_path / f).write_text((ROOT / f).read_text())
    script = tmp_path / "report.py"
    script.write_text(code)
    env = {k: v for k, v in os.environ.items() if k not in ("SMTP_USER", "SMTP_PASS")}
    env.update(REPO_DIR=str(tmp_path), EXIT_CODE="0", PUSH_FAILED="", OUT_HTML=str(tmp_path / "r.html"))
    p = subprocess.run([sys.executable, "-P", str(script)], env=env, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return (tmp_path / "r.html").read_text()


def test_the_report_escapes_candidate_text_and_counts_the_real_queue(tmp_path):
    cands = [{"id": "x", "name": "<b>X&Co</b>", "status": "visitable", "quality": "HIGH",
              "topics": "<i>t</i>", "notes": "<script>n</script>", "strategy_tags": ["<u>"]},
             {"id": "low", "name": "Low", "status": "visitable", "quality": "LOW"}]
    page = _report(tmp_path, cands, ["x"])
    assert "<b>X&Co</b>" not in page and "&lt;b&gt;X&amp;Co&lt;/b&gt;" in page
    assert "<script>" not in page and "<i>t</i>" not in page and "<u>" not in page
    assert "Queue 1</span>" in page          # LOW is visitable but never trialled
