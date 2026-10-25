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

import pytest

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
            if isinstance(saved_resp, Exception):
                raise saved_resp
            return saved_resp

        def dispose(self):
            pass

    class Ctx:
        request = types.SimpleNamespace(get=lambda url, **k: (calls.append(("ctx", url)), form_resp)[1])

        def set_default_timeout(self, ms):
            calls.append(("timeout", ms))

        def new_page(self):
            return types.SimpleNamespace()

        def storage_state(self, path):
            calls.append(("save_state", path))
            Path(path).write_text("{}")

    class Browser:
        def new_context(self, **k):
            calls.append(("context", k.get("storage_state")))
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
    assert [c[0] for c in calls if c[0] not in ("context", "timeout")] == ["saved", "form", "ctx", "save_state", "close"]


def test_no_saved_state_goes_straight_to_the_form(tmp_path, monkeypatch):
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json")
    assert [c for c in calls if c[0] not in ("context", "timeout")][0] == ("form",)


def test_a_view_pdf_link_to_another_paper_is_not_used(tmp_path, monkeypatch):
    calls = []
    other = PDF.replace("financed-water-consumption/f.pdf", "too-hot-to-work/t.pdf")
    _fake_playwright(monkeypatch, Resp(), other, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json") is None
    assert not any(c[0] in ("ctx", "save_state") for c in calls)


def test_a_failed_form_or_a_non_pdf_returns_none_and_saves_nothing(tmp_path, monkeypatch):
    for link, resp in ((None, Resp()), (PDF, Resp(200, "text/html"))):
        (tmp_path / msci_gate.FORM_PAUSE_NAME).unlink(missing_ok=True)   # each case from a fresh start
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
        pytest.skip("no local form config (not the production checkout)")
    needles = [cfg["company"].encode(), cfg["State"].encode()]
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


# --- review follow-ups, 2026-10-24 (M1 pause, M2 saved state, M3 timeouts, L2) ---

def _form_fails(tmp_path, monkeypatch, calls):
    _fake_playwright(monkeypatch, Resp(), None, Resp(), calls)
    return msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json")


def test_a_failed_form_pauses_the_form_for_other_processes(tmp_path, monkeypatch):
    """M1: the health probe, the audit and stage 2 are separate processes; a
    rejected form must not be resubmitted in the reader's name every night."""
    calls = []
    assert _form_fails(tmp_path, monkeypatch, calls) is None
    marker = json.loads((tmp_path / msci_gate.FORM_PAUSE_NAME).read_text())
    assert "no View PDF link" in marker["reason"]
    calls.clear()
    assert _form_fails(tmp_path, monkeypatch, calls) is None
    assert ("form",) not in calls and not any(c[0] == "context" for c in calls)


def test_the_saved_unlock_is_still_tried_while_the_form_is_paused(tmp_path, monkeypatch):
    calls = []
    _form_fails(tmp_path, monkeypatch, calls)
    state = tmp_path / "s.json"
    state.write_text("{}")
    calls.clear()
    _fake_playwright(monkeypatch, Resp(), None, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert calls == [("saved", PDF)]


def test_the_pause_expires_and_a_success_clears_it(tmp_path, monkeypatch):
    calls = []
    _form_fails(tmp_path, monkeypatch, calls)
    marker = tmp_path / msci_gate.FORM_PAUSE_NAME
    data = json.loads(marker.read_text())
    data["until"] = "2000-01-01T00:00:00+00:00"
    marker.write_text(json.dumps(data))
    calls.clear()
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json")
    assert ("form",) in calls and not marker.exists()


def test_an_exception_in_the_browser_also_pauses_the_form(tmp_path, monkeypatch):
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    monkeypatch.setattr(msci_gate, "_open_form_and_submit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Timeout 15000ms exceeded")))
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json") is None
    assert "Timeout" in json.loads((tmp_path / msci_gate.FORM_PAUSE_NAME).read_text())["reason"]


def test_a_failing_saved_unlock_request_still_reaches_the_form(tmp_path, monkeypatch):
    """M2: req.get raising used to end the whole fallback."""
    calls = []
    state = tmp_path / "s.json"
    state.write_text("{}")
    _fake_playwright(monkeypatch, RuntimeError("connection reset"), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert ("form",) in calls


def test_a_corrupt_saved_state_is_ignored_not_fatal(tmp_path, monkeypatch):
    """M2: an unreadable state file made every later attempt raise before the form."""
    calls = []
    state = tmp_path / "s.json"
    state.write_text("{not json")
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    assert msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert not any(c[0] == "saved" for c in calls)
    assert ("context", None) in calls and ("form",) in calls


def test_browser_steps_have_a_short_default_timeout(tmp_path, monkeypatch):
    """M3: the health check has 600 s and uses ~300; Playwright's 30 s default per step could eat the rest."""
    calls = []
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=tmp_path / "s.json")
    assert ("timeout", msci_gate.FORM_STEP_TIMEOUT_MS) in calls
    assert msci_gate.FORM_STEP_TIMEOUT_MS <= 15000


def test_same_pdf_ignores_percent_encoding():
    """L2: "Too Hot to Work - FINAL.pdf" (live, 10-24) is a raw space in pdfDamPath, %20 in an href."""
    raw = "https://www.msci.com/downloads/x/paper/too-hot-to-work/Too Hot to Work - FINAL.pdf"
    assert msci_gate._same_pdf(raw.replace(" ", "%20"), raw)
    assert not msci_gate._same_pdf(raw.replace("FINAL", "DRAFT"), raw)


def test_a_pdf_read_after_the_form_survives_a_failed_unlock_save(tmp_path, monkeypatch):
    """Review 10-25 (backfill): saving the unlock ran before the PDF was taken,
    so a failed save (disk full, permissions) dropped a PDF the reader's form
    had already paid for and paused the form for 72 hours."""
    calls = []
    state = tmp_path / "s.json"
    state.mkdir()                     # storage_state's write fails on it
    _fake_playwright(monkeypatch, Resp(), PDF, Resp(), calls)
    body = msci_gate.fetch_gated_pdf(PAGE, PDF, config_path=_cfg(tmp_path), state_path=state)
    assert body and body.startswith(b"%PDF")
    assert ("save_state", str(state)) in calls
    assert not (tmp_path / msci_gate.FORM_PAUSE_NAME).exists()
