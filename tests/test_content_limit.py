"""How much of an article the model reads (2026-10-06).

MAX_CONTENT_CHARS was 15,000. 305 of 1,592 summarised articles (19%) were
longer, so the model saw on average 67% of them and at worst 8%, and for a
good share of them what it never saw was argument, not boilerplate: a
36,041-char Rothschild update was summarised from its first 42%. Measured
the same day: luna read a 77,071-char article whole in 10.8 s (timeout 120 s)
and its summary passed the grounding check; raising the limit to 80,000 costs
about 13% more tokens a month. 80,000 covers 304 of the 305 -- the one left
out is a 187,062-char corporate responsibility report.
"""
import json

import analyze_articles as aa

GROUNDED = {"summary_en": "Withdrawals are funded from the short book.", "summary_zh": "提款来自空头部分。",
            "themes": ["Asset Allocation"], "key_takeaway_en": "Short book funds withdrawals.",
            "key_takeaway_zh": "空头部分支付提款。"}


def _body_with_markers():
    filler = "Withdrawals are funded from the short book without selling longs. "
    body = (filler * 2000)[:90000]
    body = body[:60000] + " MARKER_AT_SIXTY_THOUSAND " + body[60000:85000] + " MARKER_PAST_EIGHTY " + body[85000:]
    assert 60000 <= body.find("MARKER_AT_SIXTY") < 80000 < body.find("MARKER_PAST_EIGHTY"), "markers misplaced"
    return body


def test_the_limit_is_eighty_thousand():
    assert aa.MAX_CONTENT_CHARS == 80000


def test_text_past_fifteen_thousand_reaches_the_model(monkeypatch):
    prompts = []

    def fake(prompt, api_key, model="gpt-4.1-mini"):
        prompts.append(prompt)
        return json.dumps(GROUNDED), {}, model

    monkeypatch.setattr(aa, "_call_openai", fake)
    monkeypatch.setattr(aa, "_append_usage_log", lambda *a, **k: None)
    aa._analyze_with_fallback(_body_with_markers(), {"OPENAI_API_KEY": "k"})
    assert prompts, "the model was never asked"
    assert "MARKER_AT_SIXTY_THOUSAND" in prompts[0], "text at 60,000 chars never reached the model"
    assert "MARKER_PAST_EIGHTY" not in prompts[0], "the limit must still cut somewhere"


def test_the_grounding_check_reads_what_the_model_read(monkeypatch):
    """A summary of the second half must be checked against the second half."""
    seen = []
    real = aa.check_grounding

    def spy(result, content):
        seen.append(content)
        return real(result, content)

    monkeypatch.setattr(aa, "check_grounding", spy)
    monkeypatch.setattr(aa, "_call_openai", lambda p, k, model="gpt-4.1-mini": (json.dumps(GROUNDED), {}, model))
    monkeypatch.setattr(aa, "_append_usage_log", lambda *a, **k: None)
    aa._analyze_with_fallback(_body_with_markers(), {"OPENAI_API_KEY": "k"})
    assert seen and "MARKER_AT_SIXTY_THOUSAND" in seen[0]
