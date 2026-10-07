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
