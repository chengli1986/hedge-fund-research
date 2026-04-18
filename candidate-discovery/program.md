# GMIA Candidate Fund Discovery — Agent Program

## Goal

You are a hedge fund research analyst. Your job is to discover and evaluate candidate hedge funds that publish public research, insights, or commentary pages — potential additions to the GMIA aggregator.

## Phase 1: Run the discovery pipeline

```bash
cd /home/ubuntu/hedge-fund-research
python3 discover_fund_sites.py
python3 screen_fund_candidates.py  
python3 discover_candidate_entrypoints.py
```

Read the output. Note which funds changed status.

## Phase 2: LLM deep analysis

**Skip logic — check BEFORE analyzing each fund:**
- Read `config/fund_candidates.json`
- If `last_deep_analyzed_at` exists and is **within 7 days** AND status hasn't changed → **SKIP** (already analyzed recently)
- If status is `rejected` or `watchlist` and `last_deep_analyzed_at` is **within 30 days** → **SKIP** (low-value candidates recheck monthly)
- Only analyze funds that are: (a) never analyzed, (b) analysis is stale (>7 days), or (c) status changed since last analysis

For each candidate that NEEDS analysis:

1. **Fetch the research URL** using WebFetch
2. **Analyze the page content** — answer these questions:
   - Is this a genuine research article index (multiple articles with dates)?
   - What topics do they cover? (macro, credit, quant, equity, fixed income, alternatives, ESG?)
   - How frequently do they publish? (weekly / monthly / quarterly)
   - When was the most recent article published?
   - Is content freely accessible or gated?
   - Quality rating: HIGH (deep original analysis) / MEDIUM (market commentary) / LOW (marketing/PR)

3. **Assess GMIA fit** — our current 6 funds cover:
   - Man Group: macro, quant, credit, volatility
   - Bridgewater: macro, global economy
   - AQR: factor investing, quant, alternatives
   - GMO: asset allocation, valuation, emerging markets
   - Oaktree: credit, distressed debt, risk
   - ARK: disruptive tech, innovation, growth

   Does this candidate fill a gap? Or overlap heavily with existing coverage?

4. **Update fund_candidates.json** with your analysis:
   - Set `last_deep_analyzed_at` to current UTC ISO timestamp
   - Set `quality` field to "HIGH", "MEDIUM", or "LOW"
   - Set `topics` field to a short comma-separated list (e.g., "fixed income, macro, credit")
   - Set `strategy_tags` to a JSON array using ONLY tags from this fixed set:
     `fixed_income`, `private_credit`, `event_driven`, `macro`, `quant`,
     `private_equity`, `real_assets`, `equity`, `multi_asset`, `esg_climate`,
     `emerging_markets`, `venture_capital`
     Pick all that apply (1–4 tags typical). Example: `["fixed_income", "macro", "multi_asset"]`
   - Set `notes` field to a **one-line summary, max 60 characters** (e.g., "Weekly private credit research, fills PE gap")
   - If quality is HIGH and updated within 30 days: prefix notes with "RECOMMEND: " (still max 60 chars total)
   - If clearly not suitable: set status to "watchlist" or "rejected" with reason in notes

## Phase 3: Discover NEW candidates (beyond seed list)

**Skip logic:** If ALL existing candidates have been deep-analyzed within 7 days, spend this session's budget on discovering new funds. Otherwise, prioritize analyzing existing un-analyzed candidates first.

Use WebSearch to find additional hedge funds with public research:

```
"hedge fund" "insights" OR "research" OR "perspectives" site:.com -careers -jobs 2026
"asset manager" "quarterly letter" OR "market commentary" site:.com 2026
```

For any promising new fund NOT already in fund_seeds.json:
1. Verify it has an official website with a research/insights section
2. Add it to `config/fund_seeds.json` with appropriate metadata
3. Run `python3 discover_fund_sites.py --fund <new-id>` to discover it
4. **Maximum 1 new seed per session** (slow deliberate growth)

## Phase 4: Commit and report

```bash
cd /home/ubuntu/hedge-fund-research
DISCOVERY_DATE=$(TZ='Asia/Shanghai' date '+%Y-%m-%d')
git add config/fund_seeds.json config/fund_candidates.json config/candidate_entrypoints.json
git diff --cached --quiet || git commit -m "data(candidate): daily discovery run ${DISCOVERY_DATE}"
git push
```

Output a brief summary:
- How many funds checked vs skipped (already analyzed)
- Any status changes
- Any new seeds added
- Any recommendations

## Rules

- **NEVER** modify `config/sources.json` or `config/entrypoints.json` (production files)
- **Maximum 1 new seed per session** (deliberate growth, not bulk expansion)
- Maximum 3 WebSearch queries per session
- Maximum 8 WebFetch calls per session
- **Maximum 4 deep analyses per session** — if more candidates need analysis, pick the 4 highest priority (never-analyzed first, then stalest `last_deep_analyzed_at`), defer the rest to the next session
- If a page requires login/payment, mark as "rejected: gated content" and move on
- Keep notes concise (1-2 sentences max)
- Always set `last_deep_analyzed_at` after analyzing a fund
