#!/usr/bin/env python3
"""
GMIA Trial Manager — validates candidate sources with 7-day live article checks
and Haiku-powered quality sampling.

After the nightly discovery agent marks a candidate as validated (HIGH/MEDIUM quality),
this manager picks one at a time, fetches its research URL daily for 7 days, counts
detectable articles, and auto-decides whether to send a graduation recommendation.

Every day of the trial, the manager samples up to 3 *new* article links (deduplicated
across days), extracts their text, and sends them to Claude Haiku for quality
assessment (relevance, depth, extractability).  The quality score is factored into
the pass/fail decision.  Article URLs seen each day are stored in daily_checks for
transparency and to power the cross-day dedup.

Trial SUCCESS = quantity (≥ MIN_DAYS_WITH_ARTICLES) AND quality (avg score ≥ 0.5)
Trial FAIL    = either condition not met → candidate downgraded to watchlist

The manager does NOT automatically modify sources.json — graduation requires a human
decision (adding the source with the correct fetch method, entrypoints, etc.).

CLI:
  python3 gmia-trial-manager.py run      — normal daily run (called from pipeline)
  python3 gmia-trial-manager.py status   — print current trial state
  python3 gmia-trial-manager.py skip     — skip active trial, move to next candidate
"""

import html
import json
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
TRIAL_STATE_FILE = BASE_DIR / "config" / "trial-state.json"
ENV_FILE = Path.home() / ".stock-monitor.env"

TRIAL_DAYS = 7
MAX_CONCURRENT_TRIALS = 3
MIN_DAYS_WITH_ARTICLES = 4  # fetcher must return >0 articles on ≥4 of 7 days
MIN_QUALITY = {"HIGH", "MEDIUM"}
MIN_QUALITY_SCORE = 0.5     # avg Haiku quality score to pass (0-1)
SAMPLE_DAYS = {1, 2, 3, 4, 5, 6, 7}  # quality sampling runs every trial day
                            # each day samples SAMPLE_SIZE *new* articles (dedup
                            # across days); up to 21 unique articles scored total
SAMPLE_SIZE = 3             # articles to sample per quality check
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Path segments that indicate a NON-research page (bios, marketing, careers).
# Sampling these pollutes quality scores even when the index page looks legit.
# Lesson learned 2026-05-04: PineBridge trial scored 0.24 because /en/bio/* links
# dominated; deep_analyze rated landing page 0.80 (口径不一致).
NON_RESEARCH_SEGMENTS = (
    "/tag/", "/category/", "/page/", "/author/", "/login", "/search",
    "/bio/", "/biography/", "/team/", "/people/", "/our-people/",
    "/leadership/", "/staff/", "/about/", "/about-us/", "/contact/",
    "/event/", "/events/", "/webinar/", "/webinars/",
    "/career/", "/careers/", "/job/", "/jobs/",
)

# Path segments that strongly indicate a research article. When at least
# SAMPLE_SIZE such links are found, we prefer them over neutral links.
RESEARCH_SEGMENTS = (
    "/insights/", "/insight/", "/research/", "/perspectives/", "/perspective/",
    "/article/", "/articles/", "/analysis/", "/commentary/", "/commentaries/",
    "/blog/", "/blog-post/", "/post/", "/posts/",
    "/publication/", "/publications/", "/paper/", "/papers/",
    "/note/", "/notes/", "/letter/", "/letters/", "/market-update/",
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Markers that identify an authentication/login gate page in a redirect target.
# Kept conservative: "auth" alone would false-positive on /author/ pages.
# Real-world case 2026-06-02: Capital Group's Akamai "Content Gate" edge worker
# 302-redirects cookie-less requests to /advisor/public/authentication-0.htm.
AUTH_GATE_MARKERS = (
    "authentication", "login", "logon", "signin", "sign-in",
    "/auth/", "/sso/", "/gateway/",
)


# ── state helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if TRIAL_STATE_FILE.exists():
        data = json.loads(TRIAL_STATE_FILE.read_text())
        # Migrate old single-trial format to multi-trial list
        if "active_trial" in data:
            old = data.pop("active_trial")
            data["active_trials"] = [old] if old else []
        return data
    return {"active_trials": [], "history": []}


def save_state(state: dict) -> None:
    TRIAL_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def load_candidates() -> list[dict]:
    return json.loads(CANDIDATES_FILE.read_text())


def save_candidates(candidates: list[dict]) -> None:
    CANDIDATES_FILE.write_text(json.dumps(candidates, indent=2, ensure_ascii=False))


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip("'\"")
    return env


# ── fetcher integration ───────────────────────────────────────────────────────

def _load_fetchers() -> dict:
    """Lazy-import FETCHERS from fetch_articles.py to avoid circular imports."""
    try:
        import sys as _sys
        if str(BASE_DIR) not in _sys.path:
            _sys.path.insert(0, str(BASE_DIR))
        import fetch_articles
        return fetch_articles.FETCHERS
    except Exception as exc:
        print(f"WARNING: could not import FETCHERS from fetch_articles: {exc}")
        return {}


def _candidate_to_source_dict(candidate: dict) -> dict:
    """Build a minimal source dict from a fund_candidates entry for use with FETCHERS."""
    return {
        "id": candidate["id"],
        "name": candidate.get("name", candidate["id"]),
        "url": candidate.get("research_url") or candidate.get("homepage_url", ""),
        "method": "playwright",
        "max_articles": 10,
    }


def count_articles_with_fetcher(trial: dict) -> dict:
    """Count articles via the registered FETCHER, with httpx fallback for unknown sources.

    Returns dict with keys: accessible, article_count, date_count, error, fetcher_used
    """
    source_id = trial["id"]
    fetchers = _load_fetchers()

    if source_id not in fetchers:
        url = trial.get("research_url") or trial.get("homepage_url", "")
        result = count_articles(url)
        result["fetcher_used"] = False
        return result

    candidates = load_candidates()
    candidate = next((c for c in candidates if c["id"] == source_id), None)
    if not candidate:
        url = trial.get("research_url") or trial.get("homepage_url", "")
        result = count_articles(url)
        result["fetcher_used"] = False
        return result

    source_dict = _candidate_to_source_dict(candidate)
    try:
        articles = fetchers[source_id](source_dict)
        count = len(articles)
        return {
            "accessible": True,
            "article_count": count,
            "date_count": sum(1 for a in articles if a.get("date")),
            "article_urls": [a["url"] for a in articles if a.get("url")][:50],
            "error": None,
            "fetcher_used": True,
        }
    except Exception as exc:
        return {
            "accessible": False,
            "article_count": 0,
            "date_count": 0,
            "article_urls": [],
            "error": str(exc)[:120],
            "fetcher_used": True,
        }


# ── article detection ─────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    r"\b20\d{2}[-/]\d{2}[-/]\d{2}\b",
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? 20\d{2}\b",
    r"\b\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 20\d{2}\b",
]
_DATE_RE = re.compile("|".join(_DATE_PATTERNS), re.IGNORECASE)


def count_articles(url: str, timeout: int = 20) -> dict:
    """Fetch research URL and count detectable article signals.

    Returns dict with keys: accessible, article_count, date_count, article_urls, error
    article_urls is a list of up to 50 article-like hrefs seen on the page (best-effort).
    """
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout,
                         follow_redirects=True)
        if resp.status_code != 200:
            return {"accessible": False, "article_count": 0, "date_count": 0,
                    "article_urls": [], "error": f"HTTP {resp.status_code}"}

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove nav/footer noise
        for tag in soup.select("nav, footer, header, .nav, .footer, .header, script, style"):
            tag.decompose()

        text = soup.get_text(" ", strip=True)

        # Count article-like tags
        article_tags = len(soup.find_all(["article", "li"]))
        h_tags = len(soup.find_all(["h2", "h3"]))
        time_tags = len(soup.find_all("time"))
        date_matches = len(_DATE_RE.findall(text))

        # Best-effort URL extraction for the trial article database
        # (_extract_article_links defined below; forward reference is fine at call time)
        try:
            article_urls = _extract_article_links(url, soup)[:50]
        except Exception:
            article_urls = []

        # Heuristic: article count = strongest signal across all indicators.
        # Include len(article_urls) so article_count is never less than the
        # number of extracted links — avoids article_count/article_urls mismatch.
        article_count = max(
            time_tags,
            date_matches,
            min(h_tags, 20),       # h-tags capped (navs have many h2s)
            len(article_urls),
        )

        return {
            "accessible": True,
            "article_count": article_count,
            "date_count": date_matches,
            "article_urls": article_urls,
            "error": None,
        }

    except Exception as exc:
        return {"accessible": False, "article_count": 0, "date_count": 0,
                "article_urls": [], "error": str(exc)[:120]}


# ── quality sampling (Haiku) ─────────────────────────────────────────────────

def _extract_article_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Extract up to SAMPLE_SIZE * 3 article-like links from a research index.

    Two-pass: collects all eligible links, then prefers research-pattern paths
    (/insights/, /research/, etc.) over neutral ones. NON_RESEARCH_SEGMENTS
    (/bio/, /team/, /careers/, etc.) are filtered out entirely to prevent
    bio-page pollution that caused the PineBridge trial 0.24 score.
    """
    from urllib.parse import urljoin

    seen: set[str] = set()
    research_links: list[str] = []
    neutral_links: list[str] = []
    parsed_base = urlparse(base_url)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)

        if parsed.netloc != parsed_base.netloc:
            continue
        if parsed.path in ("", "/") or parsed.path == parsed_base.path:
            continue

        path_lower = parsed.path.lower()
        if any(seg in path_lower for seg in NON_RESEARCH_SEGMENTS):
            continue

        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            continue

        canon = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if canon in seen:
            continue
        seen.add(canon)

        if any(seg in path_lower for seg in RESEARCH_SEGMENTS):
            research_links.append(canon)
        else:
            neutral_links.append(canon)

    cap = SAMPLE_SIZE * 3
    if len(research_links) >= SAMPLE_SIZE:
        return research_links[:cap]
    return (research_links + neutral_links)[:cap]


def _is_auth_gate_url(requested_url: str, final_url: str) -> bool:
    """Return True when a request was redirected to an auth/login gate page.

    A gate redirect = the final path differs from the requested path AND the
    final path contains an auth marker. Same-path responses and ordinary
    canonical-slug redirects are not gates.
    """
    req_path = urlparse(requested_url).path.rstrip("/").lower()
    fin_path = urlparse(final_url).path.rstrip("/").lower()
    if req_path == fin_path:
        return False
    return any(marker in fin_path for marker in AUTH_GATE_MARKERS)


def _get_with_auth_retry(url: str, timeout: int = 20) -> "httpx.Response | None":
    """GET a URL with cookie persistence and a single auth-gate retry.

    Sites behind a cookie content-gate (e.g. Capital Group's Akamai "Content
    Gate" edge worker) 302-redirect cookie-less first visits to an auth page
    that *sets* session cookies; a second request with those cookies passes
    through to the real content. A stateless httpx.get() therefore always
    lands on the gate — this helper detects that and retries once inside the
    same cookie session.

    Returns the final httpx.Response (which may still be the gate page for a
    hard login wall — callers must check with _is_auth_gate_url), or None on
    network error.
    """
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout,
                          follow_redirects=True) as client:
            resp = client.get(url)
            if _is_auth_gate_url(url, str(resp.url)) and client.cookies:
                resp = client.get(url)
            return resp
    except Exception:
        return None


def _extract_body_from_soup(soup: "BeautifulSoup") -> str | None:
    """Extract clean body text from a parsed soup (shared by httpx + Playwright paths).

    Strategy (handles both standard <article> pages and component-based CMS):
    1. Longest <article> tag (not first — AEM pages have many tiny teaser
       <article> tags sprinkled around the page; the real body, if present,
       is the longest one).
    2. Component-framework selectors (.cmp-text for AEM/Wellington, .rich-text,
       .article-body, .article-content) — concatenate elements with >100 chars
       to skip subscribe/legal boilerplate.
    3. <main>, then <body> as last resort.
    """
    for tag in soup.select("nav, footer, header, .nav, .footer, .header, "
                           "script, style, aside, .sidebar"):
        tag.decompose()

    articles = soup.find_all("article")
    if articles:
        longest = max(articles, key=lambda a: len(a.get_text(" ", strip=True)))
        text = longest.get_text(" ", strip=True)
        if len(text) > 200:
            return text[:3000]

    for selector in (
        ".cmp-text",                 # Adobe Experience Manager Core Components
        "[itemprop='articleBody']",  # schema.org microdata (Reuters, NYT, WSJ, ...)
        ".rich-text",                # generic WYSIWYG output (Contentful, Prismic, Strapi)
        ".article-body",             # common news-site convention
        ".article-content",          # common news-site convention
        ".entry-content",            # WordPress default themes (~43% of the web)
        ".post-content",             # Ghost, Hugo, Jekyll, most SSGs
    ):
        parts = soup.select(selector)
        if parts:
            joined = "\n\n".join(
                p.get_text(" ", strip=True)
                for p in parts
                if len(p.get_text(" ", strip=True)) > 100
            )
            if len(joined) > 200:
                return joined[:3000]

    content = soup.find("main") or soup.find("body")
    if not content:
        return None
    text = content.get_text(" ", strip=True)
    return text[:3000] if len(text) > 200 else None


def _extract_article_text(url: str, timeout: int = 20) -> str | None:
    """Fetch a single article page (httpx) and extract clean body text."""
    try:
        resp = _get_with_auth_retry(url, timeout=timeout)
        if resp is None or resp.status_code != 200:
            return None
        if _is_auth_gate_url(url, str(resp.url)):
            return None  # still stuck on a login gate after cookie retry
        soup = BeautifulSoup(resp.text, "html.parser")
        return _extract_body_from_soup(soup)
    except Exception:
        return None


def _is_likely_js_only(url: str, timeout: int = 15) -> bool:
    """Return True when the page is likely JS-rendered (HTTP 200 + large HTML + no extractable text).

    Four non-JS failure modes must NOT return True:
    - Network error / timeout → caught by except, return False
    - HTTP non-200 (auth wall, geoblock) → check status_code, return False
    - HTTP 200 + tiny body (empty CDN response) → check len(text) < 1000, return False
    - HTTP 200 redirect to an auth/login gate (cookie content-gate) → check
      _is_auth_gate_url, return False

    Only HTTP 200 + large HTML (>1000 chars) + _extract_article_text returns None
    → the site serves a JS shell that needs browser rendering.
    """
    try:
        resp = _get_with_auth_retry(url, timeout=timeout)
        if resp is None or resp.status_code != 200:
            return False
        if _is_auth_gate_url(url, str(resp.url)):
            return False  # auth gate ≠ JS shell — do not misclassify
        if len(resp.text) < 1000:
            return False
        return _extract_article_text(url, timeout=timeout) is None
    except Exception:
        return False


def _call_haiku(prompt: str, max_retries: int = 1) -> dict | None:
    """Call Claude Haiku for quality assessment. Returns parsed JSON or None.

    On transient failures (HTTP errors, malformed/missing JSON in the response)
    retries up to ``max_retries`` additional times before giving up. The
    upstream protection this adds is small but real — without it, a single
    Haiku JSON glitch sends ``avg_score`` to ``0.0`` and can auto-reject an
    otherwise-good source (regression seen with research-affiliates 2026-04-19).
    """
    env = load_env()
    api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, skipping quality sampling")
        return None

    last_error = "unknown"
    for attempt in range(max_retries + 1):
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": HAIKU_MODEL,
                    "max_tokens": 1024,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["content"][0]["text"]
            json_match = re.search(r"\{[\s\S]*\}", text)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError as exc:
                    last_error = f"JSON parse failed: {exc}"
            else:
                last_error = "no JSON object in response"
        except Exception as exc:
            last_error = f"request failed: {exc}"

        if attempt < max_retries:
            print(f"WARNING: Haiku attempt {attempt + 1} — {last_error}; retrying...")

    print(f"WARNING: Haiku call failed after {max_retries + 1} attempts: {last_error}")
    return None


def _get_article_links_for_sampling(trial: dict) -> list[str]:
    """Return article URLs for quality sampling.

    Calls the registered FETCHER when available so sampling uses the same
    articles the pipeline will actually collect. Falls back to httpx crawl
    for sources without a registered fetcher.
    """
    source_id = trial["id"]
    fetchers = _load_fetchers()

    if source_id in fetchers:
        candidates = load_candidates()
        candidate = next((c for c in candidates if c["id"] == source_id), None)
        if not candidate:
            return []
        source_dict = _candidate_to_source_dict(candidate)
        try:
            articles = fetchers[source_id](source_dict)
            # SAMPLE_SIZE * 10 = 30 candidates; 7-day trial needs up to 21
            # unique URLs (7 × SAMPLE_SIZE), plus headroom for extraction failures.
            return [a["url"] for a in articles[:SAMPLE_SIZE * 10] if a.get("url")]
        except Exception:
            return []

    # Fallback: httpx crawl of the index page
    url = trial.get("research_url", "")
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("nav, footer, header, script, style"):
            tag.decompose()
        return _extract_article_links(url, soup)
    except Exception:
        return []


def sample_article_quality(research_url: str, trial: dict | None = None,
                            exclude_urls: set[str] | None = None) -> dict:
    """Sample articles from a source and assess quality via Haiku.

    ``exclude_urls`` is the set of URLs sampled on previous trial days; passing
    it makes each day score *new* articles instead of re-scoring whatever sat at
    the top of the listing. Each unique article is scored once across the trial,
    so the final average reflects up to ``SAMPLE_SIZE`` × ``len(SAMPLE_DAYS)``
    (currently 21) distinct articles.

    Returns dict with keys: sampled, articles, avg_score, error
    Each article entry: {url, title_hint, score, relevance, depth, extractable, notes}
    """
    # Step 1: get article links
    if trial is not None:
        links = _get_article_links_for_sampling(trial)
        if not links:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": "No article links found on index page"}
    else:
        # Original httpx path (backward compat + fallback)
        try:
            resp = httpx.get(research_url, headers=HEADERS, timeout=20,
                             follow_redirects=True)
            if resp.status_code != 200:
                return {"sampled": 0, "articles": [], "avg_score": 0.0,
                        "error": f"Index page HTTP {resp.status_code}"}
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.select("nav, footer, header, script, style"):
                tag.decompose()
        except Exception as exc:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": str(exc)[:120]}

        links = _extract_article_links(research_url, soup)
        if not links:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": "No article links found on index page"}

    if exclude_urls:
        links = [u for u in links if u not in exclude_urls]
        if not links:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": "No new (un-sampled) article links available"}

    # Step 2: extract text from each article, using fallback links if needed
    article_texts: list[tuple[str, str]] = []  # (url, text)
    js_only_count = 0
    js_only_checked = 0
    for url in links:
        text = _extract_article_text(url)
        if text:
            article_texts.append((url, text))
        else:
            # Probe to distinguish JS shell from network/auth failure
            js_only_checked += 1
            if _is_likely_js_only(url):
                js_only_count += 1
        if len(article_texts) >= SAMPLE_SIZE:
            break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article",
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked}

    # Step 3: batch assess via Haiku
    articles_block = ""
    for i, (url, text) in enumerate(article_texts, 1):
        articles_block += f"\n--- Article {i} (URL: {url}) ---\n{text}\n"

    prompt = f"""You are evaluating articles from a hedge fund / investment research source.
For each article below, score it on three dimensions (0.0 to 1.0):

1. **relevance**: Is this investment research, macro analysis, or portfolio strategy?
   (1.0 = deep investment research, 0.5 = tangentially related, 0.0 = marketing/HR/unrelated)
2. **depth**: Is this substantive analysis with data, reasoning, or original insight?
   (1.0 = detailed research paper, 0.5 = brief commentary, 0.0 = press release/summary)
3. **extractable**: Is the text clean and complete enough to be useful if auto-collected?
   (1.0 = full article text, 0.5 = partial/truncated, 0.0 = login wall/JS placeholder)

Return a JSON object with this exact structure:
{{
  "articles": [
    {{
      "article_num": 1,
      "relevance": 0.8,
      "depth": 0.7,
      "extractable": 0.9,
      "overall": 0.8,
      "notes": "one-line summary of what this article is about"
    }}
  ]
}}

The "overall" score should be: 0.4*relevance + 0.4*depth + 0.2*extractable.
{articles_block}"""

    result = _call_haiku(prompt)
    if not result or "articles" not in result:
        return {"sampled": len(article_texts), "articles": [], "avg_score": 0.0,
                "error": "Haiku returned invalid response"}

    # Build lookup by article_num so missing entries can be detected
    haiku_by_num: dict[int, dict] = {}
    for art in result.get("articles", []):
        try:
            num = int(art.get("article_num", 0))
            haiku_by_num[num] = art
        except (TypeError, ValueError):
            pass

    def _safe_float(val) -> float:
        try:
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    # Require exactly one scored entry per sampled article;
    # articles the model dropped or returned with bad fields get score 0
    scored_articles = []
    for i, (url, _) in enumerate(article_texts, 1):
        art = haiku_by_num.get(i, {})
        rel = _safe_float(art.get("relevance"))
        dep = _safe_float(art.get("depth"))
        ext = _safe_float(art.get("extractable"))
        overall = round(0.4 * rel + 0.4 * dep + 0.2 * ext, 3)
        scored_articles.append({
            "url": url,
            "relevance": rel,
            "depth": dep,
            "extractable": ext,
            "overall": overall,
            "notes": art.get("notes", ""),
        })

    avg_score = (sum(a["overall"] for a in scored_articles) / len(scored_articles)
                 if scored_articles else 0.0)

    return {
        "sampled": len(article_texts),
        "articles": scored_articles,
        "avg_score": round(avg_score, 3),
        "error": None,
        "js_only_count": js_only_count,
        "js_only_checked": js_only_checked,
    }


# ── queue logic ───────────────────────────────────────────────────────────────

def get_trial_queue(state: dict) -> list[dict]:
    """Return validated HIGH/MEDIUM candidates not yet trialed, sorted by fit_score desc.

    History entries with outcome="skipped" do NOT count as having been trialed —
    a skip is "abandon this trial run, free the slot for something else", not a
    verdict on the candidate. Without this exclusion a skipped candidate is
    permanently locked out of the queue (regression that stranded goldman-sam
    and research-affiliates after their 2026-04-19 skips).
    Same for outcome="inconclusive" (sampling never worked, no verdict reached):
    the candidate stays on watchlist, but if a human fixes the underlying issue
    and flips status back to "visitable", re-trial must not be blocked by history.
    """
    candidates = load_candidates()
    trialed_ids = {
        h["id"] for h in state.get("history", [])
        if h.get("outcome") not in ("skipped", "inconclusive")
    }
    active_ids = {t["id"] for t in state.get("active_trials", [])}

    queue = []
    for c in candidates:
        if c["status"] != "visitable":
            continue
        if c.get("quality") not in MIN_QUALITY:
            continue
        if c["id"] in trialed_ids:
            continue
        if c["id"] in active_ids:
            continue
        queue.append(c)

    label_score = {"HIGH": 1.0, "MEDIUM": 0.5}

    def priority(c: dict) -> float:
        q = c.get("quality_score") or label_score.get(c.get("quality", ""), 0.0)
        f = c.get("fit_score") or 0.0
        return 0.6 * q + 0.4 * f

    return sorted(queue, key=lambda c: -priority(c))


def existing_source_ids() -> set[str]:
    data = json.loads(SOURCES_FILE.read_text())
    return {s["id"] for s in data.get("sources", [])}


# ── email ─────────────────────────────────────────────────────────────────────

def _trial_result_label(outcome: str, passed: bool) -> tuple[str, str, str]:
    """Return (icon, text, color) for a trial verdict — shared by trial emails."""
    if passed:
        return "✅", "READY TO INTEGRATE", "#1a7f37"
    if outcome == "inconclusive":
        return ("⚠️", "INCONCLUSIVE — ARTICLES DETECTED BUT SAMPLING FAILED",
                "#9a6700")
    if outcome == "skipped":
        return "⚠️", "ABORTED — NEEDS FETCHER (ROUTED TO SYNTHESIS)", "#9a6700"
    if outcome == "fail_quality":
        return "❌", "FAILED — LOW QUALITY", "#cf222e"
    return "❌", "FAILED — INSUFFICIENT CONTENT", "#cf222e"


def _summary_bucket(t: dict) -> str:
    """Classify a decided trial into a daily-summary-email section bucket.

    Manual skips (cmd_skip, no skip_reason) land in "other" — intentionally
    invisible in the summary email: a human did it, no notification needed.
    """
    outcome = t.get("outcome", "")
    if outcome == "pass":
        return "pass"
    if outcome == "inconclusive":
        return "inconclusive"
    if outcome == "fail_quality":
        return "fail_quality"
    if outcome == "skipped" and t.get("skip_reason") == "needs_fetcher":
        return "aborted_needs_fetcher"
    if outcome == "fail_quantity":
        return "inaccessible" if t.get("total_articles", 0) == 0 else "low_cadence"
    return "other"


def send_trial_email(trial: dict, passed: bool, total_articles: int,
                     days_with_articles: int | None = None) -> None:
    env = load_env()
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    mail_to = env.get("MAIL_TO", "")
    if not smtp_user or not smtp_pass or not mail_to:
        print("WARNING: SMTP not configured, skipping trial email")
        return

    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    outcome = trial.get("outcome", "")
    result_icon, result_text, result_color = _trial_result_label(outcome, passed)

    avg_quality = trial.get("avg_quality_score", 0)
    quality_color = "#1a7f37" if avg_quality >= MIN_QUALITY_SCORE else "#cf222e"

    daily_rows = ""
    for date, info in sorted(trial.get("daily_checks", {}).items()):
        count = info.get("article_count", 0)
        accessible = info.get("accessible", False)
        err = info.get("error") or ""
        status_cell = (
            f'<span style="color:#1a7f37">{count} articles</span>' if accessible
            else f'<span style="color:#cf222e">unreachable — {err[:40]}</span>'
        )
        daily_rows += f"<tr><td style='padding:4px 8px'>{date}</td><td style='padding:4px 8px'>{status_cell}</td></tr>\n"

    # Quality sampling section
    quality_html = ""
    samples = trial.get("quality_samples", [])
    if samples:
        quality_rows = ""
        for sample in samples:
            for art in sample.get("articles", []):
                score = art.get("overall", 0)
                sc = "#1a7f37" if score >= 0.6 else "#e3b341" if score >= 0.4 else "#cf222e"
                notes = html.escape(art.get("notes", "")[:80])
                url_short = art.get("url", "")[-50:]
                quality_rows += (
                    f"<tr><td style='padding:4px 8px'>Day {sample.get('day','?')}</td>"
                    f"<td style='padding:4px 8px;color:{sc};font-weight:bold'>{score:.2f}</td>"
                    f"<td style='padding:4px 8px'>{art.get('relevance',0):.1f}</td>"
                    f"<td style='padding:4px 8px'>{art.get('depth',0):.1f}</td>"
                    f"<td style='padding:4px 8px'>{art.get('extractable',0):.1f}</td>"
                    f"<td style='padding:4px 8px;font-size:12px'>{notes}</td></tr>\n"
                )
            if sample.get("error"):
                quality_rows += (
                    f"<tr><td style='padding:4px 8px'>Day {sample.get('day','?')}</td>"
                    f"<td colspan='5' style='padding:4px 8px;color:#cf222e'>"
                    f"Error: {sample['error'][:60]}</td></tr>\n"
                )
        quality_html = f"""
<h3 style="margin:12px 0 6px">Article Quality Sampling (Haiku)</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
  <tr style="background:#f6f8fa">
    <th style="padding:4px 8px;text-align:left">Day</th>
    <th style="padding:4px 8px;text-align:left">Score</th>
    <th style="padding:4px 8px;text-align:left">Rel</th>
    <th style="padding:4px 8px;text-align:left">Depth</th>
    <th style="padding:4px 8px;text-align:left">Extr</th>
    <th style="padding:4px 8px;text-align:left">Notes</th></tr>
{quality_rows}
</table>
<p style="font-size:12px;color:#586069;margin:4px 0">
  Avg quality: <strong style="color:{quality_color}">{avg_quality:.2f}</strong>
  (threshold: {MIN_QUALITY_SCORE}) &nbsp;|&nbsp;
  Score = 0.4*relevance + 0.4*depth + 0.2*extractable
</p>"""

    action_html = ""
    if passed:
        research_url = trial.get("research_url", "")
        action_html = f"""
<div style="background:#f0fff4;border:1px solid #c3e6cb;border-radius:6px;padding:12px 16px;margin-top:16px;">
  <strong>Next step:</strong> Add <code>{trial['id']}</code> to <code>config/sources.json</code><br>
  Research URL: <a href="{research_url}">{research_url}</a><br>
  Quality: <strong>{trial.get('quality','?')}</strong> &nbsp;|&nbsp; Topics: {trial.get('topics','?')}
</div>"""

    html_body = f"""<html><body style="font-family:-apple-system,sans-serif;padding:20px;max-width:600px">
<h2 style="margin:0">{result_icon} GMIA Trial: {trial['name']}</h2>
<p style="color:#586069;margin:4px 0">{now_bjt}</p>

<table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0;background:#f6f8fa;border-radius:6px;">
  <tr><td style="padding:8px"><strong>Result</strong></td>
      <td style="padding:8px;color:{result_color};font-weight:bold">{result_text}</td></tr>
  <tr><td style="padding:8px"><strong>Trial period</strong></td>
      <td style="padding:8px">{trial.get('start_date','')} → {trial.get('end_date','')}</td></tr>
  <tr><td style="padding:8px"><strong>Days with articles</strong></td>
      <td style="padding:8px">{days_with_articles if days_with_articles is not None else '?'}/{TRIAL_DAYS} (need ≥{MIN_DAYS_WITH_ARTICLES})</td></tr>
  <tr><td style="padding:8px"><strong>Total articles collected</strong></td>
      <td style="padding:8px">{total_articles}</td></tr>
  <tr><td style="padding:8px"><strong>Avg quality score</strong></td>
      <td style="padding:8px;color:{quality_color};font-weight:bold">{avg_quality:.2f} (threshold: {MIN_QUALITY_SCORE})</td></tr>
  <tr><td style="padding:8px"><strong>Fit score</strong></td>
      <td style="padding:8px">{trial.get('fit_score', '?')}</td></tr>
</table>

<h3 style="margin:12px 0 6px">Daily checks</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
  <tr style="background:#f6f8fa"><th style="padding:4px 8px;text-align:left">Date</th>
      <th style="padding:4px 8px;text-align:left">Articles detected</th></tr>
{daily_rows}
</table>
{quality_html}
{action_html}
<p style="color:#8b949e;font-size:11px;margin-top:20px">GMIA Candidate Trial Manager — auto-generated</p>
</body></html>"""

    dwa = days_with_articles if days_with_articles is not None else trial.get("days_with_articles", "?")
    avg_q = trial.get("avg_quality_score", 0.0)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"GMIA Trial {'PASS' if passed else 'FAIL'}: {trial['name']} ({dwa}/{TRIAL_DAYS} days, Q={avg_q:.2f})"
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["MIME-Version"] = "1.0"
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"Trial email sent to {mail_to}")
    except Exception as exc:
        print(f"WARNING: trial email failed: {exc}")


# ── daily summary email ──────────────────────────────────────────────────────

AUTO_PROMOTE_LOG = BASE_DIR / "logs" / "auto-promote-history.jsonl"
PENDING_PROFILES_DIR = BASE_DIR / "pending_profiles"

# Stale-pending thresholds: <3d = recent (silent), 3-7d = needs review, >=7d = urgent
PENDING_RECENT_DAYS = 3
PENDING_URGENT_DAYS = 7


def _scan_pending_profiles(today: str) -> list[dict]:
    """Scan pending_profiles/*.json and return list of {id, age_days, validation_ok,
    high_risk} for unreviewed profiles. Skips *.validation.json companion files."""
    if not PENDING_PROFILES_DIR.is_dir():
        return []

    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []

    out = []
    for path in sorted(PENDING_PROFILES_DIR.glob("*.json")):
        if path.name.endswith(".validation.json"):
            continue

        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
            age_days = (today_dt - mtime).days
        except OSError:
            continue

        # Read companion validation file if present
        validation_ok = None
        high_risk = None
        val_path = path.with_suffix(".validation.json")
        if val_path.exists():
            try:
                v = json.loads(val_path.read_text())
                validation_ok = v.get("ok")
                high_risk = v.get("high_risk")
            except (json.JSONDecodeError, OSError):
                pass

        out.append({
            "id": path.stem,
            "path": str(path),
            "age_days": max(age_days, 0),
            "validation_ok": validation_ok,
            "high_risk": high_risk,
        })
    return out


def _read_auto_promote_today(today: str) -> list[dict]:
    """Read auto-promote agent's history file, return today's entries."""
    if not AUTO_PROMOTE_LOG.exists():
        return []
    entries = []
    for line in AUTO_PROMOTE_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            if e.get("date") == today:
                entries.append(e)
        except json.JSONDecodeError:
            continue
    return entries


def send_daily_summary_email(state: dict, today: str) -> None:
    """Send one email per day summarizing trial outcomes + auto-promote results.

    Buckets:
      - decided_today: trials whose end_date == today (PASS / fail_quality / fail_quantity→watchlist or inaccessible)
      - active_progress: still-running trials with a daily_check today
      - auto_promoted: from logs/auto-promote-history.jsonl, entries with date==today
    """
    if state.get("last_summary_sent_date") == today:
        return  # already sent today

    history = state.get("history", [])
    decided_today = [h for h in history if h.get("end_date") == today]

    actives = state.get("active_trials", [])
    active_progress = [a for a in actives if today in a.get("daily_checks", {})]

    auto_promoted = _read_auto_promote_today(today)
    pending_profiles = _scan_pending_profiles(today)
    pending_urgent = [p for p in pending_profiles if p["age_days"] >= PENDING_URGENT_DAYS]

    # Skip if absolutely nothing happened today AND no urgent pending profiles
    if (not decided_today and not active_progress and not auto_promoted
            and not pending_urgent):
        return

    env = load_env()
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    mail_to = env.get("MAIL_TO", "")
    if not smtp_user or not smtp_pass or not mail_to:
        print("WARNING: SMTP not configured, skipping daily summary email")
        return

    pass_rows = ""
    fail_quality_rows = ""
    inconclusive_rows = ""
    aborted_rows = ""
    fail_quantity_inaccessible_rows = ""
    fail_quantity_watchlist_rows = ""
    for t in decided_today:
        name = html.escape(t.get("name", t.get("id", "?")))
        dwa = t.get("days_with_articles", "?")
        avg_q = t.get("avg_quality_score", 0)
        url = t.get("research_url", "")
        row = (
            f"<tr><td style='padding:6px 10px'>{name}</td>"
            f"<td style='padding:6px 10px'>{dwa}/{TRIAL_DAYS}</td>"
            f"<td style='padding:6px 10px'>{avg_q:.2f}</td>"
            f"<td style='padding:6px 10px;font-size:11px'>"
            f"<a href='{html.escape(url)}'>{html.escape(url[:60])}</a></td></tr>\n"
        )
        bucket = _summary_bucket(t)
        if bucket == "pass":
            pass_rows += row
        elif bucket == "fail_quality":
            fail_quality_rows += row
        elif bucket == "inconclusive":
            inconclusive_rows += row
        elif bucket == "aborted_needs_fetcher":
            aborted_rows += row
        elif bucket == "inaccessible":
            fail_quantity_inaccessible_rows += row
        elif bucket == "low_cadence":
            fail_quantity_watchlist_rows += row

    active_rows = ""
    for a in active_progress:
        name = html.escape(a.get("name", a.get("id", "?")))
        start = a.get("start_date", "")
        try:
            elapsed = (datetime.strptime(today, "%Y-%m-%d")
                       - datetime.strptime(start, "%Y-%m-%d")).days
            day_label = f"Day {elapsed + 1}/{TRIAL_DAYS}"
        except (ValueError, TypeError):
            day_label = "?"
        today_check = a.get("daily_checks", {}).get(today, {})
        articles_today = today_check.get("article_count", 0)
        accessible = today_check.get("accessible", False)
        progress = (f"<span style='color:#1a7f37'>{articles_today} articles</span>"
                    if accessible else
                    f"<span style='color:#cf222e'>unreachable</span>")
        all_scores = [art["overall"] for s in a.get("quality_samples", [])
                      for art in s.get("articles", [])]
        avg_q = sum(all_scores) / len(all_scores) if all_scores else 0.0
        active_rows += (
            f"<tr><td style='padding:6px 10px'>{name}</td>"
            f"<td style='padding:6px 10px'>{day_label}</td>"
            f"<td style='padding:6px 10px'>{progress}</td>"
            f"<td style='padding:6px 10px'>{avg_q:.2f} ({len(all_scores)} samples)</td></tr>\n"
        )

    pending_rows = ""
    for p in sorted(pending_profiles, key=lambda x: -x["age_days"]):
        if p["age_days"] < PENDING_RECENT_DAYS:
            continue  # don't surface fresh ones
        age = p["age_days"]
        if age >= PENDING_URGENT_DAYS:
            color = "#cf222e"
            label = f"⚠️ {age}d (URGENT)"
        else:
            color = "#9a6700"
            label = f"{age}d"
        flags = []
        if p["high_risk"]:
            flags.append("high-risk markers")
        if p["validation_ok"] is False:
            flags.append("validation FAIL")
        elif p["validation_ok"] is None:
            flags.append("no validation file")
        flags_str = ", ".join(flags) if flags else "passed validation"
        pending_rows += (
            f"<tr><td style='padding:6px 10px'>{html.escape(p['id'])}</td>"
            f"<td style='padding:6px 10px;color:{color};font-weight:bold'>{label}</td>"
            f"<td style='padding:6px 10px;font-size:12px'>{html.escape(flags_str)}</td></tr>\n"
        )

    auto_promoted_rows = ""
    for e in auto_promoted:
        fund_id = html.escape(e.get("id", "?"))
        name = html.escape(e.get("name", fund_id))
        outcome = e.get("outcome", "?")
        notes = html.escape(e.get("notes", "")[:120])
        color = "#1a7f37" if outcome == "promoted" else "#e3b341" if outcome == "deferred" else "#cf222e"
        auto_promoted_rows += (
            f"<tr><td style='padding:6px 10px'>{name}</td>"
            f"<td style='padding:6px 10px;color:{color};font-weight:bold'>{outcome}</td>"
            f"<td style='padding:6px 10px;font-size:12px'>{notes}</td></tr>\n"
        )

    def section(title: str, rows: str, header_html: str, color: str) -> str:
        if not rows:
            return ""
        return f"""
<h3 style="margin:18px 0 6px;color:{color}">{title}</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e1e4e8;border-radius:6px;">
  <tr style="background:#f6f8fa">{header_html}</tr>
{rows}
</table>"""

    trial_header = ("<th style='padding:6px 10px;text-align:left'>Fund</th>"
                    "<th style='padding:6px 10px;text-align:left'>Days w/ articles</th>"
                    "<th style='padding:6px 10px;text-align:left'>Avg quality</th>"
                    "<th style='padding:6px 10px;text-align:left'>URL</th>")
    active_header = ("<th style='padding:6px 10px;text-align:left'>Fund</th>"
                     "<th style='padding:6px 10px;text-align:left'>Day</th>"
                     "<th style='padding:6px 10px;text-align:left'>Today's articles</th>"
                     "<th style='padding:6px 10px;text-align:left'>Quality so far</th>")
    promote_header = ("<th style='padding:6px 10px;text-align:left'>Fund</th>"
                      "<th style='padding:6px 10px;text-align:left'>Outcome</th>"
                      "<th style='padding:6px 10px;text-align:left'>Notes</th>")
    pending_header = ("<th style='padding:6px 10px;text-align:left'>Profile ID</th>"
                      "<th style='padding:6px 10px;text-align:left'>Age</th>"
                      "<th style='padding:6px 10px;text-align:left'>Flags</th>")

    n_pass = pass_rows.count("<tr>")
    n_fail_quality = fail_quality_rows.count("<tr>")
    n_inconclusive = inconclusive_rows.count("<tr>")
    n_aborted = aborted_rows.count("<tr>")
    n_inaccessible = fail_quantity_inaccessible_rows.count("<tr>")
    n_fail_qty_watch = fail_quantity_watchlist_rows.count("<tr>")
    n_active = len(active_progress)
    n_auto = len(auto_promoted)
    n_pending_urgent = len(pending_urgent)
    n_pending_total = pending_rows.count("<tr>")  # only counts rows we surfaced (>=3d)

    pending_chip = ""
    if n_pending_urgent:
        pending_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#ffebe9;"
            f"color:#cf222e;border-radius:12px;font-size:12px;font-weight:bold'>"
            f"⚠️ {n_pending_urgent} pending profile(s) ≥{PENDING_URGENT_DAYS}d</span>"
        )
    elif n_pending_total:
        pending_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#fff8c5;"
            f"color:#9a6700;border-radius:12px;font-size:12px'>"
            f"{n_pending_total} pending profile(s)</span>"
        )

    inconclusive_chip = ""
    if n_inconclusive:
        inconclusive_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#fff8c5;"
            f"color:#9a6700;border-radius:12px;font-size:12px'>{n_inconclusive} inconclusive</span>"
        )
    aborted_chip = ""
    if n_aborted:
        aborted_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#ffebe9;"
            f"color:#cf222e;border-radius:12px;font-size:12px'>{n_aborted} aborted (needs fetcher)</span>"
        )

    summary_chips = (
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#dafbe1;color:#1a7f37;border-radius:12px;font-size:12px'>{n_pass} promoted</span>"
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#fff8c5;color:#9a6700;border-radius:12px;font-size:12px'>{n_fail_quality} watchlist (low quality)</span>"
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#fff8c5;color:#9a6700;border-radius:12px;font-size:12px'>{n_fail_qty_watch} watchlist (low cadence)</span>"
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#ffebe9;color:#cf222e;border-radius:12px;font-size:12px'>{n_inaccessible} inaccessible</span>"
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#ddf4ff;color:#0969da;border-radius:12px;font-size:12px'>{n_active} active</span>"
        f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#f0e7ff;color:#6639ba;border-radius:12px;font-size:12px'>{n_auto} auto-promote actions</span>"
        f"{inconclusive_chip}{aborted_chip}"
        f"{pending_chip}"
    )

    html_body = f"""<html><body style="font-family:-apple-system,sans-serif;padding:20px;max-width:720px">
<h2 style="margin:0">📊 GMIA Trial Daily Summary — {today}</h2>
<p style="color:#586069;margin:8px 0 16px">{summary_chips}</p>

{section("✅ Promoted today (trial PASS)", pass_rows, trial_header, "#1a7f37")}
{section("⚙️ Auto-promote agent results", auto_promoted_rows, promote_header, "#6639ba")}
{section("📝 Pending profiles awaiting human review", pending_rows, pending_header, "#cf222e" if pending_urgent else "#9a6700")}
{section("⚠️ Watchlist — low quality", fail_quality_rows, trial_header, "#9a6700")}
{section("⚠️ Inconclusive — articles detected but sampling failed", inconclusive_rows, trial_header, "#9a6700")}
{section("🔧 Aborted day 1 — needs fetcher (routed to synthesis)", aborted_rows, trial_header, "#cf222e")}
{section("⚠️ Watchlist — low cadence (some articles, not enough days)", fail_quantity_watchlist_rows, trial_header, "#9a6700")}
{section("🚫 Inaccessible — 0 articles (routed to fetcher-synthesis)", fail_quantity_inaccessible_rows, trial_header, "#cf222e")}
{section("🔄 Active trials (in progress)", active_rows, active_header, "#0969da")}

<p style="color:#8b949e;font-size:11px;margin-top:24px">
  GMIA Trial Manager — daily summary, sent once per day after trial run.<br>
  Promoted funds need wiring (sources.json + content fetcher + badge + profile draft).
  Auto-promote agent runs daily 02:30 BJT and handles most of this automatically.
</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    pending_tag = f" / ⚠️{n_pending_urgent}PP" if n_pending_urgent else ""
    msg["Subject"] = (f"GMIA Daily Summary {today}: {n_pass}P / "
                      f"{n_fail_quality + n_fail_qty_watch + n_inconclusive}W / "
                      f"{n_inaccessible}I / {n_active}A / {n_auto}AP"
                      f"{pending_tag}")
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["MIME-Version"] = "1.0"
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"Daily summary email sent to {mail_to}")
        state["last_summary_sent_date"] = today
        save_state(state)
    except Exception as exc:
        print(f"WARNING: daily summary email failed: {exc}")


# ── main commands ─────────────────────────────────────────────────────────────

def cmd_run() -> None:
    state = load_state()
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    actives = state.setdefault("active_trials", [])

    # ── Step 1: process each active trial ─────────────────────────────────────
    for active in list(actives):  # iterate copy; we may remove items
        # Skip if already checked today
        if today in active.get("daily_checks", {}):
            print(f"[trial] Already checked {active['name']} today, skipping")
        else:
            url = active.get("research_url") or active.get("homepage_url", "")
            print(f"[trial] Checking {active['name']} — {url}")
            result = count_articles_with_fetcher(active)
            active.setdefault("daily_checks", {})[today] = result
            if result["accessible"]:
                print(f"[trial]   → {result['article_count']} articles detected")
            else:
                print(f"[trial]   → unreachable: {result['error']}")
            save_state(state)

        # Quality sampling on designated days
        start = datetime.strptime(active["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed = (datetime.now(BJT) - start).days
        trial_day = elapsed + 1  # 1-indexed

        if trial_day in SAMPLE_DAYS:
            existing_samples = active.get("quality_samples", [])
            already_sampled_today = any(s.get("day") == trial_day for s in existing_samples)
            if not already_sampled_today:
                already_sampled_urls = {
                    a["url"]
                    for s in existing_samples
                    for a in s.get("articles", [])
                    if a.get("url")
                }
                url = active.get("research_url") or active.get("homepage_url", "")
                print(f"[trial] Quality sampling day {trial_day} for {active['name']}"
                      f" (excluding {len(already_sampled_urls)} previously-sampled urls)...")
                qr = sample_article_quality(url, trial=active,
                                            exclude_urls=already_sampled_urls)
                qr["day"] = trial_day
                qr["date"] = today
                active.setdefault("quality_samples", []).append(qr)
                save_state(state)
                if qr["error"]:
                    print(f"[trial]   Quality sampling error: {qr['error']}")
                else:
                    print(f"[trial]   Sampled {qr['sampled']} articles, "
                          f"avg quality score: {qr['avg_score']:.2f}")
                    for art in qr.get("articles", []):
                        print(f"[trial]     {art['overall']:.1f} — {art['notes'][:60]}")

        # Check if trial period complete
        if elapsed >= TRIAL_DAYS and not active.get("auto_decided"):
            total_articles = sum(
                d.get("article_count", 0)
                for d in active.get("daily_checks", {}).values()
                if d.get("accessible")
            )
            days_with_articles = sum(
                1 for d in active.get("daily_checks", {}).values()
                if d.get("accessible") and d.get("article_count", 0) > 0
            )
            quantity_ok = days_with_articles >= MIN_DAYS_WITH_ARTICLES

            all_scores = [
                a["overall"]
                for s in active.get("quality_samples", [])
                for a in s.get("articles", [])
            ]
            avg_quality = (sum(all_scores) / len(all_scores)) if all_scores else 0.0
            quality_ok = bool(all_scores) and avg_quality >= MIN_QUALITY_SCORE

            passed = quantity_ok and quality_ok
            active["auto_decided"] = True
            active["end_date"] = today
            active["total_articles"] = total_articles
            active["days_with_articles"] = days_with_articles
            active["avg_quality_score"] = round(avg_quality, 3)
            if not quantity_ok:
                active["outcome"] = "fail_quantity"
            elif not all_scores:
                # Articles were detected on enough days but not a single one
                # could be sampled — that is a sampling failure, not a quality
                # verdict. Do not label it fail_quality / LOW QUALITY.
                active["outcome"] = "inconclusive"
            elif not quality_ok:
                active["outcome"] = "fail_quality"
            else:
                active["outcome"] = "pass"

            candidates = load_candidates()
            for c in candidates:
                if c["id"] == active["id"]:
                    if not passed:
                        if not quantity_ok:
                            if total_articles == 0:
                                # Zero articles across the entire trial window —
                                # listing-page selector is broken or page is JS-only.
                                # Route to fetcher-synthesis (which only picks up
                                # status="inaccessible") so a Playwright fetcher
                                # can be auto-synthesized next Sunday.
                                c["status"] = "inaccessible"
                                c["notes"] = (
                                    f"Trial failed: 0 articles detected across "
                                    f"{TRIAL_DAYS} days — site likely requires JS "
                                    f"rendering or selector is broken. Routed to "
                                    f"fetcher-synthesis queue."
                                )
                            else:
                                # Some articles present but not on enough days —
                                # genuine slow-publication pattern, not access issue.
                                c["status"] = "watchlist"
                                c["notes"] = (
                                    f"Trial failed: articles on only "
                                    f"{days_with_articles}/{TRIAL_DAYS} days"
                                )
                        elif not all_scores:
                            c["status"] = "watchlist"
                            c["notes"] = "Trial inconclusive: no quality samples obtained"
                        else:
                            c["status"] = "watchlist"
                            c["notes"] = f"Trial failed: low quality ({avg_quality:.2f})"
                    else:
                        c["status"] = "promoted"
                        c["notes"] = (f"RECOMMEND: trial passed "
                                      f"({days_with_articles}/{TRIAL_DAYS} days with articles, quality={avg_quality:.2f})")
                        # Aggregate js_only stats from quality_samples
                        total_js_only = sum(
                            s.get("js_only_count", 0)
                            for s in active.get("quality_samples", [])
                        )
                        total_js_checked = sum(
                            s.get("js_only_checked", 0)
                            for s in active.get("quality_samples", [])
                        )
                        if total_js_checked > 0 and total_js_only / total_js_checked > 0.5:
                            c["requires_playwright"] = True
                        # If no registered fetcher, flag for immediate synthesis
                        has_fetcher = active["id"] in _load_fetchers()
                        if not has_fetcher:
                            c["synthesis_priority"] = True
                            c["synthesis_priority_set_at"] = datetime.now(BJT).isoformat()
                    break
            save_candidates(candidates)

            state.setdefault("history", []).append(active)
            state["active_trials"] = [t for t in state["active_trials"] if t["id"] != active["id"]]
            save_state(state)
            send_trial_email(active, passed, total_articles, days_with_articles=days_with_articles)
            print(f"[trial] Trial complete for {active['name']}: "
                  f"{'PASS' if passed else 'FAIL'} "
                  f"({days_with_articles}/{TRIAL_DAYS} days, quality={avg_quality:.2f})")

    # ── Step 2: fill open slots up to MAX_CONCURRENT_TRIALS ───────────────────
    if len(state["active_trials"]) < MAX_CONCURRENT_TRIALS:
        queue = get_trial_queue(state)
        prod_ids = existing_source_ids()
        queue = [c for c in queue if c["id"] not in prod_ids]

        slots_available = MAX_CONCURRENT_TRIALS - len(state["active_trials"])
        to_start = queue[:slots_available]

        if not to_start:
            if not state["active_trials"]:
                print("[trial] No candidates queued for trial")
            return

        for next_candidate in to_start:
            print(f"[trial] Starting trial for {next_candidate['name']} "
                  f"(fit={next_candidate.get('fit_score', '?')}, "
                  f"quality={next_candidate.get('quality', '?')})")

            new_trial = {
                "id": next_candidate["id"],
                "name": next_candidate["name"],
                "research_url": next_candidate.get("research_url") or next_candidate.get("homepage_url", ""),
                "homepage_url": next_candidate.get("homepage_url", ""),
                "fit_score": next_candidate.get("fit_score"),
                "quality": next_candidate.get("quality"),
                "topics": next_candidate.get("topics"),
                "start_date": today,
                "end_date": None,
                "daily_checks": {},
                "auto_decided": False,
                "outcome": None,
            }
            state["active_trials"].append(new_trial)
            save_state(state)

            url = new_trial["research_url"]
            result = count_articles_with_fetcher(new_trial)
            new_trial["daily_checks"][today] = result
            save_state(state)
            if result["accessible"]:
                print(f"[trial]   Day 1: {result['article_count']} articles detected")
            else:
                print(f"[trial]   Day 1: unreachable — {result['error']}")

            # Fail-fast: index reachable but zero articles AND no registered
            # fetcher → static HTML has no article links (JS-rendered index).
            # A 7-day trial cannot succeed; route to fetcher-synthesis now
            # instead of wasting the window (lesson: acadian-asset 5-26→6-02
            # burned 7 days at 0 articles/day).
            has_fetcher = new_trial["id"] in _load_fetchers()
            if (not has_fetcher
                    and result.get("accessible")
                    and result.get("article_count", 0) == 0):
                print(f"[trial]   Aborting — no article links in static HTML and "
                      f"no registered fetcher; routing to fetcher-synthesis")
                new_trial["auto_decided"] = True
                new_trial["end_date"] = today
                new_trial["outcome"] = "skipped"
                new_trial["skip_reason"] = "needs_fetcher"
                new_trial["total_articles"] = 0
                new_trial["days_with_articles"] = 0
                candidates = load_candidates()
                for c in candidates:
                    if c["id"] == new_trial["id"]:
                        c["status"] = "inaccessible"
                        c["notes"] = ("Trial aborted on day 1: index reachable but "
                                      "no article links in static HTML — needs a "
                                      "fetcher. Routed to fetcher-synthesis queue.")
                        break
                save_candidates(candidates)
                state.setdefault("history", []).append(new_trial)
                state["active_trials"] = [
                    t for t in state["active_trials"] if t["id"] != new_trial["id"]
                ]
                save_state(state)
                continue

            # Day 1 quality sampling
            if 1 in SAMPLE_DAYS:
                print(f"[trial] Quality sampling day 1 for {next_candidate['name']}...")
                qr = sample_article_quality(url, trial=new_trial)
                qr["day"] = 1
                qr["date"] = today
                new_trial.setdefault("quality_samples", []).append(qr)
                save_state(state)
                if qr["error"]:
                    print(f"[trial]   Quality sampling error: {qr['error']}")
                else:
                    print(f"[trial]   Sampled {qr['sampled']} articles, "
                          f"avg quality score: {qr['avg_score']:.2f}")
                    for art in qr.get("articles", []):
                        print(f"[trial]     {art['overall']:.2f} — {art['notes'][:60]}")

    # ── Step 3: send one daily summary email per day ─────────────────────────
    send_daily_summary_email(state, today)


def cmd_status() -> None:
    state = load_state()
    actives = state.get("active_trials", [])

    if not actives:
        print("No active trials.")
    else:
        print(f"Active trials: {len(actives)}/{MAX_CONCURRENT_TRIALS}")
        for active in actives:
            start = active["start_date"]
            start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=BJT)
            elapsed = (datetime.now(BJT) - start_dt).days
            total = sum(
                d.get("article_count", 0)
                for d in active.get("daily_checks", {}).values()
                if d.get("accessible")
            )
            print(f"\n  [{active['id']}] {active['name']}")
            print(f"    Started: {start}  |  Day {elapsed+1}/{TRIAL_DAYS}")
            print(f"    URL: {active.get('research_url','')}")
            print(f"    Quality: {active.get('quality','?')}  Fit: {active.get('fit_score','?')}")
            print(f"    Total articles so far: {total} (need {MIN_DAYS_WITH_ARTICLES} days to pass)")

            samples = active.get("quality_samples", [])
            all_scores = [a["overall"] for s in samples for a in s.get("articles", [])]
            if all_scores:
                avg_q = sum(all_scores) / len(all_scores)
                print(f"    Avg quality score: {avg_q:.2f} (need {MIN_QUALITY_SCORE} to pass)")
            else:
                next_sample = min(SAMPLE_DAYS - set(s.get("day", 0) for s in samples), default=None)
                if next_sample:
                    print(f"    Quality sampling: pending (next on day {next_sample})")

            for date, info in sorted(active.get("daily_checks", {}).items()):
                status = (f"{info.get('article_count',0)} articles" if info.get("accessible")
                          else f"unreachable ({info.get('error','')})")
                print(f"    {date}: {status}")
            for sample in samples:
                print(f"    Quality sample day {sample.get('day','?')}: "
                      f"avg={sample.get('avg_score',0):.2f}, "
                      f"sampled={sample.get('sampled',0)} articles")
                for art in sample.get("articles", []):
                    print(f"      {art['overall']:.2f} — {art.get('notes','')[:60]}")

    queue = get_trial_queue(state)
    prod_ids = existing_source_ids()
    queue = [c for c in queue if c["id"] not in prod_ids]
    print(f"\nQueue ({len(queue)} candidates):")
    for c in queue:
        print(f"  - {c['name']:40} fit={c.get('fit_score',0):.3f}  quality={c.get('quality','?')}")

    history = state.get("history", [])
    if history:
        print(f"\nHistory ({len(history)} completed trials):")
        for h in history:
            outcome = h.get("outcome", "?")
            total = h.get("total_articles", "?")
            print(f"  {h['name']:40} {outcome:6}  {total} articles  ({h['start_date']}→{h.get('end_date','')})")


def cmd_skip() -> None:
    state = load_state()
    actives = state.get("active_trials", [])
    if not actives:
        print("No active trials to skip.")
        return

    # Optional: --id <fund_id> to skip a specific trial
    target_id = None
    args = sys.argv[2:]
    if "--id" in args:
        idx = args.index("--id")
        if idx + 1 < len(args):
            target_id = args[idx + 1]

    if target_id:
        match = next((t for t in actives if t["id"] == target_id), None)
        if not match:
            print(f"No active trial with id '{target_id}'. Active: {[t['id'] for t in actives]}")
            return
        to_skip = match
    else:
        to_skip = actives[0]  # skip oldest by default

    today = datetime.now(BJT).strftime("%Y-%m-%d")
    to_skip["auto_decided"] = True
    to_skip["end_date"] = today
    to_skip["outcome"] = "skipped"
    state.setdefault("history", []).append(to_skip)
    state["active_trials"] = [t for t in actives if t["id"] != to_skip["id"]]
    save_state(state)
    print(f"Skipped trial for {to_skip['name']}")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        cmd_run()
    elif cmd == "status":
        cmd_status()
    elif cmd == "skip":
        cmd_skip()
    else:
        print(f"Unknown command: {cmd}. Use: run | status | skip")
        sys.exit(1)
