#!/usr/bin/env python3
"""
GMIA Trial Manager — validates candidate sources with 7-day live article checks.

After the nightly discovery agent marks a candidate as validated (HIGH/MEDIUM quality),
this manager picks one at a time, fetches its research URL daily for 7 days, counts
detectable articles, and auto-decides whether to send a graduation recommendation.

Trial SUCCESS (≥ MIN_ARTICLES_TOTAL over 7 days) → email "READY TO INTEGRATE" report
Trial FAIL   (< MIN_ARTICLES_TOTAL)               → candidate downgraded to watchlist

The manager does NOT automatically modify sources.json — graduation requires a human
decision (adding the source with the correct fetch method, entrypoints, etc.).

CLI:
  python3 gmia-trial-manager.py run      — normal daily run (called from pipeline)
  python3 gmia-trial-manager.py status   — print current trial state
  python3 gmia-trial-manager.py skip     — skip active trial, move to next candidate
"""

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
MIN_ARTICLES_TOTAL = 3      # articles needed over trial to pass
MIN_QUALITY = {"HIGH", "MEDIUM"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── state helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if TRIAL_STATE_FILE.exists():
        return json.loads(TRIAL_STATE_FILE.read_text())
    return {"active_trial": None, "history": []}


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


# ── queue logic ───────────────────────────────────────────────────────────────

def get_trial_queue(state: dict) -> list[dict]:
    """Return validated HIGH/MEDIUM candidates not yet trialed, sorted by fit_score desc."""
    candidates = load_candidates()
    trialed_ids = {h["id"] for h in state.get("history", [])}
    active_id = (state.get("active_trial") or {}).get("id")

    queue = []
    for c in candidates:
        if c["status"] != "validated":
            continue
        if c.get("quality") not in MIN_QUALITY:
            continue
        if c["id"] in trialed_ids:
            continue
        if c["id"] == active_id:
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
    result_text = "READY TO INTEGRATE" if passed else "INSUFFICIENT CONTENT"
    result_color = "#1a7f37" if passed else "#cf222e"

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
  <tr><td style="padding:8px"><strong>Fit score</strong></td>
      <td style="padding:8px">{trial.get('fit_score', '?')}</td></tr>
</table>

<h3 style="margin:12px 0 6px">Daily checks</h3>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
  <tr style="background:#f6f8fa"><th style="padding:4px 8px;text-align:left">Date</th>
      <th style="padding:4px 8px;text-align:left">Articles detected</th></tr>
{daily_rows}
</table>
{action_html}
<p style="color:#8b949e;font-size:11px;margin-top:20px">GMIA Candidate Trial Manager — auto-generated</p>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"GMIA Trial {'PASS' if passed else 'FAIL'}: {trial['name']} ({total_articles} articles)"
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

    # ── Step 1: process active trial ──────────────────────────────────────────
    active = state.get("active_trial")

    if active:
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

        # Check if trial period complete
        start = datetime.strptime(active["start_date"], "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed = (datetime.now(BJT).replace(tzinfo=BJT) - start).days

        if elapsed >= TRIAL_DAYS and not active.get("auto_decided"):
            total_articles = sum(
                d.get("article_count", 0)
                for d in active.get("daily_checks", {}).values()
                if d.get("accessible")
            )
            passed = total_articles >= MIN_ARTICLES_TOTAL
            active["auto_decided"] = True
            active["end_date"] = today
            active["total_articles"] = total_articles
            active["outcome"] = "pass" if passed else "fail"

            # Update candidate status
            candidates = load_candidates()
            for c in candidates:
                if c["id"] == active["id"]:
                    if not passed:
                        c["status"] = "watchlist"
                        c["notes"] = f"Trial failed: only {total_articles} articles in 7 days"
                    else:
                        c["notes"] = f"RECOMMEND: trial passed ({total_articles} articles/7d)"
                    break
            save_candidates(candidates)

            state.setdefault("history", []).append(active)
            state["active_trial"] = None
            save_state(state)
            send_trial_email(active, passed, total_articles)
            print(f"[trial] Trial complete for {active['name']}: {'PASS' if passed else 'FAIL'} "
                  f"({total_articles} articles)")
            # Fall through to start next trial below
            active = None

    # ── Step 2: start next trial if slot is free ──────────────────────────────
    if not state.get("active_trial"):
        queue = get_trial_queue(state)

        # Skip candidates already in production sources
        prod_ids = existing_source_ids()
        queue = [c for c in queue if c["id"] not in prod_ids]

        if not queue:
            print("[trial] No candidates queued for trial")
            return

        next_candidate = queue[0]
        print(f"[trial] Starting trial for {next_candidate['name']} "
              f"(fit={next_candidate.get('fit_score', '?')}, "
              f"quality={next_candidate.get('quality', '?')})")

        state["active_trial"] = {
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
        save_state(state)

        # Run first check immediately
        url = state["active_trial"]["research_url"]
        result = count_articles(url)
        state["active_trial"]["daily_checks"][today] = result
        save_state(state)
        if result["accessible"]:
            print(f"[trial]   Day 1: {result['article_count']} articles detected")
        else:
            print(f"[trial]   Day 1: unreachable — {result['error']}")


def cmd_status() -> None:
    state = load_state()
    active = state.get("active_trial")

    if not active:
        print("No active trial.")
    else:
        start = active["start_date"]
        today = datetime.now(BJT).strftime("%Y-%m-%d")
        start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=BJT)
        elapsed = (datetime.now(BJT).replace(tzinfo=BJT) - start_dt).days
        total = sum(
            d.get("article_count", 0)
            for d in active.get("daily_checks", {}).values()
            if d.get("accessible")
        )
        print(f"Active trial: {active['name']} ({active['id']})")
        print(f"  Started: {start}  |  Day {elapsed+1}/{TRIAL_DAYS}")
        print(f"  URL: {active.get('research_url','')}")
        print(f"  Quality: {active.get('quality','?')}  Fit: {active.get('fit_score','?')}")
        print(f"  Total articles so far: {total} (need {MIN_ARTICLES_TOTAL} to pass)")
        print()
        for date, info in sorted(active.get("daily_checks", {}).items()):
            status = f"{info.get('article_count',0)} articles" if info.get("accessible") else f"unreachable ({info.get('error','')})"
            print(f"  {date}: {status}")

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
    active = state.get("active_trial")
    if not active:
        print("No active trial to skip.")
        return
    today = datetime.now(BJT).strftime("%Y-%m-%d")
    active["auto_decided"] = True
    active["end_date"] = today
    active["outcome"] = "skipped"
    state.setdefault("history", []).append(active)
    state["active_trial"] = None
    save_state(state)
    print(f"Skipped trial for {active['name']}")


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
