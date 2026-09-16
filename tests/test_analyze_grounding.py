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

    @pytest.mark.parametrize("zh", ["根据标题，文章认为提款无需清仓。", "作者可能通过量化分析论证。",
                                    "讨论可能还会延伸到资产配置。", "从标题来看，本文讨论税务。"])
    def test_chinese_speculation_about_the_article_is_rejected(self, zh):
        r = dict(self.SUMMARY, summary_zh=zh)
        assert any("speculat" in p for p in aa.check_grounding(r, self.BODY))

    @pytest.mark.parametrize("zh", [
        "文章警示可能出现全球经济衰退。",          # man-group: reporting the article's warning
        "作者认为这很可能是噪音。",                # pimco: reporting the author's view
        "文章推测企业集团折价正在消失。",          # research-affiliates: the article's own conjecture
    ])
    def test_chinese_reporting_of_the_articles_view_is_not_speculation(self, zh):
        """False positives found when the first version ran over the corpus."""
        r = dict(self.SUMMARY, summary_zh=zh)
        assert aa.check_grounding(r, self.BODY) == []

    def test_an_expected_market_effect_is_not_speculation(self):
        """kkr: "This regulatory change is expected to address the supply-demand imbalance"."""
        r = dict(self.SUMMARY, summary_en=self.SUMMARY["summary_en"]
                 + " The rule change is expected to address the funding gap.")
        assert aa.check_grounding(r, self.BODY + " The rule change addresses the funding gap.") == []

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

    @pytest.mark.parametrize("summary", [
        # Published for lazard-am september-11-2026 (gpt-5.6-luna), passed e10d923's check:
        "The article, titled \u201cUS-Iran Escalation Risk Returns,\u201d provides no substantive "
        "macroeconomic analysis or specific market views beyond identifying renewed US-Iran "
        "geopolitical escalation risk. The remainder consists of publication details and extensive "
        "legal and informational disclaimers.",
        # Published for lazard-am june-26-2026 (gemini-2.5-pro), passed too:
        "The provided article from Lazard Asset Management consists solely of an introduction to a "
        "weekly macroeconomic analysis and a comprehensive legal disclaimer. Despite the title, the "
        "text contains no substantive market commentary, economic analysis, or investment views.",
        "The document does not contain the article's substantive analysis.",
    ])
    def test_a_summary_saying_there_is_nothing_to_summarise_is_rejected(self, summary):
        """Not invented, but a non-summary published as one: the "provided text"
        rule matched only text/content/material/document."""
        r = dict(self.SUMMARY, summary_en=summary)
        body = summary + " Important Information. This content represents the views of the author."
        assert any("not an article" in p for p in aa.check_grounding(r, body))

    @pytest.mark.parametrize("zh", ["美联储并未提供实质性指引。", "央行没有提供实质性的前瞻指引。", "谈判未包含实质内容。"])
    def test_chinese_market_negatives_pass(self, zh):
        """Rejected by the 06e658e rule, which had no subject requirement."""
        r = dict(self.SUMMARY, summary_zh=zh)
        assert aa.check_grounding(r, self.BODY) == []

    @pytest.mark.parametrize("zh", ["所提供的文章仅包含一段介绍和法律免责声明。", "文本并未包含实质性分析。"])
    def test_chinese_nothing_to_summarise_is_rejected(self, zh):
        r = dict(self.SUMMARY, summary_zh=zh)
        assert any("not an article" in p for p in aa.check_grounding(r, self.BODY))

    @pytest.mark.parametrize("summary", [
        "The Fed provides no specific guidance on the timing of rate cuts.",
        "Talks produced no substantive progress on tariffs.",
        "The index contains no Chinese A-shares after the rebalance.",
        # The first version of this test used "no specific guidance" and so
        # never exercised the word the 06e658e rule keys on. All four of these
        # were rejected by it -- and a rejection is permanent (not retried).
        "The Fed provides no substantive guidance on rate cuts.",
        "The ECB offers no substantive forward guidance.",
        "Management includes no substantive changes to the forecast.",
        "The article does not provide a specific price target but argues valuations are stretched.",
    ])
    def test_ordinary_negatives_about_markets_pass(self, summary):
        r = dict(self.SUMMARY, summary_en=self.SUMMARY["summary_en"] + " " + summary)
        assert aa.check_grounding(r, self.BODY + " " + summary) == []

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


class TestWordingOnlyRetry:
    """2026-09-16: franklin-templeton "The Limit Does Not Exist" -- a real
    9,287-char article -- was declined because its summary said "the provided
    text". The wording rule exists for pages that are not articles, and the
    declines it has to catch are not short (gmo 30,531 chars, franklin 10,061,
    apollo 4,234), so length cannot tell them apart. Instead: when the wording
    rule is the ONLY thing that fired -- the summary's own content words are in
    the article, so the model did read it -- ask the same model once more not
    to write about its input. The retry prompt still offers the refusal, and
    the answer is checked again.
    """
    GROUNDED = TestGroundingCheckRules.SUMMARY
    BODY = TestGroundingCheckRules.BODY
    WORDING = dict(GROUNDED, summary_en="The provided text explains that tax-aware long-short "
                                        "strategies fund withdrawals from the short book without "
                                        "liquidating long positions.")

    def _chain(self, monkeypatch, replies):
        """replies: {model: [reply, ...]} consumed in order."""
        calls, prompts = [], []

        def fake_openai(prompt, api_key, model="gpt-4.1-mini"):
            calls.append(model)
            prompts.append(prompt)
            queue = replies[model]
            return (queue.pop(0) if len(queue) > 1 else queue[0], {}, model)

        monkeypatch.setattr(aa, "_call_openai", fake_openai)
        monkeypatch.setattr(aa, "_call_gemini", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        monkeypatch.setattr(aa, "_append_usage_log", lambda *a, **k: None)
        return calls, prompts

    def test_a_wording_only_rejection_is_retried_once_with_the_same_model(self, monkeypatch):
        calls, prompts = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(self.WORDING), json.dumps(self.GROUNDED)],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert not out.get("insufficient_content")
        assert out["summary_en"] == self.GROUNDED["summary_en"]
        assert calls == ["gpt-5.6-luna", "gpt-5.6-luna"]
        assert "provided text" in prompts[1] and prompts[1] != prompts[0]

    def test_the_retry_may_still_decline_and_its_reason_is_kept(self, monkeypatch):
        """The escape hatch must survive the retry: a page that really is a
        disclaimer must not be talked into a summary."""
        calls, _ = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(self.WORDING),
                             '{"insufficient_content": true, "reason": "legal disclaimer only"}'],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True
        assert out["reason"] == "legal disclaimer only"
        assert calls == ["gpt-5.6-luna", "gpt-5.6-luna"]

    def test_a_second_wording_failure_is_final(self, monkeypatch):
        calls, _ = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(self.WORDING)],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True and "grounding" in out["reason"]
        assert calls == ["gpt-5.6-luna", "gpt-5.6-luna"]      # retried once, no fall-through

    def test_a_retry_that_fails_to_parse_keeps_the_rejection(self, monkeypatch):
        calls, _ = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(self.WORDING), "not json at all"],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True and "grounding" in out["reason"]
        assert calls == ["gpt-5.6-luna", "gpt-5.6-luna"]

    def test_a_coverage_failure_is_not_retried(self, monkeypatch):
        """Coverage failing means the summary is not in the text at all --
        asking again for different words is how a fabrication gets published."""
        case = CASES["fabricated_chart_notes_metlife"]
        calls, _ = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(_result(case)), json.dumps(self.GROUNDED)],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(case["content"], {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True
        assert calls == ["gpt-5.6-luna"]

    def test_wording_together_with_another_problem_is_not_retried(self, monkeypatch):
        both = dict(self.WORDING, key_takeaway_en="The author probably argues for tax awareness.")
        calls, _ = self._chain(monkeypatch, {
            "gpt-5.6-luna": [json.dumps(both), json.dumps(self.GROUNDED)],
            "gpt-4.1-mini": [json.dumps(self.GROUNDED)]})
        out = aa._analyze_with_fallback(self.BODY, {"OPENAI_API_KEY": "k"})
        assert out["insufficient_content"] is True
        assert calls == ["gpt-5.6-luna"]

    def test_the_retry_instruction_still_offers_the_refusal(self):
        assert "insufficient_content" in aa._WORDING_RETRY_INSTRUCTION


class TestRequeueAfterARuleChange:
    """A decline is permanent -- except the ones our own rule made. When the
    wording rule was loosened (2026-09-16) the article it had wrongly declined
    would otherwise have stayed title-only forever. Only grounding_failed is
    re-queued, once per version of analyze_articles.py; a model's own "this is
    not an article" stays permanent, because the text has not changed.
    """
    def _declined(self, label, code_version):
        return {"id": "a1", "summarized": False, "content_status": "ok",
                "analysis_status": aa.INSUFFICIENT, "analysis_label": label,
                "analysis_reason": "r", "analysis_code_version": code_version}

    def test_a_rule_rejection_from_older_code_is_analysed_again(self):
        assert aa._should_analyze(self._declined("grounding_failed", "oldversion12")) is True

    def test_a_rule_rejection_from_this_code_is_not(self):
        assert aa._should_analyze(self._declined("grounding_failed", aa.CODE_VERSION)) is False

    @pytest.mark.parametrize("label", ["title_only", "disclaimer_only", "wrong_document", "duplicate_body"])
    def test_a_model_decline_is_never_re_queued(self, label):
        assert aa._should_analyze(self._declined(label, "oldversion12")) is False

    def test_a_decline_records_the_code_version_that_made_it(self):
        article = {"id": "a1"}
        aa._record_insufficient(article, {"reason": "failed grounding check: x", "_model": "m"})
        assert article["analysis_code_version"] == aa.CODE_VERSION
        assert article["analysis_label"] == "grounding_failed"

    def test_a_later_success_clears_the_stamp(self, tmp_path, monkeypatch):
        """Otherwise a summarised row keeps a version stamp that means nothing."""
        art = dict(self._declined("grounding_failed", "oldversion12"), source_id="aqr",
                   title="T", url="https://x/1", date="2026-09-16")
        data = tmp_path / "articles.jsonl"
        data.write_text(json.dumps(art) + "\n")
        content = tmp_path / "content"
        content.mkdir()
        (content / "a1.txt").write_text(TestGroundingCheckRules.BODY)
        monkeypatch.setattr(aa, "DATA_FILE", data)
        monkeypatch.setattr(aa, "CONTENT_DIR", content)
        monkeypatch.setattr(aa, "_load_api_keys", lambda: {"OPENAI_API_KEY": "k"})
        monkeypatch.setattr(aa, "_analyze_with_fallback",
                            lambda *a, **k: dict(TestGroundingCheckRules.SUMMARY, _model="m", _usage={}))
        monkeypatch.setattr(sys, "argv", ["analyze_articles.py"])
        aa.main()
        row = json.loads(data.read_text().splitlines()[0])
        assert row["summarized"] is True and "analysis_code_version" not in row
        assert "analysis_status" not in row and "analysis_label" not in row

    def test_a_summarised_article_is_not_affected(self):
        assert aa._should_analyze(dict(self._declined("grounding_failed", "old"), summarized=True)) is False


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
        assert row["analysis_label"] in __import__("failure_labels").ANALYSIS_DECLINE_LABELS
        assert row.get("themes") in (None, []), "themes survived -- publish.py files by themes[0]"
        assert rc in (0, None), "a decline is a handled outcome, not an outage"

    def test_a_decline_carries_a_label(self, tmp_path, monkeypatch):
        """analysis_label is the countable form of analysis_reason (failure_labels)."""
        _, rows, _ = self._run(tmp_path, monkeypatch, [dict(self.ART)], {"a1": "Source: Bloomberg."},
                               {"insufficient_content": True, "_model": "gpt-5.6-luna", "_usage": {},
                                "reason": "The text consists only of repeated chart source notes"})
        assert rows[0]["analysis_label"] == "chart_notes_only"

    def test_a_decline_carries_a_label_for_title_only_metadata(self, tmp_path, monkeypatch):
        ark = dict(self.ART, id="ark9", source_id="ark-invest", content_status="metadata_only")
        _, rows, _ = self._run(tmp_path, monkeypatch, [ark], {"ark9": "Title: Embodied AI"},
                               {"summary_en": "x", "_model": "m", "_usage": {}})
        assert rows[0]["analysis_label"] == "title_only"

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
