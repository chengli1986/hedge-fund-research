"""Two auto-promote gaps seen on the 2026-11-13 Graham revert.

1. verify_promotions.check_probe kept only the last 200 chars of stderr on a
   probe failure. That tail was an INFO "saved 37104 chars" line; the real
   reason ("stale: most recent article 162d old") was on stdout and was lost.
2. The Haiku quality check runs inside the agent session, where the wrapper
   has unset ANTHROPIC_API_KEY (so Claude uses the subscription). It found no
   key and the agent fell back to reading ~/.openclaw/.env, which goes away
   with the OpenClaw decommission. The key now lives in ~/.anthropic-api.env.
"""
import importlib.util
import subprocess as sp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import verify_promotions as vp  # noqa: E402

spec = importlib.util.spec_from_file_location("gtm_key", ROOT / "gmia-trial-manager.py")
gtm = importlib.util.module_from_spec(spec)
sys.modules["gtm_key"] = gtm
spec.loader.exec_module(gtm)

GRAHAM_STDOUT = """=== GMIA Fetcher Health — 2026-11-13 11:21 BJT ===
Sources probed: 1    Total runtime: 1.4s

⚠️  WARNINGS (1):
  graham-capital-management stale: most recent article 162d old (frequency=monthly, threshold 90d)

✅ HEALTHY (0):
"""
GRAHAM_STDERR = "2026-11-12 18:34:52,529 [INFO]   Graham: saved 37104 chars to healthprobe_x_0.txt\n"


def test_probe_failure_reason_from_stdout_is_kept(monkeypatch):
    monkeypatch.setattr(vp.subprocess, "run",
                        lambda *a, **k: sp.CompletedProcess(a[0], 1, stdout=GRAHAM_STDOUT, stderr=GRAHAM_STDERR))
    verdict, detail = vp.check_probe({"id": "graham-capital-management"})
    assert verdict == "fail"
    assert "stale: most recent article 162d old" in detail


def test_haiku_key_falls_back_to_the_dedicated_key_file(monkeypatch, tmp_path):
    key_file = tmp_path / ".anthropic-api.env"
    key_file.write_text('ANTHROPIC_API_KEY="sk-test-123"\n')
    monkeypatch.setattr(gtm, "load_env", lambda: {})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(gtm, "ANTHROPIC_KEY_FILE", key_file)
    assert gtm.anthropic_api_key() == "sk-test-123"


def test_haiku_key_does_not_depend_on_openclaw():
    assert ".openclaw" not in (ROOT / "gmia-trial-manager.py").read_text()
