"""msci_gate: the form-fill fallback for MSCI gated papers (2026-10-24).

No browser here: playwright.sync_api is replaced by a stand-in, so the tests
cover the module's decisions (config, saved unlock first, the View PDF link
must be this paper's, failures return None) and run where Chromium is absent.
The form itself was exercised live on 10-24 (financed-water-consumption,
when-regimes-shift: 3.4 MB and 640 KB PDFs).
"""
import json
import subprocess
import sys
import types
from pathlib import Path

import msci_gate

REPO = Path(__file__).resolve().parent.parent
PAGE = "https://www.msci.com/research-and-insights/paper/financed-water-consumption"
PDF = "https://www.msci.com/downloads/web/msci-com/research-and-insights/paper/financed-water-consumption/f.pdf"
CFG = {"firstName": "A", "lastName": "B", "email": "a@example.com", "Location": "Exampleland",
       "State": "Example State", "company": "Example Inc.", "Segment": "Other", "Interest": "Index"}


class Resp:
    def __init__(self, status=200, ctype="application/pdf", body=b"%PDF-1.6 " + b"z" * 5000):
        self.status, self.headers, self._body = status, {"content-type": ctype}, body

    def body(self):
        return self._body


def _fake_playwright(monkeypatch, saved_resp, form_link, form_resp, calls):
    class Req:
        def get(self, url, **k):
            calls.append(("saved", url))
            return saved_resp

        def dispose(self):
            pass

    class Ctx:
        request = types.SimpleNamespace(get=lambda url, **k: (calls.append(("ctx", url)), form_resp)[1])

        def new_page(self):
            return types.SimpleNamespace()

        def storage_state(self, path):
            calls.append(("save_state", path))
            Path(path).write_text("{}")

    class Browser:
        def new_context(self, **k):
            return Ctx()

        def close(self):
            calls.append(("close",))

    class P:
        request = types.SimpleNamespace(new_context=lambda **k: Req())
        chromium = types.SimpleNamespace(launch=lambda **k: Browser())

    class CM:
        def __enter__(self):
            return P()

        def __exit__(self, *a):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = CM
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)

    def fake_open_form(page, *a, **k):
        calls.append(("form",))
        return form_link
    # the page navigation and form filling, at the module boundary
    monkeypatch.setattr(msci_gate, "_open_form_and_submit", fake_open_form)


def _cfg(tmp_path, data=CFG):
    p = tmp_path / "msci_form.local.json"
    p.write_text(json.dumps(data))
    return p


def test_without_the_local_config_the_fallback_is_skipped(tmp_path, monkeypatch):
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=tmp_path / "missing.json",
                                     state_path=tmp_path / "s.json") is None
    assert calls == []


def test_an_incomplete_config_is_skipped(tmp_path):
    assert msci_gate.load_form_config(_cfg(tmp_path, {**CFG, "State": ""})) is None
    assert msci_gate.load_form_config(_cfg(tmp_path)) == CFG


def test_a_saved_unlock_is_tried_first_and_the_form_is_not_filled(tmp_path, monkeypatch):
    calls = []
    state = tmp_path / "s.json"
    state.write_text("{}")
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert calls == [("saved", PDF)]


def test_a_refused_saved_unlock_falls_through_to_the_form_and_saves_the_new_one(tmp_path, monkeypatch):
    calls = []
    state = tmp_path / "s.json"
    state.write_text("{}")
    _fake_playwright(monkeypatch, Resp(307, ""), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert [c[0] for c in calls] == ["saved", "form", "ctx", "save_state", "close"]


def test_no_saved_state_goes_straight_to_the_form(tmp_path, monkeypatch):
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json")
    assert calls[0] == ("form",)


def test_a_view_pdf_link_to_another_paper_is_not_used(tmp_path, monkeypatch):
    calls = []
    other = PDF.replace("financed-water-consumption/f.pdf", "too-hot-to-work/t.pdf")
    _fake_playwright(monkeypatch, Resp(), other, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json") is None
    assert not any(c[0] in ("ctx", "save_state") for c in calls)


def test_a_failed_form_or_a_non_pdf_returns_none_and_saves_nothing(tmp_path, monkeypatch):
    for link, resp in ((None, Resp()), (PDF, Resp(200, "text/html"))):
        calls = []
        state = tmp_path / "s.json"
        _fake_playwright(monkeypatch, Resp(), link, resp, calls)
        assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state) is None
        assert not state.exists() and ("close",) in calls


def test_an_exception_inside_the_browser_returns_none(tmp_path, monkeypatch):
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)

    def boom(*a, **k):
        raise RuntimeError("selector timeout")
    monkeypatch.setattr(msci_gate, "_open_form_and_submit", boom)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json") is None


def test_form_submissions_are_spaced(monkeypatch):
    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(msci_gate.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(msci_gate.time, "sleep", lambda s: (slept.append(s), clock.update(t=clock["t"] + s)))
    monkeypatch.setattr(msci_gate, "_last_submit", 0.0)
    msci_gate._wait_for_submit_slot()
    clock["t"] += 5
    msci_gate._wait_for_submit_slot()
    assert slept == [msci_gate.MIN_SUBMIT_INTERVAL - 5]


def test_the_readers_details_stay_out_of_the_repo():
    """The values are read from the local file itself, so the test names none of them.

    Only where the file exists (the production checkout); the email is left
    out because it was already tracked as the alert recipient
    (scripts/wrapper-candidate-discovery.sh) before this module existed.
    """
    ignored = subprocess.run(["git", "check-ignore", "-q", "config/msci_form.local.json"], cwd=REPO)
    assert ignored.returncode == 0, "the form config must be gitignored"
    cfg = msci_gate.load_form_config(REPO / "config" / "msci_form.local.json")
    if cfg is None:
        return
    needles = [cfg["company"].encode()]
    tracked = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                             cwd=REPO, capture_output=True).stdout.split(b"\0")
    for name in filter(None, tracked):
        path = REPO / name.decode()
        if path.is_file() and path.stat().st_size < 5_000_000:
            data = path.read_bytes()
            assert not any(n in data for n in needles), f"reader's details in repo file {name.decode()}"


def test_the_module_holds_no_default_details():
    """No literal of the local file's values in the module (named by reading the file, as above)."""
    src = (REPO / "msci_gate.py").read_text()
    cfg = msci_gate.load_form_config(REPO / "config" / "msci_form.local.json") or {}
    for value in ["@", *(str(v) for v in cfg.values())]:
        assert f'"{value}"' not in src


def test_a_redirect_is_not_a_pdf_whatever_its_content_type():
    assert not msci_gate._is_pdf(Resp(307, "application/pdf"))
    assert not msci_gate._is_pdf(Resp(200, "application/pdf", b"%PDF tiny"))
    assert msci_gate._is_pdf(Resp())
