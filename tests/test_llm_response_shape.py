"""An API answer that carries no text must say so, not raise IndexError.

D1 (2026-09-19) fixed exactly this for the Gemini client: a safety-filtered
request returns {"candidates": [], "promptFeedback": {"blockReason": ...}},
`candidates[0]` raised IndexError, and the model chain logged "list index
out of range" and retried -- a diagnosis that names nothing. Gemini was
removed on 2026-09-21 and the fix went with it, while both surviving
clients kept the same shape:

    _call_openai     data["choices"][0]["message"]["content"]
    _call_anthropic  data["content"][0]["text"]

Both are on the production path -- MODEL_CHAIN is two OpenAI models, and
Anthropic is the configured fallback. Behaviour is unchanged: the chain
still retries and still moves on. What changes is what the log says.
"""
import json

import pytest

import analyze_articles as aa


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _post(monkeypatch, payload):
    monkeypatch.setattr(aa.requests, "post", lambda *a, **k: _Resp(payload))


class TestOpenAI:
    OK = {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 1}}

    def test_a_normal_answer_still_comes_back(self, monkeypatch):
        _post(monkeypatch, self.OK)
        text, usage, model = aa._call_openai("p", "k")
        assert text == "hello" and usage == {"prompt_tokens": 1}

    def test_no_choices_at_all_names_what_came_back(self, monkeypatch):
        _post(monkeypatch, {"choices": [], "id": "chatcmpl-1"})
        with pytest.raises(ValueError, match="no choices"):
            aa._call_openai("p", "k")

    def test_a_refusal_names_the_refusal(self, monkeypatch):
        """The list is not empty: content is null and the reason sits beside it."""
        _post(monkeypatch, {"choices": [{"message": {"content": None,
                                                     "refusal": "I can't help with that"},
                                         "finish_reason": "content_filter"}]})
        with pytest.raises(ValueError, match="I can't help with that"):
            aa._call_openai("p", "k")

    def test_a_null_content_without_a_refusal_names_the_finish_reason(self, monkeypatch):
        _post(monkeypatch, {"choices": [{"message": {"content": None},
                                         "finish_reason": "length"}]})
        with pytest.raises(ValueError, match="length"):
            aa._call_openai("p", "k")

    def test_the_error_is_not_an_index_error(self, monkeypatch):
        """The whole point: "list index out of range" names nothing."""
        _post(monkeypatch, {"choices": []})
        with pytest.raises(Exception) as got:
            aa._call_openai("p", "k")
        assert not isinstance(got.value, IndexError)


class TestAnthropic:
    OK = {"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 1}}

    def test_a_normal_answer_still_comes_back(self, monkeypatch):
        _post(monkeypatch, self.OK)
        text, usage, model = aa._call_anthropic("p", "k")
        assert text == "hello"

    def test_no_content_blocks_names_the_stop_reason(self, monkeypatch):
        _post(monkeypatch, {"content": [], "stop_reason": "max_tokens"})
        with pytest.raises(ValueError, match="max_tokens"):
            aa._call_anthropic("p", "k")

    def test_a_block_that_is_not_text_names_its_type(self, monkeypatch):
        _post(monkeypatch, {"content": [{"type": "tool_use", "id": "t1"}],
                            "stop_reason": "tool_use"})
        with pytest.raises(ValueError, match="tool_use"):
            aa._call_anthropic("p", "k")


class TestTheChainStillCopes:
    def test_a_shaped_failure_is_retried_and_moves_on_like_any_other(self, monkeypatch):
        """Unchanged behaviour: the chain logs, retries, tries the next model."""
        calls = []

        def boom(prompt, key, model="gpt-4.1-mini"):
            calls.append(model)
            raise ValueError("openai returned no choices (id=chatcmpl-1)")

        monkeypatch.setattr(aa, "_call_openai", boom)
        monkeypatch.setattr(aa, "_call_anthropic", boom)
        out = aa._analyze_with_fallback("body", {"OPENAI_API_KEY": "k"}, title="t",
                                        source="s", date="2026-09-27")
        assert out is None
        assert len(calls) >= 2, "it must have retried rather than given up at once"
