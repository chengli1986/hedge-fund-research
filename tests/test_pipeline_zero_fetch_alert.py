"""A source that silently fetched nothing must reach a human.

The pipeline already detects this: fetch_articles.record_quality_metrics
stores `last_article_count` and `consecutive_zero_count` per source, and
check_anomalies flags two consecutive zeros. But the alert's only destination
is `log.warning("ANOMALY ...")` in logs/fetch.log, which nothing reads -- so a
detected problem was whispered rather than reported.

Worse, the one channel that does email suppresses itself in exactly this case.
gmia-fetcher-health.py sends only when `alerts["failing"] or warning or
recovered`, and it probes at 04:30 BJT, 45 minutes after the 03:45 pipeline.
On 2026-09-06 acadian-asset fetched 0 articles; by 04:30 the site answered and
every probe was OK, so the email was suppressed and the missed night went
unrecorded. Five months of fetch.log hold exactly one acadian zero -- nobody
saw it.

A zero-article fetch is therefore its own alert condition, reported separately
from the live probe results because the two are different measurements taken
at different times.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gmia-fetcher-health.py"

spec = importlib.util.spec_from_file_location("gfh_zero", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_zero"] = gfh
spec.loader.exec_module(gfh)

NO_ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}


def _state(tmp_path, payload) -> Path:
    f = tmp_path / "inspection_state.json"
    f.write_text(json.dumps(payload, ensure_ascii=False))
    return f


def _hours_ago(n: float) -> str:
    """A timestamp n hours old.

    Never hardcode one here: the freshness filter is measured against the wall
    clock, so a literal expires. The first version of these fixtures pinned
    2026-09-09T03:48+08:00, which would have turned seven tests red 30 hours
    later -- and scripts/wrapper-auto-promote.sh tells the nightly 02:30 agent
    to ROLL BACK its work if pytest fails, so a clock-driven red suite would
    have reverted a correctly promoted source.
    """
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _fresh() -> str:
    """A timestamp the last pipeline run would plausibly have written."""
    return _hours_ago(0.75)          # health check runs 45min after the pipeline


class TestDetection:
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        # The check intersects with sources.json; these fixtures use real ids
        # only where that matters, so pin the set explicitly.
        monkeypatch.setattr(gfh, "load_sources",
                            lambda: [{"id": "acadian-asset"}, {"id": "aqr"},
                                     {"id": "alpha"}, {"id": "zeta"}])

    def test_reports_a_source_that_fetched_nothing(self, tmp_path):
        f = _state(tmp_path, {
            "acadian-asset": {"last_article_count": 0, "consecutive_zero_count": 1,
                              "last_inspected_at": _fresh()},
            # last_inspected_at is required: without it this row is excluded by
            # the freshness gate, so the test would pass even if the
            # last_article_count check were deleted (caught by mutation).
            "aqr": {"last_article_count": 10, "consecutive_zero_count": 0,
                    "last_inspected_at": _fresh()},
        })
        got = gfh.pipeline_zero_fetches(f)
        assert [sid for sid, _ in got] == ["acadian-asset"]

    def test_a_source_that_fetched_articles_is_not_reported(self, tmp_path):
        f = _state(tmp_path, {"aqr": {"last_article_count": 10, "consecutive_zero_count": 0,
                                      "last_inspected_at": _fresh()}})
        assert gfh.pipeline_zero_fetches(f) == []

    def test_missing_state_file_is_not_an_error(self, tmp_path):
        # The health check must never fail because of its own extra reporting.
        assert gfh.pipeline_zero_fetches(tmp_path / "nope.json") == []

    def test_corrupt_state_file_is_not_an_error(self, tmp_path):
        f = tmp_path / "inspection_state.json"
        f.write_text("{not json")
        assert gfh.pipeline_zero_fetches(f) == []

    def test_sources_are_ordered_for_a_stable_report(self, tmp_path):
        f = _state(tmp_path, {
            "zeta": {"last_article_count": 0, "last_inspected_at": _fresh()},
            "alpha": {"last_article_count": 0, "last_inspected_at": _fresh()}})
        assert [sid for sid, _ in gfh.pipeline_zero_fetches(f)] == ["alpha", "zeta"]


class TestItActuallyGetsSent:
    """The load-bearing assertion: this is the line that suppressed the email."""

    def test_a_zero_fetch_alone_triggers_the_email(self):
        assert gfh.should_email(NO_ALERTS, [("acadian-asset", {"last_article_count": 0})]) is True

    def test_all_clear_still_sends_nothing(self):
        assert gfh.should_email(NO_ALERTS, []) is False

    def test_existing_alert_conditions_are_unchanged(self):
        for key in ("failing", "warning", "recovered"):
            alerts = dict(NO_ALERTS, **{key: [("x", {}, None)]})
            assert gfh.should_email(alerts, []) is True, key


class TestItAppearsInBothReports:
    ROWS = [("acadian-asset", {"last_article_count": 0, "consecutive_zero_count": 1,
                               "last_inspected_at": "2026-09-06T19:48:32+00:00"})]

    def test_console_report_names_the_source(self, capsys):
        gfh.print_console_report({}, 1.0, zero_fetches=self.ROWS)
        out = capsys.readouterr().out
        assert "acadian-asset" in out
        assert "0 articles" in out or "抓取 0" in out or "fetched 0" in out

    def test_html_email_names_the_source(self):
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=self.ROWS)
        assert "acadian-asset" in html

    def test_html_email_omits_the_section_when_there_is_nothing_to_say(self):
        # Assert the HEADING is absent, not the row text: rendering the section
        # with zero rows keeps "fetched 0 articles" out of the html while an
        # all-clear email carries "📉 PIPELINE FETCHED NOTHING (0)".
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=[])
        assert "PIPELINE FETCHED NOTHING" not in html


class TestTheWiringItself:
    """The helpers being right is not the same as them being connected.

    A mutation audit on 2026-09-09 found four survivors, all in main(): stop
    passing zero_fetches to the HTML renderer, stop passing it to the console
    renderer, revert needs_alert to the old `failing or warning or recovered`
    expression (i.e. reinstate the exact 09-06 suppression bug), or compute the
    list after the --source early return. Every one kept the suite at 998
    green, because the tests above only exercise pure helpers.

    That is the same shape as the backfill's dead apply(): the piece under test
    was not the piece production runs. These drive main() end to end.
    """

    SOURCE = {"id": "acadian-asset", "name": "Acadian", "short_name": "Acadian",
              "url": "https://example.com/x", "method": "playwright",
              "expected_hostname": "example.com"}
    OK_PROBE = {"status": "OK", "reason": "", "articles_count": 10,
                "content_chars": 900, "elapsed_ms": 100, "most_recent_date": "2026-09-08",
                "most_recent_age_days": 1, "stale_threshold_days": 90}

    def _drive(self, tmp_path, monkeypatch, capsys, zero_state):
        """Run main() with probes and SMTP stubbed, and nothing real written."""
        state = tmp_path / "inspection_state.json"
        state.write_text(json.dumps(zero_state, ensure_ascii=False))
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", state)
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "health.json")
        monkeypatch.setattr(gfh, "load_sources", lambda: [self.SOURCE])
        monkeypatch.setattr(gfh, "probe_source", lambda src: dict(self.OK_PROBE))
        sent = {}
        monkeypatch.setattr(gfh, "send_email",
                            lambda body, subject: sent.update(body=body, subject=subject) or True)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
        rc = gfh.main()
        return rc, sent, capsys.readouterr().out

    ZERO = {"acadian-asset": {"last_article_count": 0, "consecutive_zero_count": 1,
                              "last_inspected_at": _fresh()}}

    def test_a_zero_fetch_alone_actually_sends_an_email(self, tmp_path, monkeypatch, capsys):
        _, sent, _ = self._drive(tmp_path, monkeypatch, capsys, self.ZERO)
        assert sent, ("every probe passed and only the pipeline zero was left — "
                      "no email was sent, which is the 09-06 bug reinstated")

    def test_the_email_body_carries_the_zero_fetch_section(self, tmp_path, monkeypatch, capsys):
        # Assert the SECTION, not the source id: the same id also appears in the
        # HEALTHY listing, so "acadian-asset in body" passed even with the
        # section removed (caught by mutation 2026-09-09).
        _, sent, _ = self._drive(tmp_path, monkeypatch, capsys, self.ZERO)
        assert "PIPELINE FETCHED NOTHING" in sent["body"], (
            "the email was sent but says nothing about the zero fetch")
        assert "fetched 0 articles" in sent["body"]

    def test_the_subject_does_not_say_all_ok(self, tmp_path, monkeypatch, capsys):
        # An alert whose subject reads "all OK" is the whispering this whole
        # change exists to stop, reproduced at the last hop.
        _, sent, _ = self._drive(tmp_path, monkeypatch, capsys, self.ZERO)
        assert "all OK" not in sent["subject"], sent["subject"]
        assert "acadian-asset" in sent["subject"] or "fetched nothing" in sent["subject"].lower()

    def test_the_console_report_shows_it_too(self, tmp_path, monkeypatch, capsys):
        # "0 articles" is a substring of the HEALTHY row's "10 articles", so the
        # first version of this assertion passed with the section removed.
        _, _, out = self._drive(tmp_path, monkeypatch, capsys, self.ZERO)
        assert "PIPELINE FETCHED NOTHING" in out
        assert "fetched 0 articles" in out

    def test_a_clean_run_still_sends_nothing(self, tmp_path, monkeypatch, capsys):
        clean = {"acadian-asset": {"last_article_count": 10, "consecutive_zero_count": 0,
                                   "last_inspected_at": _fresh()}}
        _, sent, _ = self._drive(tmp_path, monkeypatch, capsys, clean)
        assert not sent, "an all-clear run must stay quiet"


class TestStaleEntriesDoNotLatch:
    """A frozen record must not alert forever.

    config/inspection_state.json is append-only in practice: record_quality_metrics
    writes, nothing prunes, and a source removed from sources.json keeps its last
    record indefinitely (pgim and pinebridge are still in there, 89 and 44 days
    after retirement). Filtering on `last_article_count == 0` alone means the
    most likely stale entry -- a source retired BECAUSE it stopped producing --
    would alert every single day with no way to clear it but hand-editing the
    file.

    The wording matters too: "fetched 0 articles in the last pipeline run" is
    simply false about a record from three months ago, and the timestamp printed
    beside it contradicts the sentence.
    """

    FRESH = staticmethod(lambda: _fresh())
    ANCIENT = staticmethod(lambda: _hours_ago(24 * 90))

    def _state(self, tmp_path, payload):
        f = tmp_path / "inspection_state.json"
        f.write_text(json.dumps(payload, ensure_ascii=False))
        return f

    def test_a_retired_source_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {
            "pgim": {"last_article_count": 0, "last_inspected_at": self.FRESH()},
            "aqr": {"last_article_count": 0, "last_inspected_at": self.FRESH()},
        })
        assert [sid for sid, _ in gfh.pipeline_zero_fetches(f)] == ["aqr"]

    def test_a_record_the_last_run_did_not_refresh_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 0, "last_inspected_at": self.ANCIENT()}})
        assert gfh.pipeline_zero_fetches(f) == [], (
            "a months-old record still alerts — nothing can ever clear it")

    def test_a_fresh_zero_still_alerts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 0, "last_inspected_at": self.FRESH()}})
        assert [sid for sid, _ in gfh.pipeline_zero_fetches(f)] == ["aqr"]

    def test_an_unreadable_timestamp_is_ignored_rather_than_trusted(self, tmp_path, monkeypatch):
        # Fail quiet, not loud: this is reporting bolted onto a health check.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {"aqr": {"last_article_count": 0, "last_inspected_at": "??"}})
        assert gfh.pipeline_zero_fetches(f) == []

    def test_load_sources_failing_does_not_break_the_check(self, tmp_path, monkeypatch):
        def boom():
            raise RuntimeError("config unreadable")
        monkeypatch.setattr(gfh, "load_sources", boom)
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 0, "last_inspected_at": self.FRESH()}})
        assert gfh.pipeline_zero_fetches(f) == []


class TestAFailedSendIsNotSilent:
    """If the email cannot go out, the run must not report success.

    send_email returns False on missing SMTP config and on any SMTP exception,
    and its return value was discarded. The exit code deliberately excludes
    zero_fetches, so cron-wrapper saw 0 — a detected zero whose email failed
    reached nobody at all, which is the failure this whole feature exists to
    stop, one layer further out.
    """

    def test_exit_code_reports_a_failed_send(self, tmp_path, monkeypatch, capsys):
        w = TestTheWiringItself()
        state = tmp_path / "inspection_state.json"
        state.write_text(json.dumps(w.ZERO, ensure_ascii=False))
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", state)
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "health.json")
        monkeypatch.setattr(gfh, "load_sources", lambda: [w.SOURCE])
        monkeypatch.setattr(gfh, "probe_source", lambda src: dict(w.OK_PROBE))
        monkeypatch.setattr(gfh, "send_email", lambda body, subject: False)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
        assert gfh.main() == 1, (
            "the email failed to send and the run still exited 0 — cron-wrapper "
            "would report the night as healthy")


class TestPipelineNotRunningIsItsOwnAlert:
    """Falling silent because nothing was recorded is the same bug, inverted.

    The freshness filter drops records the last run did not refresh. That is
    right for a retired source and wrong for a dead pipeline: if gmia-daily
    stops running, every zero ages out and this check goes quiet exactly when
    it should be loudest. A source that fetched 0 and was then never fetched
    again would alert for one more night and then never again.

    The compensating monitor does not reach: gmia_liveness_audit.check_fetch
    is whole-pipeline (newest article across all sources) with a 4-day
    threshold, and 4 days is already the largest gap observed in this repo's
    fetch history, so it cannot be tightened. That leaves a window where
    neither channel speaks. "Nothing was recorded" is therefore its own alert.
    """

    def _state(self, tmp_path, payload):
        f = tmp_path / "inspection_state.json"
        f.write_text(json.dumps(payload, ensure_ascii=False))
        return f

    def test_a_normal_night_is_not_an_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}, {"id": "gmo"}])
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 10, "last_inspected_at": _fresh()},
            "gmo": {"last_article_count": 5, "last_inspected_at": _fresh()}})
        assert gfh.pipeline_did_not_run(f) is False

    def test_nothing_refreshed_is_an_alert(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}, {"id": "gmo"}])
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 10, "last_inspected_at": _hours_ago(50)},
            "gmo": {"last_article_count": 5, "last_inspected_at": _hours_ago(50)}})
        assert gfh.pipeline_did_not_run(f) is True

    def test_one_fresh_source_is_enough_to_stay_quiet(self, tmp_path, monkeypatch):
        # A single source skipped is the zero-fetch check's job, not this one.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}, {"id": "gmo"}])
        f = self._state(tmp_path, {
            "aqr": {"last_article_count": 10, "last_inspected_at": _fresh()},
            "gmo": {"last_article_count": 5, "last_inspected_at": _hours_ago(50)}})
        assert gfh.pipeline_did_not_run(f) is False

    def test_a_missing_state_file_is_an_alert(self, tmp_path, monkeypatch):
        # In an established deployment the file is written every night; its
        # absence means the whole zero-fetch feature is dead, which is exactly
        # the silence this class exists to prevent.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        assert gfh.pipeline_did_not_run(tmp_path / "nope.json") is True

    def test_it_is_a_send_condition_on_its_own(self):
        assert gfh.should_email(NO_ALERTS, [], pipeline_stale=True) is True
        assert gfh.should_email(NO_ALERTS, [], pipeline_stale=False) is False

    def test_the_subject_says_so(self):
        subject = gfh.alerts_subject(NO_ALERTS, [], pipeline_stale=True)
        assert "all OK" not in subject, subject
        assert "pipeline" in subject.lower()

    def test_main_sends_when_nothing_was_recorded(self, tmp_path, monkeypatch, capsys):
        w = TestTheWiringItself()
        f = self._state(tmp_path, {
            "acadian-asset": {"last_article_count": 10, "last_inspected_at": _hours_ago(50)}})
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", f)
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "health.json")
        monkeypatch.setattr(gfh, "load_sources", lambda: [w.SOURCE])
        monkeypatch.setattr(gfh, "probe_source", lambda src: dict(w.OK_PROBE))
        sent = {}
        monkeypatch.setattr(gfh, "send_email",
                            lambda body, subject: sent.update(body=body, subject=subject) or True)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
        gfh.main()
        assert sent, "every probe passed and the pipeline had not run — nothing was sent"
        assert "pipeline" in sent["subject"].lower()


class TestTheDeadPipelineAlertIsNotWhisperedEither:
    """The body must say what the subject shouts.

    8dee473's entire thesis was that a detected problem must not be whispered
    at the last hop -- it fixed exactly that for zero_fetches, where the email
    arrived titled "all OK". ab3f44b then added pipeline_stale as a send
    condition and did not carry the lesson across: when it is the ONLY
    condition (the case the feature exists for), the subject reads
    "⛔ pipeline recorded nothing in 36h" and the body contains nothing but a
    HEALTHY table. alerts_subject's own docstring makes the argument; it simply
    was not applied to render_html_email.
    """

    def test_the_html_body_says_the_pipeline_recorded_nothing(self):
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=[],
                                     pipeline_stale=True)
        assert "PIPELINE RECORDED NOTHING" in html, (
            "the subject shouts and the body says everything is fine")

    def test_the_body_stays_quiet_on_a_normal_night(self):
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=[],
                                     pipeline_stale=False)
        assert "PIPELINE RECORDED NOTHING" not in html

    def test_main_puts_it_in_the_body_not_just_the_subject(self, tmp_path, monkeypatch, capsys):
        w = TestTheWiringItself()
        f = tmp_path / "inspection_state.json"
        f.write_text(json.dumps(
            {"acadian-asset": {"last_article_count": 10,
                               "last_inspected_at": _hours_ago(50)}}, ensure_ascii=False))
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", f)
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "health.json")
        monkeypatch.setattr(gfh, "load_sources", lambda: [w.SOURCE])
        monkeypatch.setattr(gfh, "probe_source", lambda src: dict(w.OK_PROBE))
        sent = {}
        monkeypatch.setattr(gfh, "send_email",
                            lambda body, subject: sent.update(body=body, subject=subject) or True)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
        gfh.main()
        assert "PIPELINE RECORDED NOTHING" in sent["body"], (
            f"subject said {sent['subject']!r} but the body never mentions it")


class TestDeadPipelineDetectionIsFullyPinned:
    """Four guarantees that a mutation audit found unpinned."""

    def _state(self, tmp_path, payload):
        tmp_path.mkdir(parents=True, exist_ok=True)
        f = tmp_path / "inspection_state.json"
        f.write_text(json.dumps(payload, ensure_ascii=False))
        return f

    def test_a_garbage_timestamp_does_not_silence_the_alert(self, tmp_path, monkeypatch):
        # The mirror of test_an_unreadable_timestamp_is_ignored_rather_than_trusted:
        # there, an unreadable stamp must not raise a zero alert; here it must
        # not COUNT AS FRESH and cancel the dead-pipeline alert.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {"aqr": {"last_article_count": 10,
                                           "last_inspected_at": "not-a-date"}})
        assert gfh.pipeline_did_not_run(f) is True

    def test_a_fresh_record_for_an_unconfigured_source_does_not_count(self, tmp_path, monkeypatch):
        # "no CONFIGURED source refreshed in 36h" -- a leftover row for a
        # retired source must not stand in for a live one.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        f = self._state(tmp_path, {
            "pgim": {"last_article_count": 8, "last_inspected_at": _fresh()},
            "aqr": {"last_article_count": 10, "last_inspected_at": _hours_ago(50)}})
        assert gfh.pipeline_did_not_run(f) is True

    def test_the_console_says_so_too(self, tmp_path, monkeypatch, capsys):
        w = TestTheWiringItself()
        f = self._state(tmp_path, {"acadian-asset": {"last_article_count": 10,
                                                     "last_inspected_at": _hours_ago(50)}})
        monkeypatch.setattr(gfh, "INSPECTION_STATE_FILE", f)
        monkeypatch.setattr(gfh, "STATE_FILE", tmp_path / "health.json")
        monkeypatch.setattr(gfh, "load_sources", lambda: [w.SOURCE])
        monkeypatch.setattr(gfh, "probe_source", lambda src: dict(w.OK_PROBE))
        monkeypatch.setattr(gfh, "send_email", lambda body, subject: True)
        monkeypatch.setattr(sys, "argv", ["gmia-fetcher-health.py", "--email"])
        gfh.main()
        assert "recorded nothing" in capsys.readouterr().out

    def test_the_freshness_window_is_pinned_at_both_ends(self, tmp_path, monkeypatch):
        # The window was only bracketed from above: 36 -> 360 was caught, but
        # 36 -> 1 was not, so the documented value could drift anywhere in
        # (1h, 50h] undetected. A record from the last run (0.75h) must be
        # fresh, and one from two missed nights (48.75h) must not.
        monkeypatch.setattr(gfh, "load_sources", lambda: [{"id": "aqr"}])
        just_ran = self._state(tmp_path / "a", {"aqr": {"last_article_count": 10,
                                                        "last_inspected_at": _hours_ago(0.75)}})
        one_missed = self._state(tmp_path / "b", {"aqr": {"last_article_count": 10,
                                                          "last_inspected_at": _hours_ago(24.75)}})
        two_missed = self._state(tmp_path / "c", {"aqr": {"last_article_count": 10,
                                                          "last_inspected_at": _hours_ago(48.75)}})
        assert gfh.pipeline_did_not_run(just_ran) is False
        assert gfh.pipeline_did_not_run(one_missed) is False, "36h must survive one missed night"
        assert gfh.pipeline_did_not_run(two_missed) is True, "36h must not survive two"
