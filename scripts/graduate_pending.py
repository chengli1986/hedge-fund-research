"""graduate_pending.py — move a pending fund profile draft from
`pending_profiles/<id>.json` into `publish._FUND_PROFILES`.

Run after auto-promote wires a new fund + you've eyeballed the pending draft
and confirmed AUM / founded / HQ aren't hallucinated. One step replaces the
manual publish.py edit + delete-pending dance.

Usage:
    python3 scripts/graduate_pending.py <fund-id>

Exit codes:
    0 — success, publish.py updated + pending files deleted
    1 — validation failed (missing fields or hard violations)
    2 — fund-id already present in publish._FUND_PROFILES
    3 — pending_profiles/<fund-id>.json not found
    4 — publish.py format unexpected (could not locate _FUND_PROFILES dict)

Why this exists: lifecycle gap that bit KKR (5-12 8a6b574) and Research
Affiliates (5-15 a05699f) — both needed manual publish.py edits to graduate
their pending draft. Pure ergonomic helper, doesn't change the human-review
design (you still eyeball the JSON before running this).
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
EXIT_ALREADY_PRESENT = 2
EXIT_PENDING_NOT_FOUND = 3
EXIT_FORMAT_UNEXPECTED = 4

PROFILE_FIELDS = ("founded", "aum", "hq", "type_en", "type_zh",
                  "desc_zh", "notable_en", "notable_zh")


def _load_validate_module(base_dir: Path):
    """Import scripts/validate_pending_profile.py — handles arbitrary base_dir
    for testing."""
    path = base_dir / "scripts" / "validate_pending_profile.py"
    if not path.exists():
        # Fall back to repo default — useful during tests where tmp_path
        # only has publish.py + pending_profiles/ (no scripts/ dir).
        path = REPO_DEFAULT / "scripts" / "validate_pending_profile.py"
    spec = importlib.util.spec_from_file_location("_vpp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _find_fund_profiles_block(source: str) -> tuple[int, int]:
    """Return (insert_pos, close_brace_pos) for the _FUND_PROFILES dict literal.

    insert_pos = where to splice the new entry (just before closing `}` line).
    close_brace_pos = position of the closing `}` itself, for sanity.

    Raises ValueError if the dict literal can't be located.
    """
    m = re.search(r"_FUND_PROFILES\s*(?::\s*dict\[[^\]]+\])?\s*=\s*\{",
                  source)
    if not m:
        raise ValueError("could not locate _FUND_PROFILES dict declaration")

    open_brace_pos = source.index("{", m.start())
    depth = 1
    i = open_brace_pos + 1
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
        raise ValueError("unbalanced braces in _FUND_PROFILES dict literal")

    # i now points at the closing `}` of the outer dict
    # Walk back to the start of that line so we splice cleanly
    line_start = source.rfind("\n", 0, i) + 1
    return line_start, i


def _format_entry(fund_id: str, profile: dict) -> str:
    """Build the publish.py-style entry text, matching the existing 5-line
    compact format used for all 22 funds."""
    def lit(s: str) -> str:
        # JSON-escape via json.dumps to handle quotes/backslashes safely
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


def graduate(fund_id: str, *, base_dir: Path | None = None) -> int:
    """Move pending_profiles/<fund_id>.json into publish._FUND_PROFILES.
    Returns exit code (0 = success)."""
    base = Path(base_dir) if base_dir else REPO_DEFAULT
    pending_path = base / "pending_profiles" / f"{fund_id}.json"
    validation_path = base / "pending_profiles" / f"{fund_id}.validation.json"
    publish_path = base / "publish.py"

    if not pending_path.exists():
        sys.stderr.write(f"[graduate] no pending profile at {pending_path}\n")
        return EXIT_PENDING_NOT_FOUND

    try:
        profile = json.loads(pending_path.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"[graduate] pending JSON malformed: {e}\n")
        return EXIT_VALIDATION_FAILED

    vpp = _load_validate_module(base)
    result = vpp.validate_profile(profile)
    hard_issues = [
        msg for msg in result.get("issues", [])
        if not msg.startswith("high_risk_marker")
    ]
    if hard_issues:
        sys.stderr.write(f"[graduate] validation failed: {hard_issues}\n")
        return EXIT_VALIDATION_FAILED

    source = publish_path.read_text()
    if re.search(rf'^\s*{re.escape(json.dumps(fund_id))}\s*:\s*\{{',
                 source, re.M):
        sys.stderr.write(
            f"[graduate] {fund_id!r} already in publish._FUND_PROFILES — "
            f"refusing to overwrite (delete the existing entry first if you "
            f"really want to re-graduate)\n"
        )
        return EXIT_ALREADY_PRESENT

    try:
        insert_pos, _close_pos = _find_fund_profiles_block(source)
    except ValueError as e:
        sys.stderr.write(f"[graduate] {e}\n")
        return EXIT_FORMAT_UNEXPECTED

    entry = _format_entry(fund_id, profile)
    new_source = source[:insert_pos] + entry + source[insert_pos:]
    publish_path.write_text(new_source)

    # Cleanup pending + validation companion only after successful write
    pending_path.unlink()
    if validation_path.exists():
        validation_path.unlink()

    high_risk = [m for m in result.get("issues", [])
                 if m.startswith("high_risk_marker")]
    suffix = f" (high_risk markers retained: {high_risk})" if high_risk else ""
    sys.stdout.write(
        f"[graduate] {fund_id} graduated into publish._FUND_PROFILES{suffix}\n"
        f"[graduate] cleaned up {pending_path.name}"
        + (f" + {validation_path.name}" if validation_path.exists() else "")
        + "\n"
        f"[graduate] suggest: git diff publish.py && pytest tests/ -q && "
        f"git add publish.py && git commit\n"
    )
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Graduate a pending fund profile into publish._FUND_PROFILES",
    )
    parser.add_argument("fund_id",
                        help="Fund id, e.g. 'research-affiliates'")
    parser.add_argument("--base-dir", type=Path, default=None,
                        help="Repo root (defaults to script's repo)")
    args = parser.parse_args()
    return graduate(args.fund_id, base_dir=args.base_dir)


if __name__ == "__main__":
    sys.exit(main())
