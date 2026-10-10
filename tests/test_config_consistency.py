"""Config sanity checks — catch misclassifications and URL drift at test time.

Classes of bugs these guard against:

1. **Frequency vs observed cadence mismatch** (Gap 1)
   The 2026-05-11 cambridge-associates + brookfield bug: both declared "weekly"
   but actually publish monthly. The runtime staleness check eventually flagged
   them via fetcher-health WARN, but only after they'd been silently sitting in
   prod with wrong thresholds. A test that compares declared frequency against
   the historical median gap from articles.jsonl catches this at config time.

2. **Candidate URL invariants** (Gap 3)
   The 2026-05-08 ares-management bug: status flipped to "visitable" but the
   research_url was 404. A static lint test cannot catch live 404s (that's the
   live URL probe extension to gmia-fetcher-health.py), but it CAN catch
   missing/malformed URLs and host-vs-official-domain drift, which are also
   common manual-edit mistakes.

3. **Fund profile coverage + pending profile validity** (Gap 4)
   The 2026-05-12 Janus Henderson manual-wire path: auto-promote agent correctly
   deferred (no fetcher in FETCHERS yet), so the Phase 5 validator never ran
   on a pending_profile because there was none — the human wrote sources.json,
   the fetcher, and _FUND_PROFILES directly in one pass. That worked this time,
   but a future manual wire that adds sources.json + fetcher and forgets
   the profile would silently render the Sources-tab card empty. These tests
   enforce: every source id has a fund profile somewhere (production
   _FUND_PROFILES or pending_profiles/<id>.json), and every pending profile
   passes the validator's hard checks.
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import pytest
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO / "config" / "sources.json"
CANDIDATES_FILE = REPO / "config" / "fund_candidates.json"
ARTICLES_FILE = REPO / "data" / "articles.jsonl"
PENDING_DIR = REPO / "pending_profiles"
SCRIPTS_DIR = REPO / "scripts"

# Mirror gmia-fetcher-health.py's FREQ_TO_STALE_DAYS — keep in sync if it changes.
FREQ_TO_STALE_DAYS = {
    "daily": 14,
    "weekly": 30,
    "biweekly": 45,
    "monthly": 90,
    "quarterly": 240,
    "annual": 540,
    "yearly": 540,
}

# Minimum articles a source needs before we trust its observed cadence
MIN_SAMPLES_FOR_CADENCE_CHECK = 5

# Some sites (e.g. troweprice, kkr) only expose month/year precision on most
# articles ("Jun 2026"). Measuring day-gaps between such dates manufactures
# fake 14-60 day gaps purely from calendar-boundary rounding, not real
# slowdowns — detect this and fall back to month-granularity gap math.
MONTH_ONLY_DATE_RE = re.compile(r"^[A-Za-z]+\.?\s+\d{4}$")
MONTH_ONLY_FRACTION_THRESHOLD = 0.8


def _load_articles_by_source() -> dict[str, list]:
    """Return {source_id: [(parsed_date, date_raw), ...]} from articles.jsonl."""
    by_source: dict[str, list] = defaultdict(list)
    if not ARTICLES_FILE.exists():
        return by_source
    with open(ARTICLES_FILE) as f:
        for line in f:
            try:
                a = json.loads(line)
            except Exception:
                continue
            sid = a.get("source_id")
            date_raw = a.get("date_raw")
            raw = a.get("date") or date_raw
            if not sid or not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            except Exception:
                continue
            by_source[sid].append((parsed, date_raw))
    return by_source


def _median_gap_days(dates: list) -> float | None:
    """Median day-gap between consecutive unique dates (descending order)."""
    unique = sorted(set(dates), reverse=True)
    if len(unique) < 2:
        return None
    gaps = [(unique[i] - unique[i + 1]).days for i in range(len(unique) - 1)]
    return statistics.median(gaps)


def _is_month_only_precision(date_raws: list) -> bool:
    """True if most date_raw strings carry only month/year precision.

    Below this fraction, a few coarse dates shouldn't distort an otherwise
    day-precise source's gap measurement.
    """
    raws = [r for r in date_raws if r]
    if not raws:
        return False
    month_only = sum(1 for r in raws if MONTH_ONLY_DATE_RE.match(r.strip()))
    return month_only / len(raws) >= MONTH_ONLY_FRACTION_THRESHOLD


def _median_gap_months(dates: list) -> float | None:
    """Median gap, in whole calendar months, between consecutive (year, month)
    buckets present in `dates` (descending order)."""
    months = sorted({(d.year, d.month) for d in dates}, reverse=True)
    if len(months) < 2:
        return None
    gaps = [
        (months[i][0] - months[i + 1][0]) * 12 + (months[i][1] - months[i + 1][1])
        for i in range(len(months) - 1)
    ]
    return statistics.median(gaps)


def test_is_month_only_precision_detects_month_year_strings():
    assert _is_month_only_precision(["Jun 2026", "May 2026", "July 2026"]) is True


def test_is_month_only_precision_false_for_full_dates():
    assert _is_month_only_precision(["April 17, 2026", "March 3, 2026"]) is False


def test_is_month_only_precision_false_for_empty():
    assert _is_month_only_precision([]) is False


def test_is_month_only_precision_majority_rule():
    mostly_month_only = ["Jun 2026", "May 2026", "Apr 2026", "Mar 2026", "April 17, 2026"]
    assert _is_month_only_precision(mostly_month_only) is True
    mostly_precise = ["April 17, 2026", "March 3, 2026", "May 2026"]
    assert _is_month_only_precision(mostly_precise) is False


def test_median_gap_months_computes_calendar_month_distance():
    dates = [date(2026, 3, 1), date(2026, 4, 1), date(2026, 6, 30), date(2026, 5, 1)]
    assert _median_gap_months(dates) == 1


def test_median_gap_months_none_when_insufficient_data():
    assert _median_gap_months([date(2026, 3, 1)]) is None


# ───────────────────────── Gap 1: cadence vs declared frequency ──────────────


def test_declared_frequency_matches_observed_cadence():
    """Each production source's declared frequency should be consistent with
    the median gap between its real articles.

    Rule: median_gap_days must not exceed the staleness threshold for the
    declared frequency. If it does, the source is being throttled to a slower
    cadence than its label admits — either the publisher slowed down (reclassify)
    or the label was wrong from day one.

    This is INTENTIONALLY one-sided: too-fast misclassifications (declared
    monthly, actually weekly) only cause overly-conservative WARN thresholds —
    not a problem. Too-slow misclassifications cause false-positive WARNs.
    """
    sources = json.loads(SOURCES_FILE.read_text())["sources"]
    by_source = _load_articles_by_source()

    failures = []
    for s in sources:
        sid = s["id"]
        freq = (s.get("frequency") or "").strip().lower()
        threshold = FREQ_TO_STALE_DAYS.get(freq)
        if threshold is None:
            continue  # unknown frequency, skip (staleness check uses DEFAULT)
        records = by_source.get(sid, [])
        dates = [d for d, _ in records]
        if len(set(dates)) < MIN_SAMPLES_FOR_CADENCE_CHECK:
            continue  # not enough data to judge

        if _is_month_only_precision([r for _, r in records]):
            # Day-level gaps are meaningless at month precision — judge
            # staleness in whole calendar months instead.
            gap_months = _median_gap_months(dates)
            if gap_months is None:
                continue
            threshold_months = max(1, round(threshold / 30))
            if gap_months > threshold_months:
                failures.append(
                    f"{sid}: declared {freq} (tolerates {threshold_months}mo gap at "
                    f"month-only date precision) but median gap is "
                    f"{gap_months:.0f}mo — publisher may have gone stale"
                )
            continue

        gap = _median_gap_days(dates)
        if gap is None:
            continue
        if gap > threshold:
            # Suggest the slowest bucket that would fit
            suggested = next(
                (f for f, t in sorted(FREQ_TO_STALE_DAYS.items(), key=lambda x: x[1])
                 if t >= gap),
                "annual",
            )
            failures.append(
                f"{sid}: declared {freq} (threshold {threshold}d) but median "
                f"article gap is {gap:.0f}d — consider reclassifying to "
                f"{suggested!r}"
            )

    assert not failures, (
        "Frequency misclassifications found:\n  " + "\n  ".join(failures)
    )


# ───────────────────────── Gap 3: candidate URL invariants ───────────────────


def _candidate_records() -> list[dict]:
    return json.loads(CANDIDATES_FILE.read_text())


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip(".")


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def test_validated_candidates_have_research_url():
    """Every candidate marked validated must have a non-empty research_url.

    A validated candidate is one the pipeline thinks is safe to put into a
    trial. Missing URL means the trial will fail Day 1 with no useful signal.
    """
    missing = [
        c["id"]
        for c in _candidate_records()
        if c.get("status") == "visitable"
        and not (c.get("research_url") or "").strip()
    ]
    assert not missing, f"validated candidates with empty research_url: {missing}"


def test_validated_candidate_research_url_is_parseable():
    """research_url must parse to a valid http(s) URL with a hostname."""
    bad = []
    for c in _candidate_records():
        if c.get("status") != "visitable":
            continue
        url = c.get("research_url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            bad.append(f"{c['id']}: {url!r}")
    assert not bad, "malformed research_url:\n  " + "\n  ".join(bad)


def test_validated_candidate_url_host_matches_official_domain():
    """research_url's hostname must sit within the candidate's official_domain.

    Catches typos / wrong-fund URLs where someone pasted a competitor's link
    or an aggregator URL into a candidate record.
    """
    mismatches = []
    for c in _candidate_records():
        if c.get("status") != "visitable":
            continue
        url = c.get("research_url", "")
        official = (c.get("official_domain") or "").lower().strip()
        if not url or not official:
            continue
        host = _strip_www(_hostname(url))
        domain = _strip_www(official)
        if not host.endswith(domain):
            mismatches.append(
                f"{c['id']}: research_url host {host!r} not within "
                f"official_domain {domain!r}"
            )
    assert not mismatches, (
        "research_url / official_domain drift:\n  " + "\n  ".join(mismatches)
    )


# ───────── Gap 4: fund profile coverage + pending profile validity ───────────


def _production_profile_ids() -> set[str]:
    """Fund IDs that have a profile entry in publish._FUND_PROFILES."""
    import publish  # repo root is on sys.path via conftest
    return set(publish._FUND_PROFILES.keys())


def _pending_profile_ids() -> set[str]:
    """Fund IDs with a pending_profiles/<id>.json draft (auto-promote output)."""
    if not PENDING_DIR.exists():
        return set()
    return {
        p.stem for p in PENDING_DIR.glob("*.json")
        if not p.name.endswith(".validation.json")
    }


def test_every_source_has_fund_profile():
    """Each entry in sources.json must have a fund profile in either
    publish._FUND_PROFILES (production) or pending_profiles/<id>.json
    (auto-promote draft awaiting human merge).

    Without a profile in one of those two places, publish.py renders an empty
    Sources-tab card for that fund. The auto-promote agent writes a pending
    profile during Phase 5 (validated by scripts/validate_pending_profile.py).
    Manual wiring paths must either add to _FUND_PROFILES directly or drop a
    pending_profiles/<id>.json — this test enforces that invariant.
    """
    sources = json.loads(SOURCES_FILE.read_text())["sources"]
    in_production = _production_profile_ids()
    in_pending = _pending_profile_ids()

    missing = [
        s["id"] for s in sources
        if s["id"] not in in_production and s["id"] not in in_pending
    ]
    assert not missing, (
        "sources.json entries without a fund profile (need entry in "
        "publish._FUND_PROFILES or pending_profiles/<id>.json): "
        f"{missing}"
    )


def _new_profile_drafts(pending: Path) -> list[Path]:
    """Drafts of NEW funds (graduate_pending's input), the only full profiles here.

    <id>.refresh.json is a monthly change-log delta for an existing fund (only
    the fields that changed), and validate_profile reads it as a full profile
    with every field missing: one rejected refresh draft left behind failed this
    test -- and the monthly refresh's own test gate -- every month after
    (pre-merge review, 2026-10-10)."""
    return [p for p in sorted(pending.glob("*.json"))
            if not p.name.endswith((".validation.json", ".refresh.json"))]


def test_refresh_drafts_are_not_read_as_new_profiles(tmp_path):
    for name in ("acme.json", "acme.validation.json", "kkr.refresh.json"):
        (tmp_path / name).write_text("{}")
    assert [p.name for p in _new_profile_drafts(tmp_path)] == ["acme.json"]


def test_pending_profiles_pass_validator():
    """Every pending_profiles/<id>.json must pass the hard checks in
    scripts/validate_pending_profile.py.

    Phase 5 policy (auto-promote/program.md): exit 0 = clean, exit 1 with only
    high-risk uncertainty markers is acceptable (human will see the flag and
    decide), exit 1 with missing fields / AUM format / founded year /
    hq format / desc length issues means the profile is unsafe to merge into
    _FUND_PROFILES. This test enforces the latter — i.e., no hard violations
    are allowed to sit in pending_profiles/ unnoticed.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    from validate_pending_profile import validate_profile  # type: ignore

    failures = []
    for path in _new_profile_drafts(PENDING_DIR):
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            failures.append(f"{path.name}: malformed JSON — {exc}")
            continue

        result = validate_profile(data)
        hard_issues = [
            i for i in result["issues"]
            if not i.startswith("high-risk uncertainty markers")
        ]
        if hard_issues:
            failures.append(f"{path.name}: {hard_issues}")

    assert not failures, (
        "pending profiles with hard validation issues:\n  "
        + "\n  ".join(failures)
    )


def test_synthesis_inner_lock_differs_from_cron_wrapper_lock():
    """wrapper-fetcher-synthesis.sh must NOT flock the same file cron-wrapper.sh
    --lock uses for this job.

    The weekly cron runs `cron-wrapper.sh --name gmia-fetcher-synthesis --lock
    -- bash wrapper-fetcher-synthesis.sh`. cron-wrapper grabs an flock on
    /tmp/cron-locks/gmia-fetcher-synthesis.lock and holds it for the whole run.
    If the wrapper's own guard locks that identical path, the child can never
    acquire it and every weekly run bails with "Another instance is running"
    (regression fixed 2026-07-20 — silently killed the weekly synthesis for
    ~5 weeks). The inner guard must use a distinct file so it still serialises
    the trial-pass immediate trigger vs the weekly run without colliding with
    the parent's outer lock.
    """
    wrapper = (SCRIPTS_DIR / "wrapper-fetcher-synthesis.sh").read_text()
    m = re.search(r'^\s*LOCK_FILE="([^"]+)"', wrapper, re.MULTILINE)
    assert m, "LOCK_FILE not found in wrapper-fetcher-synthesis.sh"
    inner_lock = m.group(1)
    cron_wrapper_lock = "/tmp/cron-locks/gmia-fetcher-synthesis.lock"
    assert inner_lock != cron_wrapper_lock, (
        f"wrapper-fetcher-synthesis.sh inner lock ({inner_lock}) collides with "
        f"the cron-wrapper --lock path ({cron_wrapper_lock}); the weekly cron "
        "run will self-deadlock. Use a distinct inner lock file."
    )


# ───────── Gap 5: stored article URLs vs the source's declared hosts ─────────


def test_stored_article_hosts_are_declared():
    """Every stored article URL must sit on a host the source declares.

    The 2026-08-21 research-affiliates rename made this gap visible: the source
    moved to syzygyassetmanagement.com and nothing in the default test run
    noticed that 18 stored articles still pointed at the old host.
    ``test_no_cross_source_contamination`` looks like it covers this, but it is
    ``nightly``-marked and inspects a *live fetch*, not what is on disk — so it
    is deselected by default and never sees stored rows at all.

    A source legitimately accumulates more than one host over its life (a
    rename, an acquisition, a move to a newsletter). Those go in
    ``historical_hostnames`` in sources.json, which is exactly the list this
    test reads — so declaring one is a deliberate, reviewable act, while an
    undeclared host is a bug: either the fetcher drifted or a config edit was
    left half-done.

    Articles whose source_id is no longer in sources.json (demoted sources keep
    their rows in articles.jsonl) are skipped — there is nothing to compare to.
    """
    if not ARTICLES_FILE.exists():
        pytest.skip("data/articles.jsonl not present (gitignored)")

    sources = {s["id"]: s for s in json.loads(SOURCES_FILE.read_text())["sources"]}
    offenders: dict[tuple[str, str], int] = defaultdict(int)

    with open(ARTICLES_FILE) as f:
        for line in f:
            if not line.strip():
                continue
            try:
                a = json.loads(line)
            except Exception:
                continue
            src = sources.get(a.get("source_id"))
            if not src:
                continue
            declared = [src.get("expected_hostname", "")]
            declared += src.get("historical_hostnames", [])
            declared = [d for d in declared if d]
            if not declared:
                continue
            host = _hostname(a.get("url", ""))
            if not any(host == d or host.endswith("." + d) for d in declared):
                offenders[(a["source_id"], host)] += 1

    assert not offenders, (
        "stored articles on undeclared hosts (add to the source's "
        "historical_hostnames if the move is intentional):\n  "
        + "\n  ".join(
            f"{sid}: {n} article(s) on {host!r} (declared: "
            f"{[sources[sid].get('expected_hostname')] + sources[sid].get('historical_hostnames', [])})"
            for (sid, host), n in sorted(offenders.items(), key=lambda kv: -kv[1])
        )
    )


def test_every_source_declares_expected_hostname():
    """Every source in sources.json must declare a non-empty expected_hostname.

    ``tests/test_sanity.py`` already asserted this, but that module is
    ``pytest.mark.live`` and therefore deselected in the default run — the
    invariant was effectively unenforced. It became load-bearing on 2026-09-03
    when the fetchers stopped carrying a hardcoded fallback host
    (``source.get("expected_hostname", "www.example.com")`` → ``source[...]``):
    the fallback used to paper over a missing field with a host baked into the
    code, which is exactly what made the research-affiliates rename return 0
    articles silently. Reading the field directly fails loudly instead — but
    only if this test keeps a missing field from ever reaching production.
    """
    sources = json.loads(SOURCES_FILE.read_text())["sources"]
    missing = [s["id"] for s in sources if not (s.get("expected_hostname") or "").strip()]
    assert not missing, (
        f"sources without expected_hostname: {missing}. "
        "The fetchers read source['expected_hostname'] directly and will "
        "KeyError; add the field rather than restoring a default in code."
    )


# ── audit C1: content_path carries no information beyond the id ─────────────

def _path_disagreements(rows):
    """Rows whose content_path is not exactly the path its id implies."""
    return [(r.get("id"), r.get("content_path")) for r in rows
            if r.get("content_path")
            and str(r["content_path"]) != f"content/{r.get('id')}.txt"]


def test_the_checker_rejects_a_path_the_id_does_not_imply():
    """Test the check before trusting it against real data."""
    assert _path_disagreements([{"id": "abc", "content_path": "content/abc.txt"}]) == []
    assert _path_disagreements([{"id": "abc"}]) == []          # absent is not a disagreement
    assert _path_disagreements([{"id": "abc", "content_path": "content/other.txt"}])
    assert _path_disagreements([{"id": "abc", "content_path": "/tmp/abc.txt"}])


def test_stored_content_paths_are_derivable_from_the_id():
    """content_path must stay a restatement of the id, because its two
    readers disagree about whether to read it at all.

    Audit C1 measured this: all 1,548 rows that carry a content_path have it
    equal to content/<id>.txt, so today the field holds no information. That
    is not a reason to relax -- it is the reason the invariant is safe to
    rely on, and two consumers already do. Stage 3's _resolve_content_path
    honours the stored value; scripts/content_audit.py's audit_article ignores it
    and derives stored_dir/<id>.txt. The day a writer stores a different
    path, those two read different files and neither says so: the audit
    would compare last night's fetch against some other article's body.

    Ten rows say content_status ok and carry no path at all. That is
    harmless for the same reason -- both readers can derive it -- so an
    absent path is not a disagreement here.
    """
    if not ARTICLES_FILE.exists():
        pytest.skip("data/articles.jsonl not present (gitignored)")

    rows = [json.loads(line) for line in
            ARTICLES_FILE.read_text().splitlines() if line.strip()]
    bad = _path_disagreements(rows)
    assert not bad, f"{len(bad)} rows whose content_path is not content/<id>.txt: {bad[:5]}"


def _is_playwright_pkg(module: str) -> bool:
    return module == "playwright" or module.startswith("playwright.")


def test_playwright_is_only_imported_inside_functions():
    """conftest's _no_network stops unit tests launching a real browser by
    replacing playwright.sync_api.sync_playwright. That only works because
    every launch imports it inside the function, at call time; a module-level
    `from playwright.sync_api import sync_playwright` would hold the real
    object and slip past. Found necessary 2026-10-02, when a unit test turned
    out to have driven a real Chromium against example.com for three months.
    """
    import ast
    offenders = []
    for path in sorted(REPO.rglob("*.py")):
        rel = path.relative_to(REPO)
        if rel.parts[0] in {"tests", ".git", "docs"} or "site-packages" in rel.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in tree.body:
            # The package itself, not modules that merely share the prefix
            # (playwright_nav is this repo's own helper).
            if isinstance(node, ast.ImportFrom) and _is_playwright_pkg(node.module or ""):
                offenders.append(f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Import) and any(_is_playwright_pkg(a.name) for a in node.names):
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"module-level playwright import bypasses the test guard: {offenders}"


# ---------------------------------------------------------------------------
# The card's AUM (publish._FUND_PROFILES) and the AUM written into the English
# description (sources.json) are kept in step by the monthly refresh, which
# trusted the draft's word for the old value until the stage-4 audit (P3): a
# wrong or tilde-less `old` left the two disagreeing, or wrote "~~$800B". On
# 2026-10-09 all 25 descriptions that carry a figure agreed with their card.
# The check is written out here on purpose rather than imported from
# apply_refresh, so a mistake in that rule cannot also blind this test.
# ---------------------------------------------------------------------------

_MONEY = re.compile(r"([$€£¥])\s*(\d+(?:\.\d+)?)\s*([KMBT])\b", re.I)


def _figures(text: str) -> set:
    """Money figures WITH their currency: "~€700B" and "$700B" are not the same AUM."""
    return {(c, n, u.upper()) for c, n, u in _MONEY.findall(text)}


def test_card_aum_agrees_with_the_aum_in_the_description():
    import ast
    tree = ast.parse((REPO / "publish.py").read_text(encoding="utf-8"))
    node = next(n.value for n in tree.body
                if isinstance(n, (ast.Assign, ast.AnnAssign))
                and "_FUND_PROFILES" in ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target))
    profiles = ast.literal_eval(node)
    descriptions = {s["id"]: s.get("description", "")
                    for s in json.loads(SOURCES_FILE.read_text())["sources"]}
    bad, checked = [], 0
    for fid, profile in profiles.items():
        desc, aum = descriptions.get(fid, ""), profile["aum"]
        figures = _figures(desc)
        if not figures:
            continue
        checked += 1
        if aum in desc or figures & _figures(aum):
            continue
        bad.append(f"{fid}: card {aum!r} vs description {desc[:90]!r}")
    assert checked >= 20, f"only {checked} descriptions carry a figure — did the format change?"
    assert not bad, "card AUM and description AUM disagree:\n" + "\n".join(bad)


def test_no_doubled_approximation_mark():
    """'~~$800B' is what a tilde-less draft `old` produced (stage-4 audit P3);
    the agreement check above reads it as fine, since '~$800B' is inside it."""
    text = (REPO / "publish.py").read_text(encoding="utf-8") + SOURCES_FILE.read_text()
    assert "~~" not in text
