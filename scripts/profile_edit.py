"""profile_edit.py — locate and rewrite publish._FUND_PROFILES entries safely.

Shared by apply_refresh.py (update an entry) and graduate_pending.py (insert
one). Both rewrite publish.py itself, unattended, once a month; the stage-4
audit (2026-10-09) found two ways that went wrong while reporting success:

- Entries were found by counting `{` and `}`, which also counted braces
  inside strings: one stray `}` in a description and the next rewrite cut the
  entry in half (IndentationError, every nightly publish failing). Spans now
  come from the parsed syntax tree, where a string is a string.
- Whatever text was produced went straight over publish.py. Now the new text
  is written beside it, compiled, run, and only renamed into place when it
  holds exactly the profiles intended and (where publish.py has one)
  generate_html still renders. Anything else leaves publish.py untouched.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

PROFILE_FIELDS = ("founded", "aum", "hq", "type_en", "type_zh",
                  "desc_zh", "notable_en", "notable_zh")


def _profiles_node(source: str) -> ast.Dict:
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "_FUND_PROFILES" for t in targets) \
                and isinstance(node.value, ast.Dict):
            return node.value
    raise ValueError("could not locate the _FUND_PROFILES dict literal")


def _offset(source: str, lineno: int, col: int) -> int:
    """Character offset of an ast (lineno, col_offset); col_offset counts UTF-8 bytes."""
    lines = source.splitlines(keepends=True)
    before = sum(len(line) for line in lines[:lineno - 1])
    return before + len(lines[lineno - 1].encode("utf-8")[:col].decode("utf-8"))


def read_profiles(source: str) -> dict:
    """The _FUND_PROFILES literal as data, without running publish.py."""
    return ast.literal_eval(_profiles_node(source))


def entry_span(source: str, fund_id: str) -> tuple[int, int] | None:
    """(start, end) of the whole `"<id>": {...},` entry including its line
    break, or None when the fund has no entry."""
    node = _profiles_node(source)
    for key, value in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and key.value == fund_id):
            continue
        key_at = _offset(source, key.lineno, key.col_offset)
        start = source.rfind("\n", 0, key_at) + 1
        if source[start:key_at].strip():
            raise ValueError(f"entry {fund_id!r} does not start its own line")
        end = _offset(source, value.end_lineno, value.end_col_offset)
        if source[end:end + 1] == ",":
            end += 1
        newline = source.find("\n", end)
        if newline != -1 and not source[end:newline].strip():
            end = newline + 1
        return start, end
    return None


def insert_position(source: str) -> int:
    """Start of the line holding the dict's closing brace: a new entry goes there."""
    node = _profiles_node(source)
    close = _offset(source, node.end_lineno, node.end_col_offset) - 1
    if source[close] != "}":
        raise ValueError("_FUND_PROFILES does not end with a closing brace")
    return source.rfind("\n", 0, close) + 1


def format_entry(fund_id: str, profile: dict) -> str:
    """publish.py-style compact entry. Every value must be a string: json.dumps
    of anything else is a Python literal that publish.py cannot run with."""
    for field in PROFILE_FIELDS:
        if not isinstance(profile.get(field), str):
            raise ValueError(f"{field} must be a string, got {type(profile.get(field)).__name__}")

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


# Runs in a child process so a candidate that hangs, exits or pollutes module
# state cannot take the caller with it.
_PROBE = """
import json, runpy, sys
ns = runpy.run_path(sys.argv[1], run_name="_profile_candidate")
if callable(ns.get("generate_html")):
    ns["generate_html"]([])
print(json.dumps(ns["_FUND_PROFILES"], ensure_ascii=False))
"""


def replace_checked(publish_path: Path, new_source: str, expected: dict, *,
                    dry_run: bool = False) -> None:
    """Prove `new_source` compiles, runs, renders, and holds exactly `expected`
    as _FUND_PROFILES; then rename it over publish.py (unless dry_run).

    Raises ValueError with the reason when any step fails; publish.py is then
    untouched and no candidate file is left behind.
    """
    real = Path(os.path.realpath(publish_path))   # never replace a symlink with a file
    try:
        compile(new_source, str(real), "exec")
    except SyntaxError as e:
        raise ValueError(f"rewritten publish.py does not compile: {e.msg} (line {e.lineno})") from e
    candidate = real.with_name(f".{real.name}.candidate{os.getpid()}.py")
    try:
        candidate.write_text(new_source, encoding="utf-8")
        os.chmod(candidate, real.stat().st_mode & 0o7777)
        try:
            run = subprocess.run([sys.executable, "-c", _PROBE, str(candidate)], cwd=real.parent,
                                 capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired as e:
            raise ValueError("rewritten publish.py did not finish its trial run in 300s") from e
        if run.returncode != 0:
            tail = (run.stderr.strip().splitlines() or ["no output"])[-1]
            raise ValueError(f"rewritten publish.py failed its trial run: {tail}")
        lines = run.stdout.strip().splitlines()
        try:
            got = json.loads(lines[-1]) if lines else None
        except json.JSONDecodeError:
            got = None
        if got != expected:
            raise ValueError("rewritten publish.py does not hold the intended _FUND_PROFILES")
        if not dry_run:
            os.replace(candidate, real)
    finally:
        candidate.unlink(missing_ok=True)
