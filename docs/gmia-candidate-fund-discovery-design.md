# GMIA Candidate Fund Discovery — Design Document

## Goal

Add a **candidate fund discovery** layer that finds mature hedge funds with public research pages, validates them through GMIA's existing entrypoint pipeline, and surfaces them for manual promotion into production.

## Non-Goals

- Auto-add candidates into production `config/sources.json`
- Let candidate data affect production AUTORESEARCH scorer tuning
- Unbounded background exploration (strict cost limits)
- Replace existing GMIA pipeline logic

## Architecture

```
Seed Pool (fund_seeds.json)
    |
    v
Discovery Layer (discover_fund_sites.py)
    |  - find official homepage
    |  - locate research/insights pages
    |  - detect RSS feeds
    |  - gather freshness signals
    v
Rule-Based Screening (screen_fund_candidates.py)
    |  - official domain check
    |  - public accessibility
    |  - research index vs marketing
    |  - freshness within 90 days
    v
Candidate Entrypoint Discovery (discover_candidate_entrypoints.py)
    |  - reuse entrypoint_scorer.py
    |  - write to candidate_entrypoints.json (NOT production)
    v
Candidate Validation (validate_candidate_entrypoints.py)
    |  - HTTP health + re-scoring
    |  - gate/login risk detection
    v
Candidate Evaluation (evaluate_candidate_sources.py)
    |  - article yield, noise ratio, freshness
    v
Manual Promotion (promote_candidate.py) — Phase 2+
```

## Isolation Principle

**Critical**: Candidate data MUST NOT touch production files.

| Production File | Candidate Parallel |
|---|---|
| `config/sources.json` | `config/fund_candidates.json` |
| `config/entrypoints.json` | `config/candidate_entrypoints.json` |
| `autoresearch/results.tsv` | (none in Phase 1) |

## Candidate Status Model

```
seed → discovered → screened → validated → watchlist/rejected/promoted
```

- **seed**: listed as target, not yet explored
- **discovered**: homepage and/or research pages found
- **screened**: passes rule-based filtering
- **validated**: passes entrypoint + quality validation
- **watchlist**: interesting but low activity / weak fit
- **rejected**: not suitable for GMIA
- **promoted**: manually accepted into production (Phase 2+)

## Phase 1 Seed Pool

Prioritized by likelihood of having public research:

| Fund | Expected | Notes |
|---|---|---|
| **PIMCO** | Best candidate | Extensive public research, high publication frequency |
| **D.E. Shaw** | Good | Has "perspectives" page |
| **Blackstone** | Good | Has insights, but PE-focused |
| **Two Sigma** | Moderate | Blog exists, mostly technical/recruiting |
| **KKR** | Moderate | Has insights page |

Excluded (already in production): Bridgewater, AQR, Man Group, GMO, Oaktree, ARK

## Data Models

### fund_seeds.json

```json
[
  {
    "id": "pimco",
    "name": "PIMCO",
    "aliases": ["Pacific Investment Management"],
    "category": "fixed_income",
    "homepage": "https://www.pimco.com",
    "notes": "Largest fixed income manager, extensive public research"
  }
]
```

### fund_candidates.json

```json
[
  {
    "id": "pimco",
    "name": "PIMCO",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  }
]
```

### candidate_entrypoints.json

Same schema as production `entrypoints.json` but only for candidates.

## Cost Control (Phase 1)

- **Tier 0**: Rules only, no model calls
- Max 5 firms per run
- Max 20 candidate links per firm
- Max runtime 10 minutes
- No background Codex exploration
- Weekly manual trigger (no cron in Phase 1)

## Phase Plan

| Phase | Scope | Production Writes |
|---|---|---|
| **Phase 1** | Seed + Discovery + Screening + Report | None |
| **Phase 2** | Candidate validation + evaluation | None |
| **Phase 3** | Manual promotion workflow | Controlled |
