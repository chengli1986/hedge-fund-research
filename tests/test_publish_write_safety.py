"""publish_html and sync_docs_site under failure (stage-4 audit P8, P9).

P8: the second rename failing left html v2 next to gz v1 (readers get the gz),
    and a gz write that failed half way left a ~4MB hidden temp file per try.
P9: an exception while copying the page into docs-site (PermissionError) was
    exit 1, which run_pipeline.sh reads as "page not published" and skips
    stage 5, although the page was already live. It must be exit 3.
"""
import gzip
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import publish  # noqa: E402


def _state(d: Path):
    html = (d / "p.html").read_text()
    gz = gzip.open(d / "p.html.gz", "rt").read()
    return html, gz, sorted(p.name for p in d.iterdir())


def test_second_rename_failing_leaves_both_files_on_the_old_version(tmp_path, monkeypatch):
    publish.publish_html(tmp_path / "p.html", "v1")
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk")
        return real(src, dst)

    monkeypatch.setattr(publish.os, "replace", flaky)
    with pytest.raises(OSError):
        publish.publish_html(tmp_path / "p.html", "v2")
    monkeypatch.setattr(publish.os, "replace", real)
    html, gz, names = _state(tmp_path)
    assert (html, gz) == ("v1", "v1")
    assert names == ["p.html", "p.html.gz"], names


def test_a_half_written_gz_leaves_no_temp_file(tmp_path, monkeypatch):
    publish.publish_html(tmp_path / "p.html", "v1")
    real = gzip.open

    def half(path, *a, **k):
        f = real(path, *a, **k)

        class W:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                f.close()

            def write(self, s):
                f.write(s[:1])
                raise OSError("disk full")
        return W()

    monkeypatch.setattr(publish.gzip, "open", half)
    with pytest.raises(OSError):
        publish.publish_html(tmp_path / "p.html", "v2")
    monkeypatch.setattr(publish.gzip, "open", real)
    html, gz, names = _state(tmp_path)
    assert (html, gz) == ("v1", "v1")
    assert names == ["p.html", "p.html.gz"], names


def test_a_normal_publish_moves_both(tmp_path):
    publish.publish_html(tmp_path / "p.html", "v1")
    publish.publish_html(tmp_path / "p.html", "v2")
    html, gz, names = _state(tmp_path)
    assert (html, gz, names) == ("v2", "v2", ["p.html", "p.html.gz"])


@pytest.fixture
def readonly_docs(tmp_path):
    repo = tmp_path / "docs"
    (repo / "pages").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    page = repo / "pages" / "hedge-fund-research.html"
    page.write_text("old")
    page.chmod(0o444)
    yield repo
    page.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_exception_during_the_docs_copy_is_a_sync_failure(readonly_docs, capsys):
    assert publish.sync_docs_site(readonly_docs, "new") is False
    assert "PermissionError" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_main_exits_3_not_1_when_the_docs_copy_raises(readonly_docs, tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "load_articles", lambda: [])
    monkeypatch.setattr(publish, "generate_html", lambda articles: "<html></html>")
    monkeypatch.setattr(publish, "_precheck", lambda html, articles: [])
    monkeypatch.setattr(publish.Path, "home", classmethod(lambda cls: readonly_docs.parent / "home"))
    (readonly_docs.parent / "home").mkdir()
    (readonly_docs.parent / "home" / "docs-site").symlink_to(readonly_docs)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(tmp_path / "page.html")])
    assert publish.main() == publish.DOCS_SYNC_FAILED
    assert (tmp_path / "page.html").read_text() == "<html></html>"
