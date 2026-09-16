"""A checkout with no production data must run the suite cleanly (2026-09-16).

Everything this repo produces is gitignored -- data/, logs/, content/,
tests/fixtures/*.html|json -- so a fresh clone has none of it. The tests that
need those files are supposed to skip, and the skips were never exercised on
the machine that has the files. A real clone showed 2 failures and 12 errors,
and one of the failures was the skip itself:

    if not ARTICLES_FILE.exists():
        pytest.skip(...)        # NameError: name 'pytest' is not defined

That branch had never run. This module guards the class of bug rather than
that one line: any test file that reaches for `pytest.<anything>` must import
pytest, or its skip/xfail/raises path is a NameError waiting for the first
machine that takes it.
"""
import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent


def _test_modules():
    return sorted(p for p in TESTS_DIR.glob("test_*.py"))


@pytest.mark.parametrize("path", _test_modules(), ids=lambda p: p.name)
def test_a_module_that_uses_pytest_imports_it(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    uses = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "pytest"
               for n in ast.walk(tree))
    if not uses:
        return
    imported = any(
        (isinstance(n, ast.Import) and any(a.name == "pytest" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "pytest")
        for n in ast.walk(tree))
    assert imported, f"{path.name} calls pytest.* but never imports pytest"


def test_every_skip_reason_says_what_is_missing():
    """A bare skip() in a data-dependent test reads as a passing run."""
    for path in _test_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "skip" and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "pytest"):
                assert n.args, f"{path.name}: pytest.skip() with no reason"
