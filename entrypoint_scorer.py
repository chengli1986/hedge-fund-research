"""Pure scoring engine for hedge fund research entrypoint discovery.

No I/O, no logging, no network calls. All functions are stateless and
operate only on the arguments passed to them.
"""

import json
import re
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# score_domain
# ---------------------------------------------------------------------------

_POSITIVE_PATH_KEYWORDS = frozenset([
    "research", "insight", "insights", "publication", "publications",
    "commentary", "market-commentary", "white-paper", "report", "reports",
    "quarterly", "annual", "letters", "outlook", "papers", "library",
    "perspectives", "thinking",
])

_NEGATIVE_PATH_KEYWORDS = frozenset([
    "about", "careers", "contact", "team", "leadership", "events",
    "podcast", "video", "subscribe", "login", "register",
])


def score_domain(url: str, allowed_domains: list[str]) -> float:
    """Score how well the URL's domain matches the allowed domains list.

    Returns:
        1.0  — exact or www. match
        0.8  — subdomain match
        0.0  — no match (or empty URL)
    """
    if not url:
        return 0.0

    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return 0.0

    if not hostname:
        return 0.0

    best = 0.0
    for domain in allowed_domains:
        domain = domain.lower().strip()
        host = hostname.lower()

        if host == domain or host == f"www.{domain}":
            return 1.0  # exact / www. match — no need to check further

        if host.endswith(f".{domain}"):
            best = 0.8  # subdomain — keep checking for a better exact match

    return best


# ---------------------------------------------------------------------------
# score_path
# ---------------------------------------------------------------------------

def score_path(url: str) -> float:
    """Score the URL path for research-content signals.

    Splits path segments by '/', '-', and '_' then counts positive and
    negative keyword hits.

    Returns:
        pos / (pos + neg), or 0.5 when there are no keyword hits.
    """
    if not url:
        return 0.5

    try:
        path = urlparse(url).path or ""
    except Exception:
        return 0.5

    # Tokenise: split on /, -, _
    tokens = set(re.split(r"[/_\-]", path.lower()))

    pos = sum(1 for t in tokens if t in _POSITIVE_PATH_KEYWORDS)
    neg = sum(1 for t in tokens if t in _NEGATIVE_PATH_KEYWORDS)

    total = pos + neg
    if total == 0:
        return 0.5

    return pos / total


# ---------------------------------------------------------------------------
# score_structure
# ---------------------------------------------------------------------------

def score_structure(html: str) -> float:
    """Score the HTML page structure for research-content richness.

    Examines the raw HTML string for structural signals without parsing.

    Returns:
        pos / (pos + neg), or 0.0 for empty HTML.
    """
    if not html:
        return 0.0

    lower = html.lower()

    pos = 0
    neg = 0

    # --- Positive signals ---

    # Multiple <article> tags
    article_count = len(re.findall(r"<article[\s>]", lower))
    if article_count >= 2:
        pos += 2
    elif article_count == 1:
        pos += 1

    # <time> tags (date signals)
    time_count = len(re.findall(r"<time[\s>]", lower))
    if time_count >= 1:
        pos += 1

    # Author / byline classes
    if re.search(r'class=["\'][^"\']*\b(author|byline)\b[^"\']*["\']', lower):
        pos += 1

    # PDF links — each .pdf link is a positive signal; bonus for multiples
    pdf_count = len(re.findall(r'href=["\'][^"\']*\.pdf["\']', lower))
    if pdf_count >= 3:
        pos += 3
    elif pdf_count >= 1:
        pos += pdf_count

    # "Read more" / "Download report" text
    if re.search(r"read\s+more", lower):
        pos += 1
    if re.search(r"download\s+report", lower):
        pos += 1

    # Pagination
    if re.search(r'class=["\'][^"\']*\bpagination\b[^"\']*["\']', lower):
        pos += 1

    # Date-like text patterns (e.g. "Jan 2026", "2026-01-01")
    if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+\d{4}\b", lower):
        pos += 1
    if re.search(r"\b\d{4}-\d{2}-\d{2}\b", lower):
        pos += 1

    # --- Negative signals ---

    # Subscribe / newsletter forms
    if re.search(r"newsletter[- ]signup|newsletter_signup", lower):
        neg += 2
    if re.search(r'type=["\']email["\']', lower):
        neg += 1

    # CTA buttons
    if re.search(r'class=["\'][^"\']*\bcta[- _]?button\b[^"\']*["\']', lower):
        neg += 1
    if re.search(r'class=["\'][^"\']*\bbtn[- _]?primary\b[^"\']*["\']', lower):
        neg += 1

    # Completely lacks article-like content
    if article_count == 0 and pdf_count == 0 and time_count == 0:
        neg += 2

    total = pos + neg
    if total == 0:
        return 0.5

    return pos / total


# ---------------------------------------------------------------------------
# score_gate
# ---------------------------------------------------------------------------

_GATE_MARKERS = [
    "subscribe to read",
    "register to continue",
    "log in to read",
    "log in to continue",
    "sign up to read",
    "register to read",
    "for clients only",
]

_DISCLAIMER_MARKERS = [
    "cookie preferences",
    "privacy policy",
    "terms of use",
    "manage cookies",
    "accept all cookies",
]


def score_gate(html: str) -> float:
    """Return a gate/wall penalty for the page.

    Each matching gate or disclaimer phrase adds 0.15, capped at 1.0.

    Returns:
        0.0  — clean, no friction markers
        1.0  — maximum penalty
    """
    if not html:
        return 0.0

    lower = html.lower()
    penalty = 0.0

    for marker in _GATE_MARKERS + _DISCLAIMER_MARKERS:
        if marker in lower:
            penalty += 0.15

    return min(penalty, 1.0)


# ---------------------------------------------------------------------------
# score_final
# ---------------------------------------------------------------------------

_DEFAULT_WEIGHTS = {"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}

_REQUIRED_WEIGHT_KEYS = frozenset(_DEFAULT_WEIGHTS.keys())


def load_weights(path: str | None = None) -> dict:
    """Load scorer weights from a JSON file.

    Falls back to ``_DEFAULT_WEIGHTS`` if the file is missing, cannot be
    parsed, is missing any of the four required keys, or the values do not
    sum to 1.0 (±0.01 tolerance).

    Args:
        path: Filesystem path to the JSON weights file.  When *None* the
              function returns the defaults immediately.

    Returns:
        A dict with keys ``domain``, ``path``, ``structure``, ``gate``.
    """
    if path is None:
        return dict(_DEFAULT_WEIGHTS)

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT_WEIGHTS)

    if not _REQUIRED_WEIGHT_KEYS.issubset(data.keys()):
        return dict(_DEFAULT_WEIGHTS)

    total = sum(data[k] for k in _REQUIRED_WEIGHT_KEYS)
    if abs(total - 1.0) > 0.01:
        return dict(_DEFAULT_WEIGHTS)

    return {k: data[k] for k in _REQUIRED_WEIGHT_KEYS}


def score_final_with_weights(
    domain: float,
    path: float,
    structure: float,
    gate_penalty: float,
    weights: dict,
) -> float:
    """Combine individual component scores using the provided weights dict.

    Args:
        domain: Output of :func:`score_domain`.
        path: Output of :func:`score_path`.
        structure: Output of :func:`score_structure`.
        gate_penalty: Output of :func:`score_gate` (higher = worse).
        weights: Dict with keys ``domain``, ``path``, ``structure``, ``gate``.

    Returns:
        Weighted final score in the range [0.0, 1.0].
    """
    return (
        domain * weights["domain"]
        + path * weights["path"]
        + structure * weights["structure"]
        + (1.0 - gate_penalty) * weights["gate"]
    )


def score_final(
    domain: float,
    path: float,
    structure: float,
    gate_penalty: float,
) -> float:
    """Combine individual component scores into a single final score.

    Weights: domain=0.2, path=0.3, structure=0.3, gate=0.2
    Formula: domain*0.2 + path*0.3 + structure*0.3 + (1.0 - gate_penalty)*0.2

    Delegates to :func:`score_final_with_weights` using :data:`_DEFAULT_WEIGHTS`.
    Signature and return values are 100% backward-compatible.
    """
    return score_final_with_weights(domain, path, structure, gate_penalty, _DEFAULT_WEIGHTS)
