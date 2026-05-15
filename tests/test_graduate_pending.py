"""Tests for scripts/graduate_pending.py — moves a pending profile draft into
publish._FUND_PROFILES so the Sources-tab card renders proper Est./AUM/HQ
instead of `—` after auto-promote.

Closes lifecycle gap that bit KKR (fixed 5-12 8a6b574) and Research Affiliates
(fixed 5-15 a05699f) — both required manual publish.py edits + delete of
pending_profiles/<id>.json. This helper does both in one step.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "graduate_pending.py"

spec = importlib.util.spec_from_file_location("graduate_pending", SCRIPT)
gp = importlib.util.module_from_spec(spec)
sys.modules["graduate_pending"] = gp
spec.loader.exec_module(gp)


PUBLISH_TEMPLATE = '''"""publish.py fixture for tests."""

_FUND_PROFILES: dict[str, dict] = {
    "man-group": {
        "founded": "1783", "aum": "~$175B", "hq": "London, UK",
        "type_en": "Listed HF Manager", "type_zh": "上市对冲基金",
        "desc_zh": "Man Group 描述。",
        "notable_en": "Notable EN.",
        "notable_zh": "Notable ZH.",
    },
    "janus-henderson": {
        "founded": "1969", "aum": "~$370B", "hq": "London, UK / Denver, CO",
        "type_en": "Active Asset Manager (NYSE)", "type_zh": "主动管理型资管 (纽交所)",
        "desc_zh": "Janus 描述。",
        "notable_en": "Notable JH EN.",
        "notable_zh": "Notable JH ZH.",
    },
}


def render():
    return _FUND_PROFILES
'''


def _setup(tmp_path: Path, fund_id: str = "research-affiliates",
           profile_overrides: dict | None = None,
           include_pending: bool = True) -> Path:
    """Build a fake repo at tmp_path with publish.py + optional pending profile."""
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)

    if include_pending:
        pending_dir = tmp_path / "pending_profiles"
        pending_dir.mkdir()
        profile = {
            "id": fund_id,
            "founded": "2002",
            "aum": "~$170B",
            "hq": "Newport Beach, CA",
            "type_en": "Quantitative Research / Index Licensor",
            "type_zh": "量化研究 / 指数授权机构",
            "desc_zh": "Newport Beach 量化投资研究机构，2002 年由 Rob Arnott 创立。开创 RAFI 基本面指数化。授权 Smart Beta 策略至约 1700 亿美元资产。",
            "notable_en": "Founded by Rob Arnott (2002); pioneered fundamental indexation.",
            "notable_zh": "Rob Arnott 2002 年创立；首创基本面指数。",
            "_generated_at": "2026-05-15T22:00:00+08:00",
            "_confidence_notes": "AUM may be outdated.",
        }
        if profile_overrides:
            profile.update(profile_overrides)
        (pending_dir / f"{fund_id}.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2))
        (pending_dir / f"{fund_id}.validation.json").write_text('{"ok": true, "issues": []}')

    return tmp_path


def test_graduate_inserts_entry_and_deletes_pending(tmp_path):
    _setup(tmp_path)
    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 0

    text = (tmp_path / "publish.py").read_text()
    assert '"research-affiliates": {' in text
    assert '"founded": "2002"' in text
    assert '"hq": "Newport Beach, CA"' in text
    assert '"notable_en": "Founded by Rob Arnott (2002); pioneered fundamental indexation."' in text
    # _generated_at and _confidence_notes are meta fields, must NOT leak
    assert "_generated_at" not in text
    assert "_confidence_notes" not in text
    # ordering: new entry sits between last existing entry and closing brace
    pos_jh = text.index('"janus-henderson":')
    pos_ra = text.index('"research-affiliates":')
    assert pos_jh < pos_ra

    # pending files cleaned up
    assert not (tmp_path / "pending_profiles" / "research-affiliates.json").exists()
    assert not (tmp_path / "pending_profiles" / "research-affiliates.validation.json").exists()


def test_graduate_loads_back_as_valid_python(tmp_path):
    """Sanity: generated publish.py must parse + render correctly."""
    _setup(tmp_path)
    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 0

    # exec the produced module in isolation
    spec_new = importlib.util.spec_from_file_location("pub_after", tmp_path / "publish.py")
    mod = importlib.util.module_from_spec(spec_new)
    spec_new.loader.exec_module(mod)
    profiles = mod.render()
    assert "research-affiliates" in profiles
    assert profiles["research-affiliates"]["founded"] == "2002"
    assert profiles["research-affiliates"]["aum"] == "~$170B"
    # existing entries untouched
    assert profiles["man-group"]["founded"] == "1783"
    assert profiles["janus-henderson"]["aum"] == "~$370B"


def test_graduate_refuses_when_already_in_FUND_PROFILES(tmp_path):
    _setup(tmp_path)
    # pre-fill: graduate once
    assert gp.graduate("research-affiliates", base_dir=tmp_path) == 0

    # re-create pending and try again — should refuse
    pending_dir = tmp_path / "pending_profiles"
    pending_dir.mkdir(exist_ok=True)
    (pending_dir / "research-affiliates.json").write_text(json.dumps({
        "id": "research-affiliates",
        "founded": "2002", "aum": "~$170B", "hq": "Newport Beach, CA",
        "type_en": "X", "type_zh": "X",
        "desc_zh": "Newport Beach 量化投资研究机构，2002 年由 Rob Arnott 创立。开创 RAFI 基本面指数化。授权 Smart Beta 策略至约 1700 亿美元资产。",
        "notable_en": "X", "notable_zh": "X",
    }, ensure_ascii=False))

    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 2  # ALREADY_PRESENT
    # pending file kept on rejection (so user can inspect)
    assert (pending_dir / "research-affiliates.json").exists()


def test_graduate_refuses_when_pending_missing(tmp_path):
    _setup(tmp_path, include_pending=False)
    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 3  # PENDING_NOT_FOUND
    # publish.py untouched
    text = (tmp_path / "publish.py").read_text()
    assert '"research-affiliates"' not in text


def test_graduate_refuses_when_pending_invalid(tmp_path):
    _setup(tmp_path, profile_overrides={"aum": "lots of money"})  # no currency symbol
    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 1  # VALIDATION_FAILED
    # pending file kept on rejection
    assert (tmp_path / "pending_profiles" / "research-affiliates.json").exists()
    # publish.py untouched
    text = (tmp_path / "publish.py").read_text()
    assert '"research-affiliates"' not in text


def test_graduate_accepts_high_risk_only(tmp_path):
    """validate_pending_profile distinguishes high_risk markers (unknown/unclear)
    from hard violations (no currency symbol). high_risk should be ACCEPTED with
    a warning, mirroring auto-promote Phase 4 policy."""
    _setup(tmp_path, profile_overrides={"_confidence_notes": "AUM unknown as of 2026"})
    rc = gp.graduate("research-affiliates", base_dir=tmp_path)
    assert rc == 0  # high_risk in _confidence_notes only, not in required fields
