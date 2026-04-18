#!/usr/bin/env python3
"""
GMIA Trial Manager — validates candidate sources with 7-day live article checks
and Haiku-powered quality sampling.

After the nightly discovery agent marks a candidate as validated (HIGH/MEDIUM quality),
this manager picks one at a time, fetches its research URL daily for 7 days, counts
detectable articles, and auto-decides whether to send a graduation recommendation.

On days 1 and 4, the manager samples up to 3 article links, extracts their text,
and sends them to Claude Haiku for quality assessment (relevance, depth,
extractability).  The quality score is factored into the pass/fail decision.

Trial SUCCESS = quantity (≥ MIN_ARTICLES_TOTAL) AND quality (avg score ≥ 0.5)
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
MIN_ARTICLES_TOTAL = 3      # articles needed over trial to pass
MIN_QUALITY = {"HIGH", "MEDIUM"}
MIN_QUALITY_SCORE = 0.5     # avg Haiku quality score to pass (0-1)
SAMPLE_DAYS = {1, 4}        # trial days on which to run quality sampling
SAMPLE_SIZE = 3             # articles to sample per quality check
HAIKU_MODEL = "claude-haiku-4-5-20251001"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


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


# ── article detection ─────────────────────────────────────────────────────────

_DATE_PATTERNS = [
    r"\b20\d{2}[-/]\d{2}[-/]\d{2}\b",
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2},? 20\d{2}\b",
    r"\b\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* 20\d{2}\b",
]
_DATE_RE = re.compile("|".join(_DATE_PATTERNS), re.IGNORECASE)


def count_articles(url: str, timeout: int = 20) -> dict:
    """Fetch research URL and count detectable article signals.

    Returns dict with keys: accessible, article_count, date_count, error
    """
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout,
                         follow_redirects=True)
        if resp.status_code != 200:
            return {"accessible": False, "article_count": 0, "date_count": 0,
                    "error": f"HTTP {resp.status_code}"}

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

        # Heuristic: article count = strongest signal
        article_count = max(
            time_tags,
            date_matches,
            min(h_tags, 20),       # h-tags capped (navs have many h2s)
        )

        return {
            "accessible": True,
            "article_count": article_count,
            "date_count": date_matches,
            "error": None,
        }

    except Exception as exc:
        return {"accessible": False, "article_count": 0, "date_count": 0,
                "error": str(exc)[:120]}


# ── quality sampling (Haiku) ─────────────────────────────────────────────────

def _extract_article_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    """Extract up to SAMPLE_SIZE article-like links from a research index page."""
    from urllib.parse import urljoin

    seen: set[str] = set()
    links: list[str] = []
    parsed_base = urlparse(base_url)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = urljoin(base_url, href)
        parsed = urlparse(full)

        # Same domain only
        if parsed.netloc != parsed_base.netloc:
            continue
        # Skip anchors, category/tag pages, non-article patterns
        if parsed.path in ("", "/") or parsed.path == parsed_base.path:
            continue
        if any(seg in parsed.path.lower() for seg in
               ("/tag/", "/category/", "/page/", "/author/", "/login", "/search")):
            continue
        # Prefer paths with date-like segments or /insights/ /research/ depth
        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) < 2:
            continue

        canon = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if canon in seen:
            continue
        seen.add(canon)
        links.append(canon)

        if len(links) >= SAMPLE_SIZE * 3:  # collect extras for fallback
            break

    return links


def _extract_article_text(url: str, timeout: int = 20) -> str | None:
    """Fetch a single article page and extract clean body text."""
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("nav, footer, header, .nav, .footer, .header, "
                               "script, style, aside, .sidebar"):
            tag.decompose()

        # Try <article> first, fall back to <main>, then full body
        content = soup.find("article") or soup.find("main") or soup.find("body")
        if not content:
            return None
        text = content.get_text(" ", strip=True)
        # Return first 3000 chars (enough for Haiku to judge quality)
        return text[:3000] if len(text) > 200 else None
    except Exception:
        return None


def _call_haiku(prompt: str) -> dict | None:
    """Call Claude Haiku for quality assessment. Returns parsed JSON or None."""
    env = load_env()
    api_key = env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, skipping quality sampling")
        return None

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
        # Extract JSON from response
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as exc:
        print(f"WARNING: Haiku call failed: {exc}")
        return None


def sample_article_quality(research_url: str) -> dict:
    """Sample articles from a source and assess quality via Haiku.

    Returns dict with keys: sampled, articles, avg_score, error
    Each article entry: {url, title_hint, score, relevance, depth, extractable, notes}
    """
    # Step 1: fetch the index page and extract article links
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

    # Step 2: extract text from each article, using fallback links if needed
    article_texts: list[tuple[str, str]] = []  # (url, text)
    for url in links:
        text = _extract_article_text(url)
        if text:
            article_texts.append((url, text))
        if len(article_texts) >= SAMPLE_SIZE:
            break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article"}

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
    }


# ── queue logic ───────────────────────────────────────────────────────────────

def get_trial_queue(state: dict) -> list[dict]:
    """Return validated HIGH/MEDIUM candidates not yet trialed, sorted by fit_score desc."""
    candidates = load_candidates()
    trialed_ids = {h["id"] for h in state.get("history", [])}
    active_ids = {t["id"] for t in state.get("active_trials", [])}

    queue = []
    for c in candidates:
        if c["status"] != "validated":
            continue
        if c.get("quality") not in MIN_QUALITY:
            continue
        if c["id"] in trialed_ids:
            continue
        if c["id"] in active_ids:
            continue
        queue.append(c)

    return sorted(queue, key=lambda c: -(c.get("fit_score") or 0))


def existing_source_ids() -> set[str]:
    data = json.loads(SOURCES_FILE.read_text())
    return {s["id"] for s in data.get("sources", [])}


# ── email ─────────────────────────────────────────────────────────────────────

def send_trial_email(trial: dict, passed: bool, total_articles: int) -> None:
    env = load_env()
    smtp_user = env.get("SMTP_USER", "")
    smtp_pass = env.get("SMTP_PASS", "")
    mail_to = env.get("MAIL_TO", "")
    if not smtp_user or not smtp_pass or not mail_to:
        print("WARNING: SMTP not configured, skipping trial email")
        return

    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    result_icon = "✅" if passed else "❌"
    outcome = trial.get("outcome", "")
    if outcome == "fail_quality":
        result_text = "FAILED — LOW QUALITY"
    elif outcome.startswith("fail"):
        result_text = "FAILED — INSUFFICIENT CONTENT"
    else:
        result_text = "READY TO INTEGRATE"
    result_color = "#1a7f37" if passed else "#cf222e"

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

    html = f"""<html><body style="font-family:-apple-system,sans-serif;padding:20px;max-width:600px">
<h2 style="margin:0">{result_icon} GMIA Trial: {trial['name']}</h2>
<p style="color:#586069;margin:4px 0">{now_bjt}</p>

<table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0;background:#f6f8fa;border-radius:6px;">
  <tr><td style="padding:8px"><strong>Result</strong></td>
      <td style="padding:8px;color:{result_color};font-weight:bold">{result_text}</td></tr>
  <tr><td style="padding:8px"><strong>Trial period</strong></td>
      <td style="padding:8px">{trial.get('start_date','')} → {trial.get('end_date','')}</td></tr>
  <tr><td style="padding:8px"><strong>Total articles detected</strong></td>
      <td style="padding:8px">{total_articles} (threshold: {MIN_ARTICLES_TOTAL})</td></tr>
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

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"GMIA Trial {'PASS' if passed else 'FAIL'}: {trial['name']} ({total_articles} articles, Q={avg_quality:.2f})"
    msg["From"] = smtp_user
    msg["To"] = mail_to
    msg["MIME-Version"] = "1.0"
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as s:
            s.login(smtp_user, smtp_pass)
            s.send_message(msg)
        print(f"Trial email sent to {mail_to}")
    except Exception as exc:
        print(f"WARNING: trial email failed: {exc}")


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
            result = count_articles(url)
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
                url = active.get("research_url") or active.get("homepage_url", "")
                print(f"[trial] Quality sampling day {trial_day} for {active['name']}...")
                qr = sample_article_quality(url)
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
            quantity_ok = total_articles >= MIN_ARTICLES_TOTAL

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
            active["avg_quality_score"] = round(avg_quality, 3)
            if not quantity_ok:
                active["outcome"] = "fail_quantity"
            elif not quality_ok:
                active["outcome"] = "fail_quality"
            else:
                active["outcome"] = "pass"

            candidates = load_candidates()
            for c in candidates:
                if c["id"] == active["id"]:
                    if not passed:
                        if not quantity_ok:
                            c["status"] = "watchlist"
                            c["notes"] = f"Trial failed: only {total_articles} articles"
                        elif not all_scores:
                            c["status"] = "watchlist"
                            c["notes"] = "Trial inconclusive: no quality samples obtained"
                        else:
                            c["status"] = "watchlist"
                            c["notes"] = f"Trial failed: low quality ({avg_quality:.2f})"
                    else:
                        c["notes"] = (f"RECOMMEND: trial passed "
                                      f"({total_articles} articles/7d, quality={avg_quality:.2f})")
                    break
            save_candidates(candidates)

            state.setdefault("history", []).append(active)
            state["active_trials"] = [t for t in state["active_trials"] if t["id"] != active["id"]]
            save_state(state)
            send_trial_email(active, passed, total_articles)
            print(f"[trial] Trial complete for {active['name']}: "
                  f"{'PASS' if passed else 'FAIL'} "
                  f"({total_articles} articles, quality={avg_quality:.2f})")

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
            result = count_articles(url)
            new_trial["daily_checks"][today] = result
            save_state(state)
            if result["accessible"]:
                print(f"[trial]   Day 1: {result['article_count']} articles detected")
            else:
                print(f"[trial]   Day 1: unreachable — {result['error']}")

            # Day 1 quality sampling
            if 1 in SAMPLE_DAYS:
                print(f"[trial] Quality sampling day 1 for {next_candidate['name']}...")
                qr = sample_article_quality(url)
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
            print(f"    Total articles so far: {total} (need {MIN_ARTICLES_TOTAL} to pass)")

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
