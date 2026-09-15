"""Stage 4's docs-site sync commits only its page and reports a failed push.

publish.py writes the dashboard, then copies it into ~/docs-site and commits
and pushes. Two defects in that sync:

  `git commit -m ...` took no pathspec, so anything another process had
  staged in docs-site went into "sync: hedge-fund-research.html from
  pipeline". docs-site is shared -- its own crons refresh research data --
  so this was a matter of timing. 189 sync commits so far happened to carry
  only the page.

  A failed commit or push printed "docs-site sync skipped: ..." and main()
  returned None, so the process exited 0 and run_pipeline.sh called the
  night clean. A push rejected because another cron pushed first would be
  retried by nobody.

A sync failure is not a publish failure -- the dashboard is already written,
and Stage 5 must still check it -- so publish.py exits DOCS_SYNC_FAILED (3),
which run_pipeline.sh records as Stage4:docs-sync. The sync does not pull or
rebase on its own: docs-site routinely has other writers' uncommitted work.
"""
import ast
import inspect
import subprocess
import sys

import pytest

import publish


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


@pytest.fixture
def docs_repo(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "docs-site"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "pages").mkdir()
    (repo / "pages" / "hedge-fund-research.html").write_text("old")
    (repo / "data.json").write_text("{}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    return repo


def test_only_the_page_is_committed_when_something_else_is_staged(docs_repo):
    (docs_repo / "data.json").write_text('{"another": "writer"}')
    _git(docs_repo, "add", "data.json")
    assert publish.sync_docs_site(docs_repo, "<html>new</html>") is True
    files = _git(docs_repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["pages/hedge-fund-research.html"]
    assert _git(docs_repo, "diff", "--cached", "--name-only").stdout.split() == ["data.json"], \
        "another writer's staged change was consumed"


def test_the_commit_is_pushed(docs_repo):
    assert publish.sync_docs_site(docs_repo, "<html>new</html>") is True
    assert _git(docs_repo, "status", "-sb").stdout.splitlines()[0].endswith("main...origin/main")


def test_a_rejected_push_is_reported(docs_repo, capsys):
    _git(docs_repo, "remote", "set-url", "origin", str(docs_repo.parent / "missing.git"))
    assert publish.sync_docs_site(docs_repo, "<html>new</html>") is False
    err = capsys.readouterr().err
    assert "docs-site" in err and "push" in err
    assert _git(docs_repo, "log", "-1", "--format=%s").stdout.startswith("sync:"), \
        "the local commit is kept so the next push carries it"


def test_a_commit_left_by_a_rejected_push_is_pushed_on_an_unchanged_rerun(docs_repo):
    """Pins b43ef86 (auto-review): after a rejected push the page is committed
    locally; the next run with identical HTML used to see "no change" and
    return True without pushing, so the commit never left the machine."""
    good_remote = _git(docs_repo, "remote", "get-url", "origin").stdout.strip()
    _git(docs_repo, "remote", "set-url", "origin", str(docs_repo.parent / "missing.git"))
    assert publish.sync_docs_site(docs_repo, "<html>v2</html>") is False
    _git(docs_repo, "remote", "set-url", "origin", good_remote)
    assert publish.sync_docs_site(docs_repo, "<html>v2</html>") is True
    assert _git(docs_repo, "rev-list", "--count", "@{u}..HEAD").stdout.strip() == "0", "local commit still unpushed"


def test_no_change_is_success_without_a_commit(docs_repo):
    before = _git(docs_repo, "rev-parse", "HEAD").stdout
    assert publish.sync_docs_site(docs_repo, "old") is True
    assert _git(docs_repo, "rev-parse", "HEAD").stdout == before


def test_an_absent_docs_site_is_not_a_failure(tmp_path):
    assert publish.sync_docs_site(tmp_path / "no-docs-site", "<html/>") is True


def test_main_exits_docs_sync_failed_when_the_sync_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "load_articles", lambda: [])
    monkeypatch.setattr(publish, "generate_html", lambda articles: "<html></html>")
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: False)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(tmp_path / "page.html")])
    assert publish.main() == publish.DOCS_SYNC_FAILED
    assert (tmp_path / "page.html").read_text() == "<html></html>", "the page is still published"


def test_main_exits_zero_when_all_is_well(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "load_articles", lambda: [])
    monkeypatch.setattr(publish, "generate_html", lambda articles: "<html></html>")
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: True)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(tmp_path / "page.html")])
    assert publish.main() in (0, None)


def test_the_entry_point_propagates_the_exit_code():
    tree = ast.parse(inspect.getsource(publish))
    guard = [n for n in tree.body if isinstance(n, ast.If) and "__name__" in ast.dump(n.test)][-1]
    exits = [c for c in ast.walk(guard) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr == "exit"]
    assert exits and any("main" in ast.dump(a) for c in exits for a in c.args)
