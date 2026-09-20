"""run_pipeline.sh, executed for real against stub stages.

Until 2026-09-14 nothing ran this script in a test; earlier fixes to it
(the --written-after epoch) were pinned only by reading its text. Each test
builds a fake ~/hedge-fund-research whose stage scripts exit with chosen
codes, runs the real run_pipeline.sh with HOME pointed at it, and checks the
exit code, the reported failed stages, and whether Stage 5 ran.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "run_pipeline.sh"


def _run(tmp_path, publish_rc=0, analyze_rc=0, validator=None):
    home = tmp_path / "home"
    repo = home / "hedge-fund-research"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    if validator is None:
        (repo / "config" / ".last_validated").touch()      # skip the weekly validation
    else:
        (repo / "validate_entrypoints.py").write_text(validator)
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


class TestEntrypointValidationReachesAHuman:
    """The weekly entrypoint check found problems and told the log (audit A2).

    run_pipeline.sh computed a list of sources whose entrypoints are not ok and
    `echo`ed it; cron-wrapper.sh alerts on the exit code and nothing else, so
    the one check that can notice an entrypoint going bad had no destination --
    the seventh instance of that shape in this repo. The result is now written
    where the daily health email can read it, and its stderr is kept instead of
    being sent to /dev/null.
    """
    BAD = ('import json, sys\n'
           'print(json.dumps({"aqr": [{"url": "https://www.aqr.com/x", "status": "404"}],'
           ' "gmo": [{"url": "https://www.gmo.com/y", "status": "ok"}]}))\n')
    CRASH = 'import sys; sys.stderr.write("boom\\n"); sys.exit(2)\n'

    def test_the_result_is_written_where_the_health_check_can_read_it(self, tmp_path):
        rc, out, _ = _run(tmp_path, validator=self.BAD)
        written = tmp_path / "home" / "hedge-fund-research" / "logs" / "entrypoint-validation.json"
        assert written.exists(), "the validation result went nowhere but the console"
        data = json.loads(written.read_text())
        assert data["aqr"][0]["status"] == "404"
        assert rc == 0, "an entrypoint problem is reported, not a reason to fail the night"

    def test_a_validator_that_crashes_leaves_its_reason(self, tmp_path):
        rc, out, _ = _run(tmp_path, validator=self.CRASH)
        written = tmp_path / "home" / "hedge-fund-research" / "logs" / "entrypoint-validation.json"
        assert written.exists()
        data = json.loads(written.read_text())
        assert "boom" in data.get("_error", ""), data
        assert rc == 0

    def test_a_crash_does_not_mark_the_week_as_validated(self, tmp_path):
        _run(tmp_path, validator=self.CRASH)
        marker = tmp_path / "home" / "hedge-fund-research" / "config" / ".last_validated"
        assert not marker.exists(), "a failed validation must be retried tomorrow"


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
