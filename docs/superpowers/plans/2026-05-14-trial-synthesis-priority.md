# Trial → Synthesis Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当 trial 通过但没有注册 fetcher 时，立即触发 fetcher-synthesis（不等每周日），并在 trial 期间检测 JS-only 站点，使 `requires_playwright` 标记真实反映生产环境的可提取性。

**Architecture:** Trial manager 在 PASS 时写入 `synthesis_priority: true` + `requires_playwright: bool` 到 `fund_candidates.json`；`synthesize_fetchers.py` 扩展 `list_targets()` 纳入 `synthesis_priority` 候选（插到队列最前）；`wrapper-fetcher-synthesis.sh` 的 cron 条目升级为 `--lock` 形式防止并发冲突。`_is_likely_js_only()` 独立辅助函数通过 HTTP 200 + 大 HTML + 无可提取文本来识别 JS shell 站点。

**Tech Stack:** Python 3.12, httpx, BeautifulSoup4, pytest, bash/flock

---

## 文件改动地图

| 文件 | 改动性质 |
|------|---------|
| `crontab` | 修改：fetcher-synthesis 条目升级为 `--name --lock` 形式 |
| `scripts/wrapper-fetcher-synthesis.sh` | 修改：添加 flock 防并发（belt-and-suspenders） |
| `synthesize_fetchers.py` | 修改：`list_targets()` 纳入 `synthesis_priority` 候选 |
| `gmia-trial-manager.py` | 修改：新增 `_is_likely_js_only()`；`sample_article_quality()` 追踪 JS-only；`cmd_run()` 写入 `synthesis_priority` / `requires_playwright` |
| `tests/test_unit_synthesize_fetchers.py` | 新增：3 个 synthesis_priority 测试 |
| `tests/test_unit_trial_quality.py` | 新增：5 个测试；更新 1 个现有测试 |

---

## Task 1: Fetcher-synthesis cron 升级为 --lock 形式

**Files:**
- Modify: `crontab` (via `crontab -e` or temp file)
- Modify: `~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh:1-10`

### 背景

当前 cron 条目用旧式位置参数形式，不支持 `--lock`：
```
0 18 * * 6 ~/cron-wrapper.sh gmia-fetcher-synthesis 20m bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```
新式 `--name --lock --` 形式才能让 `cron-wrapper.sh` 的 `flock` 机制生效。同时在 wrapper 内部也加 flock，以防 trial manager 将来直接触发 synthesis（不经过 cron-wrapper）时仍有保护。

---

- [ ] **Step 1: 写失败测试（验证 wrapper 可以被多次并发调用而第二个直接退出）**

```bash
# tests 目录没有 bash 测试，这里用手工并发验证代替单元测试
# 先确认当前 flock 状态（预期：无 flock → 两个实例都能运行）
flock --version
```
Expected: flock available (exit 0)

- [ ] **Step 2: 更新 crontab 条目**

```bash
# 导出当前 crontab
crontab -l > /tmp/crontab-backup.txt
# 确认要修改的行
grep "fetcher-synthesis" /tmp/crontab-backup.txt
```
Expected output:
```
0 18 * * 6 ~/cron-wrapper.sh gmia-fetcher-synthesis 20m bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```

用 sed 替换（整行替换，精确匹配）：
```bash
sed 's|0 18 \* \* 6 ~/cron-wrapper.sh gmia-fetcher-synthesis 20m bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh|0 18 * * 6 ~/cron-wrapper.sh --name gmia-fetcher-synthesis --timeout 1200 --lock -- bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh|' /tmp/crontab-backup.txt | crontab -
```

验证：
```bash
crontab -l | grep fetcher-synthesis
```
Expected:
```
0 18 * * 6 ~/cron-wrapper.sh --name gmia-fetcher-synthesis --timeout 1200 --lock -- bash ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```

- [ ] **Step 3: 在 wrapper-fetcher-synthesis.sh 头部添加 flock（belt-and-suspenders）**

在 `set -uo pipefail` 之后、`SCRIPT_DIR=` 之前，插入：

```bash
# Prevent concurrent synthesis runs (e.g. trial-pass immediate trigger + weekly cron)
LOCK_FILE="/tmp/cron-locks/gmia-fetcher-synthesis.lock"
mkdir -p /tmp/cron-locks
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$LOG_PREFIX Another fetcher-synthesis instance is running. Exiting."
    exit 0
fi
```

注意：`LOG_PREFIX` 在后面才定义，所以将这段移到 `LOG_PREFIX` 定义之后：

读取文件确认现有行号：
```bash
grep -n "LOG_PREFIX\|set -uo\|SCRIPT_DIR" ~/hedge-fund-research/scripts/wrapper-fetcher-synthesis.sh
```
Expected: `6: set -uo pipefail`, `8: SCRIPT_DIR=...`, `10: LOG_PREFIX=...`

在 `LOG_PREFIX` 行（第10行）之后、`cleanup()` 之前插入 flock 块。实际编辑：

```bash
# 当前文件第10行之后（LOG_PREFIX 定义完成后）插入以下内容：
```

文件修改后 `set -uo pipefail` 到 flock 之间的顺序：
```bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

# Prevent concurrent synthesis runs (trial-pass immediate trigger + weekly cron)
LOCK_FILE="/tmp/cron-locks/gmia-fetcher-synthesis.lock"
mkdir -p /tmp/cron-locks
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$LOG_PREFIX Another fetcher-synthesis instance is running. Exiting."
    exit 0
fi

cleanup() {
```

- [ ] **Step 4: 运行 pytest 确保没有破坏现有测试**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_session_heartbeat.py tests/test_synthesis_history.py -v
```
Expected: PASS（这两个测试覆盖 synthesis wrapper 的下游逻辑）

- [ ] **Step 5: 提交**

```bash
cd ~/hedge-fund-research
git add scripts/wrapper-fetcher-synthesis.sh
git commit -m "fix(synthesis): add flock to prevent concurrent synthesis runs

Fetcher-synthesis wrapper now acquires /tmp/cron-locks/gmia-fetcher-synthesis.lock
at startup. A second instance (e.g. trial-pass immediate trigger racing with
weekly cron) exits cleanly instead of both writing fetch_articles.py simultaneously.

Crontab also updated to --name --lock form (consistent with auto-promote).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

---

## Task 2: synthesize_fetchers.py 支持 synthesis_priority 候选

**Files:**
- Modify: `synthesize_fetchers.py:46-90` (`list_targets` 函数)
- Test: `tests/test_unit_synthesize_fetchers.py`

### 背景

当前 `list_targets()` 只处理 `status="inaccessible"` 的候选。Trial PASS 后的候选是 `status="promoted"`，永远不会被 synthesis 处理。新增逻辑：若候选有 `synthesis_priority: true` 且 id 不在已注册 FETCHERS 中，无论 status 如何都纳入目标列表，且插入到 inaccessible 候选之前。

---

- [ ] **Step 1: 写失败测试**

在 `tests/test_unit_synthesize_fetchers.py` 末尾添加：

```python
# ---------------------------------------------------------------------------
# synthesis_priority: promoted candidates with no fetcher get picked up
# ---------------------------------------------------------------------------

def test_list_targets_includes_synthesis_priority_promoted():
    """Promoted candidate with synthesis_priority=True and no fetcher is included."""
    candidates = [
        _make_candidate("alpha", "promoted", "HIGH"),
    ]
    candidates[0]["synthesis_priority"] = True
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert len(result) == 1
    assert result[0]["id"] == "alpha"


def test_list_targets_synthesis_priority_before_inaccessible():
    """synthesis_priority candidates appear before inaccessible candidates."""
    candidates = [
        _make_candidate("inacc", "inaccessible", "HIGH"),
        {**_make_candidate("prio", "promoted", "HIGH"), "synthesis_priority": True},
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value=set()):
        result = synthesize_fetchers.list_targets()
    assert result[0]["id"] == "prio"
    assert result[1]["id"] == "inacc"


def test_list_targets_synthesis_priority_skipped_when_fetcher_exists():
    """synthesis_priority candidate is skipped when fetcher already registered."""
    candidates = [
        {**_make_candidate("alpha", "promoted", "HIGH"), "synthesis_priority": True},
    ]
    with patch("synthesize_fetchers.load_candidates", return_value=candidates), \
         patch("synthesize_fetchers.load_fetcher_ids", return_value={"alpha"}):
        result = synthesize_fetchers.list_targets()
    assert result == []
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_synthesize_fetchers.py::test_list_targets_includes_synthesis_priority_promoted tests/test_unit_synthesize_fetchers.py::test_list_targets_synthesis_priority_before_inaccessible tests/test_unit_synthesize_fetchers.py::test_list_targets_synthesis_priority_skipped_when_fetcher_exists -v
```
Expected: 3 FAIL（`list_targets()` 目前只处理 inaccessible）

- [ ] **Step 3: 实现 synthesis_priority 支持**

修改 `synthesize_fetchers.py` 的 `list_targets()` 函数（当前第46-90行），在 `targets = []` 和最终 `targets.sort()` 之间加入 priority 候选收集逻辑：

```python
def list_targets() -> list[dict]:
    """Return candidates that need a synthesis attempt.

    Two sources:
    1. synthesis_priority=True candidates (status="promoted", no fetcher yet) — inserted first.
    2. status="inaccessible" candidates — standard weekly-synthesis queue.
    """
    candidates = load_candidates()
    fetcher_ids = load_fetcher_ids()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SKIP_WINDOW_DAYS)

    # ── Priority queue: trial-passed, no fetcher yet ──────────────────────────
    priority_targets = []
    for c in candidates:
        if not c.get("synthesis_priority"):
            continue
        if c["id"] in fetcher_ids:
            continue  # fetcher already registered (synthesis succeeded earlier)
        priority_targets.append({
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "homepage_url": c.get("homepage_url", ""),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "notes": c.get("notes", ""),
            "needs_playwright": bool(c.get("requires_playwright") or c.get("needs_playwright")),
            "quality": c.get("quality", "?"),
        })

    # ── Standard inaccessible queue ───────────────────────────────────────────
    targets = []
    for c in candidates:
        if c.get("status") != "inaccessible":
            continue
        if c.get("quality") == "LOW":
            continue
        if c["id"] in fetcher_ids:
            continue
        last = c.get("synthesis_attempted_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt > cutoff:
                    continue
            except ValueError:
                pass
        targets.append({
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "homepage_url": c.get("homepage_url", ""),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "notes": c.get("notes", ""),
            "needs_playwright": bool(c.get("needs_playwright")),
            "quality": c.get("quality", "?"),
        })

    quality_order = {"HIGH": 0, "MEDIUM": 1, "?": 2, "LOW": 3}
    targets.sort(key=lambda t: (
        not t["needs_playwright"],
        quality_order.get(t["quality"], 2),
    ))
    # Priority candidates always come first (already sorted by insertion order)
    return priority_targets + targets
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_synthesize_fetchers.py -v
```
Expected: ALL PASS（含原有 8 个测试 + 新增 3 个）

- [ ] **Step 5: 提交**

```bash
cd ~/hedge-fund-research
git add synthesize_fetchers.py tests/test_unit_synthesize_fetchers.py
git commit -m "feat(synthesis): include synthesis_priority candidates in list_targets()

Trial-passed funds with synthesis_priority=True (set by trial manager on PASS
when no fetcher exists) are now picked up by fetcher-synthesis immediately,
without waiting for status=inaccessible routing. They appear before inaccessible
candidates in the target list.

requires_playwright flag is forwarded to synthesis target as needs_playwright so
the agent skips httpx attempts and goes straight to Playwright.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

---

## Task 3: 添加 _is_likely_js_only() 辅助函数 + 更新 sample_article_quality()

**Files:**
- Modify: `gmia-trial-manager.py:301` (在 `_extract_article_text` 之后插入新函数)
- Modify: `gmia-trial-manager.py:496-506` (extraction loop in `sample_article_quality`)
- Modify: `gmia-trial-manager.py:581-586` (return dict in `sample_article_quality`)
- Test: `tests/test_unit_trial_quality.py`

### 背景

`_extract_article_text()` 返回 `None` 有三种情况：(1)网络错误/超时、(2)HTTP 非 200（登录墙/地理封锁）、(3)HTTP 200 + 大体积 HTML + 无可提取文字（JS shell）。只有第三种需要标记 `requires_playwright`。`_is_likely_js_only()` 仅在 extraction 返回 None 时被调用，做一次额外的 HTTP 请求以区分三种情况。

---

- [ ] **Step 1: 写失败测试**

在 `tests/test_unit_trial_quality.py` 末尾添加：

```python
# ---------------------------------------------------------------------------
# _is_likely_js_only: detect JS-shell pattern (HTTP 200 + large HTML + no text)
# ---------------------------------------------------------------------------

def test_is_likely_js_only_true_for_large_html_no_extractable_content(monkeypatch):
    """HTTP 200 + HTML body > 1000 chars + _extract_article_text returns None → True."""
    large_html = "<html><body>" + "<div class='nav'>" * 100 + "</div>" * 100 + "</body></html>"
    assert len(large_html) > 1000

    class FakeResp:
        status_code = 200
        text = large_html

    monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, **k: None)
    assert tm._is_likely_js_only("https://example.com/article") is True


def test_is_likely_js_only_false_for_http_non200(monkeypatch):
    """HTTP non-200 (auth wall, geoblock) → False, not a JS-rendering issue."""
    class FakeResp:
        status_code = 403
        text = "<html>Forbidden</html>"

    monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: FakeResp())
    assert tm._is_likely_js_only("https://example.com/article") is False


def test_is_likely_js_only_false_for_small_html_body(monkeypatch):
    """HTTP 200 but tiny HTML body (< 1000 chars) → False (network/empty response)."""
    class FakeResp:
        status_code = 200
        text = "<html><body>tiny</body></html>"

    monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: FakeResp())
    assert tm._is_likely_js_only("https://example.com/article") is False


def test_is_likely_js_only_false_on_exception(monkeypatch):
    """Network exception → False (not a JS-rendering signal)."""
    monkeypatch.setattr(tm.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(Exception("timeout")))
    assert tm._is_likely_js_only("https://example.com/article") is False


def test_sample_article_quality_tracks_js_only_count(monkeypatch):
    """js_only_count in return dict reflects how many extraction failures were JS shells."""
    monkeypatch.setattr(tm, "_get_article_links_for_sampling",
                        lambda trial: ["https://example.com/a1", "https://example.com/a2"])
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, **k: None)
    monkeypatch.setattr(tm, "_is_likely_js_only", lambda url, **k: True)
    monkeypatch.setattr(tm, "_call_haiku", lambda *a, **k: None)

    result = tm.sample_article_quality("https://example.com", trial={"id": "test"})

    assert result["js_only_count"] == 2
    assert result["js_only_checked"] == 2
    assert result["error"] == "Could not extract text from any article"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_is_likely_js_only_true_for_large_html_no_extractable_content tests/test_unit_trial_quality.py::test_is_likely_js_only_false_for_http_non200 tests/test_unit_trial_quality.py::test_is_likely_js_only_false_for_small_html_body tests/test_unit_trial_quality.py::test_is_likely_js_only_false_on_exception tests/test_unit_trial_quality.py::test_sample_article_quality_tracks_js_only_count -v
```
Expected: 5 FAIL

- [ ] **Step 3: 实现 _is_likely_js_only()**

在 `gmia-trial-manager.py` 的 `_extract_article_text` 函数（第301-354行）结束后、`_call_haiku` 函数（第357行）开始前，插入：

```python
def _is_likely_js_only(url: str, timeout: int = 15) -> bool:
    """Return True when the page is likely JS-rendered (HTTP 200 + large HTML + no extractable text).

    Three non-JS failure modes must NOT return True:
    - Network error / timeout → caught by except, return False
    - HTTP non-200 (auth wall, geoblock) → check status_code, return False
    - HTTP 200 + tiny body (empty CDN response) → check len(text) < 1000, return False

    Only HTTP 200 + large HTML (>1000 chars) + _extract_article_text returns None
    → the site serves a JS shell that needs browser rendering.
    """
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return False
        if len(resp.text) < 1000:
            return False
        return _extract_article_text(url, timeout=timeout) is None
    except Exception:
        return False
```

- [ ] **Step 4: 更新 sample_article_quality() 的 extraction loop**

找到当前第496-506行（extraction loop）：

```python
    article_texts: list[tuple[str, str]] = []  # (url, text)
    for url in links:
        text = _extract_article_text(url)
        if text:
            article_texts.append((url, text))
        if len(article_texts) >= SAMPLE_SIZE:
            break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article"}
```

替换为：

```python
    article_texts: list[tuple[str, str]] = []  # (url, text)
    js_only_count = 0
    js_only_checked = 0
    for url in links:
        text = _extract_article_text(url)
        if text:
            article_texts.append((url, text))
        else:
            # Probe to distinguish JS shell from network/auth failure
            js_only_checked += 1
            if _is_likely_js_only(url):
                js_only_count += 1
        if len(article_texts) >= SAMPLE_SIZE:
            break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article",
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked}
```

- [ ] **Step 5: 更新 sample_article_quality() 的 return dict**

找到当前最终 return（第581-586行）：

```python
    return {
        "sampled": len(article_texts),
        "articles": scored_articles,
        "avg_score": round(avg_score, 3),
        "error": None,
    }
```

替换为：

```python
    return {
        "sampled": len(article_texts),
        "articles": scored_articles,
        "avg_score": round(avg_score, 3),
        "error": None,
        "js_only_count": js_only_count,
        "js_only_checked": js_only_checked,
    }
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_is_likely_js_only_true_for_large_html_no_extractable_content tests/test_unit_trial_quality.py::test_is_likely_js_only_false_for_http_non200 tests/test_unit_trial_quality.py::test_is_likely_js_only_false_for_small_html_body tests/test_unit_trial_quality.py::test_is_likely_js_only_false_on_exception tests/test_unit_trial_quality.py::test_sample_article_quality_tracks_js_only_count -v
```
Expected: 5 PASS

- [ ] **Step 7: 运行全量测试确认无回归**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py -v 2>&1 | tail -20
```
Expected: ALL PASS

- [ ] **Step 8: 提交**

```bash
cd ~/hedge-fund-research
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): add _is_likely_js_only() and js_only tracking in quality sampling

_is_likely_js_only(url): returns True when HTTP 200 + large HTML (>1000 chars)
+ _extract_article_text returns None. Distinguishes JS-shell sites from network
errors and auth walls (both return False).

sample_article_quality() now tracks js_only_count and js_only_checked in its
return dict. These counters feed the requires_playwright flag set in cmd_run()
after trial decision.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

---

## Task 4: trial pass 时写入 synthesis_priority 和 requires_playwright

**Files:**
- Modify: `gmia-trial-manager.py:1156-1204` (`cmd_run` 的 trial 决策块)
- Test: `tests/test_unit_trial_quality.py`

### 背景

Trial PASS 后（第1157行 `active["outcome"] = "pass"`），需要：
1. 判断是否有注册 fetcher（`has_fetcher`）
2. 若无：设 `c["synthesis_priority"] = True`, `c["synthesis_priority_set_at"]`
3. 判断 JS-only 比例：若 `js_only_count / max(js_only_checked, 1) > 0.5` → `c["requires_playwright"] = True`

`js_only_count` 需要从 `quality_samples` 聚合——每个 sample dict 现在含 `js_only_count` / `js_only_checked`。

---

- [ ] **Step 1: 写失败测试**

在 `tests/test_unit_trial_quality.py` 末尾追加：

```python
# ---------------------------------------------------------------------------
# Trial pass: synthesis_priority and requires_playwright flags
# ---------------------------------------------------------------------------

def _make_trial_state_pass(start_days_ago=4, js_only_count=0, js_only_checked=3):
    """Build a trial_state dict for a PASS scenario with js_only tracking."""
    from datetime import datetime, timedelta
    BJT = __import__("gmia_trial_manager", fromlist=["BJT"]).BJT
    today = datetime.now(BJT)
    start = today - timedelta(days=start_days_ago)
    return {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.90,
            "quality": "HIGH",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 10,
                    "date_count": 5, "error": None,
                }
                for i in range(4)
            },
            "quality_samples": [{
                "day": 1,
                "date": start.strftime("%Y-%m-%d"),
                "sampled": 2,
                "articles": [
                    {"url": "https://example.com/a1", "relevance": 0.9,
                     "depth": 0.8, "extractable": 0.9, "overall": 0.86, "notes": "good"},
                ],
                "avg_score": 0.86,
                "error": None,
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked,
            }],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }


def test_trial_pass_sets_synthesis_priority_when_no_fetcher(trial_env, monkeypatch):
    """On PASS with no registered fetcher, synthesis_priority is set on the candidate."""
    import gmia_trial_manager as tm_mod
    trial_state = _make_trial_state_pass()
    tm_mod.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm_mod, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm_mod, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm_mod, "send_trial_email", lambda *a, **k: None)
    # Simulate no registered fetcher
    monkeypatch.setattr(tm_mod, "_load_fetchers", lambda: {})

    tm_mod.cmd_run()

    candidates = json.loads(tm_mod.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert c.get("synthesis_priority") is True
    assert "synthesis_priority_set_at" in c


def test_trial_pass_no_synthesis_priority_when_has_fetcher(trial_env, monkeypatch):
    """On PASS when fetcher already registered, synthesis_priority is NOT set."""
    import gmia_trial_manager as tm_mod
    trial_state = _make_trial_state_pass()
    tm_mod.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm_mod, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": True})
    monkeypatch.setattr(tm_mod, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm_mod, "send_trial_email", lambda *a, **k: None)
    # Simulate fetcher IS registered
    monkeypatch.setattr(tm_mod, "_load_fetchers", lambda: {"test-fund": lambda s: []})

    tm_mod.cmd_run()

    candidates = json.loads(tm_mod.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert not c.get("synthesis_priority")


def test_trial_pass_sets_requires_playwright_when_majority_js_only(trial_env, monkeypatch):
    """On PASS where >50% sampled articles were JS-only, requires_playwright is set."""
    import gmia_trial_manager as tm_mod
    # 3 out of 3 js_only_checked were JS-only → 100%
    trial_state = _make_trial_state_pass(js_only_count=3, js_only_checked=3)
    tm_mod.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm_mod, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm_mod, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm_mod, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm_mod, "_load_fetchers", lambda: {})

    tm_mod.cmd_run()

    candidates = json.loads(tm_mod.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert c.get("requires_playwright") is True
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_trial_pass_sets_synthesis_priority_when_no_fetcher tests/test_unit_trial_quality.py::test_trial_pass_no_synthesis_priority_when_has_fetcher tests/test_unit_trial_quality.py::test_trial_pass_sets_requires_playwright_when_majority_js_only -v
```
Expected: 3 FAIL

- [ ] **Step 3: 实现 cmd_run() 中的 synthesis_priority + requires_playwright 逻辑**

在 `gmia-trial-manager.py` 第1191-1194行（PASS 分支），当前代码：

```python
                    else:
                        c["status"] = "promoted"
                        c["notes"] = (f"RECOMMEND: trial passed "
                                      f"({days_with_articles}/{TRIAL_DAYS} days with articles, quality={avg_quality:.2f})")
```

替换为：

```python
                    else:
                        c["status"] = "promoted"
                        c["notes"] = (f"RECOMMEND: trial passed "
                                      f"({days_with_articles}/{TRIAL_DAYS} days with articles, quality={avg_quality:.2f})")
                        # Aggregate js_only stats from quality_samples
                        total_js_only = sum(
                            s.get("js_only_count", 0)
                            for s in active.get("quality_samples", [])
                        )
                        total_js_checked = sum(
                            s.get("js_only_checked", 0)
                            for s in active.get("quality_samples", [])
                        )
                        if total_js_checked > 0 and total_js_only / total_js_checked > 0.5:
                            c["requires_playwright"] = True
                        # If no registered fetcher, flag for immediate synthesis
                        has_fetcher = active["id"] in _load_fetchers()
                        if not has_fetcher:
                            c["synthesis_priority"] = True
                            c["synthesis_priority_set_at"] = datetime.now(BJT).isoformat()
```

- [ ] **Step 4: 更新现有 test_trial_passes_with_both_quantity_and_quality 以兼容新字段**

找到第520-585行的 `test_trial_passes_with_both_quantity_and_quality`，在 `monkeypatch.setattr(tm, "send_trial_email", ...)` 之后、`tm.cmd_run()` 之前添加：

```python
    # New: mock _load_fetchers so synthesis_priority logic is deterministic
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"test-fund": lambda s: []})
```

以及更新 `daily_checks` 条目加入缺失的 `fetcher_used` 字段（当前测试没有 `fetcher_used` 字段，但 `count_articles_with_fetcher` 已被 monkeypatched 所以无影响，保持原样即可）。

找到该测试并验证它仍然 PASS：

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_trial_passes_with_both_quantity_and_quality -v
```
Expected: PASS（因为 `_load_fetchers` 返回 `{"test-fund": ...}`，`has_fetcher=True`，`synthesis_priority` 不被设置，不影响现有断言）

- [ ] **Step 5: 运行新增三个测试**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_trial_pass_sets_synthesis_priority_when_no_fetcher tests/test_unit_trial_quality.py::test_trial_pass_no_synthesis_priority_when_has_fetcher tests/test_unit_trial_quality.py::test_trial_pass_sets_requires_playwright_when_majority_js_only -v
```
Expected: 3 PASS

- [ ] **Step 6: 全量测试**

```bash
cd ~/hedge-fund-research && python3 -m pytest -x -q 2>&1 | tail -10
```
Expected: `424 passed` 或更多（新增 ~8 个测试），0 failed

- [ ] **Step 7: 提交**

```bash
cd ~/hedge-fund-research
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): set synthesis_priority and requires_playwright on PASS

On trial PASS:
- synthesis_priority=True + synthesis_priority_set_at set on candidate when
  no registered fetcher exists. synthesize_fetchers.list_targets() picks these
  up immediately instead of waiting for the weekly Sunday cron.
- requires_playwright=True set when >50% of sampled article extractions were
  identified as JS-shell (HTTP 200 + large HTML + no extractable text).
  Forwarded to fetcher-synthesis as needs_playwright=true so agent skips httpx.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

---

## Task 5: 端到端验证

**Files:** 无代码改动，仅验证

- [ ] **Step 1: 验证 Robeco 当前状态**

```bash
cd ~/hedge-fund-research && python3 -c "
import json
c = json.loads(open('config/fund_candidates.json').read())
robeco = next(x for x in c if x['id'] == 'robeco')
print('status:', robeco.get('status'))
print('synthesis_priority:', robeco.get('synthesis_priority'))
print('requires_playwright:', robeco.get('requires_playwright'))
"
```
Expected: `status: promoted`, `synthesis_priority: None/missing`（因为 Robeco 的 trial 在本次改动之前就已通过，flag 需要手动补设）

- [ ] **Step 2: 手动为 Robeco 补设 synthesis_priority（回填已通过的 trial）**

```bash
cd ~/hedge-fund-research && python3 - << 'EOF'
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))
path = Path("config/fund_candidates.json")
candidates = json.loads(path.read_text())

import fetch_articles
for c in candidates:
    if c["id"] == "robeco":
        if c["id"] not in fetch_articles.FETCHERS:
            c["synthesis_priority"] = True
            c["synthesis_priority_set_at"] = datetime.now(BJT).isoformat()
            c["notes"] = c.get("notes", "") + " [synthesis_priority backfilled 2026-05-14]"
            print(f"Set synthesis_priority on {c['id']}")
        break

path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
EOF
```

- [ ] **Step 3: 验证 synthesize_fetchers 现在列出 Robeco**

```bash
cd ~/hedge-fund-research && python3 synthesize_fetchers.py | python3 -c "import json,sys; t=json.load(sys.stdin); [print(x['id'], x['needs_playwright']) for x in t]"
```
Expected: `robeco False`（Robeco 出现在列表中，且 `needs_playwright=False` 因为它是 SSR 站点）

- [ ] **Step 4: 全量 pytest 最终确认**

```bash
cd ~/hedge-fund-research && python3 -m pytest -q 2>&1 | tail -5
```
Expected: all passed, 0 failed

- [ ] **Step 5: 提交 Robeco 回填**

```bash
cd ~/hedge-fund-research
git add config/fund_candidates.json
git commit -m "chore(robeco): backfill synthesis_priority for pre-existing trial pass

Robeco passed trial before synthesis_priority logic existed. Manually set
synthesis_priority=True so next fetcher-synthesis run picks it up without
waiting for Sunday cron.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
git push origin main
```

---

## 自检

### 1. Spec 覆盖率

| 需求 | 覆盖任务 |
|------|---------|
| 并发保护（wrapper lockfile） | Task 1 |
| synthesis_priority 标记位触发 | Task 2 + Task 4 |
| requires_playwright 真实检测 | Task 3 + Task 4 |
| 测试覆盖新逻辑 | Task 3 Step 1 + Task 4 Step 1 |
| Robeco 立即可处理 | Task 5 Step 2 |
| extractable 分反映生产可提取性 | Task 3（js_only_count 追踪）+ Task 4（requires_playwright 标记影响 synthesis target 的 needs_playwright） |

### 2. 已知限制（v1 不处理）

- `synthesis_priority` 候选目前不受 `MAX_SYNTHESIS_FAILURES` 保护（失败不计入 exhaustion 计数）。v2 可在 Task 2 的 `list_targets()` 中加入对应逻辑。
- `requires_playwright` 仅由 quality sampling 阶段判断；若 quality sampling 全部靠 registered fetcher（`fetcher_used=True`），则 `js_only_checked=0`，不会设置标记——这是正确行为（有 fetcher → content fetcher 已知，不需要猜测）。
