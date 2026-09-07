#!/bin/bash
# Did last night's pipeline actually run on the new model chain?
#
# Written 2026-09-07, the day MODEL_CHAIN became
# [gpt-5.6-luna, gpt-4.1-mini, gemini-2.5-flash].  Every part of that switch
# was verified by direct calls, but analyze_articles.main() itself had not run
# on it: the last real pipeline analysis was 09-07 03:53 BJT on gemini-2.5-pro,
# and there were no pending articles left to trigger one early.
#
# Answers three questions and nothing else:
#   1. which model actually served last night's articles
#   2. whether anything fell through to a lower tier (and why)
#   3. whether the articles it produced carry themes
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

echo "=== 上一次 analyze 运行 ==="
grep -E "Trying |Success \(|failed to parse|All models failed|truncated" logs/analyze_articles.log \
  | tail -30

echo
echo "=== 昨夜记账（按模型） ==="
python3 - <<'PY'
import json, collections
from datetime import datetime, timedelta, timezone
BJT = timezone(timedelta(hours=8))
cut = (datetime.now(BJT) - timedelta(hours=18)).isoformat()
rows = [json.loads(l) for l in open("logs/analyze-usage.jsonl")]
recent = [r for r in rows if r["at"] >= cut]
if not recent:
    print("  ⚠ 最近 18 小时没有任何调用记录 —— 管线可能没跑")
c = collections.Counter((r["model"], r["parsed"]) for r in recent)
for (m, ok), n in sorted(c.items()):
    print(f"  {m:18} parsed={ok}  ×{n}")
bad = [r for r in recent if r.get("provider_total_tokens")
       and r["input_tokens"] + r["output_tokens"] != r["provider_total_tokens"]]
print(f"  对账不平的行: {len(bad)}")
PY

echo
echo "=== 昨夜新摘要的文章：模型与主题 ==="
python3 - <<'PY'
import json
from datetime import datetime, timedelta, timezone
BJT = timezone(timedelta(hours=8))
cut = (datetime.now(BJT) - timedelta(hours=18)).isoformat()
rows = [json.loads(l) for l in open("data/articles.jsonl")]
new = [a for a in rows if a.get("summarized") and str(a.get("fetched_at", "")) >= cut]
print(f"  新增已摘要 {len(new)} 篇")
for a in new:
    flag = "" if a.get("themes") else "   ⚠ 无主题"
    print(f"    {a.get('analysis_model','?'):18} {str(a.get('themes')):46} {a.get('title','')[:40]}{flag}")
empty = sum(1 for a in rows if a.get("summarized") and not a.get("themes"))
print(f"  全语料仍无主题: {empty} 篇 (基线应为 0)")
PY
