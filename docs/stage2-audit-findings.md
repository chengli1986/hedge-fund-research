# Stage 2 (fetch_content) design audit — findings

Evidence gathered 2026-09-26, read-only. Nineteen findings and one
observation. Stage 2 is healthy while this is written — 1,526 of 1,585 rows
have a body (96.3%), and of 89 rows ingested in the last week only 2 have
none — so none of this is firefighting. Every item is about what the stage
does when something changes, or about a mechanism that looks protective and
is not.

Stage 1's audit named a defect family, "detected and never communicated",
and once it was named the remaining instances were found by searching for
the shape rather than by luck. Stage 2's family is a variant:

> **The side that knows says nothing, and the side that does not know
> guesses or defends itself.**

Five of the findings are that shape (A1, B1/B2, C1, C3, D4): the extractor
knows why it failed and returns a bare None; the length rule nominally
belongs to stage 2 and is actually enforced by a model in stage 3; the row
says `ok` while the truth is on disk; the production content directory is
defended by each caller instead of by the writer; and "we do not know why"
is recorded as "this depends on our code".

Scope, method and exclusions: see the audit plan in the session of
2026-09-26. Not covered by choice: per-extractor selector correctness
(scripts/content_audit.py re-checks that weekly) and the session/timeout
handling of the 8 extractors that open their own browser (declared "weak"
coverage in the plan; worth its own pass later).

## ① The extractor layer

| ID | Finding | Evidence |
| --- | --- | --- |
| **A1** | 40 of 44 extractors say nothing about why they failed; 7 places hold a diagnosis and throw it away. All 44 swallow exceptions, so `fetch_with_evidence`'s `exception` is almost always None and the cause is reconstructed by running regexes over log text. | `_extract_bridgewater_text` runs a gate detector, then returns None; the caller logs "no article body found **or** page looks gated" and the classifier falls through to `body_too_short`. Real record, 2026-09-26 03:49. Exhaustive scan: 11 places hold extractor-only knowledge, 7 discard it. |
| **A2** | oaktree is the only extractor whose Playwright-delivered HTML never passes through `_normalize_html`, so a challenge page there is invisible to both mechanisms (no recorded response, no shared check). An instance of A3, but worth its own fix. | AST scan of all 8 browser-driven extractors. Note: F1's commit says bridgewater and matthews are the uncovered ones — they use `requests`, so their challenge pages *are* caught by the response path. The note is inverted. |
| **A3** | 9 extractors bypass `_normalize_html`; any fix made there misses them, silently. | gmo, oaktree, pdf_url, bridgewater, robeco, de_shaw, metlife_im, matthews_asia, gsam |
| **A4** | `_validate_json_response` has tests and no caller. Stage 2 never parses JSON (`.json()` appears 0 times); the JSON APIs belong to stage 1. Tests make dead code look maintained. | grep |

## ② What counts as a body

| ID | Finding | Evidence |
| --- | --- | --- |
| **B1** | `MIN_CONTENT_LENGTH = 100` never binds. The shortest stored body is 165 characters; nothing has ever landed between 100 and 165. | Length distribution of 1,516 stored bodies: P1 371, median 7,868. |
| **B2** | The real floor is stage 3's judgement, and it lets things through: 26 of the 31 bodies under 500 characters were summarised. Nine were read by hand; six are plainly not articles — two video blurbs (the runtime, "03:11", is in the text), a webcast announcement, an event invitation, a podcast blurb, a truncated lead-in. Live, not historical: 16 of the 26 are from current sources, 8 fetched in the last 90 days, the most recent 2026-09-07. | Read the files. |

## ③ The row, the file, and the truth

| ID | Finding | Evidence |
| --- | --- | --- |
| **C1** | 10 rows say `content_status: ok` and carry no `content_path`. Nothing breaks only because two consumers fall back to `content/<id>.txt` — stage 3's `_resolve_content_path` and the weekly audit's `old_path`. The field is decorative: a disagreement between it and the disk cannot be detected. | Exhaustive cross-check of 9 combinations; the other 7 are clean (no path escapes, no missing files, no 0-byte files, no permafail keeping a path). |
| **C2** | 45 orphan files (509 KB) that no row references, and nothing counts or reports them. Two causes: 35 written at 19:47–19:50, the nightly pipeline's own hour, for rows whose id later changed (a host or slug migration that did not rename the file — the case CLAUDE.md warns about); 10 written in one burst at 2026-09-15 06:26. | mtime clustering. |
| **C3** | Writing into the production `content/` directory is a side effect of calling an extractor. The health probe and the weekly audit each monkeypatch `CONTENT_DIR` to defend themselves — the defence is on the caller's side, so any new caller pollutes production by default. Those 10 orphans are what that looks like. | Both defences read in the code. |

## ④ The retry ledger

| ID | Finding | Evidence |
| --- | --- | --- |
| **D1** | In the traceable range, scheduled retries have rescued nothing. Of 47 articles that ever failed, 7 were later fetched successfully — all seven on 2026-09-15 at 09:55 and 11:25, daytime runs by hand after an extractor was fixed, not the nightly run. Confidence: medium — failure lines before 2026-09-15 carry no article id, so earlier fail→succeed pairs cannot be reconstructed. | 6 months of `logs/fetch_content.log`. |
| **D2** | The retry policy cannot be replayed. The labelled ledger starts 2026-09-16 (48 records, 10 days) and a row keeps only its **last** failure, as a dict, not a list. This repo's own rule — replay history before choosing a threshold — cannot be applied here. | File inspection. |
| **D4** | `body_too_short` is the classifier's catch-all *and* is marked `code_dependent`, so "we could not tell why" is recorded as "this depends on our code" and every such article is requeued whenever `fetch_content.py` changes. With A1 (40 of 44 extractors silent), that is the churn engine. | The other 8 labels' `code_dependent` flags match their semantics; this one and `pdf_not_usable` are the two judgement calls. |
| **D5** | `mark_content_failure`'s `failure=None` branch is unreachable in production **and** in the tests — its only caller always passes a labelled failure. So `MAX_CONTENT_ATTEMPTS = 5` and the "retire at max_attempts" rule it guards never run. | grep of every call site. |
| **D6** | `ATTEMPT_CEILING = 12` cannot change an outcome. Per-label caps are 3–5 and three labels retire at a streak of 2, so another gate always fires first; for a row already retired, the ceiling and `was_permafail` give the same answer. | The policy table, and the gsam rows at attempts=11. |
| *obs* | After a permafail is requeued by a code change and fails again, its `streak` keeps counting, so "streak 6" reads as six consecutive same-label failures when it is three plus three requeues. Ledger legibility only; no behaviour depends on it. | gsam: attempts 11, streak 6. |

## ⑤ The boundary with stage 3

| ID | Finding | Evidence |
| --- | --- | --- |
| **E1** | Of the 54 declines, 17 cost a model call, and **not one** comes from a source that has a stage-2 detector for its condition. Where detectors exist (apollo, matthews, verdad, bridgewater) they catch the case for free. The cost sits entirely where stage 2 is silent. | Cross-referenced every declined row against the detectors in the code. |
| **E2** | lazard-am accounts for 7 of those 17: seven near-identical 2,887–2,889 character bodies, all of them the weekly column's standing description ("Each week, I provide my views on…") rather than that week's article. Each paid for its own model call. Near-duplicate detection cannot help: it indexes only **summarised** articles, so a body that is always declined never becomes the owner the copies would match. | Read the bodies; `analyze_articles` line 796. |
| **E3** | ARK's metadata fallback is a policy disagreement, not a missing mechanism. The lighter prompt exists (`METADATA_PROMPT`) and a pre-check declines title-only bodies without a model call (28 of 29 cost nothing). The one that reached the model — 861 characters with a full publisher summary — was refused on principle: "Only article metadata and a publisher-provided summary are supplied, not the article itself." Stage 2 is built on "a publisher summary is enough"; the model's rule is that it is not, and the model decides. The path yields nothing whatever the fields contain. | 29 rows, all declined; the reason is stored on the row. |

## ⑥ Do the 2026-09-19 fixes still hold?

Each fix was reverted by hand and the suite re-run. Seven are protected:
F1, F2, F4, F5, F6, D2, D3 all turn the suite red when removed.

| ID | Finding | Evidence |
| --- | --- | --- |
| **F1** | F3 ("classify on every captured message, not only the last") has no effective guard: reverting it leaves the suite green. Its own test feeds three messages and asserts `fetch_error`, but the last of them, "all attempts failed, giving up", matches `_ERROR_MESSAGE` by itself — the test asserts the right outcome through the wrong mechanism. | Mutant survived. |
| **F2** | D1's fix died with the code it fixed. It guarded Gemini's unguarded `candidates[0]`; Gemini was removed on 2026-09-21, and both surviving clients have the same shape: `_call_openai` line 319 `data["choices"][0]["message"]["content"]`, `_call_anthropic` line 343 `data["content"][0]["text"]`. An empty list from a content filter raises IndexError, which the chain logs as an unexplained failure and retries — D1's original symptom. | Read both clients. |

## Where to start

**D4 with A1.** They are two halves of one engine: the extractor says
nothing → the failure lands in the catch-all → the catch-all is marked
"depends on our code" → every article in it is requeued on every code
change. Fixing either alone leaves the engine running.

## Corrections made during this audit

Two claims were written and then disproved by checking, both because a
conclusion was drafted before its own command output was read:

- "No lighter prompt exists" — `METADATA_PROMPT` was in the output of the
  grep that the claim was written under. Corrected in `9bd8ba1`.
- "No test covers F3" — the grep printed three test files immediately
  above the sentence saying it printed none. The finding survived (the
  mutant does survive) but for a different reason, recorded in F1.

A regression introduced earlier the same day was also found and fixed
mid-audit (`62dbbd2`): the rss_feed template dropped the two fields ARK's
metadata fallback reads, on the strength of a grep for `get("summary")`
that does not match `get("summary", "")`.
