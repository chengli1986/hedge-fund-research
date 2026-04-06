# GMIA Entrypoint Autoresearch: Scorer Weight Optimization

## Goal
Maximize **overall_precision** — a composite metric that measures how well the
scorer weights separate good research pages from bad pages (careers, legal, cookie).
Higher is better. Baseline: 0.9182.

## The ONE file you can edit
`config/scorer_weights.json` — the four scorer weights.

## The metric
Run: `cd ~/hedge-fund-research && python3 evaluate_entrypoints.py`
Read the last line: `overall_precision: 0.XXXX`
Higher is better.

## How scoring works
The entrypoint scorer grades candidate research pages on 4 dimensions:
- **domain**: does the URL match the expected fund domain? (0.0-1.0)
- **path**: does the URL path contain research-related keywords? (0.0-1.0)
- **structure**: does the HTML have article/time/PDF signals? (0.0-1.0)
- **gate**: is the page behind a paywall or login? (penalty 0.0-1.0)

The final score = `domain*W1 + path*W2 + structure*W3 + (1-gate)*W4`

Current weights: loaded from `config/scorer_weights.json`

## How evaluation works
The evaluator re-scores ALL entrypoints (good + bad) in `config/candidate_entrypoints.json`
using the current weights, then measures:
- **precision**: fraction of good URLs scoring above 0.5 threshold
- **reject_rate**: fraction of bad URLs scoring below 0.5 threshold
- **overall**: 0.6 * precision + 0.4 * reject_rate

Key problem funds to improve:
- **two-sigma**: some good URLs (cookie/privacy) incorrectly labeled — focus on path weight
- **blackstone**: career-pathways URL borderline — focus on path negative keywords

## Rules
1. **NEVER edit** any file except `config/scorer_weights.json`
2. **NEVER edit** evaluate_entrypoints.py, entrypoint_scorer.py, candidate_entrypoints.json, or any other code
3. Before EACH experiment: `cd ~/hedge-fund-research && git add -A && git commit -m "experiment: <description>"`
4. Run: `python3 evaluate_entrypoints.py` and read the overall_precision
5. If overall_precision **improved**: keep the commit, log to autoresearch/results.tsv
6. If overall_precision **worsened or stayed the same**: `git reset --hard HEAD~1`
7. Log EVERY experiment to `autoresearch/results.tsv` (even failures)
8. **NEVER STOP** — keep running experiments until told to stop

## results.tsv format
Append one line per experiment (tab-separated):
```
commit_hash	yield	status	description
```
Note: the "yield" column now contains overall_precision values.

## Constraints
- scorer_weights.json must remain valid JSON
- All 4 keys must be present: domain, path, structure, gate
- All values must be in [0.05, 0.6]
- Values must sum to 1.0 (tolerance +/- 0.01)

## Experiment ideas (try in this order)
1. Increase path weight (research keywords matter most for separation)
2. Decrease domain weight (all URLs share the fund's domain, so domain doesn't differentiate)
3. Increase gate weight (bad pages have higher gate penalties)
4. Decrease structure weight (estimated, less reliable signal)
5. Path-dominant: 0.1/0.5/0.2/0.2
6. Gate+path focus: 0.1/0.4/0.15/0.35
7. Minimize domain: 0.05/0.4/0.25/0.3
