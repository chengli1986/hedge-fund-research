import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def pytest_configure(config):
    config.addinivalue_line("markers", "live: tests that hit live websites")
    config.addinivalue_line("markers", "nightly: nightly regression tests")


@pytest.fixture(autouse=True)
def _never_write_production_usage_log(tmp_path, monkeypatch):
    """Point analyze_articles' token log at a throwaway file for every test.

    The accounting added 2026-09-06 books every HTTP-boundary call against the
    module-level USAGE_LOG_FILE, so any test that exercises the LLM chain writes
    into the real `logs/analyze-usage.jsonl` -- tests/test_unit_analyze.py alone
    added 2 fake rows (empty article_id, null tokens) per run, mixing zero-cost
    noise into the spend record the accounting exists to produce.

    Autouse rather than a monkeypatch inside each calling test: per-call-site
    patching is the shape that gets forgotten, and a test written later that
    happens to reach the chain would start polluting again silently.  Importing
    inside the fixture keeps collection free of the module import.
    """
    import analyze_articles
    monkeypatch.setattr(analyze_articles, "USAGE_LOG_FILE",
                        tmp_path / "analyze-usage.jsonl")
