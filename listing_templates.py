#!/usr/bin/env python3
"""One tool per page type, driven by a spec in config/sources.json.

Of the 42 production listing fetchers, 23 are the same shape -- a page of
cards, each with a title, a link and a date -- hand-written 23 times, 1,367
lines. Four things differ between them: how the HTML is obtained (requests, or
a browser waiting for a selector) and three CSS selectors. Everything else is
identical: validate the host, drop duplicates, parse the date, cut to
max_articles, build the same dict.

That duplication has a measured cost. The hardcoded-host sweep found one
defect in 24 of 40 fetchers; the word-fusing bug lived in four separate
paragraph loops; networkidle is in 27 places. One defect, twenty-four fixes,
and some always missed.

Nothing switches over automatically. A source keeps its hand-written fetcher
until scripts/compare_fetchers.py shows the template returning exactly the same
titles, urls and dates from the live site; `listing_template` in its config
entry is what makes fetch_articles use this instead. Types that really are
different -- JSON APIs, sitemaps, sites behind an attestation form -- keep
their own code, because forcing them in here would rebuild the forty-two
if-statements this exists to remove.

Spec (type card_list):
    fetch          "requests" | "playwright"
    wait_selector  playwright only: wait for this before reading the HTML
    card           selector for each card
    link           anchor inside the card, or "self" when the card is the <a>
    title          optional; defaults to the link's own text
    date           optional; selector for the date text inside the card
"""
from __future__ import annotations

import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SUPPORTED_TYPES = ("card_list",)
REQUIRED = {"card_list": ("fetch", "card")}
FETCH_MODES = ("requests", "playwright")


def validate_spec(spec: dict) -> None:
    """Raise ValueError if the spec cannot drive a fetch."""
    kind = (spec or {}).get("type")
    if kind not in SUPPORTED_TYPES:
        raise ValueError(f"listing_template type {kind!r} is not one of {SUPPORTED_TYPES}")
    for key in REQUIRED[kind]:
        if not spec.get(key):
            raise ValueError(f"listing_template for type {kind!r} needs {key!r}")
    if spec["fetch"] not in FETCH_MODES:
        raise ValueError(f"listing_template fetch {spec['fetch']!r} is not one of {FETCH_MODES}")


def _text(el) -> str:
    return el.get_text(strip=True) if el else ""


def parse_cards(html: str, source: dict, spec: dict) -> list[dict]:
    """Rows from a card listing, in page order. Pure: no network."""
    from fetch_articles import _validate_hostname, parse_date

    soup = BeautifulSoup(html or "", "html.parser")
    expected_host = source["expected_hostname"]
    link_sel = spec.get("link") or "a"
    rows: list[dict] = []
    seen: set[str] = set()
    for card in soup.select(spec["card"]):
        link_el = card if link_sel == "self" else card.select_one(link_sel)
        href = link_el.get("href", "") if link_el else ""
        if not href:
            continue
        url = urljoin(source["url"], href)
        if expected_host and not _validate_hostname(url, expected_host):
            continue
        if url in seen:
            continue
        title = _text(card.select_one(spec["title"])) if spec.get("title") else _text(link_el)
        if not title:
            continue
        seen.add(url)
        date_raw = _text(card.select_one(spec["date"])) if spec.get("date") else ""
        rows.append({"title": title, "url": url,
                     "date": parse_date(date_raw) if date_raw else None,
                     "date_raw": date_raw})
    return rows[:source.get("max_articles", 10)]


def _playwright_html(url: str, wait_selector: str | None = None, wait_ms: int = 5000) -> str:
    from fetch_articles import _get_playwright_page

    return _get_playwright_page(url, wait_selector=wait_selector, wait_ms=wait_ms)


def fetch(source: dict) -> list[dict]:
    """Run the source's listing_template and return rows."""
    from fetch_articles import HEADERS

    spec = source.get("listing_template") or {}
    validate_spec(spec)
    if spec["fetch"] == "playwright":
        html = _playwright_html(source["url"], wait_selector=spec.get("wait_selector"),
                                wait_ms=spec.get("wait_ms", 5000))
    else:
        resp = requests.get(source["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
    return parse_cards(html, source, spec)
