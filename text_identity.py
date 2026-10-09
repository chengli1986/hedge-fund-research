"""One definition of "the same document", shared by stages 2 and 3.

Stage 3 hides a body it has already published (duplicate_body); stage 2 now
holds back a new issue whose page still shows the previous issue's text
(page_not_updated). If the two stages measured sameness differently, stage 2
would let through a body that stage 3 then hides -- the issue is lost either
way. So both call this module.

A re-published document is rarely byte-identical: janus-henderson's "Charts
for the beach 2026" came back 13 characters longer than the stored copy and
was summarised a second time (2026-09-17). Similarity is measured as Jaccard
over 8-character shingles, and ONLY between articles of the same source that
carry the same title -- without that, ares' 1,489-char boilerplate-heavy
pieces score 0.89 against a dozen unrelated ones, and gmo's quarterly
forecasts (one template, different numbers) score 0.90 against each other.

Measured on the store, same source + same title:
  1.00 loomis monthly update, 1.00 janus chart deck  -> duplicates
  0.79 franklin survey page still being filled in    -> kept
  0.35 kkr, 0.33 gsam, 0.30 loomis, 0.18 gsam, 0.14 troweprice -> real issues
so 0.85 sits far above every genuine issue seen and below both duplicates.

A retitled copy is compared too (2026-10-09 second review): the title gate let
Baillie Gifford's "EM: enough is not enough" back in under a new title at the
same URL (J 0.998), and five pairs were on the page twice -- brookfield
0.996, principal 0.989, wellington 0.987, cohen-steers 0.971 and 0.946. Under
different titles the bar is RETITLED_JACCARD and the body must have at least
MIN_RETITLED_CHARS: measured on the store, different-title pairs of distinct
published articles top out at 0.82 (gmo's 1,360-char forecast template) and
0.75 (ares' short pieces; the 0.89 recorded above no longer reproduces), and
lazard's 2,900-char disclaimer pages score 0.99 against one another. Every
pair caught is 7,000 chars or longer. (rothschild's two July quarterlies,
1,350 chars and 0.93, stay apart: two reports whose page gave us the same
teaser, which is a capture fault, not a re-publication.)

Letters of every script count (NFKC, lower case, punctuation and spaces
dropped): keeping only [0-9a-z] gave every Japanese title the key "" and
compared two Japanese bodies on the English footer they share.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

DUPLICATE_JACCARD = 0.85
SHINGLE = 8
MIN_COMPARABLE_CHARS = 400          # below this a ratio says nothing
RETITLED_JACCARD = 0.90
MIN_RETITLED_CHARS = 3000


def normalised(text: str) -> str:
    """The text with every run of whitespace collapsed, for exact comparison."""
    return re.sub(r"\s+", " ", text).strip()


def body_hash(text: str) -> str:
    return hashlib.sha256(normalised(text).encode("utf-8")).hexdigest()


def _letters(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "").lower())


def shingles(text: str) -> set:
    compact = _letters(text)[:20000]
    return {compact[i:i + SHINGLE] for i in range(max(len(compact) - SHINGLE + 1, 0))}


def title_key(title: str) -> str:
    return _letters(title)


def jaccard(mine: set, theirs: set) -> float:
    return len(mine & theirs) / len(mine | theirs) if mine and theirs else 0.0


def similarity_bar(text: str, title: str, other_title: str) -> float | None:
    """The Jaccard at or above which `text` repeats the other body, or None
    when a ratio would say nothing (too short for this comparison)."""
    size = len(normalised(text or ""))
    if title_key(title) == title_key(other_title):
        return DUPLICATE_JACCARD if size >= MIN_COMPARABLE_CHARS else None
    return RETITLED_JACCARD if size >= MIN_RETITLED_CHARS else None


def same_document(text: str, other_text: str, title: str = "", other_title: str = "") -> bool:
    """True when stage 3 would call `text` a duplicate of `other_text`.

    Mirrors analyze_articles.duplicate_owner for one pair: identical after
    whitespace is collapsed, or shingle Jaccard at or above similarity_bar.
    An empty body repeats nothing. A test holds the two in step.
    """
    if not normalised(text or ""):
        return False
    if body_hash(text) == body_hash(other_text):
        return True
    bar = similarity_bar(text, title, other_title)
    return bar is not None and jaccard(shingles(text), shingles(other_text)) >= bar
