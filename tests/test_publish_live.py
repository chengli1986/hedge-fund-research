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
    # 正常用例固定「余量充足」，不依赖测试机临时盘的真实剩余（GM-05）
    import collections
    VFS = collections.namedtuple("VFS", "f_bavail f_frsize f_favail")
    monkeypatch.setattr(publish.os, "statvfs", lambda p: VFS(10 ** 9, 4096, 10 ** 7))
    return root, state


def _records(state):
    p = state / "records.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_state_root_is_redirected_in_tests():
    """conftest 必须把状态根重定向走（本进程与子进程），测试绝不碰 ~/.local/state/gmia-publish。"""
    assert ".local/state/gmia-publish" not in str(publish.GMIA_STATE_ROOT)
    assert ".local/state/gmia-publish" not in os.environ["GMIA_PUBLISH_STATE_ROOT"]
    r = subprocess.run([sys.executable, "-c", "import publish; print(publish.GMIA_STATE_ROOT)"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0 and ".local/state/gmia-publish" not in r.stdout


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
    called = []
    monkeypatch.setattr(publish.shutil, "copy2", lambda *a, **k: (called.append(1), (_ for _ in ()).throw(OSError("full")))[1])
    publish.publish_live(page, "<html>NEW</html>")
    assert called, "复制确实被调用（不是因为磁盘余量而跳过）"
    assert page.read_text() == "<html>NEW</html>"
    assert "备份失败" in capsys.readouterr().err
    assert not [d for d in (state / "backup").iterdir()], "残件已删"


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
    VFS = collections.namedtuple("VFS", "f_bavail f_frsize f_favail")
    monkeypatch.setattr(publish.os, "statvfs", lambda p: VFS(1, 4096, 10 ** 7))
    publish.publish_live(page, "<html>NEW</html>")
    assert page.read_text() == "<html>NEW</html>"
    assert _records(state)[-1]["backup_dir"] is None
    assert "磁盘余量" in capsys.readouterr().err


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



# ---------- Codex GM-01～07 ----------

def test_close_failure_after_busy_lock_does_not_block(site, monkeypatch):
    root, state = site
    state.mkdir(parents=True)
    real_close = os.close
    with open(state / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        monkeypatch.setattr(publish.os, "close", lambda fd: (_ for _ in ()).throw(OSError("EIO")))
        publish.publish_live(root / "hedge-fund-research.html", "<html>C</html>")
        monkeypatch.setattr(publish.os, "close", real_close)
    assert (root / "hedge-fund-research.html").read_text() == "<html>C</html>"


def test_exists_raising_in_backup_does_not_block(site, monkeypatch):
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    real_lstat = os.lstat
    monkeypatch.setattr(publish.os, "lstat", lambda p, *a, **k: (_ for _ in ()).throw(OSError(5, "EIO"))
                        if str(p).endswith("hedge-fund-research.html") else real_lstat(p, *a, **k))
    publish.publish_live(page, "<html>NEW</html>")
    monkeypatch.setattr(publish.os, "lstat", real_lstat)
    assert page.read_text() == "<html>NEW</html>"


def test_records_fifo_does_not_hang(site):
    root, state = site
    state.mkdir(parents=True)
    os.mkfifo(state / "records.jsonl")
    import time
    t0 = time.monotonic()
    publish.publish_live(root / "hedge-fund-research.html", "<html>F</html>")
    assert time.monotonic() - t0 < 5
    assert (root / "hedge-fund-research.html").read_text() == "<html>F</html>"


def test_lock_fifo_does_not_hang(site):
    root, state = site
    state.mkdir(parents=True)
    os.mkfifo(state / "lock")
    publish.publish_live(root / "hedge-fund-research.html", "<html>L</html>")
    assert (root / "hedge-fund-research.html").read_text() == "<html>L</html>"


def test_slow_backup_is_cut_off_and_publish_continues(site, monkeypatch, capsys):
    import time
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    monkeypatch.setattr(publish, "SIDE_STEP_SECONDS", 0.5)
    monkeypatch.setattr(publish.shutil, "copy2", lambda *a, **k: time.sleep(5))
    t0 = time.monotonic()
    publish.publish_live(page, "<html>NEW</html>")
    assert time.monotonic() - t0 < 3
    assert page.read_text() == "<html>NEW</html>"


def test_missing_ubuntu_account_does_not_break_import(monkeypatch):
    monkeypatch.delenv("GMIA_PUBLISH_STATE_ROOT", raising=False)
    import pwd
    monkeypatch.setattr(pwd, "getpwnam", lambda n: (_ for _ in ()).throw(KeyError(n)))
    assert publish._default_state_root() is None


def test_no_state_root_still_publishes(site, monkeypatch):
    root, state = site
    monkeypatch.setattr(publish, "GMIA_STATE_ROOT", None)
    publish.publish_live(root / "hedge-fund-research.html", "<html>N</html>")
    assert (root / "hedge-fund-research.html").read_text() == "<html>N</html>"


def test_inode_shortage_skips_backup(site, monkeypatch):
    import collections
    root, state = site
    page = root / "hedge-fund-research.html"
    publish.publish_html(page, "<html>OLD</html>")
    VFS = collections.namedtuple("VFS", "f_bavail f_frsize f_favail")
    monkeypatch.setattr(publish.os, "statvfs", lambda p: VFS(10 ** 9, 4096, 5))
    publish.publish_live(page, "<html>NEW</html>")
    assert _records(state)[-1]["backup_dir"] is None


def test_undeletable_old_backup_skips_new_backup(site, monkeypatch):
    root, state = site
    page = root / "hedge-fund-research.html"
    for i in range(4):          # 第一次没有旧页不备份 → 4 次后正好 3 份
        publish.publish_live(page, f"<html>{i}</html>")
    assert len(list((state / "backup").iterdir())) == 3
    monkeypatch.setattr(publish.shutil, "rmtree", lambda *a, **k: None)   # 删不掉
    publish.publish_live(page, "<html>X</html>")
    assert _records(state)[-1]["backup_dir"] is None
    assert len(list((state / "backup").iterdir())) == 3


def test_record_trim_keeps_tail_and_is_bounded(site, monkeypatch):
    root, state = site
    monkeypatch.setattr(publish, "RECORDS_MAX_BYTES", 600)
    for i in range(30):
        publish.publish_live(root / "hedge-fund-research.html", f"<html>{i}</html>")
    lines = (state / "records.jsonl").read_text().splitlines()
    assert all(json.loads(l) for l in lines)
    assert (state / "records.jsonl").stat().st_size < 1500
