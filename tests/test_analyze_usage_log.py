"""Token accounting for analyze_articles.

Why this exists (2026-09-06): every LLM call already returned its token counts
and `_run_llm_chain` stashed them in `parsed["_usage"]` -- which was then never
read, never persisted, never logged.  So "what does GMIA's summarisation cost?"
had no answer at all, and any decision about switching provider or moving to a
subscription would have been made on a guess.

The one trap worth a test: the three providers name the same two numbers
differently --

    gemini-2.5-pro    usageMetadata: promptTokenCount / candidatesTokenCount
    gpt-4.1-mini      usage:         prompt_tokens   / completion_tokens
    claude-*          usage:         input_tokens    / output_tokens

A normaliser that reaches for one spelling and falls back to 0 turns an
unrecognised payload into "this call was free".  Unknown MUST stay None so the
aggregate can say "unknown", never "zero".
"""
import json

import pytest

import analyze_articles as aa


class TestNormalizeUsage:
    def test_gemini_shape(self):
        u = aa._normalize_usage("gemini-2.5-pro", {
            "promptTokenCount": 4321, "candidatesTokenCount": 890,
            "totalTokenCount": 5211})
        assert u["input_tokens"] == 4321 and u["output_tokens"] == 890

    def test_openai_shape(self):
        u = aa._normalize_usage("gpt-4.1-mini", {
            "prompt_tokens": 4321, "completion_tokens": 890, "total_tokens": 5211})
        assert u["input_tokens"] == 4321 and u["output_tokens"] == 890

    def test_anthropic_shape(self):
        u = aa._normalize_usage("claude-sonnet-4-6", {
            "input_tokens": 4321, "output_tokens": 890})
        assert u["input_tokens"] == 4321 and u["output_tokens"] == 890

    def test_unknown_payload_is_none_not_zero(self):
        # A provider that renames its fields must surface as unknown, not free.
        u = aa._normalize_usage("gemini-2.5-pro", {"inputTokens": 4321})
        assert u == {"input_tokens": None, "output_tokens": None,
                     "provider_total_tokens": None}

    def test_empty_payload_is_none_not_zero(self):
        u = aa._normalize_usage("gpt-4.1-mini", {})
        assert u == {"input_tokens": None, "output_tokens": None,
                     "provider_total_tokens": None}

    def test_unknown_model_is_none_not_zero(self):
        # Adding a model to MODEL_CHAIN without teaching the normaliser its
        # shape must not silently book it as costing nothing.
        u = aa._normalize_usage("some-future-model", {"input_tokens": 10})
        assert u == {"input_tokens": None, "output_tokens": None,
                     "provider_total_tokens": None}


class TestUsageLog:
    def test_appends_one_row_per_call(self, tmp_path):
        p = tmp_path / "usage.jsonl"
        aa._append_usage_log("abc123", "gemini-2.5-pro",
                             {"promptTokenCount": 100, "candidatesTokenCount": 20}, path=p)
        aa._append_usage_log("def456", "gpt-4.1-mini",
                             {"prompt_tokens": 200, "completion_tokens": 30}, path=p)
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        assert len(rows) == 2
        assert [r["article_id"] for r in rows] == ["abc123", "def456"]
        assert [r["input_tokens"] for r in rows] == [100, 200]
        assert [r["output_tokens"] for r in rows] == [20, 30]
        assert [r["model"] for r in rows] == ["gemini-2.5-pro", "gpt-4.1-mini"]

    def test_row_carries_bjt_timestamp(self, tmp_path):
        p = tmp_path / "usage.jsonl"
        aa._append_usage_log("abc123", "gemini-2.5-pro",
                             {"promptTokenCount": 1, "candidatesTokenCount": 1}, path=p)
        row = json.loads(p.read_text().splitlines()[0])
        # EC2 runs UTC; every other timestamp in this repo is BJT, so this one
        # must be too or the daily aggregate lands in the wrong day.
        assert row["at"].endswith("+08:00")

    def test_unknown_usage_is_logged_as_null_not_dropped(self, tmp_path):
        # The row must still exist: "we made a call and don't know its cost" is
        # information, and silently skipping it understates the total.
        p = tmp_path / "usage.jsonl"
        aa._append_usage_log("abc123", "gemini-2.5-pro", {}, path=p)
        row = json.loads(p.read_text().splitlines()[0])
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None

    def test_logging_failure_never_breaks_analysis(self, tmp_path):
        # Instrumentation must not be able to kill the pipeline it measures.
        unwritable = tmp_path / "nope" / "usage.jsonl"   # parent does not exist
        aa._append_usage_log("abc123", "gemini-2.5-pro",
                             {"promptTokenCount": 1}, path=unwritable)


class TestChainRecordsEveryCall:
    """Accounting must sit at the HTTP boundary, not at the success boundary.

    _analyze_with_fallback retries a model whose output fails to parse, and
    falls through to the next model.  Those discarded calls still burned
    tokens.  Booking only the call that finally parsed would understate the
    real spend by exactly the amount the retries cost -- and retries are not
    rare here: gemini-2.5-pro 503s every few days.
    """

    PARSEABLE = ('{"summary_en":"e","summary_zh":"z","themes":[],'
                 '"key_takeaway_en":"e","key_takeaway_zh":"z"}')

    def _keys(self):
        return {"GEMINI_API_KEY": "k", "OPENAI_API_KEY": "k"}

    def test_unparseable_then_parseable_logs_both_calls(self, tmp_path, monkeypatch):
        p = tmp_path / "usage.jsonl"
        monkeypatch.setattr(aa, "USAGE_LOG_FILE", p)
        calls = {"n": 0}

        def fake_gemini(prompt, api_key):
            calls["n"] += 1
            usage = {"promptTokenCount": 100 * calls["n"], "candidatesTokenCount": 10}
            text = "not json" if calls["n"] == 1 else self.PARSEABLE
            return (text, usage, "gemini-2.5-pro")

        monkeypatch.setattr(aa, "_call_gemini", fake_gemini)
        res = aa._analyze_with_fallback("body", self._keys(), title="t",
                                        source="s", date="2026-09-06",
                                        article_id="art1")
        assert res is not None
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        assert len(rows) == 2, "the discarded first call must be booked too"
        assert [r["input_tokens"] for r in rows] == [100, 200]
        assert [r["parsed"] for r in rows] == [False, True]
        assert {r["article_id"] for r in rows} == {"art1"}

    def test_exception_logs_nothing(self, tmp_path, monkeypatch):
        # A 503 never returned a usage payload; inventing a zero row would be
        # fabricating data.
        p = tmp_path / "usage.jsonl"
        monkeypatch.setattr(aa, "USAGE_LOG_FILE", p)

        def boom(prompt, api_key):
            raise RuntimeError("503")

        monkeypatch.setattr(aa, "_call_gemini", boom)
        monkeypatch.setattr(aa, "_call_openai",
                            lambda prompt, api_key, model="gpt-4.1-mini":
                            (self.PARSEABLE, {"prompt_tokens": 5, "completion_tokens": 1}, model))
        aa._analyze_with_fallback("body", self._keys(), title="t", source="s",
                                  date="2026-09-06", article_id="art2")
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        assert [r["model"] for r in rows] == ["gpt-4.1-mini"]


class TestMainWiresArticleId:
    """The last mile: main() must pass article_id into the chain.

    article_id defaults to "" so the chain stays callable from tests, which
    means a caller that forgets it produces rows that log fine and are useless
    -- every row anonymous, no way to join spend back to a source.  There is no
    fixture harness for main() in this repo, so this is a structural check on
    the call site rather than a behavioural one; it is the same shape as
    tests/test_no_hardcoded_hosts.py.
    """

    def test_main_passes_article_id_to_the_chain(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(aa))
        main_fn = next(n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main_fn)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "id", "") == "_analyze_with_fallback"]
        assert calls, "main() no longer calls _analyze_with_fallback"
        for call in calls:
            assert any(kw.arg == "article_id" for kw in call.keywords), (
                "main() calls _analyze_with_fallback without article_id -- "
                "usage rows would be anonymous")


class TestGeminiThinkingTokens:
    """Reasoning tokens are billed as output and must not be dropped.

    Caught by probing the live API instead of trusting the field list in the
    code's comment (2026-09-06).  A real gemini-2.5-pro reply came back as

        promptTokenCount 43 / candidatesTokenCount 34 /
        thoughtsTokenCount 305 / totalTokenCount 382

    -- 43 + 34 + 305 = 382.  Booking only candidatesTokenCount as output
    understated this call's output by 9x, and every summarisation this pipeline
    runs is a reasoning call.  The provider's own total is recorded alongside so
    that any future field we fail to map shows up as a reconciliation gap in the
    data itself, rather than waiting to be noticed by eye.
    """

    def test_thinking_tokens_count_as_output(self):
        u = aa._normalize_usage("gemini-2.5-pro", {
            "promptTokenCount": 43, "candidatesTokenCount": 34,
            "thoughtsTokenCount": 305, "totalTokenCount": 382})
        assert u["input_tokens"] == 43
        assert u["output_tokens"] == 339          # 34 + 305, both billed output

    def test_reconciles_against_provider_total(self):
        u = aa._normalize_usage("gemini-2.5-pro", {
            "promptTokenCount": 43, "candidatesTokenCount": 34,
            "thoughtsTokenCount": 305, "totalTokenCount": 382})
        assert u["provider_total_tokens"] == 382
        assert u["input_tokens"] + u["output_tokens"] == u["provider_total_tokens"]

    def test_absent_thinking_tokens_are_not_required(self):
        u = aa._normalize_usage("gemini-2.5-pro", {
            "promptTokenCount": 43, "candidatesTokenCount": 34,
            "totalTokenCount": 77})
        assert u["output_tokens"] == 34
        assert u["provider_total_tokens"] == 77

    def test_openai_total_is_recorded_too(self):
        u = aa._normalize_usage("gpt-4.1-mini", {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
        assert u["provider_total_tokens"] == 120

    def test_missing_provider_total_is_none(self):
        # Anthropic sends no total; absence must not read as zero.
        u = aa._normalize_usage("claude-sonnet-4-6", {
            "input_tokens": 100, "output_tokens": 20})
        assert u["provider_total_tokens"] is None
