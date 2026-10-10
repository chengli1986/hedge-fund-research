import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

try:    # captured before _no_network replaces it for every test
    from playwright.sync_api import sync_playwright as _real_sync_playwright
except ImportError:
    _real_sync_playwright = None


# ---------------------------------------------------------------------------
# Tests that need a real browser (page JavaScript: Tags view, Load more, the
# phone layout) take the `chromium` fixture. Where no browser is installed
# they skip -- except in CI (GitHub sets CI=true), where a skip is a failure:
# CI installed no browser and every browser test skipped on every run, green,
# until the stage-4 audit (2026-10-09) read the log.
# ---------------------------------------------------------------------------

def browser_unavailable(reason: str):
    if os.environ.get("CI"):
        pytest.fail(f"CI must run the browser tests, but: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def chromium():
    if _real_sync_playwright is None:
        browser_unavailable("playwright is not installed")
    with _real_sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            browser_unavailable(f"chromium unavailable: {exc}")
        yield browser
        browser.close()


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
               ("analyze_articles", "TAG_USAGE_LOG_FILE"),
               ("fetch_articles", "INSPECTION_STATE_FILE"),
               ("fetch_content", "CONTENT_FAILURE_LOG"))


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


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Unit tests must not reach the network.

    Found 2026-09-07: tests/test_unit_discover.py never patched
    discover_entrypoints._call_llm, so two of its cases made real, billed
    gemini-2.5-pro requests on every run -- 8.5s and 6.0s, about half the
    suite's wall clock -- and neither asserted anything about the reply.  The
    daily gmia-nightly-test and gmia-auto-promote crons each run pytest, so it
    was a standing charge.

    Blocked centrally rather than mocked case by case: patching each call site
    is the shape that gets forgotten, and the next test to reach a live API
    would do it silently.  Tests marked `live` or `nightly` are exempt --
    reaching real sites is their whole purpose.
    """
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("nightly"):
        yield
        return
    import socket

    def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            "a unit test tried to open a network connection "
            f"({args[0] if args else '?'}) — mock the caller, or mark the test "
            "`live`/`nightly` if it is meant to hit the real thing")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)

    # A browser is a separate process, so blocking socket.connect here does not
    # stop it. Found 2026-10-02: test_sample_article_quality_tracks_js_only_count
    # had launched a real Chromium against example.com on every run since
    # 2026-07-01, when the trial manager gained a Playwright fallback the test
    # never stubbed -- the page it fetched sent the code down another branch,
    # and the test failed with a KeyError that hid the cause. Every launch in
    # this codebase does `from playwright.sync_api import sync_playwright`
    # inside the function (test_playwright_is_only_imported_inside_functions
    # holds that), so replacing the attribute catches all of them; a test's
    # own fake, set on the same attribute, still wins.
    launches: list[str] = []
    try:
        import playwright.sync_api as _pw
    except ImportError:
        yield
        return

    def _no_browser(*args, **kwargs):
        launches.append(request.node.nodeid)
        raise RuntimeError(
            "a unit test tried to launch a real browser — stub the Playwright "
            "call, or mark the test `live`/`nightly` if it is meant to hit the real thing")

    monkeypatch.setattr(_pw, "sync_playwright", _no_browser)
    yield
    # Checked after the test as well as raised during it: code under test
    # often swallows a fetcher's exception and carries on as if the page were
    # empty -- the trial manager's Playwright fallback does -- and then the
    # RuntimeError above is never seen and the test passes for the wrong
    # reason.
    assert not launches, (
        "a unit test tried to launch a real browser (the error may have been "
        "swallowed by the code under test) — stub the Playwright call, or mark "
        "the test `live`/`nightly`")
