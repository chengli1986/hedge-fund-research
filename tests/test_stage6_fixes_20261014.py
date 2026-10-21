"""Stage-6 audit fixes (2026-10-14): failures that read as success, and
candidates that get stuck.

  #4  the discovery job's exit was the report email's: on 09-13 (expired
      login) and 09-22 (API 500) the agent failed and cron recorded exit 0.
  #1  the trial manager returned before its daily summary whenever nobody
      was queued: one summary between 09-13 and 10-14.
  #3  auto-promote's two checks always exited 0, so a revert never failed the
      job; a failed revert was left half-done; a reverted row was probed and
      reverted again; a push failure skipped the history line.
  B   the synthesis agent chose its own two targets while the backfill charged
      the first two of synthesize_fetchers' list.
  #7  trial sampling ignored a candidate's listing_template; a templated
      candidate sent back to inaccessible was never a synthesis target again;
      a template that passed was charged a failure when the session stopped
      early.
  A   a candidate that failed its trial with 0 articles could never be trialled
      again, even after synthesis fixed it.
  #10 playbooks asked for what the code does not allow.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import listing_templates as lt
import synthesize_fetchers

REPO = Path(__file__).resolve().parent.parent
BJT = timezone(timedelta(hours=8))
TODAY = datetime.now(BJT).strftime("%Y-%m-%d")
SPEC = {"type": "card_list", "fetch": "requests", "card": "div.card", "link": "a"}


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


tm = _load("trial_manager_s6", "gmia-trial-manager.py")
bf = _load("backfill_s6", "scripts/backfill_failed_synthesis.py")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SMTP_", "ANTHROPIC_", "MAIL_"))}
    env.update(extra)
    return env


# ── #4 discovery: the job's exit is the work's, not the email's ─────────────

FAKE_DISCOVERY_CLAUDE = """#!/bin/bash
echo "fake agent"; exit ${AGENT_EXIT:-0}
"""
FAKE_TRIAL_MANAGER = """import os, sys
def get_trial_queue(state):          # the report imports this module (2026-10-18)
    return []
if __name__ == "__main__":
    print("fake trial manager"); sys.exit(int(os.environ.get("TRIAL_EXIT", "0")))
"""
FAKE_PHASE1 = "#!/bin/bash\necho fake phase 1\n"


def _discovery_sandbox(tmp_path):
    for d in ("scripts", "config", "candidate-discovery", "home"):
        (tmp_path / d).mkdir()
    shutil.copy2(REPO / "scripts/wrapper-candidate-discovery.sh", tmp_path / "scripts")
    for f in ("guard_candidate_status.py", "detect_stalled_candidates.py", "status_util.py"):
        shutil.copy2(REPO / f, tmp_path / f)
    (tmp_path / "gmia-trial-manager.py").write_text(FAKE_TRIAL_MANAGER)
    (tmp_path / "candidate-discovery" / "program.md").write_text("test playbook\n")
    (tmp_path / "config" / "fund_candidates.json").write_text("[]\n")
    (tmp_path / "config" / "sources.json").write_text('{"sources": []}\n')
    (tmp_path / "scripts" / "run-candidate-discovery.sh").write_text(FAKE_PHASE1)
    fake = tmp_path / "claude"
    fake.write_text(FAKE_DISCOVERY_CLAUDE)
    fake.chmod(0o755)
    # A real repo and remote: a failed push fails the job since 2026-10-18.
    remote = tmp_path.parent / f"{tmp_path.name}-remote.git"
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    git("remote", "add", "origin", str(remote))
    git("add", "-A"); git("commit", "-qm", "base"); git("push", "-q", "-u", "origin", "main")
    return tmp_path


def _run_discovery(sb, **env):
    return subprocess.run(
        ["bash", str(sb / "scripts" / "wrapper-candidate-discovery.sh")],
        env=_clean_env(HOME=str(sb / "home"), CLAUDE_BIN=str(sb / "claude"), **env),
        capture_output=True, text=True, timeout=120)


def test_a_failed_discovery_agent_fails_the_job(tmp_path):
    sb = _discovery_sandbox(tmp_path)
    out = _run_discovery(sb, AGENT_EXIT="1")
    assert out.returncode == 1, out.stdout[-2000:]
    assert "SMTP not configured" in out.stdout          # the report step still ran


def test_a_crashed_trial_manager_fails_the_job(tmp_path):
    out = _run_discovery(_discovery_sandbox(tmp_path), TRIAL_EXIT="2")
    assert out.returncode == 2, out.stdout[-2000:]


def test_a_clean_discovery_run_exits_zero(tmp_path):
    out = _run_discovery(_discovery_sandbox(tmp_path))
    assert out.returncode == 0, out.stdout[-2000:]


def test_a_failed_push_fails_an_otherwise_clean_run(tmp_path):
    sb = _discovery_sandbox(tmp_path)
    subprocess.run(["git", "remote", "set-url", "origin", str(tmp_path / "gone.git")],
                   cwd=sb, check=True)
    (sb / "config" / "fund_candidates.json").write_text('[{"id": "n", "name": "N", "status": "seed"}]\n')
    subprocess.run(["git", "commit", "-qam", "local only"], cwd=sb, check=True)
    out = _run_discovery(sb)
    assert out.returncode == 3, out.stdout[-2000:]
    assert "git push failed" in out.stdout


# ── #1 the daily summary is not skipped when nobody is queued ────────────────

def test_the_daily_summary_runs_when_the_queue_is_empty(monkeypatch):
    active = {"id": "acme", "name": "Acme", "start_date": TODAY,
              "daily_checks": {TODAY: {"accessible": True, "article_count": 5}},
              "quality_samples": [{"day": 1, "articles": []}]}
    state = {"active_trials": [active], "history": []}
    monkeypatch.setattr(tm, "load_state", lambda: state)
    monkeypatch.setattr(tm, "save_state", lambda s: None)
    monkeypatch.setattr(tm, "get_trial_queue", lambda s: [])
    monkeypatch.setattr(tm, "existing_source_ids", lambda: set())
    sent = []
    monkeypatch.setattr(tm, "send_daily_summary_email", lambda s, d: sent.append(d))
    tm.cmd_run()
    assert sent == [TODAY]


# ── #3 auto-promote: a revert fails the job, and is done once, cleanly ───────

FAKE_PROMOTE_CLAUDE = r"""#!/bin/bash
cat > /dev/null
python3 - <<'PY'
import json, subprocess, datetime
from pathlib import Path
today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
log = Path("logs/auto-promote-history.jsonl")
src = Path("config/sources.json")
for sid, msg in (("x", "feat: auto-promote X FAILME"), ("y", "feat: auto-promote Y")):
    d = json.loads(src.read_text()); d["sources"].append({"id": sid})
    src.write_text(json.dumps(d, indent=2) + "\n")
    subprocess.run(["git", "commit", "-qam", msg], check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with log.open("a") as f:
        f.write(json.dumps({"date": today, "id": sid, "outcome": "promoted", "commit": sha}) + "\n")
PY
exit 0
"""
# Fails any commit whose message says FAILME.
FAKE_VALIDATOR = """import subprocess, sys
c = sys.argv[sys.argv.index("--commit") + 1]
msg = subprocess.run(["git", "log", "-1", "--format=%B", c], capture_output=True, text=True).stdout
print("[FAIL] no evidence" if "FAILME" in msg else "[OK]")
sys.exit(1 if "FAILME" in msg else 0)
"""
FAKE_HEALTH = """import json, sys
sid = sys.argv[sys.argv.index("--source") + 1]
ids = {s["id"] for s in json.load(open("config/sources.json"))["sources"]}
if sid in ids:
    print(f"  {sid:25} 10 articles · 900 chars · 1ms")
sys.exit(0 if sid in ids else 2)
"""


def _promote_sandbox(tmp_path):
    sb = tmp_path / "repo"
    for d in ("scripts", "config", "logs", "auto-promote", "home/.npm-global/bin"):
        (sb / d).mkdir(parents=True)
    shutil.copy2(REPO / "scripts/wrapper-auto-promote.sh", sb / "scripts")
    shutil.copy2(REPO / "scripts/promote_today.py", sb / "scripts")
    shutil.copy2(REPO / "scripts/verify_promotions.py", sb / "scripts")
    shutil.copy2(REPO / "status_util.py", sb)
    (sb / "scripts/validate_promote_commit_msg.py").write_text(FAKE_VALIDATOR)
    (sb / "scripts/gmia-fetcher-health.py").write_text(FAKE_HEALTH)
    (sb / "fetch_articles.py").write_text('FETCHERS = {"x": None, "y": None}\n')
    (sb / "auto-promote/program.md").write_text("test playbook\n")
    (sb / "config/sources.json").write_text('{"sources": []}\n')
    (sb / "config/fund_candidates.json").write_text(json.dumps(
        [{"id": "x", "status": "promoted"}, {"id": "y", "status": "promoted"}]))
    fake = sb / "home/.npm-global/bin/claude"
    fake.write_text(FAKE_PROMOTE_CLAUDE)
    fake.chmod(0o755)
    git = lambda *a: subprocess.run(["git", *a], cwd=sb, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "base")
    return sb


def test_a_revert_fails_the_job_and_leaves_a_clean_tree(tmp_path):
    sb = _promote_sandbox(tmp_path)
    out = subprocess.run(["bash", str(sb / "scripts/wrapper-auto-promote.sh")], cwd=sb,
                         env=_clean_env(HOME=str(sb / "home")),
                         capture_output=True, text=True, timeout=120)
    log = out.stdout
    assert out.returncode != 0, log[-3000:]
    # x's revert conflicts with y's later commit: it must be aborted, not left half-done
    assert not (sb / ".git" / "REVERT_HEAD").exists()
    json.loads((sb / "config/sources.json").read_text())
    # the reverted (or un-revertable) x is not probed and reverted a second time
    assert "[probe] x" not in log
    # y still stands and was probed
    assert "[probe] y" in log


def test_todays_live_promotions_drop_reverted_rows(tmp_path):
    pt = _load("promote_today_s6", "scripts/promote_today.py")
    log = tmp_path / "h.jsonl"
    rows = [{"date": TODAY, "id": "x", "outcome": "promoted"},
            {"date": TODAY, "id": "y", "outcome": "promoted"},
            {"date": TODAY, "id": "x", "outcome": "failed_validation"},
            {"date": "2026-01-01", "id": "z", "outcome": "promoted"}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert [e["id"] for e in pt.live_promotions(log, TODAY)] == ["y"]
    assert pt.live_promotions(tmp_path / "missing.jsonl", TODAY) == []


# ── B the synthesis agent is told which targets this session plans ───────────

FAKE_SYNTH_CLAUDE = """#!/bin/bash
if [[ "$*" != *--dangerously-skip-permissions* ]]; then cat > /dev/null; echo OK; exit 0; fi
cat > "$(dirname "$0")/prompt.txt"
exit 0
"""


def test_the_synthesis_prompt_names_the_planned_targets(tmp_path):
    for d in ("scripts", "logs", "config", "home", "fetcher-synthesis"):
        (tmp_path / d).mkdir()
    for rel in ("scripts/wrapper-fetcher-synthesis.sh", "scripts/write_session_heartbeat.py",
                "scripts/backfill_failed_synthesis.py", "scripts/sync_synthesis_history.py",
                "scripts/check_template_first.py"):
        shutil.copy2(REPO / rel, tmp_path / rel)
    targets = [{"id": i, "name": i} for i in ("rbc-bluebay", "fulcrum", "cbre")]
    (tmp_path / "synthesize_fetchers.py").write_text(f"print({json.dumps(targets)!r})\n")
    (tmp_path / "fetcher-synthesis" / "program.md").write_text("test playbook\n")
    (tmp_path / "config" / "fund_candidates.json").write_text(json.dumps(
        {"candidates": [dict(t, status="inaccessible") for t in targets]}))
    fake = tmp_path / "claude"
    fake.write_text(FAKE_SYNTH_CLAUDE)
    fake.chmod(0o755)
    subprocess.run(["bash", str(tmp_path / "scripts/wrapper-fetcher-synthesis.sh")],
                   env=_clean_env(HOME=str(tmp_path / "home"), CLAUDE_BIN=str(fake),
                                  SYNTHESIS_LOCK_FILE=str(tmp_path / "t.lock")),
                   capture_output=True, text=True, timeout=120)
    prompt = (tmp_path / "prompt.txt").read_text()
    assert "rbc-bluebay, fulcrum" in prompt and "cbre" not in prompt.split("Session constraints")[1]


def test_the_playbook_defers_to_the_planned_targets():
    playbook = (REPO / "fetcher-synthesis" / "program.md").read_text()
    head = playbook.split("### Phase 1.5", 1)[0]
    assert "Session constraints" in head and "② HIGH quality" not in head


# ── #7 a templated candidate is sampled with its template ────────────────────

def test_trial_sampling_uses_the_candidates_template(monkeypatch):
    cand = {"id": "acme", "status": "visitable", "research_url": "https://www.acme.com/insights",
            "listing_template": SPEC}
    monkeypatch.setattr(tm, "load_candidates", lambda: [cand])
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})
    monkeypatch.setattr(lt, "fetch", lambda source: [
        {"title": f"T{i}", "url": f"https://www.acme.com/a/{i}"} for i in range(4)])
    monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: pytest.fail("crawled the page instead"))
    links = tm._get_article_links_for_sampling({"id": "acme", "research_url": cand["research_url"]})
    assert links == [f"https://www.acme.com/a/{i}" for i in range(4)]


# ── #7 / A candidates that used to get stuck ─────────────────────────────────

def test_a_templated_inaccessible_candidate_is_a_synthesis_target_again(monkeypatch):
    monkeypatch.setattr(synthesize_fetchers, "load_candidates", lambda: [
        {"id": "acme", "status": "inaccessible", "quality": "HIGH", "listing_template": SPEC},
        {"id": "prio", "status": "promoted", "synthesis_priority": True, "listing_template": SPEC}])
    monkeypatch.setattr(synthesize_fetchers, "load_fetcher_ids", lambda: set())
    assert [t["id"] for t in synthesize_fetchers.list_targets()] == ["acme"]


def test_a_template_that_found_nothing_is_retired_when_the_trial_gives_up(monkeypatch):
    cand = {"id": "acme", "name": "Acme", "status": "visitable", "quality": "HIGH",
            "research_url": "https://www.acme.com/insights", "listing_template": SPEC}
    cands = [cand]
    state = {"active_trials": [], "history": []}
    monkeypatch.setattr(tm, "load_state", lambda: state)
    monkeypatch.setattr(tm, "save_state", lambda s: None)
    monkeypatch.setattr(tm, "load_candidates", lambda: cands)
    monkeypatch.setattr(tm, "save_candidates", lambda c: None)
    monkeypatch.setattr(tm, "get_trial_queue", lambda s: [cand])
    monkeypatch.setattr(tm, "existing_source_ids", lambda: set())
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})
    monkeypatch.setattr(tm, "count_articles_with_fetcher",
                        lambda t: {"accessible": True, "article_count": 0, "error": None})
    monkeypatch.setattr(tm, "send_daily_summary_email", lambda s, d: None)
    tm.cmd_run()
    assert cand["status"] == "inaccessible"
    assert "listing_template" not in cand and cand["failed_listing_template"] == SPEC


def test_a_passing_template_is_not_charged_when_the_session_stops_early(tmp_path):
    start = "2026-10-18T18:00:00+00:00"
    f = tmp_path / "c.json"
    f.write_text(json.dumps([
        {"id": "acme", "status": "inaccessible", "listing_template": SPEC,
         "template_attempts": [{"at": "2026-10-18T18:05:00+00:00", "ok": True}]},
        {"id": "other", "status": "inaccessible"}]))
    out = bf.backfill(["acme", "other"], start, candidates_path=f)
    assert out["backfilled"] == ["other"]


def test_a_candidate_fixed_by_synthesis_after_a_failed_trial_can_be_trialled_again(monkeypatch):
    state = {"active_trials": [], "history": [
        {"id": "fixed", "outcome": "fail_quantity", "end_date": "2026-09-01"},
        {"id": "unfixed", "outcome": "fail_quantity", "end_date": "2026-09-01"},
        {"id": "lowq", "outcome": "fail_quality", "end_date": "2026-09-01"}]}
    monkeypatch.setattr(tm, "load_candidates", lambda: [
        {"id": "fixed", "status": "visitable", "quality": "HIGH",
         "synthesis_outcome": "success", "synthesis_attempted_at": "2026-09-14T02:10:00+08:00"},
        {"id": "unfixed", "status": "visitable", "quality": "HIGH"},
        {"id": "lowq", "status": "visitable", "quality": "HIGH",
         "synthesis_outcome": "success", "synthesis_attempted_at": "2026-08-01T02:10:00+08:00"}])
    assert [c["id"] for c in tm.get_trial_queue(state)] == ["fixed"]


# ── #10 playbooks match what the code allows ─────────────────────────────────

def test_the_synthesis_test_command_runs_here():
    assert "--timeout=30" not in (REPO / "fetcher-synthesis" / "program.md").read_text()


def test_auto_promote_may_use_the_web_tools_its_playbook_requires():
    text = (REPO / "scripts" / "wrapper-auto-promote.sh").read_text()
    assert '--allowedTools "Bash,Read,Edit,Write,Glob,Grep,WebSearch,WebFetch"' in text


def test_auto_promote_playbook_covers_a_hand_written_sources_bookkeeping():
    playbook = (REPO / "auto-promote" / "program.md").read_text()
    assert "fetch_shape" in playbook.split("**模板源", 1)[0]
    assert "git add config/sources.json publish.py fetch_content.py docs/listing-template-decisions.md" in playbook


def test_a_clean_revert_is_recorded_even_when_the_push_fails(tmp_path, monkeypatch):
    vp = _load("verify_promotions_s6", "scripts/verify_promotions.py")
    git = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    (tmp_path / "s.json").write_text('{"sources": []}\n'); git("add", "-A"); git("commit", "-qm", "base")
    (tmp_path / "s.json").write_text('{"sources": [{"id": "x"}]}\n'); git("commit", "-qam", "promote x")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (tmp_path / "logs").mkdir()
    monkeypatch.chdir(tmp_path)
    _, problem = vp.revert({"date": TODAY, "id": "x", "commit": sha}, "auto_reverted", "probe FAIL", "probe")
    assert "push failed" in problem                      # no remote here
    assert json.loads((tmp_path / "s.json").read_text()) == {"sources": []}
    row = json.loads((tmp_path / "logs/auto-promote-history.jsonl").read_text())
    assert row["outcome"] == "auto_reverted" and row["reverted_commit"] == sha


def test_promote_today_keeps_a_promotion_made_again_after_a_revert(tmp_path):
    """promoted -> reverted -> promoted again the same day (a re-run by hand):
    the last promotion stands and is the one checked (CRA finding, 2026-10-15)."""
    import json as _json
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import promote_today as pt
    log = tmp_path / "h.jsonl"
    rows = [{"date": "2026-10-15", "id": "x", "outcome": "promoted", "commit": "aaa"},
            {"date": "2026-10-15", "id": "x", "outcome": "auto_reverted", "commit": "bbb"},
            {"date": "2026-10-15", "id": "x", "outcome": "promoted", "commit": "ccc"},
            {"date": "2026-10-15", "id": "y", "outcome": "promoted", "commit": "ddd"},
            {"date": "2026-10-15", "id": "y", "outcome": "failed_validation", "commit": "eee"}]
    log.write_text("".join(_json.dumps(r) + "\n" for r in rows))
    live = pt.live_promotions(log, "2026-10-15")
    assert [(e["id"], e["commit"]) for e in live] == [("x", "ccc")]


# ── 2026-10-15 review of this range (python-reviewer, each reproduced) ──────

def test_a_report_that_crashes_fails_an_otherwise_clean_job(tmp_path):
    sb = _discovery_sandbox(tmp_path)
    (sb / "config" / "sources.json").write_text("{not json\n")   # the report reads it
    out = _run_discovery(sb)
    assert out.returncode != 0, out.stdout[-2000:]
    assert "the report crashed" in out.stdout


def test_an_agent_written_added_at_in_another_format_does_not_crash_the_report(tmp_path):
    sb = _discovery_sandbox(tmp_path)
    (sb / "config" / "fund_candidates.json").write_text(json.dumps([
        {"id": "a", "status": "watchlist", "added_at": "Oct 14"},
        {"id": "b", "status": "watchlist", "added_at": "2026-10-01"}]) + "\n")
    out = _run_discovery(sb)
    assert out.returncode == 0, out.stdout[-2000:]
    import status_util
    assert status_util.days_since_last_new_candidate(
        [{"added_at": "Oct 14"}, {"added_at": "2026-10-01"}],
        datetime(2026, 10, 15, 9, tzinfo=BJT)) == 14
    assert status_util.days_since_last_new_candidate([{"added_at": "Oct 14"}]) is None


def test_a_new_candidate_the_guard_reverts_to_seed_is_still_stamped():
    import guard_candidate_status as g
    before = [{"id": "old", "status": "watchlist"}]
    after = before + [{"id": "new", "status": "promoted"}]
    illegal = g.find_illegal_changes(before, after)
    assert [x["id"] for x in illegal] == ["new"]
    corrected = g.apply_corrections(after, illegal)
    stamped = g.stamp_legal_status_changes(before, corrected, illegal)
    new = next(c for c in corrected if c["id"] == "new")
    assert new["status"] == "seed" and stamped == ["new"]
    assert new.get("status_since") and new.get("added_at")


def test_a_hung_check_is_could_not_check_not_a_crash(monkeypatch):
    sys.path.insert(0, str(REPO / "scripts"))
    import verify_promotions as vp

    def hang(*a, **kw):
        raise subprocess.TimeoutExpired(a[0], kw.get("timeout"))
    monkeypatch.setattr(vp.subprocess, "run", hang)
    assert vp.check_probe({"id": "x"})[0] == "error"
    assert vp.check_validate({"id": "x", "commit": "abc"})[0] == "error"
