"""publish_live：GMIA 写线上时外加自己的锁、写前备份、发布记录（site-publish 第 3 期 V0.4）。

publish_html 本身一行不改；这里加的三样都是「尽力而为」——任何一样出错只告警，绝不挡住发布。
测试只写 tmp_path（站点根用 HEDGE_FUND_RESEARCH_SITE_ROOT 指过去，状态根由 conftest 重定向到 sink）。
"""
import fcntl
import gzip
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import publish  # noqa: E402


@pytest.fixture
def site(tmp_path, monkeypatch):
    root = tmp_path / "www"
    root.mkdir()
    state = tmp_path / "state"
    monkeypatch.setattr(publish, "SITE_ROOT", root)
    monkeypatch.setattr(publish, "GMIA_STATE_ROOT", state)
    return root, state


def _records(state):
    p = state / "records.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_state_root_is_redirected_in_tests():
    """conftest 必须把状态根重定向走，测试绝不碰 ~/.local/state/gmia-publish。"""
    assert ".local/state/gmia-publish" not in str(publish.GMIA_STATE_ROOT)


def test_preview_path_goes_straight_to_publish_html(site, tmp_path, monkeypatch):
    root, state = site
    called = []
    monkeypatch.setattr(publish, "publish_html", lambda p, h: called.append(p) or p.with_suffix(".html.gz"))
    out = tmp_path / "preview" / "hedge-fund-research.html"
    publish.publish_live(out, "<html>x</html>")
    assert called == [out]
    assert not state.exists(), "预览不拿锁、不备份、不记录"


def test_live_publish_backs_up_records_and_publishes(site):
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    publish.publish_live(page, "<html>NEW</html>")
    assert page.read_text() == "<html>NEW</html>"
    assert gzip.open(str(page) + ".gz", "rt").read() == "<html>NEW</html>"
    rec = _records(state)[-1]
    assert rec["page"] == "hedge-fund-research.html" and rec["lock"] == "held"
    b = Path(rec["backup_dir"])
    assert (b / "hedge-fund-research.html").read_text() == "<html>OLD</html>"
    assert gzip.open(b / "hedge-fund-research.html.gz", "rt").read() == "<html>OLD</html>"


def test_first_ever_publish_without_existing_page(site):
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_live(page, "<html>FIRST</html>")
    assert page.read_text() == "<html>FIRST</html>"
    assert _records(state)[-1]["backup_dir"] is None


def test_keeps_only_three_backups_per_page(site):
    root, state = site
    page = root / "hedge-fund-research.html"
    for i in range(6):
        publish.publish_live(page, f"<html>v{i}</html>")
    dirs = sorted((state / "backup").iterdir())
    assert len([d for d in dirs if (d / "hedge-fund-research.html").exists()]) == 3


def test_lock_busy_still_publishes_and_warns(site, capsys):
    root, state = site
    state.mkdir(parents=True)
    page = root / "hedge-fund-research.html"
    with open(state / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        publish.publish_live(page, "<html>X</html>")
    assert page.read_text() == "<html>X</html>", "锁被占也要发（不劣于今天的无锁）"
    assert "WARN" in capsys.readouterr().err
    rec = _records(state)[-1]
    assert rec["lock"] == "busy" and rec["backup_dir"] is None, "没拿到锁就不备份"


def test_unwritable_state_root_still_publishes(site, monkeypatch, capsys):
    root, state = site
    state.parent.mkdir(exist_ok=True)
    state.write_text("not a dir")             # 状态根是个文件：锁、备份、记录全失败
    page = root / "hedge-fund-research.html"
    publish.publish_live(page, "<html>Y</html>")
    assert page.read_text() == "<html>Y</html>"
    assert "WARN" in capsys.readouterr().err


def test_backup_failure_does_not_block(site, monkeypatch, capsys):
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    monkeypatch.setattr(publish.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    publish.publish_live(page, "<html>NEW</html>")
    assert page.read_text() == "<html>NEW</html>"
    assert "WARN" in capsys.readouterr().err


def test_publish_html_failure_propagates_and_releases_lock(site, monkeypatch):
    root, state = site
    page = root / "hedge-fund-research.html"
    monkeypatch.setattr(publish, "publish_html", lambda p, h: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        publish.publish_live(page, "<html>Z</html>")
    with open(state / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 锁已释放，拿得到


def test_symlinked_output_into_site_root_is_live(site, tmp_path):
    root, state = site
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    publish.publish_live(alias / "hedge-fund-research.html", "<html>A</html>")
    assert _records(state), "经链接指到站点根也算线上"


def test_main_uses_publish_live_for_live_output(site, monkeypatch):
    root, state = site
    monkeypatch.setattr(publish, "_precheck", lambda html, articles: [])
    monkeypatch.setattr(publish, "load_articles", lambda: [])
    monkeypatch.setattr(publish, "generate_html", lambda articles: "<html>M</html>")
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: True)
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(root / "hedge-fund-research.html")])
    assert publish.main() == 0
    assert _records(state)[-1]["page"] == "hedge-fund-research.html"


def test_health_page_uses_publish_live(site, monkeypatch):
    root, state = site
    sys.path.insert(0, str(REPO / "scripts"))
    import publish_health
    monkeypatch.setattr(publish_health, "DEPLOY_DIR", root)
    monkeypatch.setattr(publish_health, "load_inputs", lambda now: {})
    monkeypatch.setattr(publish_health, "build_health", lambda **k: {"stale": []})
    monkeypatch.setattr(publish_health, "render_html", lambda h: "<html>H</html>")
    monkeypatch.setattr(publish_health, "exit_code", lambda h: 0)
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html, relpath=None: True)
    assert publish_health.main([]) == 0
    assert _records(state)[-1]["page"] == publish_health.OUTPUT_NAME



def test_no_wait_on_busy_lock(site):
    """不等锁：被占时立刻返回发布，耗时远小于 1 秒。"""
    import time
    root, state = site
    state.mkdir(parents=True)
    with open(state / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        t0 = time.monotonic()
        publish.publish_live(root / "hedge-fund-research.html", "<html>Q</html>")
        assert time.monotonic() - t0 < 1.0


def test_gz_pointing_elsewhere_falls_back_to_plain_publish(site, tmp_path, monkeypatch):
    """正文在站点根、.gz 是指向别处的链接：不算线上配对，不加锁不备份，直接 publish_html（同以前）。"""
    root, state = site
    other = tmp_path / "elsewhere.gz"
    other.write_bytes(b"x")
    (root / "hedge-fund-research.html.gz").symlink_to(other)
    called = []
    monkeypatch.setattr(publish, "publish_html", lambda p, h: called.append(p) or p)
    publish.publish_live(root / "hedge-fund-research.html", "<html>G</html>")
    assert called and not state.exists()


def test_backup_skipped_when_disk_low(site, monkeypatch, capsys):
    import collections
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    Usage = collections.namedtuple("Usage", "total used free")
    monkeypatch.setattr(publish.shutil, "disk_usage", lambda p: Usage(10, 9, 1))
    publish.publish_live(page, "<html>NEW</html>")
    assert page.read_text() == "<html>NEW</html>"
    assert _records(state)[-1]["backup_dir"] is None
    assert "磁盘余量不足" in capsys.readouterr().err


def test_records_file_is_capped(site, monkeypatch):
    root, state = site
    monkeypatch.setattr(publish, "RECORDS_MAX_BYTES", 400)
    for i in range(20):
        publish.publish_live(root / "hedge-fund-research.html", f"<html>{i}</html>")
    assert (state / "records.jsonl").stat().st_size < 1200


def test_exit_codes_unchanged_for_main(site, monkeypatch):
    """docs-site 同步失败仍返回 3，precheck 失败仍返回 4（publish_live 不改语义）。"""
    root, state = site
    monkeypatch.setattr(publish, "load_articles", lambda: [])
    monkeypatch.setattr(publish, "generate_html", lambda articles: "<html>M</html>")
    monkeypatch.setattr(sys, "argv", ["publish.py", "--output", str(root / "hedge-fund-research.html")])
    monkeypatch.setattr(publish, "_precheck", lambda html, articles: [])
    monkeypatch.setattr(publish, "sync_docs_site", lambda repo, html: False)
    assert publish.main() == 3
    monkeypatch.setattr(publish, "_precheck", lambda html, articles: ["x: bad"])
    assert publish.main() == 4
