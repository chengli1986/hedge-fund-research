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
    4 — publish.py format unexpected (could not locate _FUND_PROFILES dict), or
        the rewritten publish.py failed its trial run (publish.py untouched)

Why this exists: lifecycle gap that bit KKR (5-12 8a6b574) and Research
Affiliates (5-15 a05699f) — both needed manual publish.py edits to graduate
their pending draft. Pure ergonomic helper, doesn't change the human-review
design (you still eyeball the JSON before running this).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parent.parent

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_ALREADY_PRESENT = 2
EXIT_PENDING_NOT_FOUND = 3
EXIT_FORMAT_UNEXPECTED = 4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_edit  # noqa: E402

PROFILE_FIELDS = profile_edit.PROFILE_FIELDS


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
    # Every issue is hard, uncertainty markers included (2026-10-03 decision;
    # an exemption for them never matched the validator's message anyway).
    hard_issues = list(result.get("issues", []))
    if hard_issues:
        sys.stderr.write(f"[graduate] validation failed: {hard_issues}\n")
        return EXIT_VALIDATION_FAILED

    source = publish_path.read_text(encoding="utf-8")
    try:
        current_profiles = profile_edit.read_profiles(source)
        insert_pos = profile_edit.insert_position(source)
    except (SyntaxError, ValueError) as e:
        sys.stderr.write(f"[graduate] publish.py format unexpected: {e}\n")
        return EXIT_FORMAT_UNEXPECTED
    if fund_id in current_profiles:
        sys.stderr.write(
            f"[graduate] {fund_id!r} already in publish._FUND_PROFILES — "
            f"refusing to overwrite (delete the existing entry first if you "
            f"really want to re-graduate)\n"
        )
        return EXIT_ALREADY_PRESENT

    entry = {k: profile[k] for k in PROFILE_FIELDS}
    try:
        new_source = source[:insert_pos] + profile_edit.format_entry(fund_id, entry) + source[insert_pos:]
        profile_edit.replace_checked(publish_path, new_source, {**current_profiles, fund_id: entry})
    except ValueError as e:
        sys.stderr.write(f"[graduate] {fund_id}: {e} — publish.py untouched\n")
        return EXIT_FORMAT_UNEXPECTED

    # Cleanup pending + validation companion only after successful write
    pending_path.unlink()
    if validation_path.exists():
        validation_path.unlink()

    sys.stdout.write(
        f"[graduate] {fund_id} graduated into publish._FUND_PROFILES\n"
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
