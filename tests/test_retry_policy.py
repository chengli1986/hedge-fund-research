"""Retry by failure label, and requeue after a fetcher change (2026-09-15).

Until now every failure was retried daily five times and then retired to
permafail for good, whatever the cause: a deleted page was fetched five
times, a network blip got the same five chances as a redesign, and a
permafail stayed lost after its fetcher was fixed -- aqr's two articles
extracted fine after e906481 but stayed permafail until requeued by hand.

Policy (failure_labels.RETRY_POLICY):
  fetch_error               backoff 1, 2, 4, 8 days; code-dependent
  blocked_by_bot_protection weekly, at most 4 attempts
  page_gone, media_without_text  retired after 2 consecutive same-label failures
  selector_miss, body_too_short, body_rendered_client_side, pdf_not_usable
                            daily, at most 3 attempts; code-dependent
A permafail whose label is code-dependent becomes pending again, once, when
fetch_content.py differs from the version recorded at its last failure.
"""
from datetime import datetime, timedelta, timezone

import pytest

import fetch_content as fc

BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=BJT)
SRC = next(iter(fc.CONTENT_FETCHERS))


def _fail(article, label, now=NOW):
    return fc.mark_content_failure(article, failure={"label": label, "detail": "d"}, now=now)


def _art(**kw):
    return dict({"id": "x", "source_id": SRC, "summarized": False}, **kw)


def test_fetch_error_backs_off_and_waits():
    a = _art()
    assert _fail(a, "fetch_error") == "failed"
    assert datetime.fromisoformat(a["content_retry_after"]) == NOW + timedelta(days=1)
    # Due on the retry_after date (BJT), whatever the time of day: "wait one
    # day" means the next night's run. Until 2026-10-02 this compared to the
    # second, and asserted not-pending at NOW + 12h -- midnight, already the
    # next date.
    assert not fc.is_content_pending(a, now=NOW + timedelta(hours=11, minutes=59))
    assert fc.is_content_pending(a, now=NOW + timedelta(hours=12))
    assert fc.is_content_pending(a, now=NOW + timedelta(days=1, minutes=1))
    _fail(a, "fetch_error", now=NOW + timedelta(days=1))
    assert datetime.fromisoformat(a["content_retry_after"]) == NOW + timedelta(days=3)


class TestDueByDateNotBySecond:
    """2026-10-02. A failure stamped 03:49:36 was due "one day later" at
    03:49:36, and the next night's stage 2, which starts anywhere between
    03:49:21 and 03:50:00 BJT, often checked a few seconds early and skipped
    it for a whole extra day: 11 of 68 one-day retries in the ledger came a
    day late. The apollo podcast row waiting on 2026-10-02 was one of them.
    Comparing dates makes "wait N days" mean "the run N nights later"."""
    STAMP = datetime(2026, 10, 1, 3, 49, 36, tzinfo=BJT)

    def _due(self, retry_after):
        return _art(content_status="failed", content_retry_after=retry_after.isoformat(timespec="seconds"))

    def test_the_next_nights_run_retries_even_if_it_starts_seconds_earlier(self):
        row = self._due(self.STAMP + timedelta(days=1))
        assert fc.is_content_pending(row, now=datetime(2026, 10, 2, 3, 49, 21, tzinfo=BJT))

    def test_not_before_the_date(self):
        row = self._due(self.STAMP + timedelta(days=1))
        assert not fc.is_content_pending(row, now=datetime(2026, 10, 1, 23, 59, 59, tzinfo=BJT))

    def test_the_date_is_taken_in_beijing_time(self):
        """A UTC stamp of 2026-10-01 19:49 is 10-02 03:49 BJT: due on the 2nd."""
        row = _art(content_status="failed", content_retry_after="2026-10-01T19:49:36+00:00")
        assert fc.is_content_pending(row, now=datetime(2026, 10, 2, 3, 49, 0, tzinfo=BJT))
        assert not fc.is_content_pending(row, now=datetime(2026, 10, 1, 23, 0, 0, tzinfo=BJT))

    def test_a_stamp_without_a_zone_stays_due_as_before(self):
        """No stored value lacks a zone (checked 2026-10-02); if one ever does,
        keep the old outcome -- comparing it raised and the row counted as due."""
        row = _art(content_status="failed", content_retry_after="2026-12-31T23:00:00")
        assert fc.is_content_pending(row, now=NOW)


def test_blocked_waits_a_week():
    a = _art()
    _fail(a, "blocked_by_bot_protection")
    assert datetime.fromisoformat(a["content_retry_after"]) == NOW + timedelta(days=7)


@pytest.mark.parametrize("label", ["page_gone", "media_without_text"])
def test_a_confirmed_terminal_cause_retires_after_two(label):
    a = _art()
    assert _fail(a, label) == "failed"
    assert _fail(a, label, now=NOW + timedelta(days=1)) == "permafail"


def test_a_different_label_resets_the_terminal_streak():
    a = _art()
    _fail(a, "page_gone")
    _fail(a, "fetch_error", now=NOW + timedelta(days=1))
    assert _fail(a, "page_gone", now=NOW + timedelta(days=3)) == "failed"


def test_code_dependent_labels_retire_after_three():
    a = _art()
    statuses = [_fail(a, "selector_miss", now=NOW + timedelta(days=i)) for i in range(3)]
    assert statuses == ["failed", "failed", "permafail"]


def test_the_failure_records_the_code_version():
    a = _art()
    _fail(a, "selector_miss")
    assert a["content_failure"]["code_version"] == fc.code_version_for(a)


class TestRequeueAfterCodeChange:
    def _permafail(self, label, version):
        return _art(content_status="permafail", content_attempts=3,
                    content_failure={"label": label, "detail": "d", "code_version": version})

    def test_a_code_dependent_permafail_is_retried_once_the_fetcher_changes(self):
        assert fc.is_content_pending(self._permafail("selector_miss", "old-version"))

    def test_not_while_the_fetcher_is_unchanged(self):
        a = self._permafail("selector_miss", "placeholder")
        a["content_failure"]["code_version"] = fc.code_version_for(a)
        assert not fc.is_content_pending(a)

    @pytest.mark.parametrize("label", ["page_gone", "media_without_text", "blocked_by_bot_protection"])
    def test_causes_code_cannot_fix_are_not_requeued(self, label):
        assert not fc.is_content_pending(self._permafail(label, "old-version"))

    def test_a_backfilled_label_without_a_version_is_retried_once(self):
        a = _art(content_status="permafail", content_failure={"label": "body_rendered_client_side"})
        assert fc.is_content_pending(a)

    def test_failing_again_under_the_new_code_retires_it_for_this_version(self):
        a = self._permafail("selector_miss", "old-version")
        assert _fail(a, "selector_miss") == "permafail"
        assert not fc.is_content_pending(a)


def test_success_clears_the_retry_schedule(tmp_path, monkeypatch):
    import json, sys
    art = _art(id="s1", title="T", url="https://x/t", content_status="failed", content_attempts=1,
               content_retry_after=(datetime.now(BJT) - timedelta(minutes=1)).isoformat(),
               content_failure={"label": "fetch_error", "detail": "d"})
    data = tmp_path / "articles.jsonl"; data.write_text(json.dumps(art) + "\n")
    monkeypatch.setattr(fc, "DATA_FILE", data); monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "c")
    monkeypatch.setattr(fc, "BASE_DIR", tmp_path)

    def fetcher(a):
        p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("body " * 100); return (p, "ok")

    monkeypatch.setitem(fc.CONTENT_FETCHERS, SRC, fetcher)
    monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
    fc.main()
    row = json.loads(data.read_text())
    assert row["content_status"] == "ok"
    assert "content_retry_after" not in row and "content_failure" not in row


def test_main_passes_the_label_to_the_retry_decision(tmp_path, monkeypatch):
    import json, sys
    art = _art(id="m1", title="Video", url="https://x/v")
    data = tmp_path / "articles.jsonl"; data.write_text(json.dumps(art) + "\n")
    monkeypatch.setattr(fc, "DATA_FILE", data); monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path / "c")

    def fetcher(a):
        fc.note_failure_hint("page_gone", "gone")
        return None

    monkeypatch.setitem(fc.CONTENT_FETCHERS, SRC, fetcher)
    monkeypatch.setattr(sys, "argv", ["fetch_content.py"])
    fc.main()
    row = json.loads(data.read_text())
    row["content_retry_after"] = None                      # due now
    data.write_text(json.dumps(row) + "\n")
    fc.main()
    row = json.loads(data.read_text())
    assert row["content_status"] == "permafail", "two consecutive page_gone should retire it"
