"""
Reliability test harness for the Website Audit Agent.

Goal: prove the system survives imperfect inputs and that components
chain together correctly. No real network, no real LLM calls — everything
is stubbed so the suite is fast, deterministic, and CI-safe.

Run with:
    python -m unittest tests/test_reliability.py -v

Or directly:
    python tests/test_reliability.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure project root is on the path regardless of where we run from
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Force a writable output dir for the duration of the tests
TEST_OUTPUT_DIR = tempfile.mkdtemp(prefix="audit_tests_")
os.environ.setdefault("OUTPUT_DIR", TEST_OUTPUT_DIR)
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("PAGESPEED_API_KEY", "")  # default off for tests
os.environ.setdefault("SMTP_PASSWORD", "")

import waa.config as config  # noqa: E402
config.OUTPUT_DIR = TEST_OUTPUT_DIR

import waa.discovery.scraper as scraper  # noqa: E402
import waa.analysis.analyzer as analyzer  # noqa: E402
import waa.core.output as output  # noqa: E402
import waa.outreach.sender as sender  # noqa: E402
import waa.analysis.conversion_audit as conversion_audit  # noqa: E402


# ---------------------------------------------------------------------------
# Sample HTML fixtures — diverse real-world shapes
# ---------------------------------------------------------------------------

HTML_GOOD_MEDSPA = """
<!DOCTYPE html>
<html><head>
  <title>Glow Medspa — Botox & Fillers in Scottsdale</title>
  <meta name="description" content="Premier medspa in Scottsdale offering Botox, fillers, laser, and facials.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="canonical" href="https://glowmedspa.example/">
  <script type="application/ld+json">{"@type":"MedicalBusiness"}</script>
</head><body>
  <header>
    <a href="tel:+15555551234">(555) 555-1234</a>
    <a class="cta primary" href="/book">Book Consultation</a>
  </header>
  <h1>Look Like You. Only More Rested.</h1>
  <p>Scottsdale's #1 rated medspa. 4.9 stars on Google. Established 2014.</p>
  <a href="/before-after">Before/After Gallery</a>
  <a href="/treatments">Botox · Fillers · Laser · Facials</a>
  <p>Financing available via Cherry. Board certified MD on staff.</p>
  <div class="testimonial">
    "After my first Botox treatment with Dr. Park, I felt 10 years younger.
     The staff explained every step." — Sarah M., Scottsdale
  </div>
  <form action="/book">
    <input name="name" type="text">
    <input name="email" type="email">
    <input name="phone" type="tel">
    <button type="submit">Request Consultation</button>
  </form>
  <a href="mailto:hello@glowmedspa.example">hello@glowmedspa.example</a>
  <footer>© 2026 Glow Medspa</footer>
</body></html>
"""

HTML_BAD_DENTIST = """
<html><body>
<div>welcome</div>
<img src="hero.png">
<img src="team.png">
<img src="office.png">
<p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>
<p>Coming soon: new patient portal.</p>
<p>Call us at 555-867-5309</p>
<form>
<input name="name"><input name="email"><input name="phone">
<input name="dob"><input name="insurance"><input name="ssn">
<input name="referral"><input name="reason">
</form>
<footer>© 2019 Smile Dental</footer>
</body></html>
"""

HTML_MINIMAL = "<html><body><p>hi</p></body></html>"
HTML_EMPTY = ""
HTML_NO_BODY = "<html><head><title>no body here</title></head></html>"
HTML_BROKEN = "<html><head><title>oops</title><body><div><p>unclosed"
HTML_NON_ASCII = """
<html><head><title>Café Élysée — Réservations</title></head>
<body><h1>Bienvenue à Paris</h1>
<p>Réservez votre table — ouvert 7j/7</p>
<a href="/menu">Voir le menu</a></body></html>
"""
HTML_GIANT = "<html><body>" + ("<p>filler</p>" * 5000) + "</body></html>"

# Edge: HTML with javascript-rendered content — almost nothing in source
HTML_SPA = """
<html><head><title>Loading...</title></head>
<body><div id="root"></div>
<script src="/static/js/main.js"></script></body></html>
"""


# ---------------------------------------------------------------------------
# 1. SCRAPER TESTS
# ---------------------------------------------------------------------------

class TestScraperRobustness(unittest.TestCase):

    def test_extract_seo_handles_minimal_html(self):
        seo = scraper.extract_seo_signals(HTML_MINIMAL, "https://example.com")
        self.assertIsNone(seo["title"])
        self.assertEqual(seo["title_length"], 0)
        self.assertIsNone(seo["meta_description"])
        self.assertEqual(seo["h1_count"], 0)
        self.assertEqual(seo["images_total"], 0)

    def test_extract_seo_handles_no_body(self):
        seo = scraper.extract_seo_signals(HTML_NO_BODY, "https://example.com")
        # Should not crash. word_count must be 0.
        self.assertEqual(seo["word_count"], 0)

    def test_extract_seo_handles_broken_html(self):
        # BeautifulSoup is lenient; this should parse without exception.
        seo = scraper.extract_seo_signals(HTML_BROKEN, "https://example.com")
        self.assertEqual(seo["title"], "oops")

    def test_extract_seo_handles_unicode(self):
        seo = scraper.extract_seo_signals(HTML_NON_ASCII, "https://example.fr")
        self.assertIn("Café", seo["title"] or "")
        self.assertEqual(seo["h1_count"], 1)

    def test_extract_seo_handles_giant_html(self):
        # Should not blow up on 5000 paragraphs.
        seo = scraper.extract_seo_signals(HTML_GIANT, "https://example.com")
        self.assertGreater(seo["word_count"], 1000)

    def test_extract_seo_full_signals(self):
        seo = scraper.extract_seo_signals(HTML_GOOD_MEDSPA, "https://glowmedspa.example/")
        self.assertIn("Glow Medspa", seo["title"])
        self.assertGreaterEqual(seo["h1_count"], 1)
        self.assertTrue(seo["has_viewport"])
        self.assertTrue(seo["has_schema"])
        self.assertTrue(seo["uses_https"])
        self.assertGreater(len(seo["ctas_found"]), 0)

    def test_extract_contact_emails_basic(self):
        emails = scraper.extract_contact_emails(HTML_GOOD_MEDSPA, "https://glowmedspa.example")
        self.assertIn("hello@glowmedspa.example", emails)

    def test_extract_contact_emails_filters_junk(self):
        html = """
        <html><body>
        <a href="mailto:noreply@x.com">x</a>
        <a href="mailto:support@example.com">y</a>
        <a href="mailto:owner@realbiz.com">z</a>
        <p>logo.png@cdn — definitely not an email</p>
        </body></html>
        """
        emails = scraper.extract_contact_emails(html, "https://realbiz.com")
        self.assertIn("owner@realbiz.com", emails)
        self.assertNotIn("noreply@x.com", emails)
        self.assertNotIn("support@example.com", emails)

    def test_extract_contact_emails_empty_html(self):
        emails = scraper.extract_contact_emails(HTML_EMPTY or "<html></html>", "https://x.com")
        self.assertEqual(emails, [])

    def test_detect_tech_stack_wordpress(self):
        html = '<html><head><meta name="generator" content="WordPress 6.0"></head><body>wp-content/themes</body></html>'
        tech = scraper.detect_tech_stack(html)
        self.assertEqual(tech["cms"], "WordPress")

    def test_detect_tech_stack_no_cms(self):
        tech = scraper.detect_tech_stack("<html><body>plain</body></html>")
        self.assertIsNone(tech["cms"])
        self.assertIsInstance(tech["technologies"], list)

    @patch("waa.discovery.scraper.requests.get")
    def test_fetch_html_handles_timeout(self, mock_get):
        import requests as _r
        mock_get.side_effect = _r.exceptions.Timeout("timed out")
        result = scraper.fetch_html("https://slow.example")
        self.assertIsNone(result["html"])
        self.assertIsNotNone(result["error"])

    @patch("waa.discovery.scraper.requests.get")
    def test_fetch_html_handles_404(self, mock_get):
        resp = MagicMock(status_code=404, text="not found", url="https://x.com/404")
        mock_get.return_value = resp
        result = scraper.fetch_html("https://x.com/404")
        self.assertEqual(result["status_code"], 404)
        self.assertIsNone(result["html"])
        self.assertIn("404", result["error"])

    @patch("waa.discovery.scraper.requests.get")
    def test_fetch_html_handles_ssl_fallback(self, mock_get):
        import requests as _r
        good = MagicMock(status_code=200, text="<html>ok</html>", url="https://x.com")
        # First call raises SSL, second succeeds
        mock_get.side_effect = [_r.exceptions.SSLError("bad cert"), good]
        result = scraper.fetch_html("https://x.com")
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["html"], "<html>ok</html>")

    @patch("waa.discovery.scraper.requests.get")
    def test_fetch_html_retries_then_gives_up(self, mock_get):
        import requests as _r
        mock_get.side_effect = _r.exceptions.ConnectionError("nope")
        # Speed up: monkey-patch the backoff to 0
        with patch.object(scraper.config, "RETRY_BACKOFF", 0):
            result = scraper.fetch_html("https://gone.example")
        self.assertIsNone(result["html"])
        self.assertIsNotNone(result["error"])
        # Should have retried up to MAX_RETRIES times
        self.assertGreaterEqual(mock_get.call_count, 1)


# ---------------------------------------------------------------------------
# 2. CONVERSION AUDIT TESTS
# ---------------------------------------------------------------------------

class TestConversionAuditRobustness(unittest.TestCase):

    def test_audit_good_medspa_finds_strong_signals(self):
        audit = conversion_audit.audit_conversion(
            HTML_GOOD_MEDSPA, "https://glowmedspa.example",
            niche="medspa", location="Scottsdale, AZ",
        )
        # Should detect H1, primary CTA, phone-clickable, niche elements
        self.assertIsNotNone(audit.above_fold.get("h1"))
        self.assertTrue(audit.above_fold.get("cta_visible_above_fold"))
        self.assertTrue(audit.local.get("has_phone_clickable"))
        self.assertTrue(audit.niche_check.get("checked"))
        # Most expected medspa elements should be present
        present = audit.niche_check.get("present", [])
        self.assertGreaterEqual(len(present), 3)
        # Should not crash on serialization
        d = audit.to_dict()
        self.assertEqual(d["url"], "https://glowmedspa.example")
        json.dumps(d)  # must be JSON-serializable

    def test_audit_bad_dentist_finds_problems(self):
        audit = conversion_audit.audit_conversion(
            HTML_BAD_DENTIST, "https://baddentist.example",
            niche="dentist", location="Miami, FL",
        )
        # Bad site = many findings
        labels = [f.label for f in audit.findings]
        # Must catch: no H1 (or weak), missing niche elements,
        # phone not clickable, lorem ipsum, outdated copyright
        self.assertGreater(len(audit.findings), 3)
        # Surprises detector should fire on lorem ipsum + 2019 copyright
        details = " ".join(f.detail for f in audit.findings)
        self.assertTrue(
            "Lorem" in details or "lorem" in details
            or "2019" in details
        )

    def test_audit_minimal_html_does_not_crash(self):
        audit = conversion_audit.audit_conversion(
            HTML_MINIMAL, "https://x.example", niche="", location=""
        )
        # Should return a valid object even with almost no content
        self.assertIsInstance(audit, conversion_audit.ConversionAudit)
        json.dumps(audit.to_dict())

    def test_audit_empty_html_does_not_crash(self):
        audit = conversion_audit.audit_conversion(
            "<html></html>", "https://x.example"
        )
        json.dumps(audit.to_dict())

    def test_audit_no_body_does_not_crash(self):
        audit = conversion_audit.audit_conversion(
            HTML_NO_BODY, "https://x.example"
        )
        json.dumps(audit.to_dict())

    def test_audit_unknown_niche(self):
        audit = conversion_audit.audit_conversion(
            HTML_GOOD_MEDSPA, "https://x.example", niche="space-station-architect"
        )
        self.assertFalse(audit.niche_check.get("checked"))

    def test_audit_unicode_input(self):
        audit = conversion_audit.audit_conversion(
            HTML_NON_ASCII, "https://example.fr", niche="restaurant", location="Paris"
        )
        self.assertIsNotNone(audit.above_fold.get("h1"))
        self.assertTrue(audit.local.get("city_appears_on_page"))

    def test_audit_giant_html_finishes_quickly(self):
        import time
        start = time.time()
        audit = conversion_audit.audit_conversion(
            HTML_GIANT, "https://big.example"
        )
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0, f"Audit on big HTML took {elapsed:.2f}s")
        self.assertIsInstance(audit.findings, list)

    def test_audit_spa_with_no_content(self):
        audit = conversion_audit.audit_conversion(
            HTML_SPA, "https://spa.example", niche="medspa"
        )
        # Should produce findings about missing H1, missing CTA, missing trust
        self.assertGreater(len(audit.findings), 2)

    def test_findings_are_serializable(self):
        audit = conversion_audit.audit_conversion(
            HTML_BAD_DENTIST, "https://x.example", niche="dentist"
        )
        for f in audit.findings:
            d = f.to_dict()
            self.assertIn("category", d)
            self.assertIn("label", d)
            self.assertIn("detail", d)
            self.assertIn("confidence", d)

    def test_high_confidence_filter(self):
        audit = conversion_audit.audit_conversion(
            HTML_BAD_DENTIST, "https://x.example", niche="dentist"
        )
        hi = audit.high_confidence_findings()
        for f in hi:
            self.assertEqual(f.confidence, "high")


# ---------------------------------------------------------------------------
# 3. ANALYZER + EMAIL GENERATION TESTS (with stubbed LLM)
# ---------------------------------------------------------------------------

ANALYSIS_VALID = json.dumps({
    "issues": [
        {"category": "Performance", "problem": "Site loads slowly on mobile",
         "severity": "high", "evidence": "LCP 5200ms"}
    ],
    "overall_impression": "Functional but slow",
    "lead_score": 7
})

EMAIL_VALID = json.dumps({
    "subject_line": "quick note about your site",
    "email_body": "Hi,\\nNoticed your homepage loads in 5+ seconds on mobile.\\nHappy to share a fix.\\nTomas",
    "follow_up_subject": "re: quick note",
    "follow_up_body": "Just bumping this — worth a look?\\nTomas"
})

EMAIL_TOO_LONG = json.dumps({
    "subject_line": "x",
    "email_body": "word " * 200,  # 200 words → triggers regenerate path
    "follow_up_subject": "f",
    "follow_up_body": "f"
})

EMAIL_WITH_NOISE = json.dumps({
    "subject_line": "x",
    "email_body": "Hi\\nFounder, EMTD Studio\\nNo strings attached.\\nTomas",
    "follow_up_subject": "f",
    "follow_up_body": "f"
})


class TestAnalyzerRobustness(unittest.TestCase):

    @patch.object(analyzer, "_call_llm", return_value=ANALYSIS_VALID)
    def test_analyze_returns_structured_dict(self, _m):
        result = analyzer.analyze_audit_data({"url": "https://x.com"})
        self.assertIn("issues", result)
        self.assertEqual(result["lead_score"], 7)

    @patch.object(analyzer, "_call_llm", return_value="not json {{")
    def test_analyze_handles_garbage_llm_response(self, _m):
        result = analyzer.analyze_audit_data({"url": "https://x.com"})
        # Should not raise — returns an error-shaped dict
        self.assertEqual(result["lead_score"], 0)
        self.assertEqual(result["issues"], [])

    @patch.object(analyzer, "_call_llm", return_value="```json\n" + ANALYSIS_VALID + "\n```")
    def test_analyze_strips_markdown_fences(self, _m):
        result = analyzer.analyze_audit_data({"url": "https://x.com"})
        self.assertEqual(result["lead_score"], 7)

    @patch.object(analyzer, "_call_llm", return_value=EMAIL_VALID)
    def test_generate_email_returns_subject_and_body(self, _m):
        result = analyzer.generate_email(
            "https://x.com", "X", {"issues": []},
            sender_name="Tomas",
        )
        self.assertIn("subject_line", result)
        self.assertIn("email_body", result)
        self.assertIn("Hi", result["email_body"])

    def test_generate_email_regenerates_when_too_long(self):
        # First call returns oversized email, second call returns OK
        with patch.object(analyzer, "_call_llm",
                          side_effect=[EMAIL_TOO_LONG, EMAIL_VALID]) as m:
            result = analyzer.generate_email(
                "https://x.com", "X", {"issues": []}, sender_name="Tomas",
            )
        self.assertEqual(m.call_count, 2)
        self.assertIn("Hi", result["email_body"])

    @patch.object(analyzer, "_call_llm", return_value=EMAIL_WITH_NOISE)
    def test_email_body_is_cleaned(self, _m):
        result = analyzer.generate_email(
            "https://x.com", "X", {"issues": []}, sender_name="Tomas",
        )
        body = result["email_body"]
        # "Founder, EMTD Studio" sign-off line should be stripped
        self.assertNotIn("EMTD Studio", body)
        # "No strings attached" phrase should be removed
        self.assertNotIn("No strings attached", body.replace("\\n", " "))
        self.assertNotIn("no strings attached", body.replace("\\n", " "))

    def test_clean_email_body_handles_empty(self):
        # Should not crash on empty or weird input
        out = analyzer._clean_email_body("", "Tomas")
        self.assertEqual(out, "")
        out = analyzer._clean_email_body("Hi\\nTomas", "Tomas")
        self.assertIn("Hi", out)

    def test_format_audit_for_llm_with_missing_data(self):
        # Empty audit: should not crash, returns text
        text = analyzer._format_audit_for_llm({})
        self.assertIsInstance(text, str)
        # With pagespeed unavailable
        text = analyzer._format_audit_for_llm({"url": "https://x", "pagespeed": None})
        self.assertIn("PageSpeed data not available", text)


# ---------------------------------------------------------------------------
# 4. OUTPUT LAYER TESTS
# ---------------------------------------------------------------------------

class TestOutputRobustness(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="audit_out_")
        self._old_dir = config.OUTPUT_DIR
        config.OUTPUT_DIR = self.tmpdir
        output.config = config

    def tearDown(self):
        config.OUTPUT_DIR = self._old_dir

    def test_save_json_handles_empty_list(self):
        path = output.save_json([], filename="empty.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            self.assertEqual(json.load(f), [])

    def test_save_csv_handles_empty_list(self):
        path = output.save_csv([], filename="empty.csv")
        # Empty input: file may exist empty or not be created — must not crash
        # (current implementation skips writing if no rows)
        # The path is still returned valid
        self.assertTrue(path.endswith("empty.csv"))

    def test_save_json_handles_unicode(self):
        results = [{"url": "https://café.fr", "analysis": {"overall_impression": "Très bien"}}]
        path = output.save_json(results, filename="uni.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data[0]["url"], "https://café.fr")

    def test_save_csv_handles_partial_records(self):
        results = [
            {"url": "https://a.com"},  # missing everything
            {"url": "https://b.com", "error": "fetch failed"},
            {"url": "https://c.com", "skipped_reason": "no_contact_email"},
            {
                "url": "https://d.com",
                "analysis": {"issues": [], "lead_score": 3,
                             "overall_impression": "ok"},
                "email": {"subject_line": "hi", "email_body": "hello"},
                "tech": {"cms": "WordPress"},
                "pagespeed": {"available": True,
                              "mobile": {"performance_score": 50, "seo_score": 80},
                              "desktop": {"performance_score": 70}},
            },
        ]
        path = output.save_csv(results, filename="mixed.csv")
        self.assertTrue(os.path.exists(path))
        # Must be importable as CSV
        import csv
        with open(path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        statuses = [r["status"] for r in rows]
        self.assertIn("error", statuses)
        self.assertIn("skipped", statuses)
        self.assertIn("ok", statuses)

    def test_save_csv_handles_pagespeed_with_error(self):
        results = [{
            "url": "https://x.com",
            "pagespeed": {"available": True,
                          "mobile": {"error": "HTTP 500"},
                          "desktop": None},
            "analysis": {"issues": [], "lead_score": 0,
                         "overall_impression": ""},
            "email": {},
        }]
        # Must not crash on partial pagespeed
        path = output.save_csv(results, filename="ps_err.csv")
        self.assertTrue(os.path.exists(path))

    def test_print_summary_handles_errors_silently(self):
        # Should never raise, even on weird shapes
        output.print_summary([
            {"url": "x", "error": "boom"},
            {"url": "y", "skipped_reason": "no_contact_email"},
            {"url": "z", "analysis": None, "email": None},
        ])


# ---------------------------------------------------------------------------
# 5. SENDER TESTS (no real SMTP)
# ---------------------------------------------------------------------------

class TestSenderRobustness(unittest.TestCase):

    def test_send_batch_dry_run_default(self):
        emails = [
            {"to": "a@x.com", "subject": "s1", "body": "hello"},
            {"to": "b@x.com", "subject": "s2", "body": "hello"},
        ]
        results = sender.send_batch(emails, dry_run=True, delay=0)
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertEqual(r["status"], "dry_run")

    def test_send_batch_skips_incomplete_entries(self):
        emails = [
            {"to": "a@x.com", "subject": "s1", "body": "hello"},
            {"to": "", "subject": "s2", "body": "hello"},     # missing recipient
            {"to": "c@x.com", "subject": "", "body": "hello"},  # missing subject
            {"to": "d@x.com", "subject": "s3", "body": ""},     # missing body
        ]
        results = sender.send_batch(emails, dry_run=True, delay=0)
        self.assertEqual(len(results), 1)

    def test_send_batch_real_without_password_returns_error(self):
        # No SMTP password in env → must return error shape, not crash
        with patch.object(sender, "ZOHO_PASSWORD", ""):
            results = sender.send_batch(
                [{"to": "a@x.com", "subject": "s", "body": "b"}],
                dry_run=False, delay=0,
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "error")

    @patch.object(sender, "_connect_smtp")
    def test_send_batch_handles_smtp_exception_per_email(self, mock_connect):
        import smtplib
        server = MagicMock()
        # First send works, second raises SMTPException
        server.sendmail.side_effect = [None, smtplib.SMTPRecipientsRefused({})]
        mock_connect.return_value = server
        with patch.object(sender, "ZOHO_PASSWORD", "fake"):
            results = sender.send_batch(
                [
                    {"to": "ok@x.com", "subject": "s", "body": "b"},
                    {"to": "bad@x.com", "subject": "s", "body": "b"},
                ],
                dry_run=False, delay=0,
            )
        statuses = [r["status"] for r in results]
        self.assertIn("sent", statuses)
        self.assertIn("error", statuses)

    def test_load_emails_from_audit_json(self):
        # Build a small audit json on disk
        path = os.path.join(TEST_OUTPUT_DIR, "tiny_audit.json")
        with open(path, "w") as f:
            json.dump([
                {"url": "https://a.com", "email": {"subject_line": "s", "email_body": "b"}},
                {"url": "https://b.com", "email": {"subject_line": "", "email_body": ""}},
                {"url": "https://c.com", "email": None},
            ], f)
        emails = sender.load_emails_from_audit_json(path)
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["website"], "https://a.com")


# ---------------------------------------------------------------------------
# 6. END-TO-END INTEGRATION (scraper → analyzer → audit → output)
# ---------------------------------------------------------------------------

class TestEndToEndPipeline(unittest.TestCase):
    """
    Exercise the full integration path with everything stubbed.
    Proves the modules wire together correctly.
    """

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    @patch.object(analyzer, "_call_llm")
    def test_full_pipeline_happy_path(self, mock_llm, mock_fetch, _ps):
        from waa.cli import process_single

        mock_fetch.return_value = {
            "url": "https://glowmedspa.example",
            "status_code": 200,
            "html": HTML_GOOD_MEDSPA,
            "error": None,
            "load_time_ms": 320,
            "final_url": "https://glowmedspa.example",
        }
        # First LLM call = analysis, second = email
        mock_llm.side_effect = [ANALYSIS_VALID, EMAIL_VALID]

        result = process_single(
            "https://glowmedspa.example", name="Glow Medspa",
            skip_pagespeed=True, sender_name="Tomas",
        )

        # Result must have all the layers wired
        self.assertIsNone(result.get("error"))
        self.assertIsNotNone(result.get("seo"))
        self.assertIsNotNone(result.get("tech"))
        self.assertIsNotNone(result.get("analysis"))
        self.assertIsNotNone(result.get("email"))
        self.assertIn("subject_line", result["email"])

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    def test_pipeline_aborts_cleanly_when_fetch_fails(self, mock_fetch, _ps):
        from waa.cli import process_single
        mock_fetch.return_value = {
            "url": "https://dead.example", "status_code": None, "html": None,
            "error": "ConnectTimeout", "load_time_ms": None,
        }
        result = process_single("https://dead.example", skip_pagespeed=True)
        # Returns a structured error, not a crash
        self.assertIsNotNone(result.get("error"))
        # Must not have called LLM — and email/analysis are not present
        self.assertNotIn("analysis", result)

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    def test_pipeline_skips_when_no_contact_email_required(self, mock_fetch, _ps):
        from waa.cli import process_single

        # Page has no email at all
        html_no_email = "<html><body><h1>Hi</h1></body></html>"
        mock_fetch.return_value = {
            "url": "https://noemail.example", "status_code": 200,
            "html": html_no_email, "error": None, "load_time_ms": 200,
            "final_url": "https://noemail.example",
        }
        # And the contact-page fallback also yields nothing
        with patch("waa.discovery.scraper.scrape_contact_page", return_value=[]):
            result = process_single(
                "https://noemail.example", skip_pagespeed=True,
                require_email=True,
            )
        self.assertEqual(result.get("skipped_reason"), "no_contact_email")
        self.assertIsNone(result.get("analysis"))
        self.assertIsNone(result.get("email"))

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    @patch.object(analyzer, "_call_llm")
    def test_pipeline_to_output_to_send_dry_run(self, mock_llm, mock_fetch, _ps):
        """
        End-to-end: scrape → analyze → save json → load json → dry-run send.
        Catches contract drift between analyzer output and sender input.
        """
        from waa.cli import process_single

        mock_fetch.return_value = {
            "url": "https://glowmedspa.example", "status_code": 200,
            "html": HTML_GOOD_MEDSPA, "error": None, "load_time_ms": 200,
            "final_url": "https://glowmedspa.example",
        }
        mock_llm.side_effect = [ANALYSIS_VALID, EMAIL_VALID]

        result = process_single(
            "https://glowmedspa.example", skip_pagespeed=True,
        )
        # Save → reload → send (dry run)
        json_path = output.save_json([result], filename="e2e.json")
        emails = sender.load_emails_from_audit_json(json_path)
        # Fill recipient (in production this comes from contacts CSV / scraped)
        for e in emails:
            e["to"] = "test@example.com"
        send_results = sender.send_batch(emails, dry_run=True, delay=0)
        self.assertEqual(len(send_results), 1)
        self.assertEqual(send_results[0]["status"], "dry_run")


# ---------------------------------------------------------------------------
# 7. CAMPAIGN PROGRESS + SENT REGISTRY + CSV LOADERS
# ---------------------------------------------------------------------------

class TestCampaignStateRobustness(unittest.TestCase):
    """
    These bits of state live on disk and survive across runs — corruption
    here can silently re-send to people we've already contacted, or lose
    progress mid-campaign. Worth testing.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="campaign_state_")
        # Re-import waa.cli as audit_agent with patched OUTPUT_DIR
        self._old_dir = config.OUTPUT_DIR
        config.OUTPUT_DIR = self.tmpdir
        # audit_agent computes module-level paths from config.OUTPUT_DIR,
        # so we must patch the module attributes directly.
        import waa.cli as audit_agent
        self.audit_agent = audit_agent
        self._old_progress = audit_agent.CAMPAIGN_PROGRESS_FILE
        self._old_registry = audit_agent.SENT_REGISTRY_FILE
        audit_agent.CAMPAIGN_PROGRESS_FILE = os.path.join(self.tmpdir, "campaign_progress.json")
        audit_agent.SENT_REGISTRY_FILE = os.path.join(self.tmpdir, "sent_registry.json")

    def tearDown(self):
        config.OUTPUT_DIR = self._old_dir
        self.audit_agent.CAMPAIGN_PROGRESS_FILE = self._old_progress
        self.audit_agent.SENT_REGISTRY_FILE = self._old_registry

    def test_load_progress_when_file_missing(self):
        """Fresh run — no file yet — must return baseline shape."""
        progress = self.audit_agent._load_campaign_progress()
        self.assertEqual(progress["completed"], [])
        self.assertEqual(progress["daily_logs"], {})

    def test_save_then_load_progress_roundtrip(self):
        progress = {"completed": ["dentist|Miami FL"], "daily_logs": {}}
        self.audit_agent._save_campaign_progress(progress)
        reloaded = self.audit_agent._load_campaign_progress()
        self.assertEqual(reloaded["completed"], ["dentist|Miami FL"])

    def test_load_registry_when_file_missing(self):
        registry = self.audit_agent._load_sent_registry()
        self.assertEqual(registry, {"emails": {}, "domains": {}})

    def test_already_contacted_by_email(self):
        registry = {"emails": {"a@x.com": {"website": "https://x.com",
                                            "sent_at": "2026-04-01T10:00:00"}},
                    "domains": {}}
        reason = self.audit_agent._already_contacted(registry, "a@x.com", "https://other.com")
        self.assertIsNotNone(reason)
        self.assertIn("a@x.com", reason)

    def test_already_contacted_by_domain(self):
        # Even if the email is different, same domain = skip
        registry = {"emails": {},
                    "domains": {"x.com": {"email": "old@x.com",
                                          "sent_at": "2026-04-01T10:00:00"}}}
        reason = self.audit_agent._already_contacted(registry, "new@x.com", "https://www.x.com/page")
        self.assertIsNotNone(reason)
        self.assertIn("x.com", reason)

    def test_already_contacted_returns_none_when_fresh(self):
        registry = {"emails": {}, "domains": {}}
        reason = self.audit_agent._already_contacted(registry, "fresh@x.com", "https://x.com")
        self.assertIsNone(reason)

    def test_record_sent_writes_both_indexes(self):
        registry = {"emails": {}, "domains": {}}
        self.audit_agent._record_sent(registry, "Hello@X.COM", "https://www.x.com", "Subj")
        self.assertIn("hello@x.com", registry["emails"])
        self.assertIn("x.com", registry["domains"])

    def test_corrupt_progress_file_does_not_silently_continue(self):
        # If the JSON is malformed, fail loudly — silent default would lose state
        path = self.audit_agent.CAMPAIGN_PROGRESS_FILE
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write("{not valid json")
        with self.assertRaises(json.JSONDecodeError):
            self.audit_agent._load_campaign_progress()


class TestCSVLoaders(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="csv_tests_")

    def _write(self, name, content):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_load_urls_recognizes_website_url_column(self):
        from waa.cli import load_urls_from_csv
        path = self._write("p.csv",
            "website_url,name\nhttps://a.com,Alpha\nhttps://b.com,Beta\n")
        urls = load_urls_from_csv(path)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0]["url"], "https://a.com")
        self.assertEqual(urls[0]["name"], "Alpha")

    def test_load_urls_falls_back_to_first_column(self):
        from waa.cli import load_urls_from_csv
        path = self._write("p.csv", "site\nhttps://a.com\nhttps://b.com\n")
        urls = load_urls_from_csv(path)
        self.assertEqual(len(urls), 2)
        self.assertEqual(urls[0]["url"], "https://a.com")

    def test_load_urls_skips_blank_rows(self):
        from waa.cli import load_urls_from_csv
        path = self._write("p.csv",
            "website_url,name\nhttps://a.com,Alpha\n,\nhttps://b.com,Beta\n")
        urls = load_urls_from_csv(path)
        self.assertEqual(len(urls), 2)

    def test_load_urls_handles_empty_file(self):
        from waa.cli import load_urls_from_csv
        path = self._write("p.csv", "")
        urls = load_urls_from_csv(path)
        self.assertEqual(urls, [])

    def test_load_contacts_normalizes_domain(self):
        from waa.cli import load_contacts_csv
        path = self._write("c.csv",
            "website,email,name\n"
            "https://www.X.com/path,owner@x.com,Owner\n"
            "missing.com,info@missing.com,Bob\n")
        contacts = load_contacts_csv(path)
        # www. is stripped, host is lowercased
        self.assertIn("x.com", contacts)
        self.assertEqual(contacts["x.com"]["email"], "owner@x.com")
        # missing scheme should still be normalized to https
        self.assertIn("missing.com", contacts)

    def test_load_contacts_skips_rows_missing_email(self):
        from waa.cli import load_contacts_csv
        path = self._write("c.csv",
            "website,email\nhttps://x.com,\nhttps://y.com,b@y.com\n")
        contacts = load_contacts_csv(path)
        self.assertNotIn("x.com", contacts)
        self.assertIn("y.com", contacts)


# ---------------------------------------------------------------------------
# 8. CONVERSION AUDIT — additional adversarial inputs
# ---------------------------------------------------------------------------

class TestConversionAuditAdversarial(unittest.TestCase):

    def test_html_with_only_script_tags(self):
        html = "<html><head><script>var x = 1;</script></head><body><script>more</script></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example", niche="medspa")
        json.dumps(a.to_dict())  # no crash

    def test_form_with_no_inputs(self):
        html = "<html><body><form><button>Submit</button></form></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example")
        booking = a.niche_check.get("booking", {})
        # zero meaningful fields → low friction (still valid)
        self.assertEqual(booking.get("field_count"), 0)

    def test_phone_number_in_text_only_flags_clickability(self):
        html = "<html><body><p>Call us: (555) 123-4567</p></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example")
        self.assertTrue(a.local["has_phone"])
        self.assertFalse(a.local["has_phone_clickable"])
        # Should produce a finding about phone not tappable
        labels = [f.label for f in a.findings]
        self.assertTrue(any("phone" in l.lower() for l in labels))

    def test_old_copyright_year_triggers_surprise(self):
        html = "<html><body><h1>Hi</h1><footer>© 2018 OldCo</footer></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example")
        details = " ".join(f.detail for f in a.findings)
        self.assertIn("2018", details)

    def test_recent_copyright_year_does_not_trigger_surprise(self):
        from datetime import datetime
        html = f"<html><body><h1>Hi</h1><footer>© {datetime.now().year} NewCo</footer></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example")
        details = " ".join(f.detail for f in a.findings)
        self.assertNotIn(f"© {datetime.now().year}", details)

    def test_tel_link_recognized_as_clickable(self):
        html = '<html><body><a href="tel:5551234567">Call</a></body></html>'
        a = conversion_audit.audit_conversion(html, "https://x.example")
        self.assertTrue(a.local["has_phone_clickable"])

    def test_city_match_handles_comma_separated_hint(self):
        html = "<html><body><h1>Top Dentist</h1><p>Serving Miami families.</p></body></html>"
        a = conversion_audit.audit_conversion(html, "https://x.example",
                                              niche="dentist", location="Miami, FL")
        self.assertTrue(a.local["city_appears_on_page"])

    def test_relative_cta_href_resolves_against_base(self):
        html = '<html><body><a href="/book">Book Now</a></body></html>'
        a = conversion_audit.audit_conversion(html, "https://x.example/")
        dest = a.primary_cta["primary_cta_destination"]
        self.assertTrue(dest is None or dest.startswith("https://x.example/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
