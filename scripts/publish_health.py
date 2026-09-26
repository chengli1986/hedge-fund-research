#!/usr/bin/env python3
"""Build the stage-1 health page for docs.sinostor.com.cn.

Every number here already existed -- in config/inspection_state.json,
logs/gmia-fetcher-health.json, data/articles.jsonl and
data/template-census.jsonl -- and could only be seen by reading an email at
the moment it arrived, or by grepping a log. That is this repo's recurring
defect (detected, never communicated) applied to its own instrumentation.

The dangerous failure for a page like this is not a wrong pixel, it is
yesterday's data shown as today's. So every input carries its own timestamp
and its own maximum age, a stale one is marked on the page, and the run exits
non-zero so cron mails someone. Alerts about sources do NOT fail the run:
showing a failing source is the page's job, and the fetcher-health email
already raised it.

The structure holds all five pipeline stages from the start; only stage 1 is
filled. Inventing metrics for the stages that have not been audited yet would
put numbers on a wall that nobody has checked -- worse than a blank.

    python3 scripts/publish_health.py              # build, publish, sync
    python3 scripts/publish_health.py --out DIR    # write elsewhere, no sync
    python3 scripts/publish_health.py --json       # print the model, write nothing
"""
from __future__ import annotations

import argparse
import collections
import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

BJT = timezone(timedelta(hours=8))
INSPECTION_FILE = BASE_DIR / "config" / "inspection_state.json"
PROBE_FILE = BASE_DIR / "logs" / "gmia-fetcher-health.json"
ARTICLES_FILE = BASE_DIR / "data" / "articles.jsonl"
CENSUS_FILE = BASE_DIR / "data" / "template-census.jsonl"
AB_FILE = BASE_DIR / "data" / "ab-gate.jsonl"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
OUTPUT_NAME = "hedge-fund-research-health.html"
DEPLOY_DIR = Path("/var/www/overview")
DOCS_PAGE_RELPATH = f"pages/{OUTPUT_NAME}"

# How old an input may be before the page says so. One nightly cycle plus
# nothing: at 04:50 BJT today's 04:15 data is 35 minutes old and yesterday's
# is 24.6 hours, so 24 separates "the pipeline ran last night" from "it did
# not" without a false alarm on a run started by hand during the day.
MAX_AGE_HOURS = {"pipeline": 24, "probe": 24, "census": 35 * 24, "ab": 8 * 24}
# Inputs the page is meaningless without: both run every night. The other two
# run weekly and monthly, and one that has never produced a line is absent,
# not stale -- a page that cries "stale" about a job that has not had its
# first turn yet teaches the reader to ignore the word.
REQUIRED_INPUTS = ("pipeline", "probe")
TREND_DAYS = 30
# One quiet night is normal for a fund that publishes weekly; two in a row is
# the same threshold the fetcher-health email uses.
ZERO_ALERT_AT = 2
NEW_WINDOW_HOURS = 24

STAGES = ("1_fetch_articles", "2_fetch_content", "3_analyze", "4_publish", "5_verify")


# ── model ────────────────────────────────────────────────────────────────────

def _parse(ts: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None
    if parsed and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BJT)
    return parsed


def _age_hours(ts: str | None, now: datetime) -> float | None:
    parsed = _parse(ts)
    return None if parsed is None else (now - parsed).total_seconds() / 3600


def _freshness(name: str, at: str | None, now: datetime) -> dict:
    age = _age_hours(at, now)
    return {"present": at is not None and age is not None, "at": at,
            "age_hours": None if age is None else round(age, 2),
            "max_age_hours": MAX_AGE_HOURS[name]}


def _newest_stamp(inspection: dict) -> str | None:
    """The most recent last_inspected_at, by instant.

    Not max() over the strings: that compares text. inspection_state.json
    moved from UTC to BJT on 2026-09-26 and a file holding both would rank
    "2026-09-27T03:40+08:00" above "2026-09-26T19:45+00:00", which is five
    minutes older.
    """
    stamps = [v.get("last_inspected_at") for v in (inspection or {}).values()
              if v.get("last_inspected_at") and _parse(v.get("last_inspected_at"))]
    return max(stamps, key=lambda s: _parse(s)) if stamps else None


def build_health(sources: list[dict], inspection: dict, probe: dict, rows: list[dict],
                 census: list[dict], ab: list[dict], corrupt_backups: list[str],
                 now: datetime) -> dict:
    """The whole page as data. Pure: everything it reads is an argument."""
    probe_sources = (probe or {}).get("sources") or {}
    inputs = {
        "pipeline": _freshness("pipeline", _newest_stamp(inspection), now),
        "probe": _freshness("probe", (probe or {}).get("last_run"), now),
        "census": _freshness("census", (census or [{}])[-1].get("at") if census else None, now),
        "ab": _freshness("ab", (ab or [{}])[-1].get("at") if ab else None, now),
    }
    stale = [{"input": name, "age_hours": f["age_hours"]}
             for name, f in inputs.items()
             if (not f["present"] and name in REQUIRED_INPUTS)
             or (f["present"] and f["age_hours"] > f["max_age_hours"])]

    new_counts = _new_per_source(rows, now)
    rendered: list[dict] = []
    for source in sources:
        sid = source["id"]
        state = inspection.get(sid) or {}
        probed = probe_sources.get(sid) or {}
        spec = source.get("listing_template") or {}
        recent = probed.get("last_most_recent_date")
        rendered.append({
            "id": sid,
            "name": source.get("short_name") or sid,
            "template_type": spec.get("type") if source.get("listing_template_active") else None,
            "fetch_shape": source.get("fetch_shape"),
            "found": state.get("last_article_count"),
            "new_last_night": new_counts.get(sid, 0),
            "consecutive_zero": state.get("consecutive_zero_count") or 0,
            "last_refusal": state.get("last_refusal"),
            "last_inspected_at": state.get("last_inspected_at"),
            # A source can drop out of the run without any counter moving:
            # its stored row keeps last night's count and reads as healthy.
            "not_inspected_hours": _age_hours(state.get("last_inspected_at"), now)
            if state.get("last_inspected_at") else None,
            "probe_status": probed.get("status"),
            "probe_reason": probed.get("last_failure_reason") or "",
            "most_recent_date": recent,
            "stale_days": _days_since(recent, now),
        })

    if not sources:
        # An unreadable config yields []. Rendering an empty table and
        # exiting 0 is the one thing a health page must never do.
        stale.insert(0, {"input": "config", "age_hours": None})
    alerts = _alerts(rendered, ab, corrupt_backups, stale)
    totals = {
        "sources": len(rendered),
        "found": sum(r["found"] or 0 for r in rendered),
        "new": sum(r["new_last_night"] for r in rendered),
        "template": sum(1 for r in rendered if r["template_type"]),
        "bespoke": sum(1 for r in rendered if not r["template_type"]),
        "failing": sum(1 for r in rendered if r["probe_status"] == "FAIL"),
    }
    # Counted here, not read from the monthly census: the card above it
    # counts sources.json live, and two numbers for one fact drift apart --
    # for up to a month, which is how often the census runs. The census stays
    # an input only so a page reader can see whether it is still running.
    coverage = collections.Counter(r["template_type"] for r in rendered if r["template_type"])
    stage1 = {"sources": rendered, "totals": totals, "alerts": alerts,
              "trend": _trend(rows, now), "coverage": dict(coverage)}

    return {"generated_at": now.isoformat(timespec="seconds"),
            "inputs": inputs, "stale": stale,
            "stages": {name: (stage1 if name == STAGES[0] else None) for name in STAGES}}


def _days_since(date_str: str | None, now: datetime) -> int | None:
    parsed = _parse(date_str)
    return None if parsed is None else (now.date() - parsed.date()).days


def _new_per_source(rows: list[dict], now: datetime) -> dict[str, int]:
    cutoff = now - timedelta(hours=NEW_WINDOW_HOURS)
    counts: collections.Counter = collections.Counter()
    for row in rows:
        at = _parse(row.get("fetched_at"))
        if at and at >= cutoff:
            counts[row.get("source_id")] += 1
    return counts


def _trend(rows: list[dict], now: datetime) -> list[dict]:
    """Ingest per day, including the days nothing arrived.

    Dropping empty days would draw a line that skips the night the pipeline
    did not run -- the one shape worth seeing.
    """
    counts: collections.Counter = collections.Counter()
    for row in rows:
        at = _parse(row.get("fetched_at"))
        if at:
            counts[at.astimezone(BJT).date()] += 1
    days = [(now.date() - timedelta(days=i)) for i in range(TREND_DAYS - 1, -1, -1)]
    return [{"date": d.isoformat(), "count": counts.get(d, 0)} for d in days]


def _alerts(rendered: list[dict], ab: list[dict], corrupt_backups: list[str],
            stale: list[dict]) -> list[dict]:
    alerts: list[dict] = []
    for row in rendered:
        if row["consecutive_zero"] >= ZERO_ALERT_AT:
            alerts.append({"kind": "consecutive_zero", "source": row["id"],
                           "detail": f"{row['consecutive_zero']} 晚没有抓到任何文章"})
        if row["last_refusal"]:
            alerts.append({"kind": "refusal", "source": row["id"],
                           "detail": f"上次抓取被拒收：{row['last_refusal']}"})
        if (row["not_inspected_hours"] or 0) > MAX_AGE_HOURS["pipeline"]:
            alerts.append({"kind": "not_inspected", "source": row["id"],
                           "detail": f"已 {row['not_inspected_hours'] / 24:.1f} 天未被抓取，"
                                     "表里的数字是那次留下的"})
        if row["probe_status"] == "FAIL":
            alerts.append({"kind": "probe_fail", "source": row["id"],
                           "detail": row["probe_reason"] or "探针失败"})
        elif row["probe_status"] == "WARN":
            alerts.append({"kind": "probe_warn", "source": row["id"],
                           "detail": row["probe_reason"] or "探针告警"})
    last_ab = (ab or [{}])[-1]
    if ab and not last_ab.get("agree", True):
        alerts.append({"kind": "ab_gate", "source": "",
                       "detail": "A/B 闸门不一致：" + ", ".join(last_ab.get("disagreed") or [])})
    for backup in corrupt_backups:
        alerts.append({"kind": "corrupt_state", "source": "",
                       "detail": f"状态文件损坏备份：{backup}"})
    for item in stale:
        age = item["age_hours"]
        alerts.append({"kind": "stale_input", "source": "",
                       "detail": f"输入 {item['input']} 数据过期"
                                 + (f"（{age:.1f} 小时前）" if age is not None else "（缺失）")})
    return alerts


def exit_code(health: dict) -> int:
    """Non-zero only for a page that cannot be trusted.

    A failing source is what the page is for, and the 04:30 email already
    said so; re-alerting it here would train someone to filter both.
    """
    return 1 if health["stale"] else 0


# ── rendering ────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;
--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--blue:#58a6ff;--yellow:#d29922;
--red:#f85149;--purple:#bc8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,
BlinkMacSystemFont,"Segoe UI","Noto Sans SC",Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 12px;color:var(--muted);font-weight:600}
a{color:var(--blue)}
.sub{color:var(--muted);font-size:12px;margin-bottom:20px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 4px}
.chip{background:var(--surface);border:1px solid var(--border);border-radius:999px;
padding:3px 10px;font-size:12px;color:var(--muted)}
.chip.stale{border-color:var(--yellow);color:var(--yellow)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}
.card .v{font-size:26px;font-weight:600}
.card .l{color:var(--muted);font-size:12px;margin-top:2px}
.alerts{border:1px solid var(--yellow);border-radius:8px;background:#1c1810;padding:4px 14px;margin-top:8px}
.alert{padding:8px 0;border-bottom:1px solid #2a2418;font-size:13px}
.alert:last-child{border-bottom:none}
.alert .k{display:inline-block;min-width:118px;color:var(--yellow);font-weight:600}
.alert .s{color:var(--purple)}
.ok{border:1px solid var(--border);border-radius:8px;background:var(--surface);
padding:14px;color:var(--green)}
/* 42 rows x 8 columns is 710px wide, and at 390px that made the whole page
   scroll sideways. The table is what is wide, so the table is what scrolls. */
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:680px}
th{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;
border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:var(--surface2)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.tag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--border);
color:var(--muted);white-space:nowrap}
.tag.tpl{border-color:var(--blue);color:var(--blue)}
.s-OK{color:var(--green)}.s-WARN{color:var(--yellow)}.s-FAIL{color:var(--red)}
.muted{color:var(--muted)}
.bars{display:flex;align-items:flex-end;gap:3px;height:90px;background:var(--surface);
border:1px solid var(--border);border-radius:8px;padding:10px}
.bar{flex:1;background:var(--blue);border-radius:2px 2px 0 0;min-height:2px;opacity:.85;
position:relative}
.bar.zero{background:var(--border)}
.bar:hover{opacity:1;background:var(--purple)}
/* A native title tooltip waits about a second, is unstyled, and was missed
   entirely by the first person to read the chart. This one appears at once
   and reads like the rest of the page. No script: the page is served from a
   static file and stays that way. */
.bar:hover::after{content:attr(data-label);position:absolute;bottom:calc(100% + 6px);
left:50%;transform:translateX(-50%);white-space:nowrap;background:var(--surface2);
border:1px solid var(--border);border-radius:6px;padding:3px 8px;font-size:12px;
color:var(--text);z-index:2;pointer-events:none}
/* The first and last few bars would push their label past the edge of the
   card, so those anchor to their own side instead of the centre. */
.bar:nth-child(-n+3):hover::after{left:0;transform:none}
.bar:nth-last-child(-n+3):hover::after{left:auto;right:0;transform:none}
.legend{color:var(--muted);font-size:12px;margin-top:6px}
"""


def _age_words(days: int | None) -> str:
    """Days between an article's date and today, in words.

    Never "-4 天前": parse_date resolves a month-granularity label ("September
    2026") to the month's LAST day, so a dozen sources legitimately carry a
    date ahead of today, and the first render turned that into a negative.
    """
    if days is None:
        return "—"
    if days == 0:
        return "今天"
    return f"{days} 天前" if days > 0 else f"{-days} 天后"


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _bjt_words(ts: str | None) -> str:
    """A timestamp as BJT, whatever timezone it was written in.

    inspection_state.json stamps UTC (this machine's clock) while everything
    on the page is BJT; printing it raw showed a 03:45 event as 19:45. The
    age in hours was right either way, which is exactly why this kind of
    error survives a reading.
    """
    parsed = _parse(ts)
    if parsed is None:
        return "—"
    return parsed.astimezone(BJT).strftime("%Y-%m-%d %H:%M")


def _chip(name: str, label: str, f: dict) -> str:
    if not f["present"]:
        if name in REQUIRED_INPUTS:
            return f'<span class="chip stale">{_esc(label)}：缺失 · 数据过期</span>'
        return f'<span class="chip">{_esc(label)}：尚未运行</span>'
    stale = f["age_hours"] > f["max_age_hours"]
    age = f["age_hours"]
    when = f"{age:.1f} 小时前" if age < 48 else f"{age / 24:.1f} 天前"
    mark = " · 数据过期" if stale else ""
    return (f'<span class="chip{" stale" if stale else ""}">{_esc(label)}：'
            f'{_esc(_bjt_words(f["at"]))}（{when}{mark}）</span>')


def render_html(health: dict) -> str:
    stage = health["stages"][STAGES[0]]
    t = stage["totals"]
    labels = {"pipeline": "管线抓取", "probe": "健康探针", "census": "模板普查", "ab": "A/B 闸门"}
    chips = "".join(_chip(k, labels[k], v) for k, v in health["inputs"].items())

    cards = "".join(
        f'<div class="card"><div class="v">{v}</div><div class="l">{_esc(l)}</div></div>'
        for v, l in [(t["sources"], "生产源"), (t["found"], "昨夜列表看到"),
                     (t["new"], "昨夜新增入库"), (t["failing"], "探针失败源"),
                     (f'{t["template"]}/{t["sources"]}', "跑模板")])

    if stage["alerts"]:
        kind_labels = {"consecutive_zero": "连续零抓取", "refusal": "抓取被拒收",
                       "probe_fail": "探针失败", "probe_warn": "探针告警",
                       "ab_gate": "A/B 不一致", "corrupt_state": "状态文件损坏",
                       "not_inspected": "该源未抓取", "stale_input": "数据过期"}
        rows = "".join(
            f'<div class="alert"><span class="k">{_esc(kind_labels.get(a["kind"], a["kind"]))}</span>'
            f'<span class="s">{_esc(a["source"])}</span> {_esc(a["detail"])}</div>'
            for a in stage["alerts"])
        alerts_html = f'<div class="alerts">{rows}</div>'
    else:
        alerts_html = (f'<div class="ok">✓ 无告警：{t["sources"]} 源全部正常，'
                       '无连续零抓取、无拒收、探针全绿</div>')

    body_rows = []
    for r in stage["sources"]:
        way = (f'<span class="tag tpl">模板 {_esc(r["template_type"])}</span>'
               if r["template_type"] else
               f'<span class="tag">手写 {_esc(r["fetch_shape"] or "")}</span>')
        status = r["probe_status"] or "—"
        recent = (f'{_esc(r["most_recent_date"])} <span class="muted">'
                  f'({_age_words(r["stale_days"])})</span>' if r["most_recent_date"] else
                  '<span class="muted">—</span>')
        body_rows.append(
            f'<tr><td>{_esc(r["name"])}<div class="muted">{_esc(r["id"])}</div></td>'
            f'<td>{way}</td>'
            f'<td class="num">{"—" if r["found"] is None else r["found"]}</td>'
            f'<td class="num">{r["new_last_night"] or ""}</td>'
            f'<td class="num">{r["consecutive_zero"] or ""}</td>'
            f'<td class="s-{_esc(status)}">{_esc(status)}</td>'
            f'<td>{recent}</td>'
            f'<td class="muted">{_esc((r["probe_reason"] or "")[:80])}</td></tr>')

    peak = max((d["count"] for d in stage["trend"]), default=0) or 1
    bars = "".join(
        f'<div class="bar{" zero" if d["count"] == 0 else ""}" '
        f'style="height:{max(2, round(d["count"] / peak * 100))}%" '
        f'data-label="{_esc(d["date"])} · {d["count"]} 篇" '
        f'aria-label="{_esc(d["date"])} 入库 {d["count"]} 篇"></div>'
        for d in stage["trend"])

    coverage = "、".join(f"{k} {v}" for k, v in sorted(stage["coverage"].items())) or "—"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GMIA 管线健康 — 第 1 阶段</title>
<link rel="icon" href="/favicon.svg">
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>GMIA 管线健康 · 第 1 阶段（抓文章列表）</h1>
  <div class="sub">生成于 {_esc(_bjt_words(health["generated_at"]))} BJT ·
    数据来自管线自身与健康探针，每晚 04:50 BJT 重建 ·
    <a href="/hedge-fund-research.html">研报看板 →</a></div>
  <div class="chips">{chips}</div>

  <h2>昨夜</h2>
  <div class="cards">{cards}</div>

  <h2>告警</h2>
  {alerts_html}

  <h2>逐源状态</h2>
  <div class="tablewrap"><table>
    <thead><tr><th>源</th><th>抓取方式</th><th class="num">列表看到</th>
      <th class="num">新增</th><th class="num">连续零</th><th>探针</th>
      <th>最新文章</th><th>探针备注</th></tr></thead>
    <tbody>{"".join(body_rows)}</tbody>
  </table></div>

  <h2>近 {TREND_DAYS} 天入库量</h2>
  <div class="bars">{bars}</div>
  <div class="legend">每根柱子是一晚入库的文章数，灰色是零，鼠标停在柱子上看具体数字
    · 模板覆盖：{_esc(coverage)}</div>

  <h2>其余阶段</h2>
  <div class="ok" style="color:var(--muted)">第 2–5 阶段尚未做系统审计，面板结构已预留，
    审完再填——为没审过的阶段现编指标，错的指标比没有更糟。</div>
</div>
</body>
</html>
"""


# ── io ───────────────────────────────────────────────────────────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def load_inputs(now: datetime) -> dict:
    """Read every input. A missing or unreadable one becomes stale, not a crash."""
    return {
        "sources": _read_json(SOURCES_FILE, {}).get("sources", []),
        "inspection": _read_json(INSPECTION_FILE, {}),
        "probe": _read_json(PROBE_FILE, {}),
        "rows": _read_jsonl(ARTICLES_FILE),
        "census": _read_jsonl(CENSUS_FILE),
        "ab": _read_jsonl(AB_FILE),
        "corrupt_backups": sorted(p.name for p in INSPECTION_FILE.parent.glob(
            INSPECTION_FILE.name + ".corrupt-*")),
        "now": now,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the stage-1 health page")
    parser.add_argument("--out", help="write the page to this directory instead of the deploy "
                                      "directory, and do not sync docs-site")
    parser.add_argument("--json", action="store_true", help="print the model and write nothing")
    args = parser.parse_args(argv)

    health = build_health(**load_inputs(datetime.now(BJT)))
    if args.json:
        print(json.dumps(health, ensure_ascii=False, indent=1))
        return exit_code(health)

    import publish  # reuse the atomic .html + .gz writer and the docs-site sync

    page = render_html(health)
    out_dir = Path(args.out) if args.out else DEPLOY_DIR
    publish.publish_html(out_dir / OUTPUT_NAME, page)
    print(f"wrote {out_dir / OUTPUT_NAME}")

    if not args.out:
        docs_repo = Path.home() / "docs-site"
        if not publish.sync_docs_site(docs_repo, page, relpath=DOCS_PAGE_RELPATH):
            return 3

    for item in health["stale"]:
        print(f"STALE INPUT: {item['input']} ({item['age_hours']})", file=sys.stderr)
    return exit_code(health)


if __name__ == "__main__":
    sys.exit(main())
