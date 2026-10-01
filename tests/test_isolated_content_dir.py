"""fetch_content.isolated_content_dir -- the one way to call an extractor
without writing into production content/ (audit C3, 2026-10-01).

The extractors below are shaped like the real ones on purpose: they read the
module global CONTENT_DIR at call time and write through _atomic_write, which
is what all 46 write sites in fetch_content.py do.
"""
import pytest

import fetch_content as fc


def _extractor_shaped_write(article_id: str, text: str = "body") -> object:
    content_path = fc.CONTENT_DIR / f"{article_id}.txt"
    fc._atomic_write(content_path, text.encode("utf-8"))
    return content_path


def test_a_write_inside_the_block_lands_in_the_temporary_directory():
    before = fc.CONTENT_DIR
    with fc.isolated_content_dir() as tmp:
        assert fc.CONTENT_DIR == tmp != before
        written = _extractor_shaped_write("iso1")
        assert written.parent == tmp and written.exists()
    assert not (before / "iso1.txt").exists(), "the write escaped to the previous CONTENT_DIR"


def test_content_dir_is_restored_and_the_directory_removed_afterwards():
    before = fc.CONTENT_DIR
    with fc.isolated_content_dir() as tmp:
        _extractor_shaped_write("iso2")
    assert fc.CONTENT_DIR == before
    assert not tmp.exists(), "the temporary directory outlived the block"


def test_content_dir_is_restored_when_the_block_raises():
    before = fc.CONTENT_DIR
    with pytest.raises(RuntimeError):
        with fc.isolated_content_dir():
            _extractor_shaped_write("iso3")
            raise RuntimeError("extractor blew up")
    assert fc.CONTENT_DIR == before


def test_nesting_restores_each_level():
    before = fc.CONTENT_DIR
    with fc.isolated_content_dir() as outer:
        with fc.isolated_content_dir() as inner:
            assert fc.CONTENT_DIR == inner != outer
        assert fc.CONTENT_DIR == outer
    assert fc.CONTENT_DIR == before


def _load_script(name):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_weekly_audit_does_not_overwrite_a_stored_body(tmp_path, monkeypatch):
    """content_audit fetches under the article's REAL id. Without the redirect
    the re-fetch would replace the stored body, silently. Nothing tested this
    before 2026-10-01: compare_extractors' tests stub _fetch_to_temp out.

    CONTENT_DIR is pointed at tmp_path to stand in for production: conftest
    only DETECTS writes to the real content/, it does not redirect this one,
    and the first draft of this test wrote real0001.txt there."""
    audit = _load_script("content_audit")
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    stored = fc.CONTENT_DIR / "real0001.txt"
    fc._atomic_write(stored, b"the body we stored last month")

    def fetcher(article):
        return (_extractor_shaped_write(article["id"], "tonight's re-fetch"), "ok")

    result, _evidence, new = audit._fetch_to_temp({"id": "real0001", "url": "https://x/1"}, fetcher)
    assert new == "tonight's re-fetch", "the audit must still see what it fetched"
    assert stored.read_text() == "the body we stored last month", "the stored body was overwritten"
