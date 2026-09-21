"""Unit tests for analyze_articles.py — Stage 3 LLM Analysis."""

import json
import sys
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
        [gpt-5.6-luna, gpt-4.1-mini, ...]. Both OpenAI tiers share
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

        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)

        api_keys = {"OPENAI_API_KEY": "fake-openai"}

        # The body must support GOOD_RESULT's summary: since 2026-09-13 every
        # summary passes check_grounding, and "article content" would turn this
        # into a test of a rejection that still returned _model == mini.
        result = _analyze_with_fallback("Summary of the article.", api_keys, title="Test")
        assert result is not None
        assert not result.get("insufficient_content"), result
        assert result["_model"] == "gpt-4.1-mini"
        assert calls.count("gpt-5.6-luna") == 2      # MAX_ATTEMPTS before falling through
        assert calls.count("gpt-4.1-mini") == 1

    def test_all_models_fail(self, monkeypatch):
        """When all models fail, should return None."""
        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            raise RuntimeError("down")

        def mock_anthropic(prompt, api_key, model="claude-sonnet-4-6"):
            raise RuntimeError("down")

        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)
        monkeypatch.setattr("analyze_articles._call_anthropic", mock_anthropic)

        api_keys = {
            "OPENAI_API_KEY": "fake",
            "ANTHROPIC_API_KEY": "fake",
        }

        result = _analyze_with_fallback("content", api_keys)
        assert result is None

    def test_skip_model_without_api_key(self, monkeypatch):
        """A tier with no key is skipped, not called with api_key=None.

        Rewritten 2026-09-07 (withhold the OpenAI key, expect the Gemini tier)
        and again 2026-09-21 when the chain became OpenAI-only: with no
        OPENAI_API_KEY every tier is skipped and the chain returns None.
        Without the skip branch, api_key=None reaches the caller and every
        article costs two 401s per tier instead of a clean skip.
        """
        calls = []

        def mock_openai(prompt, api_key, model="gpt-4.1-mini"):
            calls.append(("openai", model, api_key))
            return (self.GOOD_RESULT, {}, model)

        monkeypatch.setattr("analyze_articles._call_openai", mock_openai)

        result = _analyze_with_fallback("Summary of the article.", {"SOME_OTHER_KEY": "x"})

        assert result is None
        assert calls == [], "an OpenAI tier ran with no OPENAI_API_KEY"

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
    """gpt-5.6-luna leads; gpt-4.1-mini is the only fallback.

    Measured over 30 articles with the theme-allowlist prompt: luna $0.36/mo
    vs gpt-4.1-mini $0.57, both 30/30 parseable, 0/30 empty themes. Luna was
    chosen on behaviour, not price: it flags missing source material instead
    of writing around it (three cases), and files articles under the theme
    they are actually about.

    Until 2026-09-21 this class also asserted that the chain ENDS with a
    non-OpenAI provider. That guard was removed deliberately, not because it
    failed: the Google key is retired and the user chose a single provider
    over wiring a second key in (tests/test_no_gemini_tier.py has the record).
    """

    def test_luna_is_the_primary_model(self):
        from analyze_articles import MODEL_CHAIN
        assert MODEL_CHAIN[0] == "gpt-5.6-luna"

    def test_second_tier_is_a_different_model_not_a_retry(self):
        from analyze_articles import MODEL_CHAIN
        assert len(MODEL_CHAIN) == 2 and MODEL_CHAIN[1] != MODEL_CHAIN[0]


class TestChainModelsAreFullyDeclared:
    """Every tier must be declared everywhere it needs declaring.

    Adding a model to MODEL_CHAIN touches three tables: the caller dispatch,
    _OPENAI_PARAMS (OpenAI only), and _USAGE_FIELDS. A model missing from the
    last one logs as unmeasured rather than wrong -- safe, but its spend
    silently stops being counted, which is the whole point of the accounting.
    """

    def test_every_chain_model_has_usage_fields(self):
        from analyze_articles import MODEL_CHAIN, _USAGE_FIELDS
        missing = [m for m in MODEL_CHAIN if m not in _USAGE_FIELDS]
        assert not missing, f"models in MODEL_CHAIN with no usage mapping: {missing}"

class TestTotalAnalysisOutageIsAFailure:
    """Stage 3 must not report success when nothing could be summarised.

    Same shape as the stage 1 outage fixed in 9f6e291: `fail_count` could
    equal `len(pending)` and main() still returned None, so the process exited
    0 and run_pipeline.sh's `if python3 analyze_articles.py` guard saw a clean
    run. A quota exhaustion or all three MODEL_CHAIN tiers failing is invisible
    to the pipeline's own alert channel.

    The floor is "articles were waiting and not one was summarised". An empty
    pending list is the normal quiet case and must stay silent.
    """

    # No content_path: _resolve_content_path derives CONTENT_DIR/<id>.txt.
    # An earlier version set a RELATIVE "content/a1.txt", which resolves
    # against the repo cwd and was rejected as escaping the patched content
    # dir -- so the model stub was never reached and the outage test passed
    # for the wrong reason (path errors, not model failures).
    ART = {"id": "a1", "source_id": "aqr", "title": "T", "url": "https://aqr.com/1",
           "date": "2026-09-01", "content_status": "ok", "summarized": False}
    GOOD = {"summary_en": "e", "summary_zh": "摘", "themes": ["Quant/Factor"],
            "key_takeaway_en": "k", "key_takeaway_zh": "要",
            "_model": "gpt-5.6-luna", "_usage": {}}

    def _run(self, tmp_path, monkeypatch, articles, result):
        import analyze_articles as aa
        data = tmp_path / "articles.jsonl"
        data.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in articles))
        content = tmp_path / "content"
        content.mkdir()
        for a in articles:
            (content / f"{a['id']}.txt").write_text("body text long enough to analyse")
        monkeypatch.setattr(aa, "DATA_FILE", data)
        monkeypatch.setattr(aa, "CONTENT_DIR", content)
        monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
        monkeypatch.setattr(aa, "_analyze_with_fallback",
                            lambda *a, **k: (dict(result) if result else None))
        monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
        return aa.main()

    def test_every_article_failing_is_reported_as_a_failure(self, tmp_path, monkeypatch):
        rc = self._run(tmp_path, monkeypatch, [dict(self.ART)], result=None)
        assert rc == 1, ("every model failed on every article and the pipeline "
                         "still reported a clean run")

    def test_nothing_to_do_is_not_a_failure(self, tmp_path, monkeypatch):
        done = dict(self.ART, summarized=True)
        rc = self._run(tmp_path, monkeypatch, [done], result=None)
        assert rc in (0, None), "a run with nothing pending was treated as an outage"

    def test_a_partial_failure_is_not_an_outage(self, tmp_path, monkeypatch):
        import analyze_articles as aa
        arts = [dict(self.ART, id="a1"), dict(self.ART, id="a2")]
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            return dict(self.GOOD) if calls["n"] == 1 else None

        data = tmp_path / "articles.jsonl"
        data.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in arts))
        content = tmp_path / "content"
        content.mkdir()
        for a in arts:
            (content / f"{a['id']}.txt").write_text("body text long enough to analyse")
        monkeypatch.setattr(aa, "DATA_FILE", data)
        monkeypatch.setattr(aa, "CONTENT_DIR", content)
        monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
        monkeypatch.setattr(aa, "_analyze_with_fallback", flaky)
        monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
        assert aa.main() in (0, None)

    def test_the_entry_point_propagates_the_exit_code(self):
        """The tests above call main() directly and pass either way.

        On stage 1 this exact line was reverted in a mutation and all four
        behavioural tests stayed green while the whole bug came back.
        """
        import ast
        import inspect
        import analyze_articles as aa

        tree = ast.parse(inspect.getsource(aa))
        guards = [n for n in tree.body if isinstance(n, ast.If)
                  and "__name__" in ast.dump(n.test)]
        assert guards, "no `if __name__ == '__main__'` block found"
        exits = [c for c in ast.walk(guards[-1]) if isinstance(c, ast.Call)
                 and isinstance(c.func, ast.Attribute) and c.func.attr == "exit"]
        assert exits, "the entry point discards main()'s return value"
        assert any("main" in ast.dump(a) for c in exits for a in c.args)
