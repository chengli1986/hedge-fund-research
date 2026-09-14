"""run_pipeline.sh, executed for real against stub stages.

Until 2026-09-14 nothing ran this script in a test; earlier fixes to it
(the --written-after epoch) were pinned only by reading its text. Each test
builds a fake ~/hedge-fund-research whose stage scripts exit with chosen
codes, runs the real run_pipeline.sh with HOME pointed at it, and checks the
exit code, the reported failed stages, and whether Stage 5 ran.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "run_pipeline.sh"


def _run(tmp_path, publish_rc=0, analyze_rc=0):
    home = tmp_path / "home"
    repo = home / "hedge-fund-research"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    (repo / "config" / ".last_validated").touch()          # skip the weekly validation
    marker = tmp_path / "stage5-ran"
    stubs = {
        "fetch_articles.py": 0, "fetch_content.py": 0,
        "analyze_articles.py": analyze_rc, "publish.py": publish_rc,
    }
    for name, rc in stubs.items():
        (repo / name).write_text(f"import sys; sys.exit({rc})\n")
    (repo / "scripts" / "check_dashboard_html.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n")
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)
    return proc.returncode, proc.stdout + proc.stderr, marker.exists()


def test_all_stages_ok(tmp_path):
    rc, out, stage5 = _run(tmp_path)
    assert rc == 0 and "all stages OK" in out and stage5


def test_a_docs_site_sync_failure_alerts_but_the_page_is_still_checked(tmp_path):
    rc, out, stage5 = _run(tmp_path, publish_rc=3)
    assert rc == 1
    assert "Stage4:docs-sync" in out and "Stage4:publish" not in out
    assert stage5, "the page was published; Stage 5 must still check it"


def test_a_publish_failure_skips_stage5(tmp_path):
    rc, out, stage5 = _run(tmp_path, publish_rc=1)
    assert rc == 1 and "Stage4:publish" in out and not stage5


def test_an_analysis_failure_still_publishes_and_checks(tmp_path):
    rc, out, stage5 = _run(tmp_path, analyze_rc=1)
    assert rc == 1 and "Stage3:analyze" in out and stage5
