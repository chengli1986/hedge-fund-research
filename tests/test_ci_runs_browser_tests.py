"""CI must actually run the browser tests (stage-4 audit P11, 2026-10-09).

The workflow installed no browser, so every browser test skipped on every
run -- 11 SKIPPED lines in a green log -- and the page's JavaScript (Tags
view, Load more) was never tested where pushes are checked. Now CI installs
Chromium, and conftest's `chromium` fixture turns "no browser" into a failure
when CI is set, while a developer machine without one still skips.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import conftest

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "test.yml"


def test_no_browser_fails_in_ci(monkeypatch):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        conftest.browser_unavailable("chromium unavailable")


def test_no_browser_skips_elsewhere(monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        conftest.browser_unavailable("chromium unavailable")


def test_the_workflow_installs_chromium_before_running_the_tests():
    text = WORKFLOW.read_text(encoding="utf-8")
    install = re.search(r"playwright install --with-deps chromium", text)
    run = re.search(r"\n\s*run: (python -m )?pytest", text)
    assert install and run and install.start() < run.start(), text


def test_every_browser_test_goes_through_the_chromium_fixture():
    """A test that launched its own browser would skip in CI again unseen."""
    offenders = [p.name for p in (REPO / "tests").glob("*.py")
                 if p.name not in ("conftest.py", Path(__file__).name)
                 and ".chromium" + ".launch(" in p.read_text(encoding="utf-8")]
    assert offenders == []


@pytest.mark.parametrize("ci,expect_rc", [("true", 1), (None, 0)])
def test_a_missing_browser_in_ci_turns_the_run_red(tmp_path, ci, expect_rc):
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp_path / "no-browsers-here")
    if ci:
        env["CI"] = ci
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_tags_view_browser.py"],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == expect_rc, r.stdout[-2000:]
    if ci:
        assert "CI must run the browser tests" in r.stdout
    else:
        assert "skipped" in r.stdout and "passed" not in r.stdout
