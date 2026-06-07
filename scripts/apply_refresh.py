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
    key = json.dumps(fund_id)
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


def _load_publish_profiles(base: Path) -> dict:
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


def _sync_sources_aum(base: Path, fund_id: str, old_aum: str, new_aum: str) -> bool:
    """If config/sources.json[fund_id].description embeds old_aum, replace it with
    new_aum in place (preserving file formatting). Returns True if changed."""
    src_path = base / "config" / "sources.json"
    if not src_path.exists() or not old_aum:
        return False
    text = src_path.read_text()
    data = json.loads(text)
    target = next((s for s in data.get("sources", [])
                   if s.get("id") == fund_id and old_aum in s.get("description", "")), None)
    if target is None:
        return False
    enc_old = json.dumps(target["description"], ensure_ascii=False)
    enc_new = json.dumps(target["description"].replace(old_aum, new_aum), ensure_ascii=False)
    if enc_old not in text:
        return False
    src_path.write_text(text.replace(enc_old, enc_new, 1))
    return True


def _archive_draft(draft_path: Path) -> None:
    applied = draft_path.parent / "applied"
    applied.mkdir(exist_ok=True)
    draft_path.replace(applied / draft_path.name)


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
    aum_change = next((c for c in change_log if c["field"] == "aum"), None)
    if aum_change:
        _sync_sources_aum(base, fund_id, aum_change.get("old", ""),
                          aum_change.get("new", ""))
    _archive_draft(draft_path)
    sys.stdout.write(f"[apply_refresh] {fund_id}: applied {sorted(changed_fields)}\n")
    return EXIT_OK


def main() -> int:
    ap = argparse.ArgumentParser(description="Apply a profile-refresh draft to an existing fund")
    ap.add_argument("fund_id")
    ap.add_argument("--base-dir", type=Path, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return apply_refresh(args.fund_id, base_dir=args.base_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
