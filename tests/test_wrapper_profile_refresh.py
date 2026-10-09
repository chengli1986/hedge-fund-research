"""wrapper-profile-refresh.sh end to end, in a throwaway git repo.

Stage-4 audit (2026-10-09): the monthly refresh published, committed and
pushed whatever apply_refresh wrote without running a single test; its
`git commit` had no pathspec, in a working tree other jobs stage files in; and
its summary mail never left (no env sourced, no SMTP_HOST). Here the real
wrapper runs against a sandbox: a fake `claude` writes the draft, the remote
is a local bare repo, and the mail sender is a stub that records what it got.
Nothing touches the production tree, the network or a real mailbox.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PUBLISH = '''"""publish.py fixture."""
import html

_FUND_PROFILES: dict[str, dict] = {
    "kkr": {
        "founded": "1976", "aum": "~$758B", "hq": "New York, NY",
        "type_en": "Listed PE Manager", "type_zh": "上市私募股权",
        "desc_zh": "全球领先的另类资产管理机构之一，1976 年创立，业务覆盖私募股权、基础设施、房地产与私募信贷等多个领域。",
        "notable_en": "LBO pioneer.",
        "notable_zh": "杠杆收购先驱。",
    },
}


def generate_html(articles):
    return "".join(html.escape(v) for p in _FUND_PROFILES.values() for v in p.values())


if __name__ == "__main__":
    generate_html([])
    print("published")
'''

SOURCES = {"sources": [{"id": "kkr", "description": "Global alternatives leader (~$758B AUM)."}],
           "settings": {}}

DRAFT = {"id": "kkr", "aum": "~$800B", "aum_source": "https://www.sec.gov/x",
         "change_log": [{"field": "aum", "old": "~$758B", "new": "~$800B",
                         "reason": "Q2 10-Q", "source": "https://www.sec.gov/x"}]}

# Records what the wrapper handed it instead of sending anything.
SUMMARY_STUB = '''import json, os, sys
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
json.dump({"smtp_user": os.environ.get("SMTP_USER", ""), "mail_to": os.environ.get("MAIL_TO", ""),
           "applied": args.get("--applied", ""), "flagged": args.get("--flagged", "")},
          open("logs/summary.json", "w"))
'''


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _sandbox(tmp_path: Path, tests_pass: bool) -> tuple[Path, dict]:
    repo = tmp_path / "repo"
    for d in ("config", "scripts", "pending_profiles", "logs", "tests", "auto-promote"):
        (repo / d).mkdir(parents=True)
    (repo / "publish.py").write_text(PUBLISH, encoding="utf-8")
    (repo / "config" / "sources.json").write_text(json.dumps(SOURCES, indent=2), encoding="utf-8")
    for f in ("apply_refresh.py", "profile_edit.py", "validate_pending_profile.py",
              "wrapper-profile-refresh.sh"):
        shutil.copy(REPO / "scripts" / f, repo / "scripts" / f)
    (repo / "scripts" / "send_refresh_summary.py").write_text(SUMMARY_STUB)
    (repo / "auto-promote" / "refresh-program.md").write_text("refresh the profiles\n")
    (repo / "tests" / "test_gate.py").write_text(f"def test_gate():\n    assert {tests_pass}\n")
    (repo / ".gitignore").write_text("logs/\npending_profiles/\n__pycache__/\n")
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")

    claude = tmp_path / "claude"
    claude.write_text("#!/usr/bin/env bash\ncat > pending_profiles/kkr.refresh.json <<'EOF'\n"
                      + json.dumps(DRAFT) + "\nEOF\n")
    claude.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".stock-monitor.env").write_text("SMTP_USER=bot@163.com\nSMTP_PASS=pw\nMAIL_TO=me@example.com\n")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SMTP_", "MAIL_", "ANTHROPIC_", "GIT_"))}
    env.update(HOME=str(home), CLAUDE_BIN=str(claude), ALERT_ONLY="0",
               PROFILE_REFRESH_REPO=str(repo), PROFILE_REFRESH_LOCK=str(tmp_path / "lock"),
               GIT_CONFIG_GLOBAL="/dev/null")
    return repo, env


def _run(repo, env):
    return subprocess.run(["bash", str(repo / "scripts" / "wrapper-profile-refresh.sh")],
                          env=env, capture_output=True, text=True, timeout=300)


def test_sandbox_is_not_production(tmp_path):
    repo, env = _sandbox(tmp_path, tests_pass=True)
    assert Path(env["PROFILE_REFRESH_REPO"]).resolve() != REPO.resolve()
    assert str(tmp_path) in env["PROFILE_REFRESH_LOCK"]


def test_a_passing_run_commits_only_the_two_profile_files(tmp_path):
    repo, env = _sandbox(tmp_path, tests_pass=True)
    (repo / "unrelated.txt").write_text("staged by someone else\n")
    _git(repo, "add", "unrelated.txt")
    head = _git(repo, "rev-parse", "HEAD").strip()

    r = _run(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr

    assert _git(repo, "rev-parse", "HEAD~1").strip() == head, "expected exactly one new commit"
    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").split())
    assert committed == {"publish.py", "config/sources.json"}
    assert "unrelated.txt" in _git(repo, "diff", "--cached", "--name-only"), "other staged work was taken"
    assert _git(repo, "rev-parse", "origin/main").strip() == _git(repo, "rev-parse", "HEAD").strip()
    assert '"aum": "~$800B"' in (repo / "publish.py").read_text()
    assert "(~$800B AUM)" in (repo / "config" / "sources.json").read_text()
    assert not list((repo / "logs").glob("profile-refresh-backup.*")), "backup left behind"


def test_failing_tests_roll_both_files_back_and_commit_nothing(tmp_path):
    repo, env = _sandbox(tmp_path, tests_pass=False)
    before = ((repo / "publish.py").read_bytes(), (repo / "config" / "sources.json").read_bytes())
    head = _git(repo, "rev-parse", "HEAD").strip()

    r = _run(repo, env)
    assert r.returncode == 0, r.stdout + r.stderr

    assert ((repo / "publish.py").read_bytes(), (repo / "config" / "sources.json").read_bytes()) == before
    assert _git(repo, "rev-parse", "HEAD").strip() == head
    assert (repo / "pending_profiles" / "rolled_back" / "kkr.refresh.json").exists()
    assert not (repo / "pending_profiles" / "kkr.refresh.json").exists(), "would be re-applied next month"
    summary = json.loads((repo / "logs" / "summary.json").read_text())
    assert summary["applied"] == "" and "rolled back" in summary["flagged"]


def test_summary_gets_the_mail_settings_from_the_env_file(tmp_path):
    repo, env = _sandbox(tmp_path, tests_pass=True)
    assert _run(repo, env).returncode == 0
    summary = json.loads((repo / "logs" / "summary.json").read_text())
    assert summary["smtp_user"] == "bot@163.com" and summary["mail_to"] == "me@example.com"
    assert summary["applied"].strip() == "kkr"


@pytest.mark.parametrize("tests_pass", [True, False])
def test_wrapper_never_touches_the_production_tree(tmp_path, tests_pass):
    trees = {REPO.resolve(), (Path.home() / "hedge-fund-research").resolve()}
    watched = [t / f for t in trees for f in ("publish.py", "config/sources.json") if (t / f).exists()]
    before = [p.read_bytes() for p in watched]
    repo, env = _sandbox(tmp_path, tests_pass=tests_pass)
    _run(repo, env)
    assert [p.read_bytes() for p in watched] == before
