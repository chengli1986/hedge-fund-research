# Stage 2 (fetch_content) design audit — findings

Evidence gathered 2026-09-26, read-only; A5 and A6 added 2026-09-27 while
fixing F2 and A1. Twenty-one findings and one observation; F2, D4, A1, A6 and A2 are
fixed, and A3 is mitigated (see the deferred-unification section). Stage 2 is healthy while this is written — 1,526 of 1,585 rows
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
| **A6** | *(found and fixed 2026-09-27 while fixing A1)* Bridgewater refused any body containing "disclaimer", "privacy policy", "terms of use" or cookie wording, and dropped a 31,148-character research note whole because it reads "In **terms of use** cases, investors can also…". 104 of the 1,548 stored bodies (7%) contain one of those phrases, median length 11,594 — they are ordinary prose. Its own 19 stored articles contain none, so the rule had no win against that loss. | Read the live page; measured the store |
| **A1** | *(fixed 2026-09-27)* 40 of 44 extractors say nothing about why they failed; 7 places hold a diagnosis and throw it away. All 44 swallow exceptions, so `fetch_with_evidence`'s `exception` is almost always None and the cause is reconstructed by running regexes over log text. | `_extract_bridgewater_text` runs a gate detector, then returns None; the caller logs "no article body found **or** page looks gated" and the classifier falls through to `body_too_short`. Real record, 2026-09-26 03:49. Exhaustive scan: 11 places hold extractor-only knowledge, 7 discard it. |
| **A2** | *(fixed 2026-09-28)* oaktree is the only extractor whose Playwright-delivered HTML never passes through `_normalize_html`, so a challenge page there is invisible to both mechanisms (no recorded response, no shared check). An instance of A3, but worth its own fix. | AST scan of all 8 browser-driven extractors. Note: F1's commit says bridgewater and matthews are the uncovered ones — they use `requests`, so their challenge pages *are* caught by the response path. The note is inverted. |
| **A3** | *(mitigated 2026-09-28, not unified)* 9 extractors bypass `_normalize_html`; any fix made there misses them, silently. | gmo, oaktree, pdf_url, bridgewater, robeco, de_shaw, metlife_im, matthews_asia, gsam |
| **A5** | `_call_anthropic` is defined and never called: `model_to_caller` holds only the two OpenAI models, so the chain has no non-OpenAI tier. CLAUDE.md still describes "Gemini 2.5 Pro → GPT-4.1 Mini → Claude Sonnet". Found while fixing F2. | grep; the map at analyze_articles.py:646 |
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
| **D4** | *(fixed 2026-09-27)* `body_too_short` is the classifier's catch-all *and* is marked `code_dependent`, so "we could not tell why" is recorded as "this depends on our code" and every such article is requeued whenever `fetch_content.py` changes. With A1 (40 of 44 extractors silent), that is the churn engine. | The other 8 labels' `code_dependent` flags match their semantics; this one and `pdf_not_usable` are the two judgement calls. |
| **D5** | `mark_content_failure`'s `failure=None` branch is unreachable in production **and** in the tests — its only caller always passes a labelled failure. So `MAX_CONTENT_ATTEMPTS = 5` and the "retire at max_attempts" rule it guards never run. | grep of every call site. |
| **D6** | `ATTEMPT_CEILING = 12` cannot change an outcome. Per-label caps are 3–5 and three labels retire at a streak of 2, so another gate always fires first; for a row already retired, the ceiling and `was_permafail` give the same answer. | The policy table, and the gsam rows at attempts=11. |
| *obs* | After a permafail is requeued by a code change and fails again, its `streak` keeps counting, so "streak 6" reads as six consecutive same-label failures when it is three plus three requeues. Ledger legibility only; no behaviour depends on it. | gsam: attempts 11, streak 6. |

## ⑤ The boundary with stage 3

| ID | Finding | Evidence |
| --- | --- | --- |
| **E1** | Of the 54 declines, 17 cost a model call, and **not one** comes from a source that has a stage-2 detector for its condition. Where detectors exist (apollo, matthews, verdad, bridgewater) they catch the case for free. The cost sits entirely where stage 2 is silent. | Cross-referenced every declined row against the detectors in the code. |
| **E2** | lazard-am accounts for 7 of those 17: seven near-identical 2,887–2,889 character bodies, all of them the weekly column's standing description ("Each week, I provide my views on…") rather than that week's article. Each paid for its own model call. Near-duplicate detection cannot help: it indexes only **summarised** articles, so a body that is always declined never becomes the owner the copies would match. | Read the bodies; `analyze_articles` line 796. |
| **E3** | ARK's metadata fallback is a policy disagreement, not a missing mechanism. The lighter prompt exists (`METADATA_PROMPT`) and a pre-check declines title-only bodies without a model call (28 of 29 cost nothing). The one that reached the model — 861 characters with a full publisher summary — was refused on principle: "Only article metadata and a publisher-provided summary are supplied, not the article itself." Stage 2 is built on "a publisher summary is enough"; the model's rule is that it is not, and the model decides. The path yields nothing whatever the fields contain. | 29 rows, all declined; the reason is stored on the row. |

## ⑦ The decline label, 2026-09-29

Opened by two health emails (9-28, 9-29) whose only new line was three
loomis-sayles articles declined as `disclaimer_only`. The declines were
correct -- all three are video pages whose body is one sentence of blurb plus
Loomis's standard disclosure -- but checking *why* they were labelled that way
turned up something else.

`failure_labels.classify_analysis_decline` turns a decline reason back into a
countable label with an **ordered** regex list: first rule that matches wins.
That is right for a reason a model wrote in free text. It was also being
applied to the three reasons `analyze_articles.py` writes itself, and two of
the labels it can return are load-bearing:

| Label | Consumer | What a wrong label does |
| --- | --- | --- |
| `duplicate_body` | `publish.py:701` drops the row | the same body is published twice |
| `grounding_failed` | `_should_analyze` re-queues on a rule change | the row is never re-analysed |

| ID | Finding | Evidence | Status |
| --- | --- | --- | --- |
| **G1** | `duplicate_reason` interpolates the **other article's title**, so a plausible title steals the label: `"Why Only A Title Is Not Enough"` classified as `title_only`, above `duplicate_body` in the list. The row then escapes publish.py's duplicate filter silently. | Reproduced against the real builder; end-to-end guard test. | **Fixed** — the three code-written declines now declare `_label`; only a model's free text is classified. |
| **G2** | `grounding_reason` quotes up to 80 characters of the rejected summary (`_NOT_AN_ARTICLE` alternative 4 has `[^.;]{0,80}?`), so a summary calling itself "only a title" steals the label the same way. Consequence is invisible: the row is simply never re-queued. | Reproduced through the real `_analyze_with_fallback` grounding path. | **Fixed** with G1. |
| **G3** | 9 of 57 stored declines (16%) match more than one rule, so their label is decided by list position, not by evidence. `disclaimer_only` (`disclaimer\|disclosure\|legal`) is a magnet: every asset manager's page carries a disclosure. The loomis videos are 3 of the 4 `disclaimer_only`-over-`teaser_only` cases. | Counted over `articles.jsonl`. | **Recorded, not fixed.** These labels are reporting-only. Reordering would change the email's counts, and there is no evidence that any new order is more true than the current one. |
| **G4** | The decline taxonomy has no video category, while the content taxonomy does (`media_without_text`). Adding one would not help: the classifier never sees the page, only the model's one-sentence reason, and **none of the three loomis reasons contains the word "video"**. A new rule would have to match "promotional description", which would take rows from `teaser_only` on no evidence. | Checked all three stored reasons. | **Rejected** — the fix proposed first would have caught 0 of the 3 articles it was for. |

Neither G1 nor G2 has ever fired: 7 of 7 stored `duplicate_body` rows are
labelled correctly and no grounding failure has ever been stored (the
wording-only retry catches them first). Both were reachable, and both fail
quietly, which is this stage's recurring shape.

Not from this audit, found while running the suite on 2026-09-29:
`test_unit_trial_quality.py::test_sample_article_quality_tracks_js_only_count`
fails on an early-return path in `sample_article_quality` that omits
`js_only_count`, which the other two return sites set. Pre-existing, unrelated
to stage 2, unowned.

## ⑥ Do the 2026-09-19 fixes still hold?

Each fix was reverted by hand and the suite re-run. Seven are protected:
F1, F2, F4, F5, F6, D2, D3 all turn the suite red when removed.

| ID | Finding | Evidence |
| --- | --- | --- |
| **F1** | F3 ("classify on every captured message, not only the last") has no effective guard: reverting it leaves the suite green. Its own test feeds three messages and asserts `fetch_error`, but the last of them, "all attempts failed, giving up", matches `_ERROR_MESSAGE` by itself — the test asserts the right outcome through the wrong mechanism. | Mutant survived. |
| **F2** | *(fixed 2026-09-27)* D1's fix died with the code it fixed. It guarded Gemini's unguarded `candidates[0]`; Gemini was removed on 2026-09-21, and both surviving clients have the same shape: `_call_openai` line 319 `data["choices"][0]["message"]["content"]`, `_call_anthropic` line 343 `data["content"][0]["text"]`. An empty list from a content filter raises IndexError, which the chain logs as an unexplained failure and retries — D1's original symptom. | Read both clients. |

## Unifying the nine with the shared path — deferred, with its prerequisite

Considered on 2026-09-28 and deliberately not done yet. Six of the nine
(robeco, de-shaw, gsam, metlife-im, matthews-asia, bridgewater) are close
copies of `_normalize_html`: same decompose step, same `soup.select`, and
they already call the shared `_paragraph_text`. They differ only in which
containers they strip and, for bridgewater, an ordered selector ladder.
Three (gmo, oaktree, pdf_url) are genuinely different: the page is a door
to a PDF.

So unification is feasible, and the way to do it is to split
`_normalize_html` into the four things it welds together -- challenge
check, strip, select-and-record, text join -- so a caller can take three
of them and do its own selecting. What stops it today is proof, not
design:

- The repo has no corpus of saved HTML, so there is no way to run the old
  and new extraction over identical input. Comparing two live fetches does
  not substitute: the 2026-09-28 baseline (84 bodies, 2 per source) found
  only 55 identical, and most of the other 28 were the sites' own edits
  between fetches.
- The stored bodies are not a clean reference either. Several predate
  extractor fixes: troweprice's stored text carries 6,000 characters of
  OneTrust cookie panel, man-group's starts with breadcrumb navigation,
  de-shaw's has words fused from before c149894. Today's extraction is
  cleaner than what is on disk.

Prerequisite, therefore: a record/replay harness that captures each page's
HTML once and replays it into both code versions. Until that exists, a
refactor touching the 35 sources on the shared path cannot be shown to
have changed nothing, and "it looked fine" is not the standard this
pipeline holds elsewhere.

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
