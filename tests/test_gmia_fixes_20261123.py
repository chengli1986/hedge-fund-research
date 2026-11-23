"""2026-11-23 fixes: MetLife listing paths, A/B gate known_block + title whitespace."""
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fetch_articles as fa  # noqa: E402
import listing_templates as lt  # noqa: E402

_spec = importlib.util.spec_from_file_location("compare_fetchers", REPO / "scripts" / "compare_fetchers.py")
cf = importlib.util.module_from_spec(_spec)
sys.modules["compare_fetchers"] = cf
_spec.loader.exec_module(cf)



# ── MetLife: the servlet's path changed form on 2026-11-21 ────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


METLIFE_SRC = {"id": "metlife-im", "expected_hostname": "www.metlife.com", "max_articles": 10}


def _metlife(monkeypatch, entries):
    monkeypatch.setattr(fa.requests, "get", lambda *a, **k: _Resp({"articles": entries}))
    return fa.fetch_metlife_im(dict(METLIFE_SRC))


def test_the_public_path_form_is_accepted(monkeypatch):
    rows = _metlife(monkeypatch, [{
        "path": "/investments/en-us/insights/investment-perspectives/finding-hidden-gems-in-small-cap-high-yield/",
        "headlineTitle": "Finding Hidden Gems in Small Cap High Yield", "publishedDate": "NOV 18, 2026"}])
    assert [(r["url"], r["date"]) for r in rows] == [(
        "https://www.metlife.com/investments/en-us/insights/investment-perspectives/"
        "finding-hidden-gems-in-small-cap-high-yield/", "2026-11-18")]


def test_both_forms_give_the_same_url_so_stored_ids_do_not_change(monkeypatch):
    tail = "insights/outlooks-and-market-updates/october-2026-pension-funding-status/"
    old = _metlife(monkeypatch, [{"path": fa._METLIFE_CONTENT_PREFIX + tail,
                                  "headlineTitle": "T", "publishedDate": "Nov 17, 2026"}])
    new = _metlife(monkeypatch, [{"path": "/investments/en-us/" + tail,
                                  "headlineTitle": "T", "publishedDate": "NOV 17, 2026"}])
    assert old[0]["url"] == new[0]["url"]
    assert fa.article_id("metlife-im", old[0]["url"]) == fa.article_id("metlife-im", new[0]["url"])


def test_a_path_in_neither_form_is_still_dropped(monkeypatch):
    rows = _metlife(monkeypatch, [
        {"path": "/content/dam/metlifecom/x.pdf", "headlineTitle": "PDF", "publishedDate": "NOV 1, 2026"},
        {"path": "https://evil.test/investments/en-us/x/", "headlineTitle": "Off", "publishedDate": "NOV 1, 2026"},
        {"path": "/investments/en-us/insights/a/", "headlineTitle": "", "publishedDate": "NOV 1, 2026"},
    ])
    assert rows == []


# ── A/B gate ─────────────────────────────────────────────────────────────────

SRC = {"id": "t", "name": "T", "short_name": "T", "method": "ssr", "url": "https://site.test/i",
       "expected_hostname": "site.test", "max_articles": 10,
       "listing_template": {"type": "card_list", "fetch": "requests", "card": "div.card",
                            "link": "a", "date": "span.date"}}
ROW = {"title": "Geopolitics in 2027:  key themes to watch", "url": "https://site.test/a",
       "date": "2026-11-16"}


@pytest.fixture(autouse=True)
def _fake_sources_have_a_hand_written_fetcher(monkeypatch):
    monkeypatch.setattr(cf, "has_hand_written", lambda sid: True)


def test_a_title_differing_only_in_whitespace_agrees(monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "t", lambda s: [dict(ROW)])
    monkeypatch.setattr(lt, "fetch", lambda s: [dict(ROW, title="Geopolitics in 2027: key themes to watch")])
    monkeypatch.setattr(fa, "load_entrypoints", lambda: {})
    assert cf.compare_source(dict(SRC))["agree"] is True


def test_a_title_differing_in_words_still_disagrees(monkeypatch):
    monkeypatch.setitem(fa.FETCHERS, "t", lambda s: [dict(ROW)])
    monkeypatch.setattr(lt, "fetch", lambda s: [dict(ROW, title="Geopolitics in 2028: key themes to watch")])
    monkeypatch.setattr(fa, "load_entrypoints", lambda: {})
    assert cf.compare_source(dict(SRC))["agree"] is False


def test_the_evidence_pattern_matches_the_health_probe():
    # Read, not imported: loading the health script a second time under
    # another module name opens its log handlers again.
    text = (REPO / "scripts" / "gmia-fetcher-health.py").read_text()
    m = re.search(r'^KNOWN_BLOCK_EVIDENCE = re\.compile\(r"(.*)"\)$', text, re.M)
    assert m and m.group(1) == cf.KNOWN_BLOCK_EVIDENCE.pattern


FORBIDDEN = "HTTPError: 403 Client Error: Forbidden for url: https://site.test/i"


def _gate(monkeypatch, tmp_path, errors, known=True, history=()):
    """errors: per compare_source call for source t (None = agrees)."""
    hist = tmp_path / "ab-gate.jsonl"
    hist.write_text("".join(json.dumps(r) + "\n" for r in history))
    monkeypatch.setattr(cf, "HISTORY", hist)
    src = dict(SRC, known_block="Decided 2026-11-23: keep as is.") if known else dict(SRC)
    monkeypatch.setattr(cf, "_load_sources", lambda: [src])
    seq = list(errors)
    calls = []

    def fake(s):
        calls.append(s["id"])
        err = seq.pop(0)
        return {"source_id": s["id"], "agree": err is None, "error": err, "upstream_down": None,
                "bespoke": 0 if err else 1, "template": 0 if err else 1, "only_bespoke": [],
                "only_template": [], "order_differs": False, "dropped_fields": []}

    monkeypatch.setattr(cf, "compare_source", fake)
    monkeypatch.setattr(cf.sys, "argv", ["compare_fetchers.py", "--all", "--recheck"])
    code = cf.main()
    rows = [json.loads(line) for line in hist.read_text().splitlines()]
    return code, rows, calls


def test_a_known_blocked_403_passes_is_recorded_and_not_rerun(monkeypatch, tmp_path, capsys):
    code, rows, calls = _gate(monkeypatch, tmp_path, [FORBIDDEN])
    out = capsys.readouterr().out
    assert code == 0 and calls == ["t"]
    assert "known block" in out and "not compared" in out
    assert rows[-1]["agree"] is True and rows[-1]["known_block"] == ["t"]
    assert rows[-1]["disagreed"] == []


def test_without_the_flag_a_403_still_fails(monkeypatch, tmp_path):
    code, rows, calls = _gate(monkeypatch, tmp_path, [FORBIDDEN, FORBIDDEN], known=False)
    assert code == 1 and calls == ["t", "t"] and rows[-1]["known_block"] == []


def test_a_known_blocked_source_failing_another_way_still_fails(monkeypatch, tmp_path):
    code, rows, _ = _gate(monkeypatch, tmp_path, ["ValueError: selector matched nothing"] * 2)
    assert code == 1 and rows[-1]["disagreed"] == ["t"] and rows[-1]["known_block"] == []


def test_a_known_blocked_source_that_answers_is_compared_normally(monkeypatch, tmp_path):
    code, rows, _ = _gate(monkeypatch, tmp_path, [None])
    assert code == 0 and rows[-1]["known_block"] == []


def test_rothschild_carries_a_decided_known_block():
    sources = json.loads((REPO / "config" / "sources.json").read_text())["sources"]
    note = next(s for s in sources if s["id"] == "rothschild-co-am").get("known_block", "")
    assert "Decided 2026-11-23" in note and "no bypass" in note
