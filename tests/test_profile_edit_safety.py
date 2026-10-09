"""The monthly profile refresh rewrites publish.py itself, unattended.

Stage-4 audit (2026-10-09) reproduced four ways a draft could break the page or
slip past the gate while every script reported success:
  P1  a non-string value (true, 5) was written as a Python literal: publish.py
      then failed every night (NameError / AttributeError), draft archived;
  P2  the entry was located by counting braces, so a `}` inside a string
      made the NEXT apply cut the entry in half (IndentationError);
  P3  the 50% rewrite cap and the sources.json AUM sync both trusted the
      draft's own `old`: old == new waved a full rewrite through, and a wrong
      or tilde-less old left the two AUMs disagreeing or wrote "~~$800B".
Each test below failed on the code before the fix.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ar = _load("apply_refresh", REPO / "scripts" / "apply_refresh.py")
gp = _load("graduate_pending", REPO / "scripts" / "graduate_pending.py")
vpp = _load("validate_pending_profile", REPO / "scripts" / "validate_pending_profile.py")

DESC = "美国最大私募信贷与另类资产管理机构之一，1990 年创立，业务涵盖私募信贷、私募股权、房地产与保险解决方案等多个核心领域。"

# generate_html stands in for the real renderer: it does string work on every
# field (as publish.py does), and refuses a sentinel so a test can prove the
# trial render runs before publish.py is replaced.
PUBLISH = '''"""publish.py fixture."""
import html

_FUND_PROFILES: dict[str, dict] = {
    "apollo": {
        "founded": "1990", "aum": "~$700B", "hq": "New York, NY",
        "type_en": "Listed Alt Manager", "type_zh": "上市另类资管",
        "desc_zh": "%s",
        "notable_en": "ABF pioneer.",
        "notable_zh": "ABF 开创者。",
    },
    "kkr": {
        "founded": "1976", "aum": "~$758B", "hq": "New York, NY",
        "type_en": "Listed PE Manager", "type_zh": "上市私募股权",
        "desc_zh": "%s",
        "notable_en": "LBO pioneer.",
        "notable_zh": "杠杆收购先驱。",
    },
}


def generate_html(articles):
    out = []
    for fid, p in _FUND_PROFILES.items():
        if "RENDER-BOOM" in p["notable_en"]:
            raise RuntimeError("renderer refused this profile")
        out.append(" ".join(html.escape(p[k].replace("&", "&")) for k in p))
    return "\\n".join(out)
''' % (DESC, DESC)

SOURCES = {
    "sources": [
        {"id": "apollo", "description": "Private credit / alts platform (~$700B AUM). ABF."},
        {"id": "kkr", "description": "Global investment firm (~$758B AUM)."},
    ],
    "settings": {},
}
SRC = "https://www.sec.gov/x"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH, encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "sources.json").write_text(
        json.dumps(SOURCES, ensure_ascii=False, indent=2), encoding="utf-8")
    (tmp_path / "pending_profiles").mkdir()
    return tmp_path


def _draft(repo, fid, change_log, **top):
    (repo / "pending_profiles" / f"{fid}.refresh.json").write_text(
        json.dumps({"id": fid, "change_log": change_log, **top}, ensure_ascii=False))


def _profiles(repo):
    # exec the text, not an import: a same-size rewrite within one second would
    # otherwise be served from the stale __pycache__ entry.
    ns: dict = {}
    exec(compile((repo / "publish.py").read_text(encoding="utf-8"), "publish.py", "exec"), ns)
    return ns["_FUND_PROFILES"]


def _snapshot(repo):
    return ((repo / "publish.py").read_bytes(), (repo / "config" / "sources.json").read_bytes())


# ── P1: a value that is not a string never reaches publish.py ─────────────────

@pytest.mark.parametrize("bad", [True, 5, 1.5, ["x"], {"a": 1}])
def test_apply_refuses_a_value_that_is_not_a_string(repo, bad):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "type_en", "old": "Listed Alt Manager", "new": bad,
                             "reason": "rebrand", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo) != 0
    assert _snapshot(repo) == before
    assert (repo / "pending_profiles" / "apollo.refresh.json").exists(), "draft must not be archived"


def test_graduate_refuses_a_value_that_is_not_a_string(repo):
    before = _snapshot(repo)
    profile = {"id": "verdad", "founded": 2014, "aum": "~$3B", "hq": "Boston, MA",
               "type_en": "Quant Value", "type_zh": "量化价值", "desc_zh": DESC,
               "notable_en": "Small value.", "notable_zh": "小盘价值。",
               "aum_source": SRC, "founded_source": SRC}
    (repo / "pending_profiles" / "verdad.json").write_text(json.dumps(profile, ensure_ascii=False))
    assert gp.graduate("verdad", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_validator_names_the_field_that_is_not_a_string():
    issues = vpp.validate_profile({"aum": 5, "founded": "1990"})["issues"]
    assert any("aum" in i and "string" in i for i in issues), issues


# ── safety net: the rewritten publish.py must run before it replaces the old ──

def test_apply_keeps_publish_py_when_the_trial_render_fails(repo):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "notable_en", "old": "ABF pioneer.",
                             "new": "ABF pioneer. RENDER-BOOM", "reason": "add", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo) != 0
    assert _snapshot(repo) == before
    assert not [p for p in repo.iterdir() if p.name.startswith(".publish.py")], "candidate left behind"


def test_graduate_keeps_publish_py_when_the_trial_render_fails(repo):
    before = _snapshot(repo)
    profile = {"id": "verdad", "founded": "2014", "aum": "~$3B", "hq": "Boston, MA",
               "type_en": "Quant Value", "type_zh": "量化价值", "desc_zh": DESC,
               "notable_en": "RENDER-BOOM", "notable_zh": "小盘价值。",
               "aum_source": SRC, "founded_source": SRC}
    (repo / "pending_profiles" / "verdad.json").write_text(json.dumps(profile, ensure_ascii=False))
    assert gp.graduate("verdad", base_dir=repo) != 0
    assert _snapshot(repo) == before
    assert (repo / "pending_profiles" / "verdad.json").exists()


# ── P2: braces, quotes and backslashes inside values don't move the entry ─────

# one more `}` than `{`: a brace counter ends the entry inside the string
TRICKY = 'ABF pioneer (see {Apollo} forecasts} "quoted" \\ back'


def test_a_brace_in_a_value_survives_a_second_apply(repo):
    _draft(repo, "apollo", [{"field": "notable_en", "old": "ABF pioneer.", "new": TRICKY,
                             "reason": "rebrand", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo) == 0
    _draft(repo, "apollo", [{"field": "aum", "old": "~$700B", "new": "~$720B",
                             "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("apollo", base_dir=repo) == 0
    p = _profiles(repo)
    assert p["apollo"]["notable_en"] == TRICKY and p["apollo"]["aum"] == "~$720B"
    assert p["kkr"]["aum"] == "~$758B" and set(p) == {"apollo", "kkr"}


def test_graduate_inserts_correctly_after_a_value_with_a_brace(repo):
    _draft(repo, "kkr", [{"field": "notable_en", "old": "LBO pioneer.", "new": TRICKY,
                          "reason": "rebrand", "source": SRC}])
    assert ar.apply_refresh("kkr", base_dir=repo) == 0
    profile = {"id": "verdad", "founded": "2014", "aum": "~$3B", "hq": "Boston, MA",
               "type_en": "Quant Value", "type_zh": "量化价值", "desc_zh": DESC,
               "notable_en": "Small value.", "notable_zh": "小盘价值。",
               "aum_source": SRC, "founded_source": SRC}
    (repo / "pending_profiles" / "verdad.json").write_text(json.dumps(profile, ensure_ascii=False))
    assert gp.graduate("verdad", base_dir=repo) == 0
    p = _profiles(repo)
    assert list(p) == ["apollo", "kkr", "verdad"]
    assert p["kkr"]["notable_en"] == TRICKY and p["verdad"]["aum"] == "~$3B"


# ── P3: the draft's `old` is checked against publish.py, never trusted ────────

REWRITE = "Completely different sentence about something else entirely, written from scratch."


def test_old_equal_to_new_no_longer_bypasses_the_rewrite_cap(repo):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "notable_en", "old": REWRITE, "new": REWRITE,
                             "reason": "style", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_honest_old_is_still_capped(repo):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "notable_en", "old": "ABF pioneer.", "new": REWRITE,
                             "reason": "style", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_a_change_without_old_is_refused(repo):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "aum", "new": "~$720B", "reason": "Q2", "source": SRC}],
           aum_source=SRC)
    assert ar.apply_refresh("apollo", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_wrong_old_aum_is_refused_and_nothing_is_written(repo):
    before = _snapshot(repo)
    _draft(repo, "kkr", [{"field": "aum", "old": "~$750B", "new": "~$800B",
                          "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("kkr", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_tilde_less_old_aum_is_refused_rather_than_writing_a_double_tilde(repo):
    before = _snapshot(repo)
    _draft(repo, "kkr", [{"field": "aum", "old": "$758B", "new": "~$800B",
                          "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("kkr", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_aum_lands_in_both_places(repo):
    _draft(repo, "kkr", [{"field": "aum", "old": "~$758B", "new": "~$800B",
                          "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("kkr", base_dir=repo) == 0
    desc = next(s["description"] for s in json.loads((repo / "config" / "sources.json").read_text())["sources"]
                if s["id"] == "kkr")
    assert _profiles(repo)["kkr"]["aum"] == "~$800B"
    assert desc == "Global investment firm (~$800B AUM)."


def test_an_aum_the_description_disagrees_with_is_refused(repo):
    data = json.loads((repo / "config" / "sources.json").read_text())
    data["sources"][1]["description"] = "Global investment firm ($760B AUM)."
    (repo / "config" / "sources.json").write_text(json.dumps(data, indent=2))
    before = _snapshot(repo)
    _draft(repo, "kkr", [{"field": "aum", "old": "~$758B", "new": "~$800B",
                          "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("kkr", base_dir=repo) != 0
    assert _snapshot(repo) == before


def test_dry_run_still_runs_the_checks_and_writes_nothing(repo):
    before = _snapshot(repo)
    _draft(repo, "apollo", [{"field": "notable_en", "old": "ABF pioneer.",
                             "new": "ABF pioneer. RENDER-BOOM", "reason": "add", "source": SRC}])
    assert ar.apply_refresh("apollo", base_dir=repo, dry_run=True) != 0
    _draft(repo, "apollo", [{"field": "aum", "old": "~$700B", "new": "~$720B",
                             "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("apollo", base_dir=repo, dry_run=True) == 0
    assert _snapshot(repo) == before


# ── P4 (write side): the three unescaped card fields refuse markup ────────────

@pytest.mark.parametrize("field,value", [("aum", "~$700B<img src=x onerror=alert(1)>"),
                                         ("hq", "New York, NY<script>"),
                                         ("founded", "1990>")])
def test_validator_refuses_markup_in_the_card_stat_fields(field, value):
    issues = vpp.validate_profile({field: value})["issues"]
    assert any(field in i and "<" in i for i in issues), issues


def test_apply_refuses_markup_in_aum(repo):
    before = _snapshot(repo)
    _draft(repo, "kkr", [{"field": "aum", "old": "~$758B", "new": "~$800B<img src=x onerror=alert(1)>",
                          "reason": "Q2", "source": SRC}], aum_source=SRC)
    assert ar.apply_refresh("kkr", base_dir=repo) != 0
    assert _snapshot(repo) == before


# ── the shared locator/replacer, each layer on its own ───────────────────────

pe = _load("profile_edit", REPO / "scripts" / "profile_edit.py")


def test_format_entry_refuses_a_value_that_is_not_a_string():
    profile = {k: "x" for k in pe.PROFILE_FIELDS} | {"founded": 1990}
    with pytest.raises(ValueError, match="founded"):
        pe.format_entry("f", profile)


def test_replace_checked_refuses_source_that_does_not_compile(repo):
    before = _snapshot(repo)
    with pytest.raises(ValueError, match="compile"):
        pe.replace_checked(repo / "publish.py", PUBLISH + "\n  oops(\n", _profiles(repo))
    assert _snapshot(repo) == before


def test_replace_checked_refuses_profiles_other_than_intended(repo):
    before = _snapshot(repo)
    intended = _profiles(repo) | {"apollo": {**_profiles(repo)["apollo"], "aum": "~$1T"}}
    with pytest.raises(ValueError, match="intended"):
        pe.replace_checked(repo / "publish.py", PUBLISH, intended)
    assert _snapshot(repo) == before


def test_entry_span_ignores_braces_inside_strings():
    src = PUBLISH.replace('"ABF pioneer."', json.dumps(TRICKY))
    start, end = pe.entry_span(src, "apollo")
    block = src[start:end]
    assert block.startswith('    "apollo": {') and block.endswith("},\n")
    assert '"kkr"' not in block and TRICKY.replace("\\", "\\\\").replace('"', '\\"') in block
