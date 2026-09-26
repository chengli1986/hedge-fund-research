"""The census turns two maintenance rules into a scheduled report.

"Revisit at three" and "a knob with one user is a knob that should not
exist" were written into docs/listing-template-decisions.md and then
depended on me remembering them, which is not a mechanism. The census counts
instead, monthly, and exits non-zero only when there is something to act on
-- so the cron stays silent until it isn't.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("template_census", REPO / "scripts" / "template_census.py")
tc = importlib.util.module_from_spec(spec)
sys.modules["template_census"] = tc
spec.loader.exec_module(tc)


def _src(sid, shape=None, template=None, **kw):
    s = {"id": sid, "short_name": sid, **kw}
    if shape:
        s["fetch_shape"] = shape
    if template:
        s["listing_template"] = template
        s["listing_template_active"] = True
    return s


CARD = {"type": "card_list", "fetch": "requests", "card": "div", "link": "a"}


class TestRevisitAtThree:
    def test_two_of_a_shape_is_not_a_trigger(self):
        sources = [_src("a", "sitemap_plus_page"), _src("b", "sitemap_plus_page")]
        out = tc.census(sources)
        assert out["triggers"] == []

    def test_three_of_a_shape_with_no_template_triggers(self):
        sources = [_src(x, "sitemap_plus_page") for x in "abc"]
        out = tc.census(sources)
        assert len(out["triggers"]) == 1
        t = out["triggers"][0]
        assert t["shape"] == "sitemap_plus_page" and t["count"] == 3
        assert sorted(t["sources"]) == ["a", "b", "c"]

    def test_a_shape_that_already_has_a_template_does_not_trigger(self):
        """Four hand-written api_json sources exist and each is blocked for
        its own reason; the type is already built, so counting them is noise."""
        sources = [_src(x, "api_json") for x in "abcd"]
        assert tc.census(sources)["triggers"] == []

    def test_template_sources_are_not_counted_into_shapes(self):
        sources = [_src(x, None, CARD) for x in "abc"] + [_src("d", "sitemap_plus_page")]
        out = tc.census(sources)
        assert out["shapes"]["sitemap_plus_page"] == ["d"]
        assert "card_list" not in out["shapes"]


class TestUnclassified:
    def test_a_source_with_no_shape_is_reported(self):
        """auto-promote adds sources on its own; a new one needs a decision."""
        out = tc.census([_src("new-fund")])
        assert out["unclassified"] == ["new-fund"]
        assert any(t["kind"] == "unclassified" for t in out["triggers"])

    def test_an_unknown_shape_value_is_reported(self):
        out = tc.census([_src("odd", "something-made-up")])
        assert any(t["kind"] == "unknown_shape" for t in out["triggers"])


class TestKnobUsage:
    """The two-user rule is about knobs that describe page shape.

    params is what to ask the endpoint for, categories is which items of a
    feed we want, date_part is a mode of date_separator -- none of them is a
    shape knob, and flagging them every month is how a monthly report gets
    filtered into a folder nobody opens.
    """

    def test_a_request_knob_with_one_user_is_counted_but_not_flagged(self):
        sources = [_src("a", None, {"type": "api_json", "endpoint": "/x", "items": "i",
                                    "title": "t", "url": "u", "params": {"q": "1"}})]
        out = tc.census(sources)
        assert out["knobs"]["params"] == 1 and out["triggers"] == []

    def test_a_knob_nobody_classified_is_flagged(self):
        """A new knob must be declared a shape knob or not; the rule only
        means something if new knobs cannot slip past it unclassified."""
        out = tc.census([_src("a", None, dict(CARD, brand_new_knob="x"))])
        assert any(t["kind"] == "unclassified_knob" and t["knob"] == "brand_new_knob"
                   for t in out["triggers"])

    def test_a_single_user_knob_is_reported(self):
        sources = [_src("a", None, dict(CARD, path_prefix="/i/")),
                   _src("b", None, CARD)]
        out = tc.census(sources)
        assert out["knobs"]["path_prefix"] == 1
        assert any(t["kind"] == "single_user_knob" and t["knob"] == "path_prefix"
                   for t in out["triggers"])

    def test_a_knob_with_two_users_is_counted_but_not_flagged(self):
        sources = [_src(x, None, dict(CARD, path_prefix="/i/")) for x in "ab"]
        out = tc.census(sources)
        assert out["knobs"]["path_prefix"] == 2
        assert out["triggers"] == []

    def test_required_keys_are_not_knobs(self):
        out = tc.census([_src("a", None, CARD)])
        assert out["knobs"] == {}


class TestCoverage:
    def test_coverage_counts_by_template_type(self):
        sources = [_src("a", None, CARD), _src("b", None, CARD),
                   _src("c", None, {"type": "rss_feed", "feed": "url"}),
                   _src("d", "card_list")]
        out = tc.census(sources)
        assert out["coverage"] == {"card_list": 2, "rss_feed": 1}
        assert out["totals"] == {"sources": 4, "template": 3, "bespoke": 1}


class TestExitCode:
    """--no-record everywhere: main() appends to data/, which tests/conftest.py
    fails the session over (and rightly -- it caught this on the first run)."""

    def test_nothing_to_act_on_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(tc, "_load_sources",
                            lambda: [_src("a", None, CARD), _src("b", "attestation")])
        assert tc.main(["--no-record"]) == 0

    def test_a_trigger_exits_one_so_cron_mails_it(self, monkeypatch, capsys):
        monkeypatch.setattr(tc, "_load_sources",
                            lambda: [_src(x, "sitemap_plus_page") for x in "abc"])
        assert tc.main(["--no-record"]) == 1
        assert "sitemap_plus_page" in capsys.readouterr().out

    def test_recording_is_on_by_default(self, monkeypatch, tmp_path):
        """The snapshot is what makes the coverage trend visible."""
        monkeypatch.setattr(tc, "HISTORY", tmp_path / "census.jsonl")
        monkeypatch.setattr(tc, "_load_sources", lambda: [_src("a", None, CARD)])
        tc.main([])
        row = json.loads((tmp_path / "census.jsonl").read_text().splitlines()[0])
        assert row["template"] == 1 and row["sources"] == 1


class TestAgainstTheRealConfig:
    def test_every_production_source_is_classified(self):
        out = tc.census(json.loads((REPO / "config" / "sources.json").read_text())["sources"])
        assert out["unclassified"] == []

    def test_the_real_config_has_no_unknown_shape(self):
        out = tc.census(json.loads((REPO / "config" / "sources.json").read_text())["sources"])
        assert [t for t in out["triggers"] if t["kind"] == "unknown_shape"] == []
