import importlib.util, json, sys
from pathlib import Path
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "apply_refresh.py"
spec = importlib.util.spec_from_file_location("apply_refresh", SCRIPT)
ar = importlib.util.module_from_spec(spec)
sys.modules["apply_refresh"] = ar
spec.loader.exec_module(ar)

PUBLISH_TEMPLATE = '''"""publish.py fixture."""

_FUND_PROFILES: dict[str, dict] = {
    "man-group": {
        "founded": "1783", "aum": "~$175B", "hq": "London, UK",
        "type_en": "Listed HF Manager", "type_zh": "上市对冲基金",
        "desc_zh": "Man Group 描述，至少五十个字符以满足校验下限要求要求要求要求要求。",
        "notable_en": "Notable EN.",
        "notable_zh": "Notable ZH.",
    },
    "apollo-global-management": {
        "founded": "1990", "aum": "~$700B", "hq": "New York, NY",
        "type_en": "Listed Alt Manager (NYSE)", "type_zh": "上市另类资管 (纽交所)",
        "desc_zh": "美国最大私募信贷与另类资产管理机构之一，1990 年创立，业务涵盖私募信贷、私募股权、房地产与保险解决方案等多个核心领域。",
        "notable_en": "ABF pioneer.",
        "notable_zh": "ABF 开创者。",
    },
}

def render():
    return _FUND_PROFILES
'''

SOURCES_TEMPLATE = '''{
  "sources": [
    {"id": "apollo-global-management", "name": "Apollo", "description": "Largest US private credit / alts platform (~$700B). Pioneered ABF."},
    {"id": "man-group", "name": "Man", "description": "Multi-strategy: macro, quant, credit."}
  ],
  "settings": {}
}'''


def _write_draft(tmp_path, fund_id, fields, change_log):
    pdir = tmp_path / "pending_profiles"
    pdir.mkdir(exist_ok=True)
    draft = {"id": fund_id, **fields, "change_log": change_log}
    (pdir / f"{fund_id}.refresh.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2))
    return pdir


def test_find_entry_block_locates_existing(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    src = (tmp_path / "publish.py").read_text()
    span = ar._find_entry_block(src, "apollo-global-management")
    assert span is not None
    start, end = span
    block = src[start:end]
    assert '"apollo-global-management": {' in block
    assert block.rstrip().endswith("},")


def test_find_entry_block_missing_returns_none(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    src = (tmp_path / "publish.py").read_text()
    assert ar._find_entry_block(src, "verdad-capital") is None


def test_apply_updates_only_changelog_fields(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    rc = ar.apply_refresh("apollo-global-management", base_dir=tmp_path)
    assert rc == 0
    s2 = importlib.util.spec_from_file_location("pub2", tmp_path / "publish.py")
    mod = importlib.util.module_from_spec(s2); s2.loader.exec_module(mod)
    p = mod.render()["apollo-global-management"]
    assert p["aum"] == "~$1.03T"
    assert p["founded"] == "1990"
    assert p["desc_zh"].startswith("美国最大私募信贷")
    assert mod.render()["man-group"]["aum"] == "~$175B"
    assert not (tmp_path / "pending_profiles" / "apollo-global-management.refresh.json").exists()


def test_apply_refuses_missing_fund(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    _write_draft(tmp_path, "verdad-capital",
                 {"aum": "~$1B", "aum_source": "https://hedgefundalpha.com/x"},
                 [{"field": "aum", "old": "~$500M", "new": "~$1B",
                   "reason": "10yr", "source": "https://hedgefundalpha.com/x"}])
    rc = ar.apply_refresh("verdad-capital", base_dir=tmp_path)
    assert rc == ar.EXIT_NOT_PRESENT


def test_apply_dry_run_does_not_write(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    before = (tmp_path / "publish.py").read_text()
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    rc = ar.apply_refresh("apollo-global-management", base_dir=tmp_path, dry_run=True)
    assert rc == ar.EXIT_OK
    assert (tmp_path / "publish.py").read_text() == before
    assert (tmp_path / "pending_profiles" / "apollo-global-management.refresh.json").exists()


def test_apply_syncs_sources_json_aum(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "sources.json").write_text(SOURCES_TEMPLATE)
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    rc = ar.apply_refresh("apollo-global-management", base_dir=tmp_path)
    assert rc == 0
    data = json.loads((cfg / "sources.json").read_text())
    apollo = next(s for s in data["sources"] if s["id"] == "apollo-global-management")
    assert "~$1.03T" in apollo["description"]
    assert "~$700B" not in apollo["description"]
    man = next(s for s in data["sources"] if s["id"] == "man-group")
    assert man["description"] == "Multi-strategy: macro, quant, credit."


def test_apply_leaves_all_unchanged_fields_byte_identical(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    s0 = importlib.util.spec_from_file_location("pub0", tmp_path / "publish.py")
    m0 = importlib.util.module_from_spec(s0); s0.loader.exec_module(m0)
    orig = dict(m0.render()["apollo-global-management"])
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    assert ar.apply_refresh("apollo-global-management", base_dir=tmp_path) == 0
    s1 = importlib.util.spec_from_file_location("pub1b", tmp_path / "publish.py")
    m1 = importlib.util.module_from_spec(s1); s1.loader.exec_module(m1)
    after = m1.render()["apollo-global-management"]
    assert after["aum"] == "~$1.03T"
    for f in ("founded", "hq", "type_en", "type_zh", "desc_zh", "notable_en", "notable_zh"):
        assert after[f] == orig[f], f"{f} changed unexpectedly"


def test_apply_uses_changelog_when_top_level_field_absent(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum_source": "https://sec.gov/x"},  # no top-level aum key
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    assert ar.apply_refresh("apollo-global-management", base_dir=tmp_path) == 0
    s1 = importlib.util.spec_from_file_location("pub_cl", tmp_path / "publish.py")
    m1 = importlib.util.module_from_spec(s1); s1.loader.exec_module(m1)
    assert m1.render()["apollo-global-management"]["aum"] == "~$1.03T"


def test_apply_rejects_malformed_changelog_entry(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    pdir = tmp_path / "pending_profiles"; pdir.mkdir(exist_ok=True)
    (pdir / "apollo-global-management.refresh.json").write_text(
        json.dumps({"id": "apollo-global-management",
                    "change_log": [{"old": "~$700B", "new": "~$1.03T"}]}))  # no "field"
    assert ar.apply_refresh("apollo-global-management", base_dir=tmp_path) == ar.EXIT_VALIDATION_FAILED


def test_apply_event_text_field_change(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    s0 = importlib.util.spec_from_file_location("pub_e0", tmp_path / "publish.py")
    m0 = importlib.util.module_from_spec(s0); s0.loader.exec_module(m0)
    old_desc = m0.render()["apollo-global-management"]["desc_zh"]
    new_desc = old_desc + " 2026 年完成对某平台的收购。"
    _write_draft(tmp_path, "apollo-global-management",
                 {"desc_zh": new_desc, "aum_source": "https://apollo.com/x"},
                 [{"field": "desc_zh", "old": old_desc, "new": new_desc,
                   "reason": "收购 acquisition completed", "source": "https://apollo.com/x"}])
    assert ar.apply_refresh("apollo-global-management", base_dir=tmp_path) == 0
    s1 = importlib.util.spec_from_file_location("pub_e1", tmp_path / "publish.py")
    m1 = importlib.util.module_from_spec(s1); s1.loader.exec_module(m1)
    assert m1.render()["apollo-global-management"]["desc_zh"] == new_desc
