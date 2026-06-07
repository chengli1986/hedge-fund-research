import importlib.util, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "validate_pending_profile.py"
spec = importlib.util.spec_from_file_location("vpp", SCRIPT)
vpp = importlib.util.module_from_spec(spec)
sys.modules["vpp"] = vpp
spec.loader.exec_module(vpp)

BASE = {
    "id": "apollo-global-management", "founded": "1990", "aum": "~$1.03T",
    "hq": "New York, NY", "type_en": "Listed Alt Manager (NYSE)",
    "type_zh": "上市另类资管 (纽交所)",
    "desc_zh": "美国最大私募信贷与另类资产管理机构之一，1990 年由 Leon Black 等人创立，业务涵盖私募信贷、私募股权、房地产与保险解决方案等多个核心领域。",
    "notable_en": "ABF pioneer.", "notable_zh": "ABF 开创者。",
    "aum_source": "https://sec.gov/x", "founded_source": "apollo.com",
}
CUR = {**BASE, "aum": "~$700B"}


def test_validate_refresh_ok_aum_change_with_source():
    data = {**BASE, "change_log": [
        {"field": "aum", "old": "~$700B", "new": "~$1.03T",
         "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}]}
    r = vpp.validate_refresh(data, current=CUR)
    assert r["ok"], r["issues"]


def test_validate_refresh_changelog_entry_without_source_fails():
    data = {**BASE, "change_log": [
        {"field": "aum", "old": "~$700B", "new": "~$1.03T", "reason": "x"}]}  # no source
    r = vpp.validate_refresh(data, current=CUR)
    assert not r["ok"]
    assert any("source" in i for i in r["issues"])


def test_validate_refresh_text_field_oversize_diff_fails():
    big = "完全不同的描述内容" * 12
    data = {**BASE, "desc_zh": big, "change_log": [
        {"field": "desc_zh", "old": CUR["desc_zh"], "new": big,
         "reason": "tweak", "source": "https://apollo.com/x"}]}
    r = vpp.validate_refresh(data, current=CUR)
    assert not r["ok"]
    assert any("diff too large" in i for i in r["issues"])


def test_validate_refresh_empty_changelog_fails():
    data = {**BASE, "change_log": []}
    r = vpp.validate_refresh(data, current=CUR)
    assert not r["ok"]
    assert any("change_log" in i for i in r["issues"])
