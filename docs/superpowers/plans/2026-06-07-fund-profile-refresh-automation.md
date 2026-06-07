# 月度基金 Profile 自动刷新 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每月用 headless Claude（Max Plan）核验 28 个生产基金 profile 的时效字段（AUM + 公司事件），过证据闸门则自动改 `publish.py`/`sources.json` 并发布，否则发告警邮件。

**Architecture:** 复用 GMIA auto-promote 的证据闸门（`validate_pending_profile.py`）。新增「更新现有条目」路径 `apply_refresh.py`（补 `graduate_pending.py` 只插不改的缺口），新增 change_log 校验 + 静态事实守卫 + 刷新汇总邮件，cron wrapper 沿用现有 `unset ANTHROPIC_API_KEY → claude --print` Max Plan 模式。分阶段：dry-run → 告警模式 1-2 月 → 自动应用。

**Tech Stack:** Python 3.12（stdlib only：json/re/importlib/argparse/pathlib/smtplib/email）、Bash 5.2、pytest、Claude Code CLI（`~/.npm-global/bin/claude` 2.x）、cron-wrapper.sh。

**Spec:** `docs/superpowers/specs/2026-06-07-fund-profile-refresh-automation-design.md`

---

## 文件结构

| 文件 | 职责 | 新增/修改 |
|------|------|-----------|
| `scripts/apply_refresh.py` | 把 `<id>.refresh.json` 应用到现有 `_FUND_PROFILES` 条目（仅改 change_log 字段）+ 同步 sources.json AUM | Create |
| `scripts/validate_pending_profile.py` | 加 `validate_refresh()`：闸门 + change_log 校验 + 文本 diff 守卫 | Modify |
| `scripts/send_refresh_summary.py` | 渲染并发送刷新汇总 HTML 邮件 | Create |
| `auto-promote/refresh-program.md` | agent「刷新模式」指令 | Create |
| `scripts/wrapper-profile-refresh.sh` | cron wrapper（auth/lock/timeout/trap）+ 编排 | Create |
| `tests/test_apply_refresh.py` | apply_refresh 单测 | Create |
| `tests/test_validate_refresh.py` | validate_refresh 单测 | Create |
| `tests/test_profile_static_facts.py` | 静态事实守卫 | Create |
| `tests/test_send_refresh_summary.py` | 邮件渲染单测 | Create |

---

### Task 1: `apply_refresh.py` — 定位现有条目

**Files:**
- Create: `scripts/apply_refresh.py`
- Test: `tests/test_apply_refresh.py`

- [ ] **Step 1: 写失败测试 — 定位 + 缺失 fund 返回 None**

```python
# tests/test_apply_refresh.py
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
        "desc_zh": "美国最大私募信贷/另类资管之一，1990 年创立，业务涵盖私募信贷与私募股权及房地产等。",
        "notable_en": "ABF pioneer.",
        "notable_zh": "ABF 开创者。",
    },
}

def render():
    return _FUND_PROFILES
'''


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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_apply_refresh.py -q`
Expected: FAIL（`apply_refresh.py` 不存在 / `_find_entry_block` 未定义）

- [ ] **Step 3: 写最小实现（模块骨架 + `_find_entry_block`）**

```python
# scripts/apply_refresh.py
"""apply_refresh.py — apply a monthly profile-refresh draft to an EXISTING
publish._FUND_PROFILES entry (and sync config/sources.json AUM if embedded).

Companion to graduate_pending.py (which only INSERTS new funds and refuses to
overwrite). This UPDATES an existing entry, replacing only the fields listed in
the draft's change_log — minimal diff, so hand-audited static facts are never
touched unless explicitly changed with a cited source.

Usage:
    python3 scripts/apply_refresh.py <fund-id> [--base-dir DIR] [--dry-run]

Exit codes:
    0 — success (publish.py + sources.json updated, draft archived)
    1 — validation failed (gate or change_log)
    3 — pending_profiles/<id>.refresh.json not found
    4 — fund-id NOT present in publish._FUND_PROFILES (use graduate_pending)
    5 — publish.py format unexpected
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_DRAFT_NOT_FOUND = 3
EXIT_NOT_PRESENT = 4
EXIT_FORMAT_UNEXPECTED = 5

PROFILE_FIELDS = ("founded", "aum", "hq", "type_en", "type_zh",
                  "desc_zh", "notable_en", "notable_zh")


def _find_entry_block(source: str, fund_id: str) -> tuple[int, int] | None:
    """Return (start, end) char span of the existing `"<id>": { ... },` entry
    in _FUND_PROFILES, or None if absent. `end` includes the trailing comma and
    newline so the span can be cleanly replaced."""
    key = json.dumps(fund_id)  # e.g. "apollo-global-management" with quotes
    m = re.search(rf'^[ \t]*{re.escape(key)}\s*:\s*\{{', source, re.M)
    if not m:
        return None
    start = m.start()
    open_pos = source.index("{", m.end() - 1)
    depth, i = 1, open_pos + 1
    while i < len(source) and depth > 0:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    if depth != 0:
        raise ValueError("unbalanced braces locating entry")
    end = i + 1
    if source[end:end + 1] == ",":
        end += 1
    nl = source.find("\n", end)
    if nl != -1:
        end = nl + 1
    return start, end
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_apply_refresh.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
git add scripts/apply_refresh.py tests/test_apply_refresh.py
git commit -m "feat(apply_refresh): locate existing _FUND_PROFILES entry block"
```

---

### Task 2: `apply_refresh.py` — 应用 change_log 更新现有条目

**Files:**
- Modify: `scripts/apply_refresh.py`
- Test: `tests/test_apply_refresh.py`

- [ ] **Step 1: 写失败测试 — 只改 change_log 字段，其余不动**

```python
# 追加到 tests/test_apply_refresh.py
def _write_draft(tmp_path, fund_id, fields, change_log):
    pdir = tmp_path / "pending_profiles"
    pdir.mkdir(exist_ok=True)
    draft = {"id": fund_id, **fields, "change_log": change_log}
    (pdir / f"{fund_id}.refresh.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2))
    return pdir


def test_apply_updates_only_changelog_fields(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    rc = ar.apply_refresh("apollo-global-management", base_dir=tmp_path)
    assert rc == 0
    # reload module to read new values
    s2 = importlib.util.spec_from_file_location("pub2", tmp_path / "publish.py")
    mod = importlib.util.module_from_spec(s2); s2.loader.exec_module(mod)
    p = mod.render()["apollo-global-management"]
    assert p["aum"] == "~$1.03T"                 # changed
    assert p["founded"] == "1990"                # untouched static fact
    assert p["desc_zh"].startswith("美国最大私募信贷")  # untouched
    # other fund untouched
    assert mod.render()["man-group"]["aum"] == "~$175B"
    # draft archived (not left in place)
    assert not (tmp_path / "pending_profiles" / "apollo-global-management.refresh.json").exists()


def test_apply_refuses_missing_fund(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    _write_draft(tmp_path, "verdad-capital",
                 {"aum": "~$1B", "aum_source": "https://hedgefundalpha.com/x"},
                 [{"field": "aum", "old": "~$500M", "new": "~$1B",
                   "reason": "10yr", "source": "https://hedgefundalpha.com/x"}])
    rc = ar.apply_refresh("verdad-capital", base_dir=tmp_path)
    assert rc == EXIT_NOT_PRESENT


def test_apply_dry_run_does_not_write(tmp_path):
    (tmp_path / "publish.py").write_text(PUBLISH_TEMPLATE)
    before = (tmp_path / "publish.py").read_text()
    _write_draft(tmp_path, "apollo-global-management",
                 {"aum": "~$1.03T", "aum_source": "https://sec.gov/x"},
                 [{"field": "aum", "old": "~$700B", "new": "~$1.03T",
                   "reason": "Q1 2026 8-K", "source": "https://sec.gov/x"}])
    rc = ar.apply_refresh("apollo-global-management", base_dir=tmp_path, dry_run=True)
    assert rc == EXIT_OK
    assert (tmp_path / "publish.py").read_text() == before  # unchanged
    assert (tmp_path / "pending_profiles" / "apollo-global-management.refresh.json").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_apply_refresh.py -q`
Expected: FAIL（`apply_refresh` 未定义）

- [ ] **Step 3: 实现 `apply_refresh` + 复用 graduate 的格式化器**

```python
# 追加到 scripts/apply_refresh.py
def _load_publish_profiles(base: Path) -> dict:
    """exec publish.py in isolation and return its _FUND_PROFILES dict."""
    spec = importlib.util.spec_from_file_location("_pub_for_apply", base / "publish.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod._FUND_PROFILES)


def _load_validate_module(base: Path):
    path = base / "scripts" / "validate_pending_profile.py"
    if not path.exists():
        path = REPO_DEFAULT / "scripts" / "validate_pending_profile.py"
    spec = importlib.util.spec_from_file_location("_vpp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _format_entry(fund_id: str, profile: dict) -> str:
    """publish.py-style 5-line compact entry (matches graduate_pending)."""
    def lit(s: str) -> str:
        return json.dumps(s, ensure_ascii=False)
    return (
        f'    {lit(fund_id)}: {{\n'
        f'        "founded": {lit(profile["founded"])}, '
        f'"aum": {lit(profile["aum"])}, '
        f'"hq": {lit(profile["hq"])},\n'
        f'        "type_en": {lit(profile["type_en"])}, '
        f'"type_zh": {lit(profile["type_zh"])},\n'
        f'        "desc_zh": {lit(profile["desc_zh"])},\n'
        f'        "notable_en": {lit(profile["notable_en"])},\n'
        f'        "notable_zh": {lit(profile["notable_zh"])},\n'
        f'    }},\n'
    )


def apply_refresh(fund_id: str, *, base_dir: Path | None = None,
                  dry_run: bool = False) -> int:
    base = Path(base_dir) if base_dir else REPO_DEFAULT
    draft_path = base / "pending_profiles" / f"{fund_id}.refresh.json"
    publish_path = base / "publish.py"

    if not draft_path.exists():
        sys.stderr.write(f"[apply_refresh] no draft at {draft_path}\n")
        return EXIT_DRAFT_NOT_FOUND
    try:
        draft = json.loads(draft_path.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[apply_refresh] draft JSON malformed: {e}\n")
        return EXIT_VALIDATION_FAILED

    current_profiles = _load_publish_profiles(base)
    if fund_id not in current_profiles:
        sys.stderr.write(f"[apply_refresh] {fund_id!r} not in _FUND_PROFILES "
                         f"(use graduate_pending for new funds)\n")
        return EXIT_NOT_PRESENT

    change_log = draft.get("change_log", [])
    changed_fields = {c["field"] for c in change_log}
    merged = dict(current_profiles[fund_id])
    for field in changed_fields:
        if field in draft:
            merged[field] = draft[field]

    # gate: validate the merged full profile (+ change_log via validate_refresh)
    vpp = _load_validate_module(base)
    result = vpp.validate_refresh({**merged, "id": fund_id,
                                   "change_log": change_log,
                                   "aum_source": draft.get("aum_source", ""),
                                   "founded_source": draft.get("founded_source", "")},
                                  current=current_profiles[fund_id])
    hard = [m for m in result["issues"] if not m.startswith("high_risk_marker")]
    if hard:
        sys.stderr.write(f"[apply_refresh] validation failed: {hard}\n")
        return EXIT_VALIDATION_FAILED

    source = publish_path.read_text()
    span = _find_entry_block(source, fund_id)
    if span is None:
        sys.stderr.write("[apply_refresh] could not locate entry block\n")
        return EXIT_FORMAT_UNEXPECTED
    start, end = span
    new_source = source[:start] + _format_entry(fund_id, merged) + source[end:]

    if dry_run:
        sys.stdout.write(f"[apply_refresh] DRY-RUN {fund_id}: would change "
                         f"{sorted(changed_fields)}\n")
        return EXIT_OK

    publish_path.write_text(new_source)
    # sources.json AUM sync handled in Task 2 follow-up (sync_sources_aum)
    _archive_draft(draft_path)
    sys.stdout.write(f"[apply_refresh] {fund_id}: applied {sorted(changed_fields)}\n")
    return EXIT_OK


def _archive_draft(draft_path: Path) -> None:
    applied = draft_path.parent / "applied"
    applied.mkdir(exist_ok=True)
    draft_path.replace(applied / draft_path.name)


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply a profile-refresh draft to an existing fund")
    ap.add_argument("fund_id")
    ap.add_argument("--base-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return apply_refresh(args.fund_id, base_dir=args.base_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
```

> NOTE: `validate_refresh` is defined in Task 4. Until then, these tests depend on it — implement Task 4 before running Step 4 here, or temporarily stub `validate_refresh` to call `validate_profile`. Recommended order: Task 4 first, then Task 2 Step 4. (Document the dependency; do Task 4 now.)

- [ ] **Step 4: （在 Task 4 完成后）跑测试确认通过**

Run: `python3 -m pytest tests/test_apply_refresh.py -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交**

```bash
git add scripts/apply_refresh.py tests/test_apply_refresh.py
git commit -m "feat(apply_refresh): update existing entry from change_log (minimal diff + dry-run)"
```

---

### Task 3: `apply_refresh.py` — sources.json AUM 同步

**Files:**
- Modify: `scripts/apply_refresh.py`
- Test: `tests/test_apply_refresh.py`

> 动机（本次会话踩过的坑）：9 只基金的英文 `sc-desc` 来自 `config/sources.json` 的 `description`，内嵌 AUM 数字。改 AUM 不同步 → sc-stat 与 sc-desc 矛盾。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_apply_refresh.py
SOURCES_TEMPLATE = '''{
  "sources": [
    {"id": "apollo-global-management", "name": "Apollo", "description": "Largest US private credit / alts platform (~$700B). Pioneered ABF."},
    {"id": "man-group", "name": "Man", "description": "Multi-strategy: macro, quant, credit."}
  ],
  "settings": {}
}'''


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
    import json as _j
    data = _j.loads((cfg / "sources.json").read_text())
    apollo = next(s for s in data["sources"] if s["id"] == "apollo-global-management")
    assert "~$1.03T" in apollo["description"]
    assert "~$700B" not in apollo["description"]
    # other source untouched
    man = next(s for s in data["sources"] if s["id"] == "man-group")
    assert man["description"] == "Multi-strategy: macro, quant, credit."
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_apply_refresh.py::test_apply_syncs_sources_json_aum -q`
Expected: FAIL（描述里仍是 ~$700B）

- [ ] **Step 3: 实现 sync + 接到 apply_refresh**

```python
# 追加到 scripts/apply_refresh.py
def _sync_sources_aum(base: Path, fund_id: str, old_aum: str, new_aum: str) -> bool:
    """If config/sources.json[fund_id].description embeds old_aum, replace with
    new_aum. Returns True if the file was changed. Plain substring replace —
    descriptions are short and the AUM token is unique within one description."""
    src_path = base / "config" / "sources.json"
    if not src_path.exists():
        return False
    data = json.loads(src_path.read_text())
    changed = False
    for s in data.get("sources", []):
        if s.get("id") == fund_id and old_aum and old_aum in s.get("description", ""):
            s["description"] = s["description"].replace(old_aum, new_aum)
            changed = True
    if changed:
        src_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return changed
```

在 `apply_refresh()` 写完 publish.py 之后、archive 之前插入：

```python
    # sync sources.json embedded AUM (the sc-desc lang-en figure)
    aum_change = next((c for c in change_log if c["field"] == "aum"), None)
    if aum_change:
        _sync_sources_aum(base, fund_id, aum_change.get("old", ""),
                          aum_change.get("new", ""))
```

> 注意：现有 `config/sources.json` 缩进/格式与 `json.dumps(indent=2)` 不完全一致。生产中改用「定点子串替换」而非整体重写以保留格式——见 Step 3b。

- [ ] **Step 3b: 改为定点子串替换（保留原文件格式，避免整档 reflow）**

```python
# 用文本替换版替换 _sync_sources_aum 的写回逻辑：
def _sync_sources_aum(base: Path, fund_id: str, old_aum: str, new_aum: str) -> bool:
    src_path = base / "config" / "sources.json"
    if not src_path.exists() or not old_aum:
        return False
    text = src_path.read_text()
    data = json.loads(text)  # validate target exists + figure is in its desc
    target = next((s for s in data.get("sources", [])
                   if s.get("id") == fund_id and old_aum in s.get("description", "")), None)
    if target is None:
        return False
    # the AUM token is unique enough within the file when combined with desc text;
    # replace only within the target description's JSON-encoded string.
    enc_old = json.dumps(target["description"], ensure_ascii=False)
    enc_new = json.dumps(target["description"].replace(old_aum, new_aum), ensure_ascii=False)
    if enc_old not in text:
        return False
    src_path.write_text(text.replace(enc_old, enc_new, 1))
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_apply_refresh.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/apply_refresh.py tests/test_apply_refresh.py
git commit -m "feat(apply_refresh): sync embedded AUM in sources.json description"
```

---

### Task 4: `validate_refresh()` — change_log + 文本 diff 守卫

**Files:**
- Modify: `scripts/validate_pending_profile.py`
- Test: `tests/test_validate_refresh.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_validate_refresh.py
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
    "desc_zh": "美国最大私募信贷/另类资管之一，1990 年创立，业务涵盖私募信贷与私募股权及房地产等等等。",
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
    # rewriting desc_zh wholesale without an event reason -> blocked
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_validate_refresh.py -q`
Expected: FAIL（`validate_refresh` 未定义）

- [ ] **Step 3: 实现 `validate_refresh`（复用 `validate_profile` + 增量校验）**

```python
# 追加到 scripts/validate_pending_profile.py（在 validate_profile 之后）

# desc/notable 类字段无「公司事件理由」时允许的最大改动比例（防止整段重写）
TEXT_FIELDS = ("desc_zh", "notable_en", "notable_zh", "type_en", "type_zh")
MAX_TEXT_DIFF_RATIO = 0.5
EVENT_KEYWORDS = ("acqui", "merg", "take-private", "delist", "rename", "rebrand",
                  "spun out", "spin-off", "并购", "收购", "退市", "私有化", "改名", "更名", "合并")


def _diff_ratio(old: str, new: str) -> float:
    """Crude change ratio: symmetric char delta over max length."""
    import difflib
    if not old and not new:
        return 0.0
    sm = difflib.SequenceMatcher(None, old or "", new or "")
    return 1.0 - sm.ratio()


def validate_refresh(data: dict, *, current: dict) -> dict:
    """Validate a refresh draft against the CURRENT profile.

    Runs the full validate_profile gate on the merged profile, then adds:
      - change_log must be non-empty
      - every change_log entry needs field/old/new/source; source must be a citation
      - text-field rewrites with no event keyword in `reason` are capped at
        MAX_TEXT_DIFF_RATIO (blocks wholesale prose rewrites of stable copy)
    """
    result = validate_profile(data)
    issues = list(result["issues"])

    change_log = data.get("change_log")
    if not change_log:
        issues.append("change_log is empty (a refresh must list its changes)")
    else:
        for c in change_log:
            field = c.get("field", "")
            if field not in (*REQUIRED_FIELDS, "aum", "founded"):
                issues.append(f"change_log: unknown field {field!r}")
            src = str(c.get("source", "")).strip()
            if not src or not _CITATION_RE.search(src):
                issues.append(f"change_log[{field}]: missing/weak source (need URL/domain)")
            if field in TEXT_FIELDS:
                reason = str(c.get("reason", "")).lower()
                has_event = any(k in reason for k in EVENT_KEYWORDS)
                ratio = _diff_ratio(str(c.get("old", "")), str(c.get("new", "")))
                if not has_event and ratio > MAX_TEXT_DIFF_RATIO:
                    issues.append(
                        f"change_log[{field}]: diff too large ({ratio:.0%}) without a "
                        f"corporate-event reason — route to human")

    return {"ok": len(issues) == 0, "issues": issues,
            "high_risk": result["high_risk"],
            "missing_fields": result["missing_fields"]}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_validate_refresh.py -q`
Expected: PASS（4 passed）

- [ ] **Step 5: 跑全量回归（确认未破坏现有闸门）**

Run: `python3 -m pytest tests/ -q`
Expected: 全部 passed（原 510 + 新增）

- [ ] **Step 6: 提交**

```bash
git add scripts/validate_pending_profile.py tests/test_validate_refresh.py
git commit -m "feat(validate): add validate_refresh (change_log + source + text-diff guard)"
```

---

### Task 5: 静态事实守卫测试

**Files:**
- Test: `tests/test_profile_static_facts.py`

- [ ] **Step 1: 写守卫测试（直接断言生产 publish.py 内容）**

```python
# tests/test_profile_static_facts.py
"""Guard: hand-audited static facts must never be silently regressed by the
monthly refresh (or any future edit). Asserts known-correct tokens are present
and known-wrong tokens never reappear in production publish._FUND_PROFILES."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("publish_real", REPO / "publish.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
PROFILES = pub._FUND_PROFILES

MUST_CONTAIN = {
    "gmo": ("desc_zh", "Eyk van Otterloo"),
}
MUST_NOT_CONTAIN = [
    "Nicholas Otis",          # GMO wrong founder
    "Hudson River Trading",   # D.E. Shaw wrong alumnus claim
    "英国最大独立",            # Aberdeen overclaim
    "全球最大另类信贷",        # Oaktree overclaim
]


def test_required_static_facts_present():
    for fund_id, (field, token) in MUST_CONTAIN.items():
        assert token in PROFILES[fund_id][field], f"{fund_id}.{field} lost {token!r}"


def test_regressed_facts_never_reappear():
    blob = "\n".join(
        f"{v.get('desc_zh','')} {v.get('notable_en','')} {v.get('notable_zh','')}"
        for v in PROFILES.values())
    for bad in MUST_NOT_CONTAIN:
        assert bad not in blob, f"regressed fact reappeared: {bad!r}"
```

- [ ] **Step 2: 跑测试确认通过（生产 publish.py 已是正确状态）**

Run: `python3 -m pytest tests/test_profile_static_facts.py -q`
Expected: PASS（2 passed）

- [ ] **Step 3: 提交**

```bash
git add tests/test_profile_static_facts.py
git commit -m "test(guard): static-fact regression guard for fund profiles"
```

---

### Task 6: `auto-promote/refresh-program.md` — agent 刷新模式指令

**Files:**
- Create: `auto-promote/refresh-program.md`

- [ ] **Step 1: 写指令文件（完整内容如下）**

````markdown
# GMIA Profile Refresh — Agent Program (monthly)

You are refreshing the **time-sensitive** facts of the 28 production fund
profiles that render the Sources tab of hedge-fund-research.html.

## Hard rules
1. ONLY touch time-sensitive fields: `aum` (and the embedded AUM figure in the
   English description), plus corporate events that change `type_en`/`type_zh`/
   `desc_zh`/`notable_*` (M&A, delisting/take-private, rebrand).
2. NEVER rewrite stable prose, founder names, founding years, or history. If a
   field has no sourced reason to change, leave it out of the draft.
3. Every changed fact MUST be web-verified with a recorded source URL. Do NOT
   recall figures from memory (this caused the PineBridge/Ares confabulations
   that `13c51f0` fixed).

## Steps (per fund in publish._FUND_PROFILES)
1. Read the current profile (founded/aum/hq/type/desc/notable).
2. WebSearch the latest AUM. Prefer authoritative sources: SEC 8-K/10-K for
   listed managers, Form ADV, the firm's own "by the numbers"/factsheet, or
   year-end results. Record the source URL.
3. WebSearch for material corporate events since the profile looks last-updated:
   acquisitions, IPO/delisting/take-private, rebrands. Record source URLs.
4. If (and only if) something changed materially (AUM drift ≥ ~15% or a
   confirmed event), write `pending_profiles/<id>.refresh.json`:

```json
{
  "id": "<fund-id>",
  "aum": "<new value, only if changed>",
  "aum_source": "<url>",
  "change_log": [
    {"field": "aum", "old": "<old>", "new": "<new>", "reason": "<one line>", "source": "<url>"}
  ]
}
```
   - Keep the same currency/scale convention as the existing value (e.g. `~$1.03T`).
   - For口径-ambiguous managers (e.g. GSAM total AUS vs AM vs alternatives),
     state the basis in `reason` and keep the existing basis.
   - For corporate events, include the changed text field(s) with full new text
     and an event keyword in `reason` (acqui/merg/delist/take-private/rebrand/
     收购/退市/私有化/改名).
5. If nothing changed for a fund, write NO file for it.

## Do not
- Do not run publish.py, edit publish.py, or git commit. The wrapper applies
  validated drafts via apply_refresh.py.
- Do not invent sources. No source → do not propose the change.
````

- [ ] **Step 2: 校验 markdown 可读 + 提交**

Run: `head -5 auto-promote/refresh-program.md`
Expected: 显示标题

```bash
git add auto-promote/refresh-program.md
git commit -m "docs(refresh): agent program for monthly profile refresh mode"
```

---

### Task 7: `wrapper-profile-refresh.sh` — cron wrapper + 编排

**Files:**
- Create: `scripts/wrapper-profile-refresh.sh`

> 以 `scripts/wrapper-candidate-discovery.sh` 为骨架（auth/lock/timeout/trap），编排：agent 产草稿 → 逐草稿 validate_refresh → 通过则 apply_refresh（默认 dry-run/alert，见 ALERT_ONLY）→ publish.py → commit → 发汇总邮件。

- [ ] **Step 1: 写 wrapper（完整内容）**

```bash
#!/usr/bin/env bash
# wrapper-profile-refresh.sh — monthly fund-profile freshness refresh.
# Launches a headless Claude (Max Plan) to web-verify AUM + corporate events,
# gates drafts through validate_refresh, applies passing ones via apply_refresh,
# publishes, and emails a summary. ALERT_ONLY=1 => never auto-apply (Phase 1).
set -uo pipefail

REPO="/home/ubuntu/hedge-fund-research"
LOCK="/tmp/cron-locks/profile-refresh.lock"
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
ALERT_ONLY="${ALERT_ONLY:-1}"   # Phase 1 default: alert, do not auto-apply
DRY_RUN_FLAG=""
[[ "$ALERT_ONLY" == "1" ]] && DRY_RUN_FLAG="--dry-run"

mkdir -p /tmp/cron-locks
exec 9>"$LOCK"
if ! flock -n 9; then echo "[profile-refresh] another run holds the lock; exit"; exit 0; fi

# --- Max Plan auth: unset API key so claude uses the subscription, restore after
if [[ -z "${ANTHROPIC_API_KEY:-}" ]]; then
  SAVED_KEY="$(grep -h '^ANTHROPIC_API_KEY=' "$HOME/.openclaw/.env" "$HOME/.stock-monitor.env" 2>/dev/null | head -1 | cut -d= -f2-)"
else
  SAVED_KEY="${ANTHROPIC_API_KEY}"
fi
unset ANTHROPIC_API_KEY

cleanup() {
  local pids; pids=$(jobs -p 2>/dev/null)
  [[ -n "$pids" ]] && kill $pids 2>/dev/null
  [[ -n "${SAVED_KEY:-}" ]] && export ANTHROPIC_API_KEY="$SAVED_KEY"
}
trap cleanup EXIT

cd "$REPO" || exit 1
PROMPT="$(cat auto-promote/refresh-program.md)"

# 1) agent generates pending_profiles/*.refresh.json
timeout --kill-after=30 1500 "$CLAUDE_BIN" --print --max-turns 120 "$PROMPT" \
  > logs/profile-refresh-agent.log 2>&1 || echo "[profile-refresh] agent exit $? (max-turns ok)"

# 2) gate + apply each draft
APPLIED=(); FLAGGED=()
shopt -s nullglob
for draft in pending_profiles/*.refresh.json; do
  fid="$(basename "$draft" .refresh.json)"
  if python3 scripts/validate_pending_profile.py "$draft" >/dev/null 2>&1; then
    if python3 scripts/apply_refresh.py "$fid" $DRY_RUN_FLAG >>logs/profile-refresh.log 2>&1; then
      APPLIED+=("$fid")
    else
      FLAGGED+=("$fid (apply failed)")
    fi
  else
    FLAGGED+=("$fid (gate failed)")
  fi
done

# 3) publish only if something was actually applied (not in alert-only)
if [[ "$ALERT_ONLY" != "1" && ${#APPLIED[@]} -gt 0 ]]; then
  python3 publish.py >>logs/profile-refresh.log 2>&1 \
    && git add publish.py config/sources.json \
    && git commit -m "chore(profiles): monthly AUM/event refresh ($(date -u +%Y-%m-%d))" \
    && git push
fi

# 4) summary email (notification only — never affect exit code)
python3 scripts/send_refresh_summary.py \
  --applied "${APPLIED[*]:-}" --flagged "${FLAGGED[*]:-}" \
  --alert-only "$ALERT_ONLY" >>logs/profile-refresh.log 2>&1 || echo "[profile-refresh] summary email WARN"

echo "[profile-refresh] done: applied=${#APPLIED[@]} flagged=${#FLAGGED[@]} alert_only=$ALERT_ONLY"
```

- [ ] **Step 2: 语法检查**

Run: `bash -n scripts/wrapper-profile-refresh.sh && echo OK`
Expected: OK

- [ ] **Step 3: 提交**

```bash
chmod +x scripts/wrapper-profile-refresh.sh
git add scripts/wrapper-profile-refresh.sh
git commit -m "feat(refresh): cron wrapper (Max Plan auth + gate/apply/publish/email)"
```

---

### Task 8: `send_refresh_summary.py` — 汇总邮件

**Files:**
- Create: `scripts/send_refresh_summary.py`
- Test: `tests/test_send_refresh_summary.py`

> 渲染与发送分离，便于单测（仿 `send_synthesis_summary.py`）。SMTP 走 `~/.stock-monitor.env`（`export` 后用）。

- [ ] **Step 1: 写失败测试（只测渲染，不真正发信）**

```python
# tests/test_send_refresh_summary.py
import importlib.util, sys
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("srs", REPO / "scripts" / "send_refresh_summary.py")
srs = importlib.util.module_from_spec(spec); sys.modules["srs"] = srs
spec.loader.exec_module(srs)


def test_render_lists_applied_and_flagged():
    html = srs.render_summary(applied=["apollo-global-management", "kkr"],
                              flagged=["gsam (gate failed)"], alert_only=False)
    assert "apollo-global-management" in html
    assert "kkr" in html
    assert "gsam (gate failed)" in html
    assert "<html" in html.lower()


def test_render_alert_only_banner():
    html = srs.render_summary(applied=["kkr"], flagged=[], alert_only=True)
    assert "ALERT-ONLY" in html.upper() or "告警模式" in html


def test_render_empty_is_noop_message():
    html = srs.render_summary(applied=[], flagged=[], alert_only=False)
    assert "no change" in html.lower() or "无变化" in html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_send_refresh_summary.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# scripts/send_refresh_summary.py
"""Render + send the monthly profile-refresh summary email.

render_summary() is pure (testable); send() does SMTP via ~/.stock-monitor.env.
Notification only — callers must not let this affect their exit code.
"""
from __future__ import annotations
import argparse, html, os, smtplib, sys
from email.mime.text import MIMEText


def render_summary(*, applied: list[str], flagged: list[str], alert_only: bool) -> str:
    def li(items):
        return "".join(f"<li>{html.escape(x)}</li>" for x in items) or "<li>—</li>"
    banner = ('<p style="background:#fff3cd;padding:8px;border-radius:6px">'
              '⚠️ ALERT-ONLY 告警模式：未自动应用，请人工确认。</p>' if alert_only else "")
    if not applied and not flagged:
        body = "<p>本月无变化（no change）。</p>"
    else:
        body = (f"<h3>已自动应用 ({len(applied)})</h3><ul>{li(applied)}</ul>"
                f"<h3>转人工 ({len(flagged)})</h3><ul>{li(flagged)}</ul>")
    return (f"<html><head><meta charset='utf-8'><title>GMIA Profile Refresh</title></head>"
            f"<body style='font-family:sans-serif'>{banner}"
            f"<h2>GMIA 月度 Profile 刷新</h2>{body}</body></html>")


def send(subject: str, html_body: str) -> bool:
    host = os.environ.get("SMTP_HOST"); user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS"); to = os.environ.get("MAIL_TO", user)
    if not all([host, user, pw, to]):
        sys.stderr.write("[send_refresh_summary] SMTP env missing; skip\n"); return False
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject; msg["From"] = user; msg["To"] = to
    msg["MIME-Version"] = "1.0"
    with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--applied", default=""); ap.add_argument("--flagged", default="")
    ap.add_argument("--alert-only", default="1")
    a = ap.parse_args()
    applied = [x for x in a.applied.split() if x]
    flagged = [x for x in a.flagged.split() if x]
    alert_only = a.alert_only == "1"
    htmlb = render_summary(applied=applied, flagged=flagged, alert_only=alert_only)
    n = len(applied) + len(flagged)
    send(f"GMIA Profile Refresh — {len(applied)} applied / {len(flagged)} flagged", htmlb)
    print(f"[send_refresh_summary] rendered ({n} items), alert_only={alert_only}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `python3 -m pytest tests/test_send_refresh_summary.py tests/ -q`
Expected: 全 passed

- [ ] **Step 5: 提交**

```bash
git add scripts/send_refresh_summary.py tests/test_send_refresh_summary.py
git commit -m "feat(refresh): summary email (render/send split, notification-only)"
```

---

### Task 9: cron 接线（Phase 1 告警模式上线）

**Files:**
- Modify: crontab（通过 `crontab -e`，非 repo 文件）

- [ ] **Step 1: dry-run 端到端手测（不发布、不提交）**

```bash
cd ~/hedge-fund-research
ALERT_ONLY=1 bash scripts/wrapper-profile-refresh.sh
```
Expected: log 显示 `applied=N flagged=M alert_only=1`；publish.py / sources.json **未改**（`git status` 干净）；收到一封告警模式汇总邮件。

- [ ] **Step 2: 加 cron 条目（每月 1 号 05:00 BJT = 21:00 UTC 前一日；经 cron-wrapper.sh）**

```bash
# crontab -e，新增一行（UTC）：ALERT_ONLY=1 起步
0 21 28-31 * * [ "$(date -u -d tomorrow +\%d)" = "01" ] && ALERT_ONLY=1 /home/ubuntu/cron-wrapper.sh profile-refresh /home/ubuntu/hedge-fund-research/scripts/wrapper-profile-refresh.sh
```
> 说明：cron 无「每月最后一天」语义，用 `28-31 *` + 「明天是否为 1 号」判定，确保每月恰好触发一次（BJT 1 号 05:00）。或按团队习惯直接用 `0 21 L * *` 若 cron 支持 `L`。

- [ ] **Step 3: 验证 cron 已登记**

Run: `crontab -l | grep profile-refresh`
Expected: 显示该行

- [ ] **Step 4: 记录 + 提交计划状态**

```bash
git add docs/superpowers/plans/2026-06-07-fund-profile-refresh-automation.md
git commit -m "docs(plan): fund-profile refresh implementation plan"
```

> **转 Phase 2（自动应用）**：告警模式跑满 1–2 个月、人工确认 agent 质量稳定后，把 cron 行的 `ALERT_ONLY=1` 改为 `ALERT_ONLY=0`。其余代码不变。

---

## 自检（Spec 覆盖）

- AUM 自动刷新 → Task 1/2/3（apply_refresh + sources.json 同步）✓
- 公司事件自动改写 → Task 4（validate_refresh 文本守卫）+ Task 6（agent 指令）+ Task 1（apply 任意 change_log 字段）✓
- 证据闸门复用 → Task 4 复用 `validate_profile` ✓
- 静态事实保护 → Task 4 文本 diff 守卫 + Task 5 守卫测试 + Task 1 最小 diff ✓
- Max Plan auth → Task 7 wrapper ✓
- 分阶段（dry-run/告警/自动）→ Task 1 `--dry-run` + Task 7 `ALERT_ONLY` + Task 9 ✓
- 汇总邮件 → Task 8 ✓
- 月度调度 → Task 9 ✓

无占位符；类型/函数签名跨任务一致（`validate_refresh(data, *, current)`、`apply_refresh(fund_id, *, base_dir, dry_run)`、`render_summary(*, applied, flagged, alert_only)`、`_find_entry_block`、`_format_entry`）。
