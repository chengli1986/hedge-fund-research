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
"""
from __future__ import annotations

import hashlib
import re

DUPLICATE_JACCARD = 0.85
SHINGLE = 8
MIN_COMPARABLE_CHARS = 400          # below this a ratio says nothing


def normalised(text: str) -> str:
    """The text with every run of whitespace collapsed, for exact comparison."""
    return re.sub(r"\s+", " ", text).strip()


def body_hash(text: str) -> str:
    return hashlib.sha256(normalised(text).encode("utf-8")).hexdigest()


def shingles(text: str) -> set:
    compact = re.sub(r"[^0-9a-z]", "", (text or "").lower())[:20000]
    return {compact[i:i + SHINGLE] for i in range(max(len(compact) - SHINGLE + 1, 0))}


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def same_document(text: str, other_text: str, title: str = "", other_title: str = "") -> bool:
    """True when stage 3 would call `text` a duplicate of `other_text`.

    Mirrors analyze_articles.duplicate_owner for one pair: identical after
    whitespace is collapsed, or -- for a body of at least
    MIN_COMPARABLE_CHARS under the same title -- shingle Jaccard at or above
    DUPLICATE_JACCARD. A test holds the two in step.
    """
    if body_hash(text) == body_hash(other_text):
        return True
    if title_key(title) != title_key(other_title):
        return False
    if len(normalised(text or "")) < MIN_COMPARABLE_CHARS:
        return False
    mine, theirs = shingles(text), shingles(other_text)
    if not mine or not theirs:
        return False
    return len(mine & theirs) / len(mine | theirs) >= DUPLICATE_JACCARD
