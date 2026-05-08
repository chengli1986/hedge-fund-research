"""Unit tests for scripts/validate_promote_commit_msg.py — guarantees the
auto-promote 4 hard-gate commit-msg evidence is actually checked.

Without this validator, agent could fabricate evidence in commit messages
without anyone noticing. These tests lock the contract: every required field
+ threshold must be present or the validator returns FAIL.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "validate_promote_commit_msg.py"

spec = importlib.util.spec_from_file_location("vpcm", SCRIPT)
vpcm = importlib.util.module_from_spec(spec)
sys.modules["vpcm"] = vpcm
spec.loader.exec_module(vpcm)


GOOD_MSG = """feat: auto-promote BlackRock to production sources

Trial passed — wired sources.json + BADGE_COLORS + CONTENT_FETCHERS.

Live test: 10 articles, content 4823 chars
Haiku quality: avg=0.72 (rel=0.8 dep=0.7 ext=0.7 / rel=0.7 dep=0.6 ext=0.7 / rel=0.65 dep=0.5 ext=0.6)
Method: ssr (requests.get + BeautifulSoup, verified)
Preview: "BlackRock Investment Institute weekly outlook — Markets digest signals from a busy week"

Auto-promote agent run 2026-05-08.
"""


def test_good_message_passes():
    result = vpcm.validate_message(GOOD_MSG)
    assert result["ok"] is True
    for check in result["checks"]:
        assert check["passed"], f"{check['check']} unexpectedly failed: {check['detail']}"


def test_missing_live_test_fails():
    msg = GOOD_MSG.replace("Live test: 10 articles, content 4823 chars", "")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "live_test" in failed


def test_too_few_articles_fails():
    msg = GOOD_MSG.replace("10 articles", "2 articles")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "live_test"]
    assert failed[0]["passed"] is False
    assert "below threshold" in failed[0]["detail"]


def test_too_few_content_chars_fails():
    """500 char threshold catches "I claim it works" empty-evidence cases."""
    msg = GOOD_MSG.replace("4823 chars", "120 chars")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "live_test"]
    assert failed[0]["passed"] is False


def test_avg_quality_below_threshold_fails():
    msg = GOOD_MSG.replace("avg=0.72", "avg=0.30")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "haiku_quality"]
    assert failed[0]["passed"] is False
    assert "below threshold" in failed[0]["detail"]


def test_no_relevance_above_threshold_fails():
    """avg can be padded by depth/extractable; we need at least 1 article rel>=0.6."""
    bad = GOOD_MSG.replace(
        "Haiku quality: avg=0.72 (rel=0.8 dep=0.7 ext=0.7 / rel=0.7 dep=0.6 ext=0.7 / rel=0.65 dep=0.5 ext=0.6)",
        "Haiku quality: avg=0.55 (rel=0.3 dep=0.7 ext=0.7 / rel=0.4 dep=0.6 ext=0.7 / rel=0.5 dep=0.5 ext=0.6)",
    )
    result = vpcm.validate_message(bad)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "haiku_quality"]
    assert failed[0]["passed"] is False


def test_missing_haiku_section_fails():
    msg = GOOD_MSG.replace(
        "Haiku quality: avg=0.72 (rel=0.8 dep=0.7 ext=0.7 / rel=0.7 dep=0.6 ext=0.7 / rel=0.65 dep=0.5 ext=0.6)",
        "",
    )
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "haiku_quality" in failed


def test_invalid_method_fails():
    msg = GOOD_MSG.replace("Method: ssr", "Method: magic")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "method"]
    assert failed[0]["passed"] is False
    assert "magic" in failed[0]["detail"]


def test_missing_method_fails():
    msg = GOOD_MSG.replace("Method: ssr (requests.get + BeautifulSoup, verified)", "")
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "method"]
    assert failed[0]["passed"] is False


def test_short_preview_fails():
    """Preview < 50 chars looks like agent quoted a header rather than body."""
    msg = GOOD_MSG.replace(
        '"BlackRock Investment Institute weekly outlook — Markets digest signals from a busy week"',
        '"too short"',
    )
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "preview"]
    assert failed[0]["passed"] is False


def test_missing_preview_fails():
    msg = GOOD_MSG.replace(
        'Preview: "BlackRock Investment Institute weekly outlook — Markets digest signals from a busy week"',
        "",
    )
    result = vpcm.validate_message(msg)
    assert result["ok"] is False
    failed = [c for c in result["checks"] if c["check"] == "preview"]
    assert failed[0]["passed"] is False


def test_curly_quotes_preview_accepted():
    """Some agents (or humans) auto-correct quotes to curly. Both must work."""
    msg = GOOD_MSG.replace(
        '"BlackRock Investment Institute weekly outlook — Markets digest signals from a busy week"',
        "“BlackRock Investment Institute weekly outlook — Markets digest signals from a busy week”",
    )
    result = vpcm.validate_message(msg)
    assert result["ok"] is True


def test_completely_empty_message_fails_all_4():
    result = vpcm.validate_message("feat: auto-promote XYZ\n\nNo evidence here.\n")
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert failed == {"live_test", "haiku_quality", "method", "preview"}


def test_main_returns_1_on_validation_fail(tmp_path):
    """CLI must exit 1 (not 0) when message fails — wrapper relies on this to revert."""
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text("feat: bogus\n")
    sys_argv_orig = sys.argv
    try:
        sys.argv = ["validate_promote_commit_msg.py", "--message-file", str(msg_file), "--quiet"]
        rc = vpcm.main()
        assert rc == 1
    finally:
        sys.argv = sys_argv_orig


def test_main_returns_0_on_validation_pass(tmp_path):
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(GOOD_MSG)
    sys_argv_orig = sys.argv
    try:
        sys.argv = ["validate_promote_commit_msg.py", "--message-file", str(msg_file), "--quiet"]
        rc = vpcm.main()
        assert rc == 0
    finally:
        sys.argv = sys_argv_orig


def test_main_returns_2_on_missing_file():
    sys_argv_orig = sys.argv
    try:
        sys.argv = ["validate_promote_commit_msg.py", "--message-file", "/nonexistent/path/foo.txt", "--quiet"]
        rc = vpcm.main()
        assert rc == 2
    finally:
        sys.argv = sys_argv_orig
