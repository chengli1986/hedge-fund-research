"""Tests for scripts/validate_pending_profile.py — the gate that stops the
auto-graduate pipeline from writing unverified fund facts into publish.py.

Added 2026-05-29 after the auto-graduate path promoted confabulated AUM
figures (PineBridge ~$190B → actually ~$100B; Ares ~$450B → ~$484B) straight
into production because the old validator only checked *format*, never
*evidence* or *plausibility*. These tests pin the new guards:
  - aum_source / founded_source citation requirement (the real fix)
  - aum ↔ desc_zh internal-consistency check
  - aum magnitude plausibility band
  - expanded uncertainty markers
"""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "validate_pending_profile.py"

spec = importlib.util.spec_from_file_location("validate_pending_profile", SCRIPT)
vpp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpp)


def _profile(**overrides) -> dict:
    """A profile that passes ALL checks; override individual fields per test."""
    base = {
        "id": "example-fund",
        "founded": "2010",
        "aum": "~$100B",
        "hq": "New York, USA",
        "type_en": "Global Asset Manager",
        "type_zh": "全球资产管理公司",
        "desc_zh": ("全球资产管理公司（约 $100B AUM），总部位于纽约，亚太业务"
                    "渊源深厚，专注固定收益、多资产与主动股票，服务全球机构客户。"),
        "notable_en": "Notable english blurb.",
        "notable_zh": "中文备注说明。",
        "aum_source": "https://www.example.com/about/aum",
        "founded_source": "example.com",
    }
    base.update(overrides)
    return base


def test_valid_profile_passes():
    r = vpp.validate_profile(_profile())
    assert r["ok"], r["issues"]


# ───────────── evidence requirement (the core fix) ─────────────

def test_missing_aum_source_fails():
    r = vpp.validate_profile(_profile(aum_source=""))
    assert not r["ok"]
    assert any("aum_source" in i for i in r["issues"])


def test_missing_founded_source_fails():
    r = vpp.validate_profile(_profile(founded_source=""))
    assert not r["ok"]
    assert any("founded_source" in i for i in r["issues"])


def test_non_citation_source_fails():
    """A source that isn't a URL or domain (model just asserting it checked)
    must be rejected — that's the confabulation loophole."""
    r = vpp.validate_profile(_profile(aum_source="I verified this myself"))
    assert not r["ok"]
    assert any("not a citation" in i for i in r["issues"])


def test_full_url_source_passes():
    r = vpp.validate_profile(
        _profile(aum_source="https://www.metlife.com/newsroom/acquire-pinebridge",
                 founded_source="https://en.wikipedia.org/wiki/Example_Fund"))
    assert r["ok"], r["issues"]


def test_bare_domain_source_passes():
    r = vpp.validate_profile(_profile(aum_source="sec.gov", founded_source="deshaw.com"))
    assert r["ok"], r["issues"]


# ───────────── aum ↔ desc consistency ─────────────

def test_aum_desc_figure_mismatch_fails():
    # aum says 190B but desc says 100B → confabulation signal
    r = vpp.validate_profile(_profile(aum="~$190B"))
    assert not r["ok"]
    assert any("mismatch" in i for i in r["issues"])


def test_aum_desc_chinese_notation_no_false_positive():
    """desc written with Chinese 亿/万亿 (no $ token) must NOT trip the
    consistency check — we only compare when both sides have $ tokens."""
    r = vpp.validate_profile(_profile(
        desc_zh=("全球资产管理公司，管理约 1000 亿美元资产，总部位于纽约，"
                 "亚太业务渊源深厚，专注固定收益与多资产，服务全球机构客户。")))
    assert r["ok"], r["issues"]
    assert not any("mismatch" in i for i in r["issues"])


# ───────────── aum plausibility band ─────────────

def test_aum_too_large_fails():
    r = vpp.validate_profile(_profile(
        aum="~$50T",
        desc_zh=("某基金管理约 $50T 资产，总部位于纽约，规模极其庞大，覆盖全球"
                 "多资产策略，服务机构客户群体广泛深入。")))
    assert not r["ok"]
    assert any("implausible" in i for i in r["issues"])


def test_aum_too_small_fails():
    r = vpp.validate_profile(_profile(
        aum="$5M",
        desc_zh=("某小型基金管理约 $5M 资产，总部位于纽约，规模很小，覆盖单一"
                 "资产策略，服务少量机构客户群体相对集中。")))
    assert not r["ok"]
    assert any("implausible" in i for i in r["issues"])


def test_aum_with_qualifier_in_band_passes():
    """Real production values like '$15T+ benchmarked' parse the 15T part and
    must stay in band."""
    r = vpp.validate_profile(_profile(
        aum="$15T+ benchmarked",
        desc_zh=("全球领先的指数与因子提供商，约 $15T+ benchmarked 资产跟踪其"
                 "基准，总部位于纽约，覆盖多资产风险模型，服务全球机构客户。")))
    assert r["ok"], r["issues"]


# ───────────── expanded uncertainty markers ─────────────

def test_reportedly_marker_is_high_risk():
    r = vpp.validate_profile(_profile(notable_en="Reportedly the largest in its class."))
    assert r["high_risk"]


def test_chinese_speculation_marker_is_high_risk():
    r = vpp.validate_profile(_profile(notable_zh="据传该基金规模业内最大。"))
    assert r["high_risk"]
