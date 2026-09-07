import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def pytest_configure(config):
    config.addinivalue_line("markers", "live: tests that hit live websites")
    config.addinivalue_line("markers", "nightly: nightly regression tests")


# ---------------------------------------------------------------------------
# Keeping the test suite out of production data
#
# Two leaks were found this way, one day apart, and both were the SAME shape:
# a production path enumerated somewhere a future writer would not think to
# look.  2026-09-06 the token accounting made tests/test_unit_analyze.py append
# fake rows to logs/analyze-usage.jsonl.  2026-09-07 an audit found (a)
# scripts/write_session_heartbeat.py pinned its path into a DEFAULT ARGUMENT so
# every monkeypatch of it was dead code -- 551 of 560 heartbeat rows in
# logs/fetcher-synthesis-history.jsonl were test artefacts forging the
# freshness signal gmia_liveness_audit.py trusts -- and (b) four tests writing
# fabricated source ids into config/inspection_state.json.
#
# So there are two layers here, deliberately:
#   PREVENT  — redirect the paths we know about, so a normal run stays clean.
#   DETECT   — fingerprint the production directories and fail if ANY file
#              changed.  This layer enumerates nothing, imports nothing, and
#              survives a rename or a path added later.  The prevention layer
#              is a list, and lists are what we keep getting wrong.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_DIRS = ("logs", "config", "data", "content", "pending_profiles")

# Filled by the redirect fixture with each module's TRUE path, captured before
# patching.  Tests must compare against this rather than rebuilding the path by
# hand: a hand-copied literal keeps passing after the real path is renamed,
# which is the test and the code failing to share one abstraction.
PRODUCTION_PATHS: dict[str, Path] = {}

_REDIRECTED = (("analyze_articles", "USAGE_LOG_FILE"),
               ("fetch_articles", "INSPECTION_STATE_FILE"))


@pytest.fixture(scope="session")
def _production_sink(tmp_path_factory):
    """One throwaway directory for the whole session.

    Session-scoped on purpose: a per-test tmp_path cost ~12% of total wall
    clock and left 11M of litter, to hold files nothing ever reads.
    """
    return tmp_path_factory.mktemp("production-sink")


@pytest.fixture(autouse=True)
def _redirect_production_writes(_production_sink, monkeypatch):
    """Point known production write targets at the throwaway sink.

    Import failures are swallowed rather than raised: making this autouse
    fixture import modules means one broken import would error all ~940 tests
    instead of the two files that actually use it.  Nothing is silently lost —
    the fingerprint guard below still fails the run if anything reaches a real
    production file.
    """
    import importlib
    for mod_name, attr in _REDIRECTED:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        real = getattr(mod, attr)
        PRODUCTION_PATHS[attr] = real
        monkeypatch.setattr(mod, attr, _production_sink / real.name)


def _fingerprint() -> dict[str, tuple[int, int]]:
    """size + mtime_ns of every file under the production directories."""
    out: dict[str, tuple[int, int]] = {}
    for d in PRODUCTION_DIRS:
        root = REPO_ROOT / d
        if not root.is_dir():
            continue
        for f in root.rglob("*"):
            if f.is_file():
                st = f.stat()
                out[str(f.relative_to(REPO_ROOT))] = (st.st_size, st.st_mtime_ns)
    return out


@pytest.fixture(scope="session", autouse=True)
def _no_production_writes():
    """Fail the session if a test changed anything under the production dirs.

    Detection, not prevention — but it is the only half that cannot be outrun
    by a renamed constant or a path nobody remembered to add to the list.
    """
    before = _fingerprint()
    yield
    after = _fingerprint()
    changed = sorted(k for k in set(before) | set(after)
                     if before.get(k) != after.get(k))
    assert not changed, (
        "test run modified production files (see tests/conftest.py):\n  "
        + "\n  ".join(changed))
