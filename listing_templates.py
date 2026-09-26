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
    wait_until     playwright only: "networkidle" (default) or "domcontentloaded"
    card           selector for each card
    link           anchor inside the card, or "self" when the card is the <a>
    title          optional; defaults to the link's own text
    date           optional; selector for the date text inside the card
    date_attr      optional; read the date from this attribute of that element
                   (time[datetime]) and fall back to its text
    path_prefix    optional; keep only links whose path starts with this
    date_separator optional; the date shares a text node with something else
                   ("Date · Category", "Category | Date") -- split on the
                   first occurrence of this
    date_part      optional; "before" (default) or "after" the separator.
                   Text without the separator is the date either way.

Spec (type rss_feed):
    feed           which config key holds the feed URL: "rss_url" or "url"
    categories     optional whitelist of <category> values, case-insensitive
    path_prefix    optional; keep only links whose path starts with this
    sort           optional; "date_desc" to sort before the max_articles cut,
                   for feeds that are not in date order
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

SUPPORTED_TYPES = ("card_list", "rss_feed")
REQUIRED = {"card_list": ("fetch", "card"), "rss_feed": ("feed",)}
FETCH_MODES = ("requests", "playwright")
DATE_PARTS = ("before", "after")
FEED_KEYS = ("rss_url", "url")
SORTS = ("date_desc",)


def validate_spec(spec: dict) -> None:
    """Raise ValueError if the spec cannot drive a fetch."""
    kind = (spec or {}).get("type")
    if kind not in SUPPORTED_TYPES:
        raise ValueError(f"listing_template type {kind!r} is not one of {SUPPORTED_TYPES}")
    for key in REQUIRED[kind]:
        if not spec.get(key):
            raise ValueError(f"listing_template for type {kind!r} needs {key!r}")
    if kind == "card_list" and spec["fetch"] not in FETCH_MODES:
        raise ValueError(f"listing_template fetch {spec['fetch']!r} is not one of {FETCH_MODES}")
    if kind == "rss_feed" and spec["feed"] not in FEED_KEYS:
        raise ValueError(f"listing_template feed {spec['feed']!r} is not one of {FEED_KEYS}; "
                         "it names the config key holding the feed URL, not the URL itself")
    sort = spec.get("sort")
    if sort is not None and sort not in SORTS:
        raise ValueError(f"listing_template sort {sort!r} is not one of {SORTS}")
    part = spec.get("date_part")
    if part is not None and part not in DATE_PARTS:
        raise ValueError(f"listing_template date_part {part!r} is not one of {DATE_PARTS}")
    if part is not None and not spec.get("date_separator"):
        raise ValueError("listing_template date_part needs a date_separator")


def _split_date(text: str, spec: dict) -> str:
    """Pull the date out of a text node it shares with a category."""
    sep = spec.get("date_separator")
    if not sep or sep not in text:
        return text
    before, after = text.split(sep, 1)
    return (after if spec.get("date_part") == "after" else before).strip()


def _text(el) -> str:
    """Text with a space between inline tags, not fused.

    get_text(strip=True) welds "for" and "Infrastructure" into
    "forInfrastructure" when a site wraps part of a headline in a <span> --
    the same defect c149894 fixed in four content fetchers, reproduced here
    and caught by the A/B run against northleaf-capital before this template
    was switched on.
    """
    if not el:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ")).strip()


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
        href = (link_el.get("href", "") if link_el else "").strip()
        # An anchor that goes nowhere is not an article: oaktree leaves
        # href="#" on cards whose real target is in a data-link attribute, and
        # urljoin turns that into the listing page itself, which then passes
        # the host check (found by the A/B run, 2026-09-25).
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        url = urljoin(source["url"], href)
        if expected_host and not _validate_hostname(url, expected_host):
            continue
        prefix = spec.get("path_prefix")
        if prefix and not urlparse(url).path.startswith(prefix):
            continue
        if url in seen:
            continue
        title = _text(card.select_one(spec["title"])) if spec.get("title") else _text(link_el)
        if not title:
            continue
        seen.add(url)
        date_raw = ""
        if spec.get("date"):
            date_el = card.select_one(spec["date"])
            if date_el is not None:
                attr = spec.get("date_attr")
                date_raw = (date_el.get(attr) or "").strip() if attr else ""
                date_raw = date_raw or _text(date_el)
                date_raw = _split_date(date_raw, spec)
        rows.append({"title": title, "url": url,
                     "date": parse_date(date_raw) if date_raw else None,
                     "date_raw": date_raw})
    return rows[:source.get("max_articles", 10)]


def _feed_date(raw: str) -> str | None:
    """RFC 2822 first, then the shared parser.

    Feeds in this set use RFC 2822 ("Mon, 21 Sep 2026 10:00:00 +0000") except
    amundi, which emits ISO ("2026-09-21T12:34:59+0200"). parse_date already
    reads ISO, so there is no separate fromisoformat step -- one was written
    and removed the same day: no test could tell it apart from parse_date,
    which is what a dead branch looks like.
    """
    from fetch_articles import parse_date

    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return parse_date(raw)


def parse_feed(xml_text: str, source: dict, spec: dict) -> list[dict]:
    """Rows from an RSS 2.0 feed."""
    from fetch_articles import _validate_hostname

    root = ET.fromstring(xml_text.lstrip("\ufeff"))
    wanted = {c.lower() for c in spec.get("categories") or ()}
    prefix = spec.get("path_prefix")
    expected_host = source.get("expected_hostname")

    rows: list[dict] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        if wanted:
            cats = {(c.text or "").strip().lower() for c in item.findall("category")}
            if not (cats & wanted):
                continue
        title = _feed_text(item, "title")
        url = _feed_text(item, "link")
        if not title or not url:
            continue
        if expected_host and not _validate_hostname(url, expected_host):
            continue
        if prefix and not urlparse(url).path.startswith(prefix):
            continue
        if url in seen:
            continue
        seen.add(url)
        date_raw = _feed_text(item, "pubDate", unescape=False)
        rows.append({"title": title, "url": url, "date": _feed_date(date_raw),
                     "date_raw": date_raw})
    if spec.get("sort") == "date_desc":
        # "" sorts below any real date, so undated rows land at the end.
        rows.sort(key=lambda r: r["date"] or "", reverse=True)
    return rows[:source.get("max_articles", 10)]


def _feed_text(item, tag: str, unescape: bool = True) -> str:
    """Text of a child element, unescaped.

    Some feeds double-escape: ARK's arrives as "ARK&apos;s" after XML parsing
    and was stored and displayed that way.
    """
    el = item.find(tag)
    text = (el.text or "").strip() if el is not None else ""
    return html.unescape(text) if unescape else text


def _playwright_html(url: str, wait_selector: str | None = None, wait_ms: int = 5000,
                     wait_until: str = "networkidle") -> str:
    from fetch_articles import _get_playwright_page

    return _get_playwright_page(url, wait_selector=wait_selector, wait_ms=wait_ms,
                                wait_until=wait_until)


def fetch(source: dict) -> list[dict]:
    """Run the source's listing_template and return rows."""
    from fetch_articles import HEADERS

    spec = source.get("listing_template") or {}
    validate_spec(spec)
    if spec["type"] == "rss_feed":
        feed_url = source.get(spec["feed"]) or source["url"]
        resp = requests.get(feed_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return parse_feed(resp.text, source, spec)
    if spec["fetch"] == "playwright":
        html = _playwright_html(source["url"], wait_selector=spec.get("wait_selector"),
                                wait_ms=spec.get("wait_ms", 5000),
                                wait_until=spec.get("wait_until", "networkidle"))
    else:
        resp = requests.get(source["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
    return parse_cards(html, source, spec)
