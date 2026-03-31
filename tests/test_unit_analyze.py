"""Unit tests for analyze_articles.py — Stage 3 LLM Analysis."""

import json
import pytest

from analyze_articles import (
    _should_analyze,
    _parse_llm_output,
    _analyze_with_fallback,
    VALID_THEMES,
)


# ---------------------------------------------------------------------------
# _should_analyze
# ---------------------------------------------------------------------------

class TestShouldAnalyze:
    def test_skip_already_summarized(self):
        article = {"summarized": True, "content_status": "ok", "source_id": "gmo"}
        assert _should_analyze(article) is False

    def test_skip_bridgewater(self):
        article = {"summarized": False, "content_status": "ok", "source_id": "bridgewater"}
        assert _should_analyze(article) is False

    def test_skip_failed_content(self):
        article = {"summarized": False, "content_status": "failed", "source_id": "gmo"}
        assert _should_analyze(article) is False

    def test_skip_missing_content_status(self):
        article = {"summarized": False, "source_id": "gmo"}
        assert _should_analyze(article) is False

    def test_accept_eligible(self):
        article = {"summarized": False, "content_status": "ok", "source_id": "aqr"}
        assert _should_analyze(article) is True

    def test_accept_no_summarized_field(self):
        """summarized field absent means not yet summarized."""
        article = {"content_status": "ok", "source_id": "man-group"}
        assert _should_analyze(article) is True


# ---------------------------------------------------------------------------
# _parse_llm_output
# ---------------------------------------------------------------------------

class TestParseLlmOutput:
    VALID_JSON = json.dumps({
        "summary_en": "English summary",
        "summary_zh": "中文摘要",
        "themes": ["AI/Tech", "Macro/Rates"],
        "key_takeaway_en": "Key point",
        "key_takeaway_zh": "关键点",
    })

    def test_parse_valid_json(self):
        result = _parse_llm_output(self.VALID_JSON)
        assert result is not None
        assert result["summary_en"] == "English summary"
        assert result["summary_zh"] == "中文摘要"
        assert result["themes"] == ["AI/Tech", "Macro/Rates"]

    def test_reject_invalid_themes(self):
        data = {
            "summary_en": "x",
            "summary_zh": "x",
            "themes": ["AI/Tech", "BogusTheme", "NotReal"],
            "key_takeaway_en": "x",
            "key_takeaway_zh": "x",
        }
        result = _parse_llm_output(json.dumps(data))
        assert result is not None
        assert result["themes"] == ["AI/Tech"]

    def test_all_invalid_themes_filtered_to_empty(self):
        data = {
            "summary_en": "x",
            "summary_zh": "x",
            "themes": ["Fake1", "Fake2"],
            "key_takeaway_en": "x",
            "key_takeaway_zh": "x",
        }
        result = _parse_llm_output(json.dumps(data))
        assert result is not None
        assert result["themes"] == []

    def test_parse_json_from_markdown_fences(self):
        wrapped = f"```json\n{self.VALID_JSON}\n```"
        result = _parse_llm_output(wrapped)
        assert result is not None
        assert result["summary_en"] == "English summary"

    def test_parse_json_from_plain_fences(self):
        wrapped = f"```\n{self.VALID_JSON}\n```"
        result = _parse_llm_output(wrapped)
        assert result is not None

    def test_return_none_for_garbage(self):
        assert _parse_llm_output("this is not json at all") is None

    def test_return_none_for_missing_fields(self):
        data = {"summary_en": "x", "summary_zh": "x"}
        assert _parse_llm_output(json.dumps(data)) is None

    def test_return_none_for_non_dict(self):
        assert _parse_llm_output(json.dumps([1, 2, 3])) is None


# ---------------------------------------------------------------------------
# _analyze_with_fallback
# ---------------------------------------------------------------------------

class TestAnalyzeWithFallback:
    GOOD_RESULT = json.dumps({
        "summary_en": "Summary",
        "summary_zh": "摘要",
        "themes": ["AI/Tech"],
        "key_takeaway_en": "Takeaway",
        "key_takeaway_zh": "要点",
    })

    def test_gemini_fails_openai_succeeds(self, monkeypatch):
        """When Gemini fails, should fall back to OpenAI."""
        call_order = []

        def mock_gemini(prompt, api_key):
            call_order.append("gemini")
            raise RuntimeError("Gemini down")

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            call_order.append("openai")
            return (self.GOOD_RESULT, {"total_tokens": 100}, model)

        def mock_anthropic(prompt, api_key, model="claude-sonnet-4-6"):
            call_order.append("anthropic")
            return (self.GOOD_RESULT, {}, model)

        monkeypatch.setattr("analyze_articles._call_gemini", mock_gemini)
        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_anthropic", mock_anthropic)

        api_keys = {
            "GEMINI_API_KEY": "fake-gemini",
            "OPENAI_API_KEY": "fake-openai",
            "ANTHROPIC_API_KEY": "fake-anthropic",
        }

        result = _analyze_with_fallback("article content", api_keys, title="Test")
        assert result is not None
        assert result["_model"] == "gpt-4.1-mini"
        # Gemini should have been tried MAX_ATTEMPTS times before falling back
        assert call_order.count("gemini") == 2
        assert call_order.count("openai") == 1
        assert "anthropic" not in call_order

    def test_all_models_fail(self, monkeypatch):
        """When all models fail, should return None."""
        def mock_gemini(prompt, api_key):
            raise RuntimeError("down")

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            raise RuntimeError("down")

        def mock_anthropic(prompt, api_key, model="claude-sonnet-4-6"):
            raise RuntimeError("down")

        monkeypatch.setattr("analyze_articles._call_gemini", mock_gemini)
        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_anthropic", mock_anthropic)

        api_keys = {
            "GEMINI_API_KEY": "fake",
            "OPENAI_API_KEY": "fake",
            "ANTHROPIC_API_KEY": "fake",
        }

        result = _analyze_with_fallback("content", api_keys)
        assert result is None

    def test_skip_model_without_api_key(self, monkeypatch):
        """Models without API keys should be skipped entirely."""
        call_order = []

        def mock_gemini(prompt, api_key):
            call_order.append("gemini")
            raise RuntimeError("down")

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            call_order.append("openai")
            return (self.GOOD_RESULT, {}, model)

        def mock_anthropic(prompt, api_key, model="claude-sonnet-4-6"):
            call_order.append("anthropic")
            return (self.GOOD_RESULT, {}, model)

        monkeypatch.setattr("analyze_articles._call_gemini", mock_gemini)
        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_anthropic", mock_anthropic)

        # No Gemini key — should skip straight to OpenAI
        api_keys = {"OPENAI_API_KEY": "fake-openai"}

        result = _analyze_with_fallback("content", api_keys)
        assert result is not None
        assert "gemini" not in call_order
        assert call_order[0] == "openai"
