"""MSCI gated research papers: the form-fill fallback (2026-10-24).

fetch_content._fetch_content_msci first reads a gated paper's PDF straight
from /downloads/. For some papers that path answers 307 back to the article
page unless the visitor has unlocked research papers; for those this module
gets the PDF the way a reader does:

1. With the saved browser state of an earlier unlock (cookie
   "research-papers", which the form button itself declares), request the
   PDF directly. On 10-24 one form submission unlocked other papers too.
2. Otherwise open the page in a headless browser, follow "Unlock for free",
   fill the form with the details in FORM_CONFIG, submit, take the "View PDF"
   link of the success page, download it in the same browser context and
   save the state for step 1 next time.

Decided with the user on 10-24 after a manual submission in their name. The
details live in a gitignored local file (FORM_CONFIG), never in the repo;
without it the fallback is skipped. MSCI's Akamai check runs in the browser,
so plain requests cannot submit the form.

Every failure (no config, no form, a captcha frame, a validation message, no
View PDF link, a link to another paper, a non-PDF) returns None with a
warning; the caller then stores nothing and the paper is retried. Form
submissions are spaced at least MIN_SUBMIT_INTERVAL seconds apart within a
process, and a failed form attempt pauses the form for FORM_PAUSE_HOURS
across processes (a marker next to the saved state): stage 2, the nightly
health probe and the weekly audit would otherwise resubmit in the reader's
name every night while MSCI refuses it (review M1, 2026-10-24). The saved
unlock is still tried during a pause; it submits nothing.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
FORM_CONFIG = BASE_DIR / "config" / "msci_form.local.json"
SESSION_STATE = BASE_DIR / "data" / "msci-session-state.json"
FORM_FIELDS = ("firstName", "lastName", "email", "Location", "State", "company", "Segment", "Interest")
MIN_SUBMIT_INTERVAL = 30.0
FORM_PAUSE_NAME = "msci-form-pause.json"   # next to the saved state
FORM_PAUSE_HOURS = 72
# Playwright's default is 30 s per step; the health check has 600 s in all and
# uses ~300 (review M3). Measured 10-24, a whole successful fill took ~26 s.
FORM_STEP_TIMEOUT_MS = 15000
PAGE_LOAD_TIMEOUT_MS = 30000
SUCCESS_TEXT = "Access your content below"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

_last_submit = 0.0


def load_form_config(path: Optional[Path] = None) -> Optional[dict]:
    """The reader's details, or None when the local file is missing or incomplete."""
    path = path or FORM_CONFIG
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("  MSCI gate: %s not found; form fallback skipped", path.name)
        return None
    except Exception as e:
        log.warning("  MSCI gate: cannot read %s: %s; form fallback skipped", path.name, e)
        return None
    missing = [f for f in FORM_FIELDS if not str(cfg.get(f) or "").strip()]
    if missing:
        log.warning("  MSCI gate: %s lacks %s; form fallback skipped", path.name, ", ".join(missing))
        return None
    return cfg


def _is_pdf(resp) -> bool:
    return (resp.status == 200 and "application/pdf" in (resp.headers.get("content-type") or "")
            and len(resp.body()) > 1024)


def _same_pdf(link: str, pdf_url: str) -> bool:
    # unquote: pdfDamPath keeps raw spaces ("Too Hot to Work - FINAL.pdf"), an href has %20
    return unquote(urlsplit(link).path).rstrip("/") == unquote(urlsplit(pdf_url).path).rstrip("/")


def _usable_state(state_path: Path) -> Optional[Path]:
    """The saved unlock, if there is one Playwright can load."""
    try:
        if isinstance(json.loads(state_path.read_text(encoding="utf-8")), dict):
            return state_path
    except FileNotFoundError:
        return None
    except Exception:
        pass
    log.warning("  MSCI gate: saved state %s is unreadable; ignored", state_path.name)
    return None


def _paused_until(marker: Path) -> Optional[str]:
    """When the form pause ends, while one is in force."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if datetime.fromisoformat(data["until"]) > datetime.now(timezone.utc):
            return f"{data['until']} ({data.get('reason', '')})"
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("  MSCI gate: pause marker %s unreadable (%s); ignored", marker.name, e)
    return None


def _pause_form(marker: Path, reason: str) -> None:
    now = datetime.now(timezone.utc)
    data = {"since": now.isoformat(timespec="seconds"),
            "until": (now + timedelta(hours=FORM_PAUSE_HOURS)).isoformat(timespec="seconds"),
            "reason": reason[:300]}
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        tmp = marker.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, marker)
        log.warning("  MSCI gate: form paused until %s", data["until"])
    except Exception as e:
        log.warning("  MSCI gate: cannot write pause marker %s: %s", marker, e)


def _with_saved_unlock(p, state: Path, pdf_url: str) -> Optional[bytes]:
    """The PDF requested with the saved unlock, or None; never raises (review M2)."""
    try:
        req = p.request.new_context(storage_state=str(state), user_agent=USER_AGENT)
        try:
            resp = req.get(pdf_url, max_redirects=0, timeout=60000)
            if _is_pdf(resp):
                log.info("  MSCI gate: PDF read with the saved unlock")
                return resp.body()
        finally:
            req.dispose()
    except Exception as e:
        log.warning("  MSCI gate: saved unlock request failed: %s", e)
    return None


def _wait_for_submit_slot() -> None:
    global _last_submit
    wait = _last_submit + MIN_SUBMIT_INTERVAL - time.monotonic()
    if _last_submit and wait > 0:
        log.info("  MSCI gate: waiting %.0fs before the next form submission", wait)
        time.sleep(wait)
    _last_submit = time.monotonic()


def _pick(page, label: str, option: str, typed: Optional[str] = None) -> None:
    box = page.get_by_label(label, exact=False).first
    box.click(force=True)
    page.wait_for_timeout(800)
    if typed:
        box.fill(typed)
        page.wait_for_timeout(800)
    opts = page.locator("[role=option]")
    exact = [i for i, t in enumerate(opts.all_inner_texts()) if t.strip() == option]
    if not exact:
        raise RuntimeError(f"no {label} option {option!r}")
    opts.nth(exact[0]).click()
    page.wait_for_timeout(1200)


def _fill_and_submit(page, cfg: dict) -> Optional[str]:
    """Fill the open form and submit; the View PDF link, or None."""
    try:
        page.locator("#onetrust-accept-btn-handler").click(timeout=5000)
    except Exception:
        pass
    for name in ("firstName", "lastName", "email", "company"):
        page.locator(f"input[name={name}]").fill(str(cfg[name]))
    _pick(page, "Location", cfg["Location"], typed=cfg["Location"])
    _pick(page, "State", cfg["State"])
    _pick(page, "Segment", cfg["Segment"])
    _pick(page, "Interest", cfg["Interest"])
    page.locator("input[name=privacy]").check(force=True)
    if any("captcha" in f.url.lower() for f in page.frames):
        log.warning("  MSCI gate: captcha on the form; not submitted")
        return None
    _wait_for_submit_slot()
    page.locator("form button[type=submit]:has-text('Submit')").click()
    try:
        page.get_by_text(SUCCESS_TEXT).first.wait_for(timeout=30000)
    except Exception:
        body = page.evaluate("() => document.body.innerText")
        reason = next((ln for ln in body.splitlines()
                       if "required" in ln or "not supported" in ln or "invalid" in ln.lower()), "")
        log.warning("  MSCI gate: form not accepted%s", f" ({reason.strip()})" if reason else "")
        return None
    link = page.locator("a:has-text('View PDF')").first
    return link.get_attribute("href") if link.count() else None


def _open_form_and_submit(page, page_url: str, cfg: dict) -> Optional[str]:
    """From the article page to the submitted form; the View PDF link, or None."""
    page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    page.wait_for_timeout(4000)
    unlock = page.locator("a:has-text('Unlock for free')").first
    href = unlock.get_attribute("href") if unlock.count() else None
    if not href:
        log.warning("  MSCI gate: no Unlock link on %s", page_url)
        return None
    page.goto(urljoin(page_url, href), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
    page.wait_for_timeout(8000)
    return _fill_and_submit(page, cfg)


def _unlock_with_form(p, page_url: str, pdf_url: str, cfg: dict, state: Optional[Path],
                      state_path: Path) -> tuple[Optional[bytes], str]:
    """(the PDF, "") after filling the form, or (None, why)."""
    browser = p.chromium.launch(headless=True)
    try:
        ctx = browser.new_context(user_agent=USER_AGENT, accept_downloads=True,
                                  storage_state=str(state) if state else None)
        ctx.set_default_timeout(FORM_STEP_TIMEOUT_MS)
        link = _open_form_and_submit(ctx.new_page(), page_url, cfg)
        if not link:
            return None, "no View PDF link after the form"
        if not _same_pdf(urljoin(page_url, link), pdf_url):
            return None, f"View PDF {link} is not {pdf_url}"
        resp = ctx.request.get(urljoin(page_url, link), timeout=60000)
        if not _is_pdf(resp):
            return None, f"View PDF answered {resp.status} {resp.headers.get('content-type')}"
        body = resp.body()
        # The form has been submitted and the PDF is in hand: a failed save
        # costs only the next unlock, so it must not cost this PDF (review 10-25).
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            ctx.storage_state(path=str(state_path))
            log.info("  MSCI gate: PDF read after the form; unlock saved")
        except Exception as e:
            log.warning("  MSCI gate: PDF read after the form; unlock not saved (%s: %s)",
                        type(e).__name__, e)
        return body, ""
    finally:
        browser.close()


def fetch_gated_pdf(page_url: str, pdf_url: str, *, config_path: Optional[Path] = None,
                    state_path: Optional[Path] = None) -> Optional[bytes]:
    """The PDF behind an MSCI gated paper via a reader's unlock, or None."""
    cfg = load_form_config(config_path)
    if cfg is None:
        return None
    state_path = state_path or SESSION_STATE
    marker = state_path.parent / FORM_PAUSE_NAME
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            state = _usable_state(state_path)
            if state:
                body = _with_saved_unlock(p, state, pdf_url)
                if body is not None:
                    return body
            paused = _paused_until(marker)
            if paused:
                log.warning("  MSCI gate: form paused until %s; not filled", paused)
                return None
            body, why = _unlock_with_form(p, page_url, pdf_url, cfg, state, state_path)
    except Exception as e:
        body, why = None, f"fallback failed: {type(e).__name__}: {e}"
    if body is None:
        log.warning("  MSCI gate: %s (%s)", why, page_url)
        _pause_form(marker, why)
        return None
    marker.unlink(missing_ok=True)
    return body
