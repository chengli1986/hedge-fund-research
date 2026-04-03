# GMIA Entrypoint Autoresearch: Scorer Weight Optimization

## Goal
Maximize the **article yield** — the fraction of fetched articles that are
genuine, summarized market insights (not disclaimers, not noise).

## The ONE file you can edit
`config/scorer_weights.json` — the four scorer weights.

## The metric
Run: `cd ~/hedge-fund-research && python3 evaluate_entrypoints.py`
Read the last line: `Overall yield: 0.XXXX`
Higher is better.

## How scoring works
The entrypoint scorer grades candidate research pages on 4 dimensions:
- **domain**: does the URL match the expected fund domain? (0.0-1.0)
- **path**: does the URL path contain research-related keywords? (0.0-1.0)
- **structure**: does the HTML have article/time/PDF signals? (0.0-1.0)
- **gate**: is the page behind a paywall or login? (penalty 0.0-1.0)

The final score = `domain*W1 + path*W2 + structure*W3 + (1-gate)*W4`

Current weights: `{"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}`

## Rules
1. **NEVER edit** any file except `config/scorer_weights.json`
2. **NEVER edit** evaluate_entrypoints.py, entrypoint_scorer.py, or any other code
3. Before EACH experiment: `cd ~/hedge-fund-research && git add -A && git commit -m "experiment: <description>"`
4. Run: `python3 evaluate_entrypoints.py` and read the yield
5. If yield **improved**: keep the commit, log to autoresearch/results.tsv
6. If yield **worsened or stayed the same**: `git reset --hard HEAD~1`
7. Log EVERY experiment to `autoresearch/results.tsv` (even failures)
8. **NEVER STOP** — keep running experiments until told to stop

## results.tsv format
Append one line per experiment (tab-separated):
```
commit_hash	yield	status	description
```

## Constraints
- scorer_weights.json must remain valid JSON
- All 4 keys must be present: domain, path, structure, gate
- All values must be in [0.05, 0.6]
- Values must sum to 1.0 (tolerance +/- 0.01)

## Experiment ideas (try in this order)
1. Increase structure weight (0.3 -> 0.4), decrease domain (0.2 -> 0.1)
2. Increase path weight (0.3 -> 0.35), decrease gate (0.2 -> 0.15)
3. Equal weights (0.25/0.25/0.25/0.25)
4. Structure-dominant (0.1/0.2/0.5/0.2)
5. Path-dominant (0.1/0.5/0.2/0.2)
6. Minimize gate penalty influence (0.25/0.3/0.4/0.05)
7. Domain-heavy for trust (0.4/0.2/0.2/0.2)
