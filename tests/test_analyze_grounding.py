"""Stage 3 must not publish a summary the article text does not support.

Found 2026-09-13. When stage 2 saved navigation, a disclaimer or chart source
notes instead of an article, the model had no way to decline and wrote a
summary anyway -- from the title. Examples from the published page:

  research-affiliates  1,326 chars of navigation -> "The author likely uses
                       quantitative analysis ... The discussion probably extends"
  franklin-templeton   5,293 chars of risk disclosures -> "Based on the
                       article's title, the analysis likely argues ..."
  metlife-im           357 chars of chart source notes -> a full bullish thesis
                       on Japanese equities, with no hedging word at all
  lazard-am            intro + disclaimer -> "The provided text ... is a
                       standard legal disclaimer", published as the summary

So a keyword filter alone cannot be the fix (metlife has no tell). Three
independent layers, each tested here:
  1. the prompt forbids inference from the title and gives the model a way
     to decline ({"insufficient_content": true, "reason": ...});
  2. every summary is checked against the text deterministically -- its
     content words must mostly occur in the article, and it must not
     speculate about the article or describe the input instead of it;
  3. metadata with nothing but a title is never sent to a model.
A declined or rejected article is recorded with its reason, is not retried
nightly, and is not published as summarised.

The fixtures are real articles and the summaries that were published for them.
"""

import json
import sys
from pathlib import Path

import pytest

import analyze_articles as aa

# tests/grounding, not tests/fixtures: fixtures/*.json is gitignored because
# those files are re-downloadable (download_fixtures.sh), and any "data/" is
# ignored too. These are not re-downloadable -- they are the summaries that
# were published and are removed from the corpus.
CASES = json.loads((Path(__file__).parent / "grounding" / "grounding_cases.json").read_text())


def _result(case):
    return {k: case[k] for k in ("summary_en", "summary_zh", "key_takeaway_en",
                                 "key_takeaway_zh", "themes")}


# ── layer 2: deterministic check against the text ──────────────────────────

class TestGroundingCheckOnRealCases:
    @pytest.mark.parametrize("name", [
        "fabricated_chart_notes_metlife",
        "fabricated_risk_disclosure_franklin",
        "fabricated_nav_research_affiliates",
        "described_disclaimer_lazard",
    ])
    def test_published_fabrications_are_rejected(self, name):
        case = CASES[name]
        problems = aa.check_grounding(_result(case), case["content"])
        assert problems, f"{name}: a summary the text does not support passed the check"

    @pytest.mark.parametrize("name", [
        "legit_paraphrase_resonanz",   # heavy paraphrase, coverage ~0.31
        "legit_chart_numbers_gmo",     # body is mostly a chart's numbers
        "legit_short_teaser_robeco",   # 267-char teaser, summarised faithfully
    ])
    def test_faithful_summaries_pass(self, name):
        case = CASES[name]
        assert aa.check_grounding(_result(case), case["content"]) == []

    def test_metlife_is_caught_by_coverage_not_by_wording(self):
        """It carries no speculative phrase; only the coverage layer sees it."""
        case = CASES["fabricated_chart_notes_metlife"]
        problems = aa.check_grounding(_result(case), case["content"])
        assert any("coverage" in p for p in problems), problems


class TestGroundingCheckRules:
    BODY = ("Tax-aware long-short strategies balance pre-tax returns with realised "
            "losses that offset gains elsewhere in the portfolio. Withdrawals can be "
            "funded from the short book without liquidating long positions.")
    SUMMARY = {"summary_en": "Tax-aware long-short strategies can fund withdrawals from "
                             "the short book without liquidating long positions, while "
                             "realised losses offset gains elsewhere.",
               "key_takeaway_en": "Withdrawals need not force liquidation.",
               "summary_zh": "摘要", "key_takeaway_zh": "要点", "themes": ["Quant/Factor"]}

    def test_a_supported_summary_passes(self):
        assert aa.check_grounding(dict(self.SUMMARY), self.BODY) == []

    @pytest.mark.parametrize("phrase", [
        "Based on the title, the paper argues that withdrawals need not force liquidation.",
        "The article likely discusses withdrawals from the short book.",
        "The author probably uses realised losses to offset gains.",
        "The discussion probably extends to long positions.",
        "The title suggests that withdrawals are funded from the short book.",
    ])
    def test_speculation_about_the_article_is_rejected(self, phrase):
        r = dict(self.SUMMARY, summary_en=self.SUMMARY["summary_en"] + " " + phrase)
        assert any("speculat" in p for p in aa.check_grounding(r, self.BODY))

    @pytest.mark.parametrize("zh", ["根据标题，文章认为提款无需清仓。", "作者可能使用了量化分析。", "从标题来看，本文讨论税务。"])
    def test_chinese_speculation_about_the_article_is_rejected(self, zh):
        r = dict(self.SUMMARY, summary_zh=zh)
        assert any("speculat" in p for p in aa.check_grounding(r, self.BODY))

    def test_market_hedging_is_not_speculation_about_the_article(self):
        """"may"/"可能" about markets is normal analysis, not a fabrication tell."""
        r = dict(self.SUMMARY,
                 summary_en=self.SUMMARY["summary_en"] + " Realised losses may offset gains elsewhere.",
                 summary_zh="利率可能继续上升，市场可能波动。")
        assert aa.check_grounding(r, self.BODY) == []

    @pytest.mark.parametrize("phrase", [
        "The provided text is a standard legal disclaimer.",
        "The text does not contain the article itself.",
        "提供的文本是一份免责声明。",
    ])
    def test_describing_the_input_instead_of_an_article_is_rejected(self, phrase):
        r = dict(self.SUMMARY, summary_en=phrase + " " + self.SUMMARY["summary_en"])
        if not phrase.isascii():
            r = dict(self.SUMMARY, summary_zh=phrase)
        assert any("not an article" in p for p in aa.check_grounding(r, self.BODY))

    def test_coverage_is_skipped_for_a_non_latin_article(self):
        """A Japanese or Chinese source summarised in English shares almost no
        words with its text; the coverage layer cannot judge it, the others still apply."""
        body = "株主価値最大化の鍵はバランスシートの適正化にあり。" * 20
        r = dict(self.SUMMARY, summary_en="Japanese companies are optimising balance sheets.")
        assert aa.check_grounding(r, body) == []
        r["summary_en"] += " Based on the title, the article argues this."
        assert aa.check_grounding(r, body)


# ── layer 1: the model can decline, and the prompt says so ─────────────────

class TestTheModelCanDecline:
    def test_a_refusal_parses(self):
        out = aa._parse_llm_output('{"insufficient_content": true, "reason": "only navigation"}')
        assert out == {"insufficient_content": True, "reason": "only navigation"}

    def test_a_refusal_without_a_reason_still_parses(self):
        out = aa._parse_llm_output('{"insufficient_content": true}')
        assert out["insufficient_content"] is True and out["reason"]

    @pytest.mark.parametrize("template", ["ANALYSIS_PROMPT", "METADATA_PROMPT"])
    def test_both_prompts_forbid_title_inference_and_offer_the_refusal(self, template):
        prompt = getattr(aa, template)
        assert '"insufficient_content": true' in prompt
        assert "title" in prompt.lower() and "never" in prompt.lower()

    def test_metadata_prompt_no_longer_invites_title_based_analysis(self):
        assert "keep analysis conservative" not in aa.METADATA_PROMPT


class TestFallbackChainOnDecline:
    GROUNDED = TestGroundingCheckRules.SUMMARY
    BODY = TestGroundingCheckRules.BODY

    def _chain(self, monkeypatch, replies):
        calls = []

        def fake_openai(prompt, api_key, model="gpt-4.1-mini"):
            calls.append(model)
            return (replies[model], {}, model)

        monkeypatch.setattr(aa, "_call_openai", fake_openai)
        monkeypatch.setattr(aa, "_call_gemini", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(aa, "_append_usage_log", lambda *a, **k: None)
        return calls

    def test_a_refusal_is_final_and_does_not_fall_through(self, monkeypatch):
        """Another tier would be asked the same impossible question -- and a
        weaker model is the one likelier to invent an answer."""
        calls = self._chain(monkeypatch, {
            "gpt-5.6-luna": '{"insufficient_content": true, "reason": "navigation only"}',
            "gpt-4.1-mini": json.dumps(self.GROUNDED)})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True
        assert calls == ["gpt-5.6-luna"]

    def test_an_ungrounded_summary_is_rejected_and_does_not_fall_through(self, monkeypatch):
        case = CASES["fabricated_chart_notes_metlife"]
        calls = self._chain(monkeypatch, {
            "gpt-5.6-luna": json.dumps(_result(case)),
            "gpt-4.1-mini": json.dumps(self.GROUNDED)})
        out = aa._analyze_with_fallback(case["content"], {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True
        assert "grounding" in out["reason"]
        assert calls == ["gpt-5.6-luna"]

    def test_a_grounded_summary_is_returned(self, monkeypatch):
        self._chain(monkeypatch, {"gpt-5.6-luna": json.dumps(self.GROUNDED)})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert not out.get("insufficient_content")
        assert out["summary_en"] == self.GROUNDED["summary_en"]


# ── main(): recording, not retrying, not publishing; layer 3 ───────────────

class TestMainRecordsDeclines:
    ART = {"id": "a1", "source_id": "metlife-im", "title": "Japan Equities", "url": "https://x/1",
           "date": "2026-09-01", "content_status": "ok", "summarized": False}

    def _run(self, tmp_path, monkeypatch, articles, bodies, result):
        data = tmp_path / "articles.jsonl"
        data.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in articles))
        content = tmp_path / "content"
        content.mkdir()
        for a in articles:
            (content / f"{a['id']}.txt").write_text(bodies[a["id"]])
        calls = []

        def fake(content_, *a, **k):
            calls.append(k.get("article_id"))
            return dict(result)

        monkeypatch.setattr(aa, "DATA_FILE", data)
        monkeypatch.setattr(aa, "CONTENT_DIR", content)
        monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
        monkeypatch.setattr(aa, "_analyze_with_fallback", fake)
        monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
        rc = aa.main()
        rows = [json.loads(l) for l in data.read_text().splitlines()]
        return rc, rows, calls

    def test_a_decline_is_recorded_and_not_published(self, tmp_path, monkeypatch):
        stale = dict(self.ART, summarized=True, summary_en="old invented text",
                     summary_zh="旧", key_takeaway_en="k", key_takeaway_zh="k",
                     themes=["Equities/Value"])
        stale["summarized"] = False  # re-queued for analysis, e.g. by the backfill
        rc, rows, _ = self._run(tmp_path, monkeypatch, [stale], {"a1": "Source: Bloomberg."},
                                {"insufficient_content": True, "reason": "chart notes only",
                                 "_model": "gpt-5.6-luna", "_usage": {}})
        row = rows[0]
        assert row["summarized"] is False
        assert row["analysis_status"] == "insufficient_content"
        assert "chart notes only" in row["analysis_reason"]
        for field in ("summary_en", "summary_zh", "key_takeaway_en", "key_takeaway_zh"):
            assert not row.get(field), f"{field} survived a decline"
        assert row.get("themes") in (None, []), "themes survived -- publish.py files by themes[0]"
        assert rc in (0, None), "a decline is a handled outcome, not an outage"

    def test_a_declined_article_is_not_retried(self):
        assert aa._should_analyze(dict(self.ART, analysis_status="insufficient_content")) is False

    def test_title_only_metadata_is_never_sent_to_a_model(self, tmp_path, monkeypatch):
        ark = dict(self.ART, id="ark1", source_id="ark-invest", content_status="metadata_only",
                   title="From Robotaxis To Humanoids")
        rc, rows, calls = self._run(tmp_path, monkeypatch, [ark],
                                    {"ark1": "Title: From Robotaxis To Humanoids: Why Embodied AI Represents A Leap"},
                                    {"summary_en": "invented", "_model": "m", "_usage": {}})
        assert calls == [], "a title was sent to a model to be summarised"
        assert rows[0]["analysis_status"] == "insufficient_content"
        assert "title" in rows[0]["analysis_reason"]
        assert rows[0]["summarized"] is False

    def test_metadata_with_a_real_description_is_still_analysed(self, tmp_path, monkeypatch):
        ark = dict(self.ART, id="ark2", source_id="ark-invest", content_status="metadata_only")
        body = "Title: Embodied AI\n" + "ARK estimates humanoid robots could address a large labour market. " * 5
        _, _, calls = self._run(tmp_path, monkeypatch, [ark], {"ark2": body},
                                {"insufficient_content": True, "reason": "r", "_model": "m", "_usage": {}})
        assert calls == ["ark2"]
