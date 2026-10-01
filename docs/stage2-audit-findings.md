# Stage 2 (fetch_content) design audit — findings

Evidence gathered 2026-09-26, read-only; A5 and A6 added 2026-09-27 while
fixing F2 and A1. Twenty-five findings and one observation. Fixed: F2, D4, A1, A6, A2, G1,
G2, and -- in the 2026-09-30 batch below -- F1, D5, C1, A4, A5; on 2026-10-01, C2 and C3.
Mitigated: A3 (see the deferred-unification section). Rejected on the
evidence: G4, D6. Recorded without a fix by decision: G3. Seven remain
open: B1, B2, D1, D2, E1, E2, E3. Stage 2 is healthy while this is written — 1,526 of 1,585 rows
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
| **C2** | *(re-counted 2026-09-30: **35 files, 261 KB**, and the causal story below no longer holds — the 2026-09-15 06:26 burst of 10 is gone and nobody recorded why. Re-derive the cause before acting.)* 45 orphan files (509 KB) that no row references, and nothing counts or reports them. Two causes: 35 written at 19:47–19:50, the nightly pipeline's own hour, for rows whose id later changed (a host or slug migration that did not rename the file — the case CLAUDE.md warns about); 10 written in one burst at 2026-09-15 06:26. | mtime clustering. |
| **C3** | Writing into the production `content/` directory is a side effect of calling an extractor. The health probe and the weekly audit each monkeypatch `CONTENT_DIR` to defend themselves — the defence is on the caller's side, so any new caller pollutes production by default. Those 10 orphans are what that looks like. | Both defences read in the code. |

## ④ The retry ledger

| ID | Finding | Evidence |
| --- | --- | --- |
| **D1** | In the traceable range, scheduled retries have rescued nothing. Of 47 articles that ever failed, 7 were later fetched successfully — all seven on 2026-09-15 at 09:55 and 11:25, daytime runs by hand after an extractor was fixed, not the nightly run. Confidence: medium — failure lines before 2026-09-15 carry no article id, so earlier fail→succeed pairs cannot be reconstructed. | 6 months of `logs/fetch_content.log`. |
| **D2** | The retry policy cannot be replayed. The labelled ledger starts 2026-09-16 (48 records, 10 days) and a row keeps only its **last** failure, as a dict, not a list. This repo's own rule — replay history before choosing a threshold — cannot be applied here. | File inspection. |
| **D4** | *(fixed 2026-09-27)* `body_too_short` is the classifier's catch-all *and* is marked `code_dependent`, so "we could not tell why" is recorded as "this depends on our code" and every such article is requeued whenever `fetch_content.py` changes. With A1 (40 of 44 extractors silent), that is the churn engine. | The other 8 labels' `code_dependent` flags match their semantics; this one and `pdf_not_usable` are the two judgement calls. |
| **D5** | `mark_content_failure`'s `failure=None` branch is unreachable **in production**: its only production caller (`_record_content_failure`) always passes a labelled failure, so `MAX_CONTENT_ATTEMPTS = 5` and the "retire at max_attempts" rule it guards never run. *Corrected 2026-09-30: the original wording said "and in the tests", which is false* — `TestContentDeadLetterCap` calls `mark_content_failure(a)` with no failure and asserts the retirement. That makes it a sharper case, not a weaker one: two green tests assert a rule production cannot reach. | grep of every call site; tests/test_unit_fetch_content.py:268,275. |
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

## C2 re-derived — and then corrected, 2026-10-01

**The 2026-09-30 version of this section was wrong about the cause.** It
attributed the lost rows to the store race fixed on 2026-09-21
(`_carry_over_unseen`). That mechanism cannot have done it: it drops rows
*appended while a stage is working*, and these were rows dated June to
August, already in every stage's read. The claim was written from a timing
coincidence ("no orphan after 9-21") and never tested against the mechanism.
It is withdrawn.

What actually happened, established on 2026-10-01 while preparing to rebuild
the 17 "only copy" rows:

- Each of the 17 was recovered by id from old copies of the published page
  in docs-site's history (`<article id="a-<id>">` carries title, date,
  source and URL). All 17 were last on the page on 2026-09-13/14.
- **All 17 are already in the store under a new URL**: same title, body
  overlap 0.83-1.00 against a live row of the same source. They were not
  only copies -- the byte fingerprint missed them because the site re-renders
  the text slightly between fetches.
- On 2026-09-14 a run of deliberate dedup fixes landed: `06e658e`
  (lazard-am stored each article twice, once under its AEM repository path
  `/content/lam/....html` and once under its public URL -- *"The existing 15
  rows are merged separately"*), `5621d8d`, `8cacc65`, and `a8d9c6f`
  (cohen-steers' `-fp` / `-inst` audience editions are one article). The
  merge removed the duplicate rows from `articles.jsonl` and left their
  `content/<old id>.txt` behind. 15 lazard orphans, 15 merged rows; 3
  cohen-steers orphans, the audience editions.

So the original 2026-09-26 guess -- "a host or slug migration that did not
rename the file, the case CLAUDE.md warns about" -- was the right one, and the
re-derivation replaced it with a wrong one. The pgim asymmetry noted on 09-30
stands, and now has its explanation: pgim was never merged, lazard was.

| Cause | Files | Disposition |
| --- | --- | --- |
| Byte-identical to a file the store references | 16 | **Archived 2026-10-01** to `~/backups/gmia-orphans-2026-10-01/` with a manifest |
| Written outside the nightly pipeline (one is `content/test-amundi.txt`) -- audit C3 | 2 | **Archived** with the 16 |
| Left behind by the 2026-09-14 lazard / cohen-steers dedup merge; the article survives under its canonical URL | 17 | Not rebuilt -- restoring them would re-create exactly the duplicates the merge removed. **Archived 2026-10-01** with the others, after re-checking at move time that each has a live same-title row with body overlap >= 0.8 and a content file. `content/` now has **0 orphans**; the manifest lists all 35 with their survivor ids. |

**What the fix for C2 actually is**, then: any operation that removes or
re-ids rows must move or delete their content files in the same step, and
the health report should count orphans *created since the last report*,
not the standing total. That belongs with C3 (who owns writes into
`content/`).

Still unexplained: the 2026-09-26 count was 45, and ten files went away
before 2026-09-30 with no record.

## C3 and C2 fixed together, 2026-10-01

Two halves of one problem: C3 is who is allowed to write into `content/`,
C2 is noticing when something got in anyway.

**C3.** `fetch_content.isolated_content_dir()` is now the one way to call an
extractor without writing into production. It replaces the two hand-rolled
"save the global, patch it, restore it in a finally" blocks in the health
probe and `content_audit` (`compare_extractors` inherits the latter). The probe
block was moved, not rewritten: its body's AST is identical before and after.

Measured while doing it, and more important than the orphans C3 was found
through: `content_audit` and `compare_extractors` fetch under the article's
**real** id, so a missing redirect there does not leave an orphan -- it
overwrites the stored body with tonight's re-fetch, and nothing reports it.

Neither redirect had a direct test. The audit's was stubbed out by
compare_extractors' tests; the probe's was guarded only implicitly, by
`test_probe_is_wired_to_the_real_extractor` writing through the real
`CONTENT_DIR` and conftest's session-level detector erroring if it landed in
production. Both now have behavioural tests, each red when its redirect is
removed. Live check: the probe was run `--dry-run` against all 42 sources --
41 real extractor writes, every one into the temporary directory; `content/`
fingerprint (names, sizes, mtimes of 1,606 files) and the probe's state file
were byte-identical before and after.

Residual, not closed: a REPL or one-off script that calls an extractor
without the context manager still writes to production, and under a real id
that is an overwrite the orphan check below cannot see. `.claude/CLAUDE.md`
now says so where a future caller will read it.

**C2.** The health probe computes `content/*.txt` with no row in the store,
remembers the list in its state file, and the email reports only orphans
**new since the previous run** (section "🗂️ NEW ORPHAN FILES", a send
condition, named in the subject). Decisions:

- an unreadable *or missing* store returns None, not "every file is an
  orphan" -- `jsonl_store` reads a missing file as an empty store;
- when the list is unknown, the stored one is kept, so the next readable run
  does not report the whole standing set as new;
- at most 20 rows in the email, then "… and N more": a mass event should read
  as one alarm.

Four mutants, each killed: reporting the standing set every day, not storing
what was reported, not making it a send condition, and taking a missing store
at face value.

## Batch 1, 2026-09-30 — "looks protective, is not"

Six items picked because they share a shape and none of them changes what
the pipeline does at night. Two outcomes were not what the audit predicted.

| ID | Outcome |
| --- | --- |
| **D6** | **Rejected. The finding was wrong.** `ATTEMPT_CEILING = 12` is not redundant: `streak` resets whenever the label changes, so an article alternating labels (fetch_error, block, fetch_error…) holds streak at 1 forever and neither per-label cap ever fires. Simulated: it retires at attempt 12 and only because of the ceiling. 2 of the 10 articles with a failure history have changed label at least once. It is also *already* guarded — `test_alternating_labels_hit_the_lifetime_ceiling` goes red when the clause is deleted, which is how a live backstop came to be written up as dead code. |
| **F1** | **Fixed.** The real gap was narrower than "F3 has no guard". Commit `53fcf4f` did two things at once: it added "all attempts failed" to `_ERROR_MESSAGE` *and* started scanning every captured message. The T. Rowe Price night is fixed by the pattern alone, because its last line matches the new pattern by itself — so the existing test passes either way. The shape that separates them is **an informative error followed by an uninformative last line** (`Playwright timeout…`, then `no article body found`): scanning gives `fetch_error` with the timeout as detail, last-message-only gives `unknown`. Two tests added, one for `_ERROR_MESSAGE` and one for `_PDF_MESSAGE`; both go red under the reverted scan. |
| **D5** | **Fixed.** `failure` is now a required argument, so the `failure=None` branch and the two tests asserting a retirement production could not reach are gone. `MAX_CONTENT_ATTEMPTS` survives as the cap for a label with no `RETRY_POLICY` entry, and a new test (`test_every_content_label_has_a_retry_policy`) keeps that fallback from quietly becoming a road. |
| **C1** | **Fixed as an invariant, not as a defect.** All 1,548 rows carrying a `content_path` have it equal to `content/<id>.txt`, so the field holds no information and the 10 rows without one are harmless. What matters is that the two readers disagree about whether to read it: stage 3 honours the stored value, `content_audit.py:174` derives its own. A guard test pins the invariant, plus a second test that the checker rejects a bad path before it is trusted against real data. |
| **A4** | **Fixed.** `_validate_json_response` and its five tests deleted. No caller anywhere; stage 2 parses no JSON. |
| **A5** | **Fixed.** `.claude/CLAUDE.md` claimed `MODEL_CHAIN = Gemini 2.5 Pro → GPT-4.1 Mini → Claude Sonnet`; all three names are wrong and so is the provider count. Corrected to `gpt-5.6-luna → gpt-4.1-mini`, both OpenAI, with the single-provider fact stated. Two other claims in that file were stale too: "34 hedge funds" (42) and "714 passing" (2015). `_call_anthropic` was a working client for a second provider that `model_to_caller` never listed, with no `ANTHROPIC_API_KEY` configured, so wiring it would only have logged "Skipping". **Deleted 2026-09-30 by decision** (user, when offered delete or fund), together with `_anthropic_text`, the `claude-sonnet-4-6` entry in `_USAGE_FIELDS`, and the four tests that exercised them. The usage log holds no claude rows, and `_USAGE_FIELDS` is consulted at write time only — it already lacks entries for the gemini models that appear in the log — so nothing historical depends on it. The `ANTHROPIC_API_KEY` in `scripts/wrapper-*.sh` belongs to the Claude Code agent workflows and was not touched. Summarisation is now single-provider by decision rather than by accident. |

## ⑧ Observations from the 2026-09-30 email — checked, no action

Nothing was wrong with it. Recorded so the same three things are not
re-investigated from scratch.

**The `unknown` retry earned its keep.** The bridgewater row that appeared as
`unknown×1` on 9-28 and 9-29 was gone on 9-30 -- not retired, **recovered**:
31,309 characters, summarised, counter cleared, on attempt 3 of 3 (`9-26` →
`9-28` → `9-30`). The failure was transient, and `"no article body found or
page looks gated"` never said otherwise. Two decisions are vindicated by this
one row: relabelling it `unknown` instead of asserting `body_too_short`, and
keeping `unknown` `code_dependent` with `max_attempts: 3` rather than treating
an unexplained failure as deterministic. Had it been retired at attempt 2 the
article was lost. Cf. audit D1: the code-change requeue is the only mechanism
that has ever recovered an article.

**The table counts configured sources only — the 28-vs-29 gap is correct.**
`articles.jsonl` holds 28 `permafail` plus 1 `failed`, but the email's "目前未
取得正文" sums to 28. The missing row is pgim, a source that left
`sources.json`; 19 of its rows are still stored. All three places agree:
`failure_stats` filters by `configured`, `publish.py:690` drops articles whose
source has left the config, and the published page contains the string "pgim"
zero times. No orphan data reaches a reader.

**ares: the declared frequency is looser than the observed one, and that is a
known, deliberate ambiguity.** The WARN prints its own tension -- `frequency=
weekly, threshold 30d` beside `median gap 2d` -- so a source whose real rhythm
is two days went 35 days before anyone was told. `_observed_cadence_note`
exists precisely to surface this, and its docstring already says why it is a
hint and not a reclassification: the fetch carries only the most-recent
`max_articles`, so ares's "2d" is one August burst, not a rate. Reclassifying
ares to `daily` (14d) on that evidence would be fitting a threshold to a burst.
**Left as is on 2026-09-30**, with the tension recorded rather than resolved:
changing a value whose correct setting is unknown is worse than keeping a
value known to be loose. Revisit only with a cadence measured over full
history rather than one page of listings.

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
