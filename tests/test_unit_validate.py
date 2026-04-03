"""Unit tests for validate_entrypoints.py — 6 tests covering validate_entrypoint, validate_source, and main()."""

import json
import sys
from unittest.mock import MagicMock, patch
import pytest
from validate_entrypoints import validate_entrypoint, validate_source


ALLOWED_DOMAINS = ["man.com", "bridgewater.com", "aqr.com"]

RESEARCH_HTML = """
<html>
<body>
  <article>
    <h2>Q1 2026 Outlook</h2>
    <time datetime="2026-01-15">January 15, 2026</time>
    <p class="author">By John Smith</p>
    <a href="/report.pdf">Download report</a>
    <a href="/report2.pdf">Download report 2</a>
    <a href="/report3.pdf">Download report 3</a>
  </article>
  <article>
    <h2>Market Commentary Feb 2026</h2>
    <time datetime="2026-02-01">Feb 2026</time>
  </article>
  <div class="pagination">Next</div>
</body>
</html>
"""

GATED_HTML = """
<html>
<body>
  <h1>Welcome to Man Group Research</h1>
  <p>Subscribe to read our latest insights.</p>
  <p>Log in to read our premium content.</p>
  <p>Register to continue your journey.</p>
  <input type="email" placeholder="your@email.com" />
</body>
</html>
"""


# ---------------------------------------------------------------------------
# TestValidateEntrypoint  (3 tests)
# ---------------------------------------------------------------------------

class TestValidateEntrypoint:

    def test_valid_entrypoint(self):
        """Mock requests.get returns research-like HTML → status='ok', final > 0.5."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = RESEARCH_HTML

        with patch("validate_entrypoints.requests.get", return_value=mock_resp):
            result = validate_entrypoint(
                "https://www.man.com/insights/research",
                ALLOWED_DOMAINS,
            )

        assert result["status"] == "ok"
        assert result["error"] is None
        assert result["scores"]["final"] > 0.5

    def test_http_error(self):
        """Mock raises exception → status='error', error message present."""
        with patch(
            "validate_entrypoints.requests.get",
            side_effect=Exception("Connection refused"),
        ):
            result = validate_entrypoint(
                "https://www.man.com/insights",
                ALLOWED_DOMAINS,
            )

        assert result["status"] == "error"
        assert "Connection refused" in result["error"]
        assert result["scores"] == {}

    def test_gated_page(self):
        """Mock returns gated HTML → gate_penalty > 0.3."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.text = GATED_HTML

        with patch("validate_entrypoints.requests.get", return_value=mock_resp):
            result = validate_entrypoint(
                "https://www.man.com/insights",
                ALLOWED_DOMAINS,
            )

        assert result["scores"]["gate_penalty"] > 0.3


# ---------------------------------------------------------------------------
# TestValidateSource  (2 tests)
# ---------------------------------------------------------------------------

class TestValidateSource:

    def test_validates_all_active_entrypoints(self):
        """2 active entrypoints → validate_entrypoint called twice, 2 results returned."""
        source_config = {
            "entrypoints": [
                {"url": "https://www.man.com/insights", "active": True},
                {"url": "https://www.man.com/publications", "active": True},
            ]
        }

        fake_result = {"url": "x", "status": "ok", "scores": {}, "error": None}

        with patch(
            "validate_entrypoints.validate_entrypoint",
            return_value=fake_result,
        ) as mock_ve:
            results = validate_source("man-group", source_config, ALLOWED_DOMAINS)

        assert mock_ve.call_count == 2
        assert len(results) == 2

    def test_skips_inactive(self):
        """1 inactive entrypoint → validate_entrypoint not called, 0 results."""
        source_config = {
            "entrypoints": [
                {"url": "https://www.man.com/insights", "active": False},
            ]
        }

        with patch(
            "validate_entrypoints.validate_entrypoint",
            return_value={"url": "x", "status": "ok", "scores": {}, "error": None},
        ) as mock_ve:
            results = validate_source("man-group", source_config, ALLOWED_DOMAINS)

        assert mock_ve.call_count == 0
        assert results == []


# ---------------------------------------------------------------------------
# TestMainJsonOutput  (1 test)
# ---------------------------------------------------------------------------

class TestMainJsonOutput:

    def test_main_json_output(self, tmp_path, monkeypatch, capsys):
        """--json flag produces parseable JSON to stdout with expected structure."""
        entrypoints = {
            "sources": {
                "man-group": {
                    "entrypoints": [
                        {"url": "https://www.man.com/insights", "active": True}
                    ]
                }
            }
        }
        sources = {
            "sources": [
                {"id": "man-group", "expected_hostname": "man.com"}
            ]
        }

        ep_file = tmp_path / "entrypoints.json"
        src_file = tmp_path / "sources.json"
        ep_file.write_text(json.dumps(entrypoints))
        src_file.write_text(json.dumps(sources))

        fake_result = {
            "url": "https://www.man.com/insights",
            "status": "ok",
            "scores": {"domain": 1.0, "path": 0.5, "structure": 0.8, "gate_penalty": 0.0, "final": 0.7},
            "error": None,
        }

        monkeypatch.setattr("validate_entrypoints.ENTRYPOINTS_FILE", ep_file)
        monkeypatch.setattr("validate_entrypoints.SOURCES_FILE", src_file)

        with patch("validate_entrypoints.validate_entrypoint", return_value=fake_result):
            monkeypatch.setattr(sys, "argv", ["validate_entrypoints", "--json"])
            from validate_entrypoints import main
            main()

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "man-group" in data
        assert isinstance(data["man-group"], list)
        assert len(data["man-group"]) == 1
        assert data["man-group"][0]["status"] == "ok"
        assert data["man-group"][0]["url"] == "https://www.man.com/insights"
        assert captured.err == ""
