"""One taxonomy for why an article has no body (stage 2) or no summary (stage 3).

Until 2026-09-15 every content failure was the same "failed"/"permafail"
status and every stage-3 decline a free-text reason. A video page, a deleted
page, a dead selector and a Cloudflare block looked identical, so nothing
could be counted, retried selectively, or learned from. Each label here names
a cause seen while fixing sources that week; docs/content-failure-casebook.md
records the cases behind them.

Classification is evidence-based: the HTTP responses the fetch made, which
extraction path _normalize_html took, and the fetcher's own log messages.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

CONTENT_FAILURE_LABELS = {
    "blocked_by_bot_protection": "网站防护拦截（403 / Cloudflare 验证页）",
    "access_denied": "站方拒绝这一篇（撤下、需登录或地区限制）——返回的是网站自己的拒绝页，不是防护验证页",
    "page_gone": "页面已删除（404/410）或跳转到首页、栏目列表页",
    "media_without_text": "视频或播客页，只有一段简介，没有可读正文",
    "selector_miss": "抓取规则未匹配，只能退回通用容器或整页（网站改版的典型信号）",
    "body_too_short": "抓取规则匹配到了，但正文太短",
    "body_rendered_client_side": "正文由浏览器脚本渲染，网页源码里没有（且没有可用的替代简介）",
    "pdf_not_usable": "PDF 读不出来，或不是这篇文章",
    "fetch_error": "网络错误、超时、服务器 5xx 或程序异常",
}

# How a content failure is retried, by label.
#   backoff_days: wait before the Nth retry (the last value repeats)
#   max_attempts: retire to permafail at this many attempts
#   retire_streak: retire earlier after this many consecutive same-label failures
#   code_dependent: a permafail is retried once when fetch_content.py changes
RETRY_POLICY = {
    "fetch_error":               {"backoff_days": [1, 2, 4, 8], "max_attempts": 5, "code_dependent": True},
    "blocked_by_bot_protection": {"backoff_days": [7], "max_attempts": 4, "code_dependent": False},
    "access_denied":              {"backoff_days": [1], "max_attempts": 5, "retire_streak": 2,
                                  "code_dependent": False},
    "page_gone":                 {"backoff_days": [1], "max_attempts": 5, "retire_streak": 2,
                                  "code_dependent": False},
    "media_without_text":        {"backoff_days": [1], "max_attempts": 5, "retire_streak": 2,
                                  "code_dependent": False},
    "selector_miss":             {"backoff_days": [1], "max_attempts": 3, "code_dependent": True},
    "body_too_short":            {"backoff_days": [1], "max_attempts": 3, "code_dependent": True},
    "body_rendered_client_side": {"backoff_days": [1], "max_attempts": 3, "code_dependent": True},
    "pdf_not_usable":            {"backoff_days": [1], "max_attempts": 3, "code_dependent": True},
}

ANALYSIS_DECLINE_LABELS = {
    "title_only": "只有标题或列表元数据（至多一句出版方简介），没有可总结的正文",
    "duplicate_body": "正文与另一篇已摘要文章完全相同",
    "grounding_failed": "摘要没通过防编造核对",
    "wrong_document": "存下的是另一份文件（税表、监管披露、别的报告）",
    "chart_notes_only": "只有图表数据来源注释",
    "navigation_or_login_wall": "只有网站导航、元数据或登录墙",
    "disclaimer_only": "只有栏目介绍、免责声明或风险披露",
    "teaser_only": "只有预告、节目简介或推广链接",
    "other_declined": "其他原因",
}

_CHALLENGE = re.compile(
    r"<title>\s*Just a moment\.\.\.|Enable JavaScript and cookies to continue|"
    r"Attention Required! \| Cloudflare|cf-browser-verification|challenge-platform",
    re.IGNORECASE)
_MEDIA = re.compile(
    r"data-video=|<video[\s>-]|video-js|players\.brightcove|brightcove-player|vimeo\.com/video|"
    r"youtube\.com/embed|wistia|vidyard|podcast-player|<audio[\s>]|buzzsprout|simplecast|megaphone\.fm|"
    r"brightcoveVideoId|libsyn\.com/embed|spotify\.com/embed|podbean\.com/player|soundcloud\.com/player",
    re.IGNORECASE)
_ERROR_MESSAGE = re.compile(r"timeout|timed out|fetch failed|failed to fetch|connection|"
                            r"download failed|failed to download|all attempts failed|"
                            r"http error|ssl|refused|reset by peer", re.IGNORECASE)
# A PDF was reached but is not usable as one: served as HTML, or unparseable.
_PDF_MESSAGE = re.compile(r"invalid PDF response|PDF extraction failed|pdfplumber extraction failed|"
                          r"PDF article: extraction failed", re.IGNORECASE)


# A refusal that carries this many links came from the site's own template.
SITE_PAGE_MIN_LINKS = 20
_REFUSAL_STATUSES = (401, 403, 451)


def count_links(html: str) -> int:
    return (html or "").count("<a ")


def looks_like_challenge(html: str) -> bool:
    return bool(_CHALLENGE.search(html or ""))


def has_media_player(html: str) -> bool:
    return bool(_MEDIA.search(html or ""))


def _redirected_away(url: str, final_url: str) -> bool:
    """A redirect to a site root or index page rather than to an article.

    Same host: the final path is an ancestor of the requested one
    (metlife .../insights/investment-perspectives/x -> .../insights/).
    Another host: the final path is shallow (at most two segments) and
    shallower than the request -- a retired domain sending everything to the
    new site's home (pinebridge.com/en/insights/x -> metlife.com/investments/en-us/).
    A move to the same article on a new domain (researchaffiliates.com ->
    syzygyassetmanagement.com/.../articles/1122-...) keeps a deep path and is
    not counted.
    """
    if not final_url or final_url.rstrip("/") == (url or "").rstrip("/"):
        return False
    a, b = urlsplit(url), urlsplit(final_url)
    req = [p for p in a.path.lower().split("/") if p]
    got = [p for p in b.path.lower().split("/") if p]
    if a.netloc and b.netloc and a.netloc.lower() != b.netloc.lower():
        return len(got) <= 2 and len(got) < len(req)
    return len(got) < len(req) and req[:len(got)] == got


def classify_content_failure(evidence: dict) -> tuple[str, str]:
    """(label, detail) for one failed content fetch.

    evidence: {"exception": str|None, "messages": [log text], "responses":
    [{"status","url","final_url","content_type","challenge","media_player"}],
    "extraction_paths": [...], "hints": [(label, detail)]}. Order matters:
    an explicit hint from the fetcher, then what the server said, then what
    the extraction saw.
    """
    for label, detail in evidence.get("hints") or []:
        if label in CONTENT_FAILURE_LABELS:
            return label, detail

    responses = [r for r in evidence.get("responses") or []
                 if "html" in (r.get("content_type") or "html")]
    # The failing response is the evidence. GMO and Oaktree fetch the article
    # page (200) and then its PDF; judging responses[0] filed a 404 on the PDF
    # as body_too_short.
    failing = [r for r in responses if int(r.get("status") or 0) >= 400]
    page = failing[-1] if failing else (responses[0] if responses else None)
    messages = evidence.get("messages") or []
    last_message = messages[-1] if messages else ""
    # Every captured message, not only the last: T. Rowe Price logs two
    # Playwright timeouts and then "all attempts failed, giving up", and the
    # summary line hid the two that said why.
    error_message = next((m for m in reversed(messages) if _ERROR_MESSAGE.search(m)), "")
    pdf_message = next((m for m in reversed(messages) if _PDF_MESSAGE.search(m)), "")

    if page is not None:
        status = int(page.get("status") or 0)
        if not page.get("challenge") and status in _REFUSAL_STATUSES \
                and (page.get("links") or 0) >= SITE_PAGE_MIN_LINKS:
            # The site's own refusal page, rendered with its navigation: the
            # CMS answered, so this document is withdrawn, gated or
            # region-locked. A block looks different -- the ARK Cloudflare
            # page is 5,897 bytes with 0 links; blue-owl's 403 is 123KB with
            # 88 (measured 2026-09-16). 429 is us asking too fast, so it is
            # not here.
            return "access_denied", f"HTTP {status} at {page.get('url')} (site's own page)"
        if page.get("challenge") or status in (403, 429):
            return "blocked_by_bot_protection", f"HTTP {status} at {page.get('url')}"
        if status in (404, 410):
            return "page_gone", f"HTTP {status} at {page.get('url')}"
        if _redirected_away(page.get("url", ""), page.get("final_url", "")):
            return "page_gone", f"redirected to {page.get('final_url')}"
        if status >= 500:
            return "fetch_error", f"HTTP {status} at {page.get('url')}"

    if evidence.get("exception"):
        return "fetch_error", str(evidence["exception"])[:300]

    paths = evidence.get("extraction_paths") or []
    if any(p != "primary" for p in paths):
        return "selector_miss", f"extraction took {', '.join(sorted(set(p for p in paths if p != 'primary')))}"

    if page is None and error_message:
        return "fetch_error", error_message[:300]

    if page is not None and page.get("media_player"):
        return "media_without_text", last_message[:300] or "media player, no article text"

    if error_message:
        return "fetch_error", error_message[:300]

    if pdf_message:
        return "pdf_not_usable", pdf_message[:300]

    return "body_too_short", last_message[:300] or "no usable text"


_DECLINE_RULES = (
    ("title_only", r"only a title|only article metadata"),
    ("duplicate_body", r"same text as the already-summarised"),
    ("grounding_failed", r"failed grounding check"),
    ("wrong_document", r"tax document|relationship summary|different, earlier paper|not the stated|"
                       r"different document|not the article itself|not the [a-z ]*article"),
    ("chart_notes_only", r"chart source notes|source notes"),
    ("navigation_or_login_wall", r"navigation|login wall|registration"),
    ("disclaimer_only", r"disclaimer|disclosure|legal"),
    ("teaser_only", r"teaser|video description|podcast description|promotional|scheduling|"
                    r"listing|preview|introductory paragraph|brief description"),
)


def classify_analysis_decline(reason: str) -> str:
    text = (reason or "").lower()
    for label, pattern in _DECLINE_RULES:
        if re.search(pattern, text):
            return label
    return "other_declined"
