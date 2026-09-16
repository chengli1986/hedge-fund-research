"""Weekly content audit: re-fetch stored articles and catch silent drift (2026-09-15).

Every source problem found this week was found by opening pages one source at
a time; man.com's new role gate surfaced only because a test email happened
to run the health probe that day. scripts/content_audit.py re-fetches a few
already-stored articles per source with today's fetchers (into a temporary
directory -- production content is never touched) and compares them with
what is stored.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

import fetch_articles as fa

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("content_audit", REPO / "scripts" / "content_audit.py")
ca = importlib.util.module_from_spec(spec)
sys.modules["content_audit"] = ca
spec.loader.exec_module(ca)

BODY = ("Private markets remained active through the first half of 2026, supported by strong "
        "origination and disciplined underwriting across infrastructure and asset-based finance. ") * 30


class TestCompare:
    def test_the_same_body_is_clean(self):
        assert ca.compare_bodies(BODY, BODY) == []

    def test_whitespace_and_word_fusion_differences_are_not_drift(self):
        """Bodies stored before c149894 have fused words; today's have spaces."""
        # Worst case: every space gone (the first version fused one phrase,
        # too little to tell a normalising comparison from a raw one).
        fused = BODY.replace(" ", "")
        assert ca.compare_bodies(fused, BODY) == []

    def test_a_much_shorter_body_is_flagged(self):
        assert "body_shrunk" in ca.compare_bodies(BODY, BODY[: len(BODY) // 3])

    def test_a_much_longer_body_is_flagged(self):
        assert "body_grew" in ca.compare_bodies(BODY, BODY * 3)

    def test_different_text_is_flagged(self):
        other = ("Cookie preferences. Accept all. Manage settings. Privacy policy. Terms of use. " * 60)
        assert "content_changed" in ca.compare_bodies(BODY, other)

    def test_a_small_edit_is_clean(self):
        edited = BODY.replace("disciplined underwriting", "careful underwriting", 3)
        assert ca.compare_bodies(BODY, edited) == []


def _row(i, source="aqr", status="ok", date="2026-09-0", url=None, **kw):
    url = url or f"https://www.aqr.com/insights/{i}"
    return dict({"id": fa.article_id(source, url), "source_id": source, "url": url, "title": f"T{i}",
                 "date": f"{date}{i}", "content_status": status}, **kw)


class TestSample:
    def test_most_recent_ok_rows_with_bodies(self, tmp_path):
        rows = [_row(i) for i in range(1, 6)] + [_row(9, status="permafail")]
        for r in rows:
            (tmp_path / f"{r['id']}.txt").write_text(BODY)
        got = ca.sample_articles(rows, {"aqr"}, per_source=3, content_dir=tmp_path)
        assert [r["title"] for r in got] == ["T5", "T4", "T3"]

    def test_reused_url_rows_are_skipped(self, tmp_path):
        url = "https://www.franklintempletonglobal.com/articles/series/allocation-views"
        first = _row(1, source="franklin-templeton", url=url)
        issue = dict(_row(2, source="franklin-templeton", url=url),
                     id=fa.article_id("franklin-templeton", url, issue_date="2026-09-02"))
        plain = _row(3, source="franklin-templeton")
        for r in (first, issue, plain):
            (tmp_path / f"{r['id']}.txt").write_text(BODY)
        got = ca.sample_articles([first, issue, plain], {"franklin-templeton"}, per_source=3, content_dir=tmp_path)
        assert [r["title"] for r in got] == ["T3"]

    def test_rows_without_a_stored_body_or_unconfigured_are_skipped(self, tmp_path):
        rows = [_row(1), _row(2, source="retired")]
        assert ca.sample_articles(rows, {"aqr"}, per_source=3, content_dir=tmp_path) == []

    def test_articles_the_analysis_already_declined_are_skipped(self, tmp_path):
        """gmo's video page (stored body = an HR tax form) was flagged every
        week although it is already declined as wrong_document."""
        rows = [_row(1), _row(2, analysis_status="insufficient_content", analysis_label="wrong_document")]
        for r in rows:
            (tmp_path / f"{r['id']}.txt").write_text(BODY)
        assert [r["title"] for r in ca.sample_articles(rows, {"aqr"}, 3, content_dir=tmp_path)] == ["T1"]


class TestAuditArticle:
    def _stored(self, tmp_path):
        r = _row(1)
        (tmp_path / f"{r['id']}.txt").write_text(BODY)
        return r

    def test_an_unchanged_article_has_no_findings(self, tmp_path):
        r = self._stored(tmp_path)

        def fetcher(a):
            import fetch_content as fc
            p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(BODY)
            fc._extraction_paths.append("primary")
            return (p, "ok")

        result = ca.audit_article(r, fetcher, stored_dir=tmp_path)
        assert result["findings"] == []

    def test_a_fetch_now_off_primary_is_extraction_drift(self, tmp_path):
        r = self._stored(tmp_path)

        def fetcher(a):
            import fetch_content as fc
            p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(BODY)
            fc._extraction_paths.append("fallback:main")
            return (p, "ok")

        assert "extraction_drift" in ca.audit_article(r, fetcher, stored_dir=tmp_path)["findings"]

    def test_a_fetch_that_now_fails_carries_its_label(self, tmp_path):
        r = self._stored(tmp_path)

        def fetcher(a):
            import fetch_content as fc
            fc.note_failure_hint("selector_miss", "gone")
            return None

        result = ca.audit_article(r, fetcher, stored_dir=tmp_path)
        assert result["findings"] == ["now_failing"] and result["label"] == "selector_miss"

    def test_a_deleted_page_is_recorded_but_not_an_alert(self, tmp_path):
        r = self._stored(tmp_path)

        def fetcher(a):
            import fetch_content as fc
            fc.note_failure_hint("page_gone", "404")
            return None

        result = ca.audit_article(r, fetcher, stored_dir=tmp_path)
        assert result["findings"] == ["page_gone"]
        assert not ca.is_alert(result)

    CHROME = "Skip to main content. Insights. Careers. Contact. ${ numberSection }${ text } " * 120

    def _fetch(self, text, path="primary"):
        def fetcher(a):
            import fetch_content as fc
            p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(text)
            fc._extraction_paths.append(path)
            return (p, "ok")
        return fetcher

    def test_a_trimmed_body_without_a_baseline_still_alerts_and_says_it_is_contained(self, tmp_path):
        """aqr/troweprice 2026-09-15: bodies stored before the selector fixes
        carried page chrome. Text alone cannot tell that from a selector now
        catching half the article, so it stays an alert -- with the evidence."""
        r = _row(1)
        (tmp_path / f"{r['id']}.txt").write_text(self.CHROME + BODY)
        base = tmp_path / "baseline"
        result = ca.audit_article(r, self._fetch(BODY), stored_dir=tmp_path, baseline_dir=base)
        assert "body_shrunk" in result["findings"] and ca.is_alert(result)
        assert result["compared_with"] == "stored" and result["new_in_old"] >= 0.99

    def test_an_accepted_baseline_replaces_the_stored_body(self, tmp_path):
        r = _row(1)
        (tmp_path / f"{r['id']}.txt").write_text(self.CHROME + BODY)
        base = tmp_path / "baseline"; base.mkdir()
        (base / f"{r['id']}.txt").write_text(BODY)
        result = ca.audit_article(r, self._fetch(BODY), stored_dir=tmp_path, baseline_dir=base)
        assert result["findings"] == [] and result["compared_with"] == "baseline"
        assert result["stored_chars"] == len(BODY)

    def test_a_shrink_against_the_baseline_still_alerts(self, tmp_path):
        r = _row(1)
        (tmp_path / f"{r['id']}.txt").write_text(BODY)
        base = tmp_path / "baseline"; base.mkdir()
        (base / f"{r['id']}.txt").write_text(BODY)
        result = ca.audit_article(r, self._fetch(BODY[: len(BODY) // 3]), stored_dir=tmp_path, baseline_dir=base)
        assert "body_shrunk" in result["findings"]

    def test_accept_saves_todays_primary_fetch_as_the_baseline(self, tmp_path):
        r = _row(1)
        base = tmp_path / "baseline"
        ok, why = ca.accept_article(r, self._fetch(BODY), baseline_dir=base)
        assert ok, why
        assert (base / f"{r['id']}.txt").read_text() == BODY

    @pytest.mark.parametrize("fetch", ["failed", "off_primary", "metadata_only"])
    def test_accept_refuses_a_fetch_that_is_not_a_clean_primary_body(self, tmp_path, fetch):
        r = _row(1)
        base = tmp_path / "baseline"
        if fetch == "failed":
            fetcher = lambda a: None
        elif fetch == "off_primary":
            fetcher = self._fetch(BODY, path="fallback:main")
        else:
            def fetcher(a):
                import fetch_content as fc
                p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(BODY)
                fc._extraction_paths.append("primary")
                return (p, "metadata_only")
        ok, why = ca.accept_article(r, fetcher, baseline_dir=base)
        assert not ok and why
        assert not (base / f"{r['id']}.txt").exists()

    def test_a_document_the_site_refuses_is_recorded_not_alerted(self, tmp_path):
        """blue-owl 2026-09-16: one withdrawn article would otherwise be an
        alert every week for as long as it stays in the sample."""
        r = self._stored(tmp_path)

        def fetcher(a):
            import fetch_content as fc
            fc.note_failure_hint("access_denied", "HTTP 403 (site's own page)")
            return None

        result = ca.audit_article(r, fetcher, stored_dir=tmp_path)
        assert result["findings"] == ["access_denied"] and not ca.is_alert(result)

    def test_the_audit_never_writes_production_content(self, tmp_path):
        import fetch_content as fc
        r = self._stored(tmp_path)
        before = fc.CONTENT_DIR

        seen = []

        def fetcher(a):
            # Recorded, not asserted here: fetch_with_evidence turns an
            # exception inside the fetcher into a failed fetch, which would
            # hide the assertion (the first version of this test did).
            seen.append(fc.CONTENT_DIR)
            p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(BODY)
            return (p, "ok")

        ca.audit_article(r, fetcher, stored_dir=tmp_path)
        assert seen and seen[0] != before, "the fetch ran in the production content directory"
        assert fc.CONTENT_DIR == before


class TestSiteWideLoss:
    """One withdrawn document is housekeeping; two at once from the same site
    is the shape of a new gate (man.com added a role wall on 2026-09-15 and
    every article went at once), so it has to reach a human.
    """
    def _r(self, source, finding, i=0):
        return {"source_id": source, "id": f"{source}{i}", "title": "T", "url": "u",
                "findings": [finding], "label": finding}

    def test_two_lost_documents_from_one_source_become_an_alert(self):
        results = [self._r("man-group", "access_denied", 1), self._r("man-group", "access_denied", 2)]
        ca.mark_site_wide_losses(results)
        assert all(ca.is_alert(r) for r in results)
        assert all("site_wide_access_loss" in r["findings"] for r in results)

    def test_a_deleted_page_counts_towards_the_same_signal(self):
        results = [self._r("man-group", "access_denied", 1), self._r("man-group", "page_gone", 2)]
        ca.mark_site_wide_losses(results)
        assert all(ca.is_alert(r) for r in results)

    def test_one_lost_document_stays_informational(self):
        results = [self._r("blue-owl-capital", "access_denied"), self._r("aqr", "page_gone")]
        ca.mark_site_wide_losses(results)
        assert not any(ca.is_alert(r) for r in results)

    def test_losses_at_different_sources_do_not_add_up(self):
        results = [self._r("aqr", "access_denied"), self._r("gmo", "access_denied")]
        ca.mark_site_wide_losses(results)
        assert not any(ca.is_alert(r) for r in results)

    def test_the_run_marks_them(self, monkeypatch, tmp_path):
        """run_audit must call it -- a helper nothing calls is not a signal."""
        import fetch_content as fc
        rows = [_row(1, source="man-group", url="https://www.man.com/a"),
                _row(2, source="man-group", url="https://www.man.com/b")]
        content = tmp_path / "content"
        content.mkdir()
        for r in rows:
            (content / f"{r['id']}.txt").write_text(BODY)
        data = tmp_path / "articles.jsonl"
        data.write_text("\n".join(json.dumps(r) for r in rows))
        monkeypatch.setattr(ca, "DATA_FILE", data)
        monkeypatch.setattr(ca, "SOURCES_FILE", tmp_path / "sources.json")
        (tmp_path / "sources.json").write_text(json.dumps({"sources": [{"id": "man-group"}]}))
        monkeypatch.setattr(ca, "BASE_DIR", tmp_path)
        monkeypatch.setattr(fc, "CONTENT_DIR", content)

        def fetcher_for(article):
            def fetcher(a):
                fc.note_failure_hint("access_denied", "HTTP 403 (site's own page)")
                return None
            return fetcher

        monkeypatch.setattr(fc, "content_fetcher_for", fetcher_for)
        results, _ = ca.run_audit(per_source=2)
        assert len(results) == 2 and all(ca.is_alert(r) for r in results)


class TestMarkGone:
    """--mark-gone stamps url_status on rows whose original the publisher took
    down, so the dashboard can say so. It is human-triggered and evidence-gated:
    the page is fetched first and only page_gone / access_denied is accepted,
    because a stamp on a live article would put a false "removed" on the page.
    """
    def _data(self, tmp_path, rows):
        f = tmp_path / "articles.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return f

    def _fetcher(self, hint):
        def factory(article):
            def fetcher(a):
                import fetch_content as fc
                if hint:
                    fc.note_failure_hint(hint, "checked")
                    return None
                p = fc.CONTENT_DIR / f"{a['id']}.txt"; p.write_text(BODY)
                fc._extraction_paths.append("primary")
                return (p, "ok")
            return fetcher
        return factory

    def _run(self, tmp_path, monkeypatch, hint, ids=None, capsys=None):
        import fetch_content as fc
        row = _row(1, source="research-affiliates", url="https://www.syzygyassetmanagement.com/x")
        data = self._data(tmp_path, [row])
        monkeypatch.setattr(ca, "DATA_FILE", data)
        monkeypatch.setattr(fc, "content_fetcher_for", self._fetcher(hint))
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--mark-gone", *(ids or [row["id"]])])
        rc = ca.main()
        return rc, json.loads(data.read_text().splitlines()[0])

    @pytest.mark.parametrize("hint", ["page_gone", "access_denied"])
    def test_a_page_the_publisher_removed_is_stamped(self, tmp_path, monkeypatch, hint):
        rc, row = self._run(tmp_path, monkeypatch, hint)
        assert rc == 0 and row["url_status"] == "gone"
        assert row["url_checked_at"] and row["url_note"].startswith(hint)

    def test_a_live_article_is_refused_for_being_alive(self, tmp_path, monkeypatch, capsys):
        """Refused by the first gate, and it must say so: "the page still
        serves an article" is a different thing to check than a label that
        happens not to be a takedown."""
        rc, row = self._run(tmp_path, monkeypatch, None)
        assert rc == 1 and "url_status" not in row
        assert "still serves an article" in capsys.readouterr().out

    def test_a_transient_failure_is_refused(self, tmp_path, monkeypatch):
        """A timeout is not a takedown."""
        rc, row = self._run(tmp_path, monkeypatch, "fetch_error")
        assert rc == 1 and "url_status" not in row

    def test_an_unknown_id_is_refused_and_writes_nothing(self, tmp_path, monkeypatch):
        rc, row = self._run(tmp_path, monkeypatch, "page_gone", ids=["nope"])
        assert rc == 1 and "url_status" not in row

    def test_the_other_rows_are_left_byte_identical(self, tmp_path, monkeypatch):
        import fetch_content as fc
        rows = [_row(1, source="research-affiliates", url="https://www.syzygyassetmanagement.com/x"),
                _row(2, source="aqr")]
        data = self._data(tmp_path, rows)
        before = data.read_text().splitlines()[1]
        monkeypatch.setattr(ca, "DATA_FILE", data)
        monkeypatch.setattr(fc, "content_fetcher_for", self._fetcher("page_gone"))
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--mark-gone", rows[0]["id"]])
        assert ca.main() == 0
        assert data.read_text().splitlines()[1] == before


class TestReportAndEmail:
    RESULTS = [
        {"source_id": "aqr", "title": "A", "url": "u1", "findings": [], "label": None},
        {"source_id": "man-group", "title": "B", "url": "u2", "findings": ["now_failing"], "label": "selector_miss",
         "detail": "no cards"},
        {"source_id": "metlife-im", "title": "C", "url": "u3", "findings": ["page_gone"], "label": "page_gone"},
        {"source_id": "gmo", "title": "D", "url": "u4", "findings": ["body_grew"], "label": None},
    ]

    def test_alerts_exclude_deleted_pages_and_growth(self):
        assert [r["source_id"] for r in self.RESULTS if ca.is_alert(r)] == ["man-group"]

    def test_email_lists_alerts_by_source_with_descriptions(self):
        html = ca.render_email(self.RESULTS, sources_audited=4)
        assert "每周正文体检" in html and "man-group" in html and "selector_miss" in html
        assert "metlife-im" in html                      # recorded, in the informational part
        assert "<script>" not in ca.render_email(
            [dict(self.RESULTS[1], title="<script>x</script>")], sources_audited=1)

    def test_main_writes_a_report_and_sends_only_with_alerts(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(ca, "run_audit", lambda per_source: (self.RESULTS, 4))
        monkeypatch.setattr(ca, "send_email", lambda html, subject, to=None: sent.append((subject, to)) or True)
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--email", "--report-dir", str(tmp_path)])
        assert ca.main() == 0
        report = json.loads(next(tmp_path.glob("content-audit-*.json")).read_text())
        assert report["alerts"] == 1 and len(report["results"]) == 4
        assert len(sent) == 1 and "1" in sent[0][0]

    def test_no_alerts_no_email(self, tmp_path, monkeypatch):
        sent = []
        clean = [dict(r, findings=[]) for r in self.RESULTS]
        monkeypatch.setattr(ca, "run_audit", lambda per_source: (clean, 4))
        monkeypatch.setattr(ca, "send_email", lambda *a, **k: sent.append(a) or True)
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--email", "--report-dir", str(tmp_path)])
        ca.main()
        assert sent == []

    def test_email_shows_how_much_of_a_shrunk_body_was_inside_the_old_one(self):
        r = {"source_id": "aqr", "title": "W", "url": "u", "findings": ["body_shrunk"], "label": None,
             "stored_chars": 5043, "new_chars": 1392, "new_in_old": 1.0, "compared_with": "stored", "id": "d29e"}
        html = ca.render_email([r], sources_audited=1)
        assert "100%" in html and "--accept d29e" in html
        partial = ca.render_email([dict(r, new_in_old=0.6)], sources_audited=1)
        assert "60%" in partial and "--accept" not in partial   # half the text is new: not chrome removal
        accepted = ca.render_email([dict(r, compared_with="baseline")], sources_audited=1)
        assert "已确认的基线" in accepted and "--accept" not in accepted

    def test_accept_cli_saves_baselines_and_fails_if_any_is_refused(self, tmp_path, monkeypatch):
        rows = [_row(1), _row(2)]
        data = tmp_path / "articles.jsonl"
        data.write_text("\n".join(json.dumps(r) for r in rows))
        monkeypatch.setattr(ca, "DATA_FILE", data)
        calls = []
        monkeypatch.setattr(ca, "accept_article",
                            lambda a, fetcher, baseline_dir=None: calls.append(a["id"]) or (a["title"] == "T1", "x"))
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--accept", rows[0]["id"], rows[1]["id"]])
        assert ca.main() == 1                            # T2 refused
        assert calls == [rows[0]["id"], rows[1]["id"]]
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--accept", rows[0]["id"], "nope"])
        assert ca.main() == 1                            # unknown id
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--accept", rows[0]["id"]])
        assert ca.main() == 0

    def test_test_email_always_sends_to_that_address(self, tmp_path, monkeypatch):
        sent = []
        clean = [dict(r, findings=[]) for r in self.RESULTS]
        monkeypatch.setattr(ca, "run_audit", lambda per_source: (clean, 4))
        monkeypatch.setattr(ca, "send_email", lambda html, subject, to=None: sent.append((subject, to)) or True)
        monkeypatch.setattr(sys, "argv", ["content_audit.py", "--test-email", "x@example.com", "--report-dir", str(tmp_path)])
        ca.main()
        assert sent and sent[0][1] == "x@example.com" and sent[0][0].startswith("[测试]")
