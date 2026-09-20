#!/usr/bin/env python3
"""Navigating with a wait strategy that may never be satisfied.

Playwright's wait_until="networkidle" needs 500ms of network silence, and a
site with always-on analytics never gives it: the page is rendered seconds in,
and the fetch fails at the timeout anyway. Eight fetchers in this repo carry a
comment about being moved off networkidle for exactly that -- BlackRock,
Capital Group, PIMCO, T. Rowe Price, MSCI, Wellington, Franklin, and
cambridge-associates, whose single timeout produced the first false alarm in
the weekly audit email on 2026-09-20.

Eleven listing fetchers and three content fetchers still use networkidle.
Flipping all fourteen would mean changing fourteen sources on the evidence of
one, and each needs its own proof that the text comes out unchanged. So this
degrades instead: a timeout is retried once with domcontentloaded, which makes
a never-settling site slow rather than broken, and the WARNING it logs names
the sources worth migrating properly -- with the date it happened.

Only a timeout falls back. A DNS failure or a refused connection is not fixed
by waiting differently.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

FALLBACK_WAIT = "domcontentloaded"


def _is_timeout(exc: Exception) -> bool:
    return type(exc).__name__ == "TimeoutError" or "timeout" in str(exc).lower()


def goto_with_fallback(page, url: str, wait_until: str = "networkidle",
                       timeout: int = 30000) -> str:
    """page.goto(url), falling back to domcontentloaded on a timeout.

    Returns the wait strategy that actually loaded the page, so a caller can
    record which one it got.
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
        return wait_until
    except Exception as exc:
        if wait_until == FALLBACK_WAIT or not _is_timeout(exc):
            raise
        log.warning("PLAYWRIGHT WAIT FALLBACK: %s did not reach %s in %dms; reloading with %s. "
                    "This source is a candidate for being moved off %s for good.",
                    url, wait_until, timeout, FALLBACK_WAIT, wait_until)
    page.goto(url, wait_until=FALLBACK_WAIT, timeout=timeout)
    return FALLBACK_WAIT
