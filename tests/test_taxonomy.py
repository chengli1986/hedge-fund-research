"""The 41-tag taxonomy: the rules the model's answer is held to."""
import re
from pathlib import Path

import pytest

import taxonomy as tx

GOOD = {"article_type": "event", "regions": ["us"], "assets": ["govt_bonds"],
        "topics": ["monetary_policy", "growth_inflation"], "methods": []}


def test_five_groups_and_forty_one_tags():
    assert list(tx.GROUPS) == ["article_type", "regions", "assets", "topics", "methods"]
    assert len(tx.ALL_TAGS) == 41


def test_every_tag_is_offered_to_the_model():
    text = tx.instruction()
    for tid in tx.ALL_TAGS:
        assert f'"{tid}":' in text


def test_a_good_answer_passes_and_flattens_type_first():
    assert tx.validate(GOOD) == []
    assert tx.flatten(GOOD) == ["event", "us", "govt_bonds", "monetary_policy", "growth_inflation"]


@pytest.mark.parametrize("change", [
    {"article_type": None},                          # missing type
    {"article_type": ["event"]},                     # type must be one id, not a list
    {"regions": []},                                 # at least one region
    {"regions": ["us", "europe", "japan"]},          # at most two
    {"regions": ["global", "us"]},                   # global stands alone
    {"assets": ["equities", "credit", "fx", "commodities"]},
    {"topics": ["AI"]},                              # near miss is not guessed
    {"topics": ["ai_tech", "ai_tech"]},
    {"methods": ["factors_style", "behavioral", "active_passive"]},
])
def test_broken_answers_are_rejected(change):
    assert tx.validate({**GOOD, **change}) != []


def test_not_an_object_is_rejected():
    assert tx.validate(["event"]) == ["not an object"]


def test_the_spec_document_lists_every_chinese_name():
    doc = (Path(__file__).resolve().parent.parent / "docs" / "tag-taxonomy.md").read_text(encoding="utf-8")
    cells = set(re.findall(r"^\|\s*([^|]+?)\s*\|", doc, re.M))
    missing = [zh for _, (_, _, tags) in tx.GROUPS.items() for (_, zh, _) in tags.values() if zh not in cells]
    assert missing == []


TEXT = "Intro.\nThe 10-year Treasury yield rose 20 basis points as \u201cduration\u201d sold\noff \u2014 sharply. Then other things."


def _ans(**ev):
    return dict(GOOD, evidence=ev)


def test_a_passage_copied_from_the_text_keeps_the_tag_despite_punctuation_and_line_breaks():
    kept, dropped = tx.check_evidence(
        _ans(govt_bonds='The 10-year Treasury yield rose 20 basis points as "duration" sold off - sharply',
             monetary_policy="x", growth_inflation=""), TEXT)
    assert kept["assets"] == ["govt_bonds"]
    assert kept["topics"] == [] and len(dropped) == 2


@pytest.mark.parametrize("passage, reason", [
    ("Treasury yields fell as investors bought long bonds", "not in document"),   # invented
    ("Treasury yield rose", "too short"),                                         # too short to prove anything
    ("The 10-year Treasury yield ... sold off sharply", "not in document"),       # ellipsis is not a copy
])
def test_an_unsupported_passage_drops_only_that_tag(passage, reason):
    kept, dropped = tx.check_evidence(_ans(govt_bonds=passage), TEXT)
    assert kept["assets"] == [] and any(d.startswith("govt_bonds") and reason in d for d in dropped)
    assert kept["article_type"] == "event" and kept["regions"] == ["us"]      # type/region need none


def test_malformed_evidence_is_rejected():
    assert tx.validate(dict(GOOD, evidence=["not", "a", "dict"])) == ["evidence malformed"]


def test_words_the_scraper_glued_together_still_match():
    text = "Inflation outlookThe fight against inflation looks increasingly difficult as a manufacturing revival.Central banks"
    kept, _ = tx.check_evidence(_ans(growth_inflation="The fight against inflation looks increasingly difficult as a "
                                                      "manufacturing revival. Central banks"), text)
    assert kept["topics"] == ["growth_inflation"]
