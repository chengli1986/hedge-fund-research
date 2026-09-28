"""The catch-all label must say "we could not tell", not assert a cause.

Audit D4. `body_too_short` means "the rule matched but the text was too
short", and it is also what classify_content_failure returns when nothing
matched. In the whole recorded ledger it has only ever been the second
thing: every stored instance borrowed another message ("GMO: no article PDF
on page", "Bridgewater: no article body found or page looks gated"). A
label that names a cause nobody established misleads the email, the ledger
and whoever reads them.

The retry policy is copied unchanged, deliberately. D1 showed that the
code-change requeue is the only mechanism that has ever recovered an
article: all seven rescues were permafails re-queued after a fetcher fix.
An `unknown` label missing from RETRY_POLICY would read as
code_dependent=False and silently close that path.
"""
import failure_labels as fl


def _ev(messages=(), responses=(), paths=(), hints=(), exc=None):
    return {"exception": exc, "messages": list(messages), "responses": list(responses),
            "extraction_paths": list(paths), "hints": list(hints)}


class TestTheCatchAll:
    def test_nothing_to_go_on_is_unknown(self):
        assert fl.classify_content_failure(_ev())[0] == "unknown"

    def test_a_borrowed_message_is_unknown_not_too_short(self):
        """The real ledger entries: a message about something else entirely."""
        label, detail = fl.classify_content_failure(
            _ev(["  GMO: no article PDF on page https://gmo/x"]))
        assert label == "unknown" and "no article PDF" in detail

    def test_a_measured_length_is_still_body_too_short(self):
        """When an extractor did measure, the specific label is the true one."""
        for msg in ("  Matthews Asia: extracted text too short (322 chars, min 500)",
                    "  Robeco: extracted text too short (0 chars)"):
            assert fl.classify_content_failure(_ev([msg]))[0] == "body_too_short"

    def test_the_more_specific_labels_are_unaffected(self):
        page = {"status": 404, "url": "u", "final_url": "u", "content_type": "text/html",
                "challenge": False, "links": 3, "media_player": False}
        assert fl.classify_content_failure(_ev(responses=[page]))[0] == "page_gone"


class TestTheNewLabelIsWiredEverywhere:
    def test_it_has_a_human_description(self):
        assert fl.CONTENT_FAILURE_LABELS.get("unknown")

    def test_it_has_a_retry_policy(self):
        """Missing here, is_content_pending reads code_dependent as false and
        a permafail is never re-queued after a fix -- the one path that works."""
        assert "unknown" in fl.RETRY_POLICY

    def test_its_policy_is_the_one_the_catch_all_had(self):
        assert fl.RETRY_POLICY["unknown"] == fl.RETRY_POLICY["body_too_short"]

    def test_it_is_code_dependent(self):
        assert fl.RETRY_POLICY["unknown"]["code_dependent"] is True


class TestTheRecoveryPathStaysOpen:
    def test_an_unknown_permafail_is_retried_when_its_fetcher_changes(self, monkeypatch):
        import fetch_content as fc
        article = {"id": "a1", "source_id": "gmo", "url": "https://gmo/x",
                   "content_status": "permafail",
                   "content_failure": {"label": "unknown", "code_version": "old-version"}}
        monkeypatch.setattr(fc, "code_version_for", lambda a: "new-version")
        assert fc.is_content_pending(article) is True

    def test_it_is_left_alone_while_the_code_is_the_same(self, monkeypatch):
        import fetch_content as fc
        article = {"id": "a1", "source_id": "gmo", "url": "https://gmo/x",
                   "content_status": "permafail",
                   "content_failure": {"label": "unknown", "code_version": "same"}}
        monkeypatch.setattr(fc, "code_version_for", lambda a: "same")
        assert fc.is_content_pending(article) is False


class TestBridgewaterGateSpeaks:
    """Audit A1, the one site where the discarded knowledge is not recovered.

    _extract_bridgewater_text runs a gate detector and then returns a bare
    None; the caller logs "no article body found **or** page looks gated"
    and the classifier, having nothing else, used to answer body_too_short.
    Only the strong markers hint: "disclaimer" and "privacy policy" appear
    in ordinary prose, and access_denied retires after two consecutive
    failures and is never re-queued by a code change, so a false positive
    costs more than the missed label saves.
    """

    def _kind(self, text):
        import fetch_content as fc
        return fc._bridgewater_gate_kind(text)

    def test_a_registration_wall_is_a_gate(self):
        for t in ("Subscribe to read the full research.",
                  "Please register to continue reading",
                  "Log in to read more from Bridgewater"):
            assert self._kind(t) == "gate", t

    def test_legal_wording_in_prose_is_not_a_gate(self):
        """Measured across the whole store: 104 of 1,548 bodies (7%) contain
        one of these phrases, median length 11,594 characters -- they are
        ordinary research notes that mention a disclaimer. Bridgewater's own
        19 stored articles contain none, and the single time one appeared it
        was this false positive."""
        for t in ("... see our privacy policy for details.",
                  "This material includes a disclaimer.",
                  "Manage cookies"):
            assert self._kind(t) == "", t

    def test_an_ordinary_article_is_not_a_gate(self):
        assert self._kind("Rates fell in September as growth cooled.") == ""

    def test_the_boolean_now_means_only_a_gate(self):
        import fetch_content as fc
        assert fc._looks_like_bridgewater_gate("Subscribe to read") is True
        assert fc._looks_like_bridgewater_gate("see our privacy policy") is False
        assert fc._looks_like_bridgewater_gate("Rates fell in September") is False

    def test_a_real_article_that_says_in_terms_of_use_cases_is_kept(self):
        """The live case, 2026-09-27: a 31,148-character Bridgewater note was
        dropped whole because it contains "In terms of use cases, investors
        can also..." and the matcher was looking for "terms of use"."""
        import fetch_content as fc
        body = ("Carbon pricing schemes cover about 30% of global emissions. "
                "In terms of use cases, investors can also apply this. ") * 60
        html = f"<html><body><div class='RichTextBody'>{body}</div></body></html>"
        got = fc._extract_bridgewater_text(html)
        assert got is not None and len(got) > 3000

    def test_a_gate_page_is_labelled_access_denied(self, monkeypatch):
        import fetch_content as fc
        import failure_labels as fl
        html = ("<html><body><div class='RichTextBody'>"
                "Subscribe to read the full research note." * 12 + "</div></body></html>")
        resp = type("R", (), {"status_code": 200, "text": html,
                              "headers": {"Content-Type": "text/html"},
                              "url": "https://www.bridgewater.com/a",
                              "raise_for_status": lambda self: None})()
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        result, evidence = fc.fetch_with_evidence(
            {"id": "bw1", "url": "https://www.bridgewater.com/a"},
            fc._fetch_content_bridgewater)
        assert result is None
        assert fl.classify_content_failure(evidence)[0] == "access_denied"

    def test_legal_wording_no_longer_refuses_the_page(self, tmp_path, monkeypatch):
        """It used to return None here; the text is kept now and stage 3
        decides. CONTENT_DIR is redirected because a successful extraction
        writes a file -- audit C3, and this test was the caller that forgot."""
        import fetch_content as fc
        monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
        html = ("<html><body><div class='RichTextBody'>"
                "Please see our privacy policy. " * 40 + "</div></body></html>")
        resp = type("R", (), {"status_code": 200, "text": html,
                              "headers": {"Content-Type": "text/html"},
                              "url": "https://www.bridgewater.com/b",
                              "raise_for_status": lambda self: None})()
        monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
        result, evidence = fc.fetch_with_evidence(
            {"id": "bw2", "url": "https://www.bridgewater.com/b"},
            fc._fetch_content_bridgewater)
        assert result is not None
        assert evidence["hints"] == []
