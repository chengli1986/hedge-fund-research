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

For each candidate with status "discovered" or "validated" in `config/fund_candidates.json`:

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
   - Add a `notes` field with your assessment (1-2 sentences)
   - If quality is HIGH and updated within 30 days: prefix notes with "RECOMMEND: "
   - If clearly not suitable: set status to "watchlist" or "rejected" with reason

## Phase 3: Discover NEW candidates (beyond seed list)

Use WebSearch to find additional hedge funds with public research:

```
"hedge fund" "insights" OR "research" OR "perspectives" site:.com -careers -jobs 2026
"asset manager" "quarterly letter" OR "market commentary" site:.com 2026
```

For any promising new fund NOT already in fund_seeds.json:
1. Verify it has an official website with a research/insights section
2. Add it to `config/fund_seeds.json` with appropriate metadata
3. Run `python3 discover_fund_sites.py --fund <new-id>` to discover it
4. Maximum 2 new seeds per session (cost control)

## Phase 4: Commit and report

```bash
cd /home/ubuntu/hedge-fund-research
git add config/fund_seeds.json config/fund_candidates.json config/candidate_entrypoints.json
git diff --cached --quiet || git commit -m "data(candidate): daily discovery run $(TZ=Asia/Shanghai date +%Y-%m-%d)"
git push
```

Output a summary table:
```
Fund            Status      Fit Score  Quality  Last Updated  Recommendation
----            ------      ---------  -------  ------------  --------------
pimco           validated   0.850      HIGH     2026-04-02    RECOMMEND: deep fixed income research
de-shaw         validated   1.000      MEDIUM   2026-03-15    Watchlist: infrequent updates
...
```

## Rules

- **NEVER** modify `config/sources.json` or `config/entrypoints.json` (production files)
- Maximum 2 new seeds per session
- Maximum 5 WebSearch queries per session
- Maximum 10 WebFetch calls per session
- If a page requires login/payment, mark as "rejected: gated content" and move on
- Keep notes concise (1-2 sentences max)
