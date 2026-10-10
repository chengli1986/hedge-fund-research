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
    5 — publish.py format unexpected, or the rewritten publish.py / sources.json
        failed its checks (publish.py is then untouched)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_DRAFT_NOT_FOUND = 3
EXIT_NOT_PRESENT = 4
EXIT_FORMAT_UNEXPECTED = 5

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_edit  # noqa: E402

PROFILE_FIELDS = profile_edit.PROFILE_FIELDS


def _load_validate_module(base: Path):
    path = base / "scripts" / "validate_pending_profile.py"
    if not path.exists():
        path = REPO_DEFAULT / "scripts" / "validate_pending_profile.py"
    spec = importlib.util.spec_from_file_location("_vpp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Currency kept: validate_pending_profile._money_tokens drops the symbol on
# purpose (it compares magnitudes), which let a card at "~€700B" agree with a
# description at "$700B" (pre-merge review, 2026-10-10).
_MONEY_WITH_CURRENCY = re.compile(r"([\$¥€£])\s*(\d+(?:\.\d+)?)\s*([KMBT])\b", re.I)


def money_figures(text: str) -> set[tuple[str, str, str]]:
    """{(currency symbol, number, unit)} in `text`, e.g. {('$', '190', 'B')}."""
    return {(c, n, u.upper()) for c, n, u in _MONEY_WITH_CURRENCY.findall(text or "")}


def _aum_agrees(aum: str, description: str, vpp=None) -> bool:
    """The card's AUM and the one written into the English description agree:
    the description carries the AUM verbatim, shares its figure in the same
    currency ("$18T+" for "~$18T+ benchmarked"), or carries no money figure."""
    if aum and aum in description:
        return True
    figures = money_figures(description)
    return not figures or bool(figures & money_figures(aum))


def _synced_sources_text(base: Path, fund_id: str, old_aum: str, new_aum: str, vpp) -> str | None:
    """sources.json text with the fund's description moved from old_aum to
    new_aum (formatting preserved), or None when nothing there changes.

    old_aum is publish.py's current value, never the draft's word for it: a
    wrong or tilde-less draft `old` used to leave the two AUMs disagreeing or
    write "~~$800B" (stage-4 audit P3). Raises ValueError when the result
    would still disagree, so a description in an unexpected form goes to a
    human instead of being half-updated.
    """
    src_path = base / "config" / "sources.json"
    if not src_path.exists():
        return None
    text = src_path.read_text(encoding="utf-8")
    target = next((s for s in json.loads(text).get("sources", []) if s.get("id") == fund_id), None)
    if target is None:
        return None
    description = target.get("description", "")
    updated = description.replace(old_aum, new_aum) if old_aum and old_aum in description else description
    if not _aum_agrees(new_aum, updated, vpp):
        raise ValueError(f"sources.json description would disagree with AUM {new_aum!r}: {description!r}")
    if updated == description:
        return None
    enc_old = json.dumps(description, ensure_ascii=False)
    if text.count(enc_old) != 1:
        raise ValueError(f"cannot locate {fund_id}'s description in sources.json exactly once")
    return text.replace(enc_old, json.dumps(updated, ensure_ascii=False), 1)


def _write_atomic(path: Path, text: str) -> None:
    path = Path(os.path.realpath(path))      # never replace a symlink with a file
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


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

    source = publish_path.read_text(encoding="utf-8")
    try:
        current_profiles = profile_edit.read_profiles(source)
        span = profile_edit.entry_span(source, fund_id)
    except (SyntaxError, ValueError) as e:
        sys.stderr.write(f"[apply_refresh] publish.py format unexpected: {e}\n")
        return EXIT_FORMAT_UNEXPECTED
    if fund_id not in current_profiles or span is None:
        sys.stderr.write(f"[apply_refresh] {fund_id!r} not in _FUND_PROFILES "
                         f"(use graduate_pending for new funds)\n")
        return EXIT_NOT_PRESENT
    current = current_profiles[fund_id]

    # change_log is the single source of truth for what changes and to what.
    # Build `merged` from each entry's `new` (not from optional top-level keys),
    # so a draft can never silently "apply" a field while publish.py keeps the
    # old value. Each entry's `old` must match publish.py: the gate below
    # measures the change against the real current text, never the draft's
    # account of it (old == new used to wave a full rewrite through).
    change_log = draft.get("change_log", [])
    if not isinstance(change_log, list):
        sys.stderr.write("[apply_refresh] change_log must be a list\n")
        return EXIT_VALIDATION_FAILED
    merged = dict(current)
    checked_log = []
    changed_fields: set[str] = set()
    for c in change_log:
        if not isinstance(c, dict):
            sys.stderr.write(f"[apply_refresh] change_log entry is not an object: {c!r}\n")
            return EXIT_VALIDATION_FAILED
        field, new_val = c.get("field"), c.get("new")
        if not field or new_val is None:
            sys.stderr.write(f"[apply_refresh] change_log entry missing field/new: {c}\n")
            return EXIT_VALIDATION_FAILED
        if field not in PROFILE_FIELDS:
            sys.stderr.write(f"[apply_refresh] change_log: {field!r} is not a profile field\n")
            return EXIT_VALIDATION_FAILED
        if not isinstance(new_val, str):
            sys.stderr.write(f"[apply_refresh] change_log[{field}].new must be a string, "
                             f"got {type(new_val).__name__}: {new_val!r}\n")
            return EXIT_VALIDATION_FAILED
        if not isinstance(c.get("old"), str) or c["old"].strip() != str(current.get(field, "")).strip():
            sys.stderr.write(f"[apply_refresh] change_log[{field}].old {c.get('old')!r} does not match "
                             f"publish.py ({current.get(field)!r}) — route to human\n")
            return EXIT_VALIDATION_FAILED
        merged[field] = new_val
        checked_log.append({**c, "old": current.get(field, "")})
        changed_fields.add(field)

    vpp = _load_validate_module(base)
    result = vpp.validate_refresh({**merged, "id": fund_id,
                                   "change_log": checked_log,
                                   "aum_source": draft.get("aum_source", ""),
                                   "founded_source": draft.get("founded_source", "")},
                                  current=current)
    # Every issue is hard, uncertainty markers included (2026-10-03 decision).
    hard = list(result["issues"])
    if hard:
        sys.stderr.write(f"[apply_refresh] validation failed: {hard}\n")
        return EXIT_VALIDATION_FAILED

    sources_path = base / "config" / "sources.json"
    try:
        new_sources = (_synced_sources_text(base, fund_id, current["aum"], merged["aum"], vpp)
                       if "aum" in changed_fields else None)
        start, end = span
        new_source = source[:start] + profile_edit.format_entry(fund_id, merged) + source[end:]
        profile_edit.replace_checked(publish_path, new_source,
                                     {**current_profiles, fund_id: merged}, dry_run=dry_run)
    except ValueError as e:
        sys.stderr.write(f"[apply_refresh] {fund_id}: {e} — publish.py untouched\n")
        return EXIT_FORMAT_UNEXPECTED

    if dry_run:
        sys.stdout.write(f"[apply_refresh] DRY-RUN {fund_id}: would change "
                         f"{sorted(changed_fields)}\n")
        return EXIT_OK

    if new_sources is not None:
        try:
            _write_atomic(sources_path, new_sources)
        except OSError as e:
            # publish.py already moved; put it back so the two never disagree.
            _write_atomic(publish_path, source)
            sys.stderr.write(f"[apply_refresh] {fund_id}: writing sources.json failed ({e}); "
                             f"publish.py restored\n")
            return EXIT_FORMAT_UNEXPECTED
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
