"""Unit tests for analyze_articles.py — Stage 3 LLM Analysis."""

import json
import pytest

from analyze_articles import (
    _should_analyze,
    _parse_llm_output,
    _analyze_with_fallback,
    _resolve_content_path,
    VALID_THEMES,
    BASE_DIR,
)


# ---------------------------------------------------------------------------
# _should_analyze
# ---------------------------------------------------------------------------

class TestShouldAnalyze:
    def test_skip_already_summarized(self):
        article = {"summarized": True, "content_status": "ok", "source_id": "gmo"}
        assert _should_analyze(article) is False

    def test_bridgewater_eligible_if_content_ok(self):
        article = {"summarized": False, "content_status": "ok", "source_id": "bridgewater"}
        assert _should_analyze(article) is True

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

    def test_themes_as_dict_list_extracts_name(self):
        # Gemini 2.5 Pro occasionally returns themes as objects instead of strings,
        # causing unhashable type: 'dict' in the set membership check.
        data = {
            "summary_en": "x", "summary_zh": "x",
            "themes": [{"name": "AI/Tech", "rationale": "…"}, {"name": "Macro/Rates"}],
            "key_takeaway_en": "x", "key_takeaway_zh": "x",
        }
        result = _parse_llm_output(json.dumps(data))
        assert result is not None
        assert result["themes"] == ["AI/Tech", "Macro/Rates"]

    def test_themes_as_dict_list_without_name_field_skipped(self):
        data = {
            "summary_en": "x", "summary_zh": "x",
            "themes": [{"rationale": "no name key here"}, "AI/Tech"],
            "key_takeaway_en": "x", "key_takeaway_zh": "x",
        }
        result = _parse_llm_output(json.dumps(data))
        assert result is not None
        assert result["themes"] == ["AI/Tech"]


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

    def test_primary_fails_second_tier_succeeds(self, monkeypatch):
        """When the leading model fails, the next tier runs — and each tier is
        called with its OWN model id.

        Rewritten 2026-09-07 when MODEL_CHAIN became
        [gpt-5.6-luna, gpt-4.1-mini, gemini-2.5-pro]. Both OpenAI tiers share
        one caller, so the assertion that matters is that the model ids differ:
        without partial() binding them, both tiers would run _call_openai's
        default and the chain would be one model tried twice.
        """
        calls = []

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            calls.append(model)
            if model == "gpt-5.6-luna":
                raise RuntimeError("luna down")
            return (self.GOOD_RESULT, {"total_tokens": 100}, model)

        def mock_gemini(prompt, api_key):
            calls.append("gemini-2.5-pro")
            return (self.GOOD_RESULT, {}, "gemini-2.5-pro")

        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_gemini", mock_gemini)

        api_keys = {"OPENAI_API_KEY": "fake-openai", "GEMINI_API_KEY": "fake-gemini"}

        result = _analyze_with_fallback("article content", api_keys, title="Test")
        assert result is not None
        assert result["_model"] == "gpt-4.1-mini"
        assert calls.count("gpt-5.6-luna") == 2      # MAX_ATTEMPTS before falling through
        assert calls.count("gpt-4.1-mini") == 1
        assert "gemini-2.5-pro" not in calls

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
        """A tier with no key is skipped, not called with api_key=None.

        Rewritten 2026-09-07. The old version withheld GEMINI_API_KEY and
        asserted OpenAI ran first — but after the chain reorder OpenAI runs
        first unconditionally, so it passed without ever reaching the skip
        branch: deleting that branch outright kept the whole suite green. The
        skippable tier now is the OpenAI one, so this withholds that key.
        Without the branch, api_key=None reaches the caller and every article
        costs two 401s per OpenAI tier instead of a clean skip.
        """
        calls = []

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            calls.append(("openai", model, api_key))
            return (self.GOOD_RESULT, {}, model)

        def mock_gemini(prompt, api_key):
            calls.append(("gemini", "gemini-2.5-pro", api_key))
            return (self.GOOD_RESULT, {}, "gemini-2.5-pro")

        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_gemini", mock_gemini)

        result = _analyze_with_fallback("content", {"GEMINI_API_KEY": "fake-gemini"})

        assert result is not None
        assert result["_model"] == "gemini-2.5-pro"
        assert not [c for c in calls if c[0] == "openai"], (
            "an OpenAI tier ran with no OPENAI_API_KEY")
        assert all(c[2] is not None for c in calls), "a caller was handed api_key=None"

class TestResolveContentPath:
    def test_uses_explicit_relative_content_path(self):
        article = {"id": "abc123", "content_path": "content/custom.txt"}
        assert _resolve_content_path(article) == BASE_DIR / "content/custom.txt"

    def test_falls_back_to_id_based_path(self):
        article = {"id": "abc123"}
        assert _resolve_content_path(article) == BASE_DIR / "content" / "abc123.txt"

    def test_rejects_path_traversal(self):
        article = {"id": "abc123", "content_path": "../../escape.txt"}
        with pytest.raises(ValueError):
            _resolve_content_path(article)

    def test_rejects_absolute_path_outside_content_dir(self):
        article = {"id": "abc123", "content_path": "/tmp/escape.txt"}
        with pytest.raises(ValueError):
            _resolve_content_path(article)


class TestPromptsEnumerateThemes:
    """The prompt must name the themes it will be graded against.

    Root cause of the empty-theme problem found 2026-09-07: neither prompt
    listed VALID_THEMES -- they only showed a JSON skeleton with
    `"themes": [...]`.  The model invented free-form labels and
    _parse_llm_output then filtered them against a closed 15-item allowlist, so
    they were all dropped.  Measured on one article: the model returned
    ['Retirement Plan Management', 'Benchmark Risk', 'Fiduciary Scrutiny',
    'Regulatory Changes', 'Investment Policy', 'Portfolio Construction'] and the
    stored result was [].  611 of 1331 summarised articles (46%) had no themes,
    and publish.py's theme view only lists articles that have them -- so nearly
    half the corpus was missing from the site's theme navigation.

    Asking the model to choose from a list it can see is the fix; the parser
    keeps filtering as a defence, not as the mechanism.
    """

    def test_analysis_prompt_lists_every_valid_theme(self):
        from analyze_articles import ANALYSIS_PROMPT, VALID_THEMES
        missing = sorted(t for t in VALID_THEMES if t not in ANALYSIS_PROMPT)
        assert not missing, f"ANALYSIS_PROMPT does not show these themes: {missing}"

    def test_metadata_prompt_lists_every_valid_theme(self):
        from analyze_articles import METADATA_PROMPT, VALID_THEMES
        assert METADATA_PROMPT, "prompt is empty; the check below would pass vacuously"
        missing = sorted(t for t in VALID_THEMES if t not in METADATA_PROMPT)
        assert not missing, f"METADATA_PROMPT does not show these themes: {missing}"

    def test_prompts_forbid_inventing_labels(self):
        # Without this the model returns precise-but-unlisted labels that the
        # parser silently drops -- the exact failure being fixed.
        import re
        from analyze_articles import ANALYSIS_PROMPT, METADATA_PROMPT
        for name, p in (("ANALYSIS_PROMPT", ANALYSIS_PROMPT),
                        ("METADATA_PROMPT", METADATA_PROMPT)):
            # Normalise whitespace: the assertion is about what the prompt says,
            # not about where its lines happen to wrap.
            flat = re.sub(r"\s+", " ", p)
            assert "exactly as written" in flat, f"{name} does not pin the label wording"
            assert "invent" in flat, f"{name} does not forbid invented labels"

    def test_theme_list_stays_in_sync_with_the_allowlist(self):
        # A theme added to VALID_THEMES but not to the prompts is invisible to
        # the model and can never be chosen; one removed from VALID_THEMES but
        # left in the prompts is offered and then filtered away.
        from analyze_articles import ANALYSIS_PROMPT, METADATA_PROMPT, VALID_THEMES
        import re
        for name, p in (("ANALYSIS_PROMPT", ANALYSIS_PROMPT),
                        ("METADATA_PROMPT", METADATA_PROMPT)):
            listed = set(re.findall(r'"([A-Za-z][A-Za-z/ ]+)"', p.split("Allowed themes")[-1]))
            stale = sorted(listed - VALID_THEMES)
            assert not stale, f"{name} offers themes not in VALID_THEMES: {stale}"


class TestPrimaryThemeOrdering:
    """publish.py clusters each article by themes[0], so order is not cosmetic.

    `publish.py` assigns every article to ONE primary cluster using
    `article_themes[0]`; the rest only feed the sidebar. Measured on 30
    articles, gpt-4.1-mini put "AI/Tech" first on 14 of them against
    gpt-5.6-luna's 8, and the disagreements were articles like "Global Fixed
    Income Team Views & Outlook" and "Tight bond spreads mean no stone
    unturned" -- a fixed-income outlook filed under AI on the research page.
    Until now nothing in either prompt said the order meant anything.
    """

    def test_prompts_state_that_the_first_theme_is_primary(self):
        import re
        from analyze_articles import ANALYSIS_PROMPT, METADATA_PROMPT
        for name, p in (("ANALYSIS_PROMPT", ANALYSIS_PROMPT),
                        ("METADATA_PROMPT", METADATA_PROMPT)):
            flat = re.sub(r"\s+", " ", p)
            assert "most important theme first" in flat, (
                f"{name} does not tell the model that theme order matters")


class TestOpenAIParamStyles:
    """Each OpenAI model gets the request shape it actually accepts.

    Verified live 2026-09-07: gpt-5.6-luna rejects `max_tokens` (400,
    "Use 'max_completion_tokens' instead") and rejects `temperature: 0.4`
    (400, "Only the default (1) value is supported"). gpt-4.1-mini accepts
    both. Sending one shape to both models makes the newer one fail every
    call, and the chain would silently fall through to the next tier -- which
    looks like a working fallback, not a misconfiguration.
    """

    def test_every_openai_model_in_the_chain_has_a_param_style(self):
        from analyze_articles import MODEL_CHAIN, OPENAI_MODELS, _OPENAI_PARAMS
        missing = [m for m in MODEL_CHAIN if m in OPENAI_MODELS and m not in _OPENAI_PARAMS]
        assert not missing, f"OpenAI models in MODEL_CHAIN with no param style: {missing}"

    def test_luna_sends_max_completion_tokens_and_no_temperature(self, monkeypatch):
        import analyze_articles as aa
        sent = {}

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

        monkeypatch.setattr(aa.requests, "post",
                            lambda url, **kw: (sent.update(kw["json"]), R())[1])
        aa._call_openai("p", "k", "gpt-5.6-luna")
        assert "max_tokens" not in sent, "luna rejects max_tokens"
        assert "temperature" not in sent, "luna only accepts the default temperature"
        assert sent["max_completion_tokens"] > 0

    def test_gpt41_mini_keeps_the_classic_shape(self, monkeypatch):
        import analyze_articles as aa
        sent = {}

        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

        monkeypatch.setattr(aa.requests, "post",
                            lambda url, **kw: (sent.update(kw["json"]), R())[1])
        aa._call_openai("p", "k", "gpt-4.1-mini")
        assert sent["max_tokens"] == 4000
        assert sent["temperature"] == 0.4

    def test_unknown_openai_model_fails_loudly(self):
        # Adding a model to MODEL_CHAIN without declaring its request shape must
        # raise, not quietly send a shape the API will reject on every call.
        import pytest as _pytest
        import analyze_articles as aa
        with _pytest.raises(KeyError):
            aa._call_openai("p", "k", "gpt-9-imaginary")


class TestModelChainOrder:
    """gpt-5.6-luna leads; the chain keeps a non-OpenAI tier at the end.

    Measured over 30 articles with the theme-allowlist prompt: luna $0.36/mo
    vs gpt-4.1-mini $0.57 vs gemini-2.5-pro $8.76, all three 30/30 parseable,
    0/30 empty themes for both OpenAI models. Luna was chosen on behaviour,
    not price: it flags missing source material instead of writing around it
    (three cases), and files articles under the theme they are actually about.
    gemini-2.5-pro stays last so an OpenAI-wide outage still has a tier.
    """

    def test_luna_is_the_primary_model(self):
        from analyze_articles import MODEL_CHAIN
        assert MODEL_CHAIN[0] == "gpt-5.6-luna"

    def test_chain_ends_with_a_non_openai_provider(self):
        from analyze_articles import MODEL_CHAIN, OPENAI_MODELS
        assert MODEL_CHAIN[-1] not in OPENAI_MODELS, (
            "every tier is OpenAI — one provider outage would empty the chain")


class TestGeminiOutputBudget:
    """gemini-2.5-pro is the last tier, and its thinking shares the output cap.

    On 2026-09-07 BJT (09-06 UTC) two calls on the same article stopped at
    exactly 3996 of a 4000-token cap and both failed to parse -- the thinking
    had eaten the budget mid-JSON. The chain then fell through, but gemini is
    now the LAST tier, so the same truncation would fail the whole chain. A
    revert of the cap must not be silent, and neither must a truncation.
    """

    def _post(self, monkeypatch, finish_reason="STOP"):
        import analyze_articles as aa
        sent = {}

        class R:
            def raise_for_status(self): pass
            def json(self):
                return {"candidates": [{"finishReason": finish_reason,
                                        "content": {"parts": [{"text": "{}"}]}}],
                        "usageMetadata": {}}

        monkeypatch.setattr(aa.requests, "post",
                            lambda url, **kw: (sent.update(kw["json"]), R())[1])
        return sent

    def test_output_budget_leaves_room_for_thinking(self, monkeypatch):
        import analyze_articles as aa
        sent = self._post(monkeypatch)
        aa._call_gemini("p", "k")
        assert sent["generationConfig"]["maxOutputTokens"] >= 12000, (
            "back at a cap the model's thinking can exhaust — the 09-06 failure")

    def test_truncation_is_reported_as_truncation(self, monkeypatch, caplog):
        import logging
        import analyze_articles as aa
        self._post(monkeypatch, finish_reason="MAX_TOKENS")
        with caplog.at_level(logging.WARNING):
            aa._call_gemini("p", "k")
        assert any("MAX_TOKENS" in r.message or "truncat" in r.message.lower()
                   for r in caplog.records), (
            "a truncated reply surfaces only as 'failed to parse output' — the "
            "same misleading symptom as the 09-06 incident")
