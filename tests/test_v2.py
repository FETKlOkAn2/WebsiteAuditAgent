"""
Tests for the v2 pipeline: personalization.SiteFacts, prompts_v2 wiring,
analyzer_v2 with validation + retry.

Everything is offline (LLM stubbed). Run with:
    .venv/bin/python -m unittest tests.test_v2 -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("OUTPUT_DIR", tempfile.mkdtemp(prefix="v2_tests_"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import waa.config as config  # noqa: E402
import waa.analysis.personalization as personalization  # noqa: E402
import waa.analysis.prompts_v2 as prompts_v2  # noqa: E402
import waa.analysis.analyzer_v2 as analyzer_v2  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

HTML_RICH_SK = """
<html><head><title>Reštaurácia U Karola — Bratislava</title></head>
<body>
  <header>
    <a href="tel:+421900111222">+421 900 111 222</a>
    <a class="cta primary" href="/rezervacia">Rezervovať stôl</a>
  </header>
  <h1>Domáca slovenská kuchyňa v centre Bratislavy</h1>
  <p>Otvorené denne 11:00–22:00. Bratislava, Hviezdoslavovo námestie.</p>
  <a href="/menu">Menu</a>
  <div class="testimonial">"Najlepší rezeň v meste!" — Peter</div>
  <footer>© 2019 Reštaurácia U Karola</footer>
</body></html>
"""

HTML_POOR = "<html><body><p>welcome</p></body></html>"

HTML_NO_FACTS = "<html><body></body></html>"


# ---------------------------------------------------------------------------
# personalization.py
# ---------------------------------------------------------------------------

class TestSiteFacts(unittest.TestCase):

    def test_rich_site_is_personalizable(self):
        facts = personalization.extract_facts(
            HTML_RICH_SK, "https://ukarola.example",
            niche="restauracia", location="Bratislava",
        )
        self.assertTrue(facts.is_personalizable())
        self.assertGreaterEqual(facts.fact_count(), 3)
        self.assertIsNotNone(facts.h1)
        self.assertIn("Bratislav", facts.h1 or "")
        self.assertIsNotNone(facts.primary_cta_text)
        self.assertEqual(facts.city_or_area, "Bratislava")
        self.assertTrue(facts.has_phone_clickable)

    def test_poor_site_not_personalizable(self):
        facts = personalization.extract_facts(
            HTML_POOR, "https://x.example", niche="restauracia",
        )
        self.assertFalse(facts.is_personalizable())

    def test_empty_html_yields_empty_facts(self):
        facts = personalization.extract_facts("", "https://x.example")
        self.assertFalse(facts.is_personalizable())

    def test_no_body_does_not_crash(self):
        facts = personalization.extract_facts(HTML_NO_FACTS, "https://x.example")
        json.dumps(facts.to_dict())  # serializable

    def test_quotable_strings_filters_short_junk(self):
        facts = personalization.extract_facts(
            HTML_RICH_SK, "https://x.example",
            niche="restauracia", location="Bratislava",
        )
        for q in facts.quotable_strings():
            self.assertGreaterEqual(len(q.strip()), 2)

    def test_city_detected_from_page_when_no_hint(self):
        facts = personalization.extract_facts(
            HTML_RICH_SK, "https://x.example", niche="restauracia",
        )
        self.assertEqual(facts.city_or_area, "Bratislava")

    def test_serializable(self):
        facts = personalization.extract_facts(
            HTML_RICH_SK, "https://x.example",
            niche="restauracia", location="Bratislava",
        )
        s = json.dumps(facts.to_dict(), ensure_ascii=False)
        self.assertIn("Bratislava", s)


# ---------------------------------------------------------------------------
# prompts_v2 helpers
# ---------------------------------------------------------------------------

class TestPromptsV2(unittest.TestCase):

    def test_translate_niche_sk_known(self):
        self.assertEqual(prompts_v2.translate_niche("restauracia", "sk"), "reštaurácie")
        self.assertEqual(prompts_v2.translate_niche("zubar", "sk"), "zubné ambulancie")

    def test_translate_niche_sk_unknown_returns_input(self):
        self.assertEqual(prompts_v2.translate_niche("xyz", "sk"), "xyz")

    def test_translate_niche_en_passthrough(self):
        self.assertEqual(prompts_v2.translate_niche("dentist", "en"), "dentist")

    def test_translate_niche_empty(self):
        self.assertEqual(prompts_v2.translate_niche("", "sk"), "firmy")
        self.assertEqual(prompts_v2.translate_niche("", "en"), "businesses")

    def test_sk_template_has_required_placeholders(self):
        # Sanity: the template should contain the placeholders that
        # analyzer_v2._build_prompt fills in. Catches accidental drift.
        for ph in ["{quotable_facts}", "{sender_name}", "{niche_sk}",
                   "{city}", "{h1}", "{primary_cta}", "{phone_clickable}",
                   "{niche_missing}", "{niche_present}", "{surprise}",
                   "{hi_finding}", "{url}", "{site_name}"]:
            self.assertIn(ph, prompts_v2.EMAIL_PROMPT_SK,
                          f"SK template missing {ph}")

    def test_en_template_has_required_placeholders(self):
        for ph in ["{quotable_facts}", "{sender_name}", "{city}", "{niche}",
                   "{h1}", "{primary_cta}", "{phone_clickable}",
                   "{niche_missing}", "{niche_present}", "{surprise}",
                   "{hi_finding}", "{url}", "{site_name}"]:
            self.assertIn(ph, prompts_v2.EMAIL_PROMPT_EN,
                          f"EN template missing {ph}")


# ---------------------------------------------------------------------------
# analyzer_v2 — validation + retry
# ---------------------------------------------------------------------------

EMAIL_QUOTING_FACT = json.dumps({
    "subject_line": "tlačidlo nikam",
    "email_body": (
        "Vsimol som si, ze tlacidlo \"Rezervovať stôl\" na vasom webe "
        "vedie naspat hore.\\n\\nPre restauracie v Bratislave to znamena "
        "kazdy kto chce rezervovat na mobile, odide skor.\\n\\nMozem "
        "poslat detail?\\n\\nTomas"
    ),
    "follow_up_subject": "Re: tlačidlo nikam",
    "follow_up_body": "Bumping — opravim pre vas?\\nTomas",
})

EMAIL_NOT_QUOTING_FACT = json.dumps({
    "subject_line": "vase weby",
    "email_body": (
        "Vsimol som si nieco na vasom webe, co stoji za pozornost. "
        "Mohli by sme to opravit. Mam vam poslat detail?\\nTomas"
    ),
    "follow_up_subject": "Re: vase weby",
    "follow_up_body": "Bumping.\\nTomas",
})


class TestAnalyzerV2(unittest.TestCase):

    def test_skipped_when_not_personalizable(self):
        with patch.object(analyzer_v2, "_call_llm") as mock_llm:
            result = analyzer_v2.generate_email_v2(
                html=HTML_POOR, url="https://x.example",
                site_name="X", niche="restauracia", location="",
                sender_name="Tomas", lang="sk",
            )
        self.assertIsNotNone(result["skipped_reason"])
        self.assertIn("insufficient_facts", result["skipped_reason"])
        # LLM should never be called when there's nothing to ground on
        mock_llm.assert_not_called()

    def test_passes_when_email_quotes_a_fact(self):
        with patch.object(analyzer_v2, "_call_llm", return_value=EMAIL_QUOTING_FACT) as mock:
            result = analyzer_v2.generate_email_v2(
                html=HTML_RICH_SK, url="https://ukarola.example",
                site_name="Reštaurácia U Karola",
                niche="restauracia", location="Bratislava",
                sender_name="Tomas", lang="sk",
            )
        self.assertTrue(result["validation"]["passed"])
        self.assertFalse(result["validation"]["retried"])
        self.assertGreater(len(result["validation"]["quoted_facts"]), 0)
        self.assertEqual(mock.call_count, 1)

    def test_retries_once_when_no_fact_quoted(self):
        # First call ignores facts → retry, second call quotes facts → pass
        with patch.object(analyzer_v2, "_call_llm",
                          side_effect=[EMAIL_NOT_QUOTING_FACT, EMAIL_QUOTING_FACT]) as mock:
            result = analyzer_v2.generate_email_v2(
                html=HTML_RICH_SK, url="https://ukarola.example",
                site_name="Reštaurácia U Karola",
                niche="restauracia", location="Bratislava",
                sender_name="Tomas", lang="sk",
            )
        self.assertEqual(mock.call_count, 2)
        self.assertTrue(result["validation"]["retried"])
        self.assertTrue(result["validation"]["passed"])

    def test_marks_as_failed_when_retry_also_misses(self):
        with patch.object(analyzer_v2, "_call_llm",
                          side_effect=[EMAIL_NOT_QUOTING_FACT, EMAIL_NOT_QUOTING_FACT]):
            result = analyzer_v2.generate_email_v2(
                html=HTML_RICH_SK, url="https://ukarola.example",
                site_name="Reštaurácia U Karola",
                niche="restauracia", location="Bratislava",
                sender_name="Tomas", lang="sk",
            )
        self.assertTrue(result["validation"]["retried"])
        self.assertFalse(result["validation"]["passed"])
        # We still return something usable, the caller decides whether to send
        self.assertTrue(result["email_body"])

    def test_handles_garbage_llm_response_gracefully(self):
        with patch.object(analyzer_v2, "_call_llm", return_value="not json {{"):
            result = analyzer_v2.generate_email_v2(
                html=HTML_RICH_SK, url="https://ukarola.example",
                site_name="X", niche="restauracia", location="Bratislava",
                sender_name="Tomas", lang="sk",
            )
        # Should not raise; should return skipped_reason
        self.assertIsNotNone(result.get("skipped_reason"))


# ---------------------------------------------------------------------------
# Validator (_facts_quoted) — directly
# ---------------------------------------------------------------------------

class TestFactValidator(unittest.TestCase):

    def _facts(self, **kwargs) -> personalization.SiteFacts:
        defaults = {"url": "https://x.example",
                    "h1": "Domáca slovenská kuchyňa",
                    "primary_cta_text": "Rezervovať stôl",
                    "city_or_area": "Bratislava",
                    "niche": "restauracia",
                    "niche_specific_present": [],
                    "niche_specific_missing": ["online rezervacia"]}
        defaults.update(kwargs)
        return personalization.SiteFacts(**defaults)

    def test_substring_match(self):
        f = self._facts()
        body = "Vsimol som si, ze \"Rezervovať stôl\" tlacidlo nefunguje."
        matched = analyzer_v2._facts_quoted(body, f)
        self.assertIn("Rezervovať stôl", matched)

    def test_case_insensitive(self):
        f = self._facts()
        body = "rezervovať stôl - this is broken"
        matched = analyzer_v2._facts_quoted(body, f)
        self.assertIn("Rezervovať stôl", matched)

    def test_whitespace_tolerant(self):
        f = self._facts()
        body = "Rezervovať    stôl is the broken button"
        matched = analyzer_v2._facts_quoted(body, f)
        self.assertIn("Rezervovať stôl", matched)

    def test_no_match_returns_empty(self):
        f = self._facts()
        body = "Generic email with no specific content from the site."
        matched = analyzer_v2._facts_quoted(body, f)
        self.assertEqual(matched, [])

    def test_short_facts_filtered_out(self):
        f = self._facts(h1="ok")  # too short
        body = "ok"
        matched = analyzer_v2._facts_quoted(body, f)
        self.assertNotIn("ok", matched)


# ---------------------------------------------------------------------------
# Integration: process_single in v2 mode (LLM stubbed)
# ---------------------------------------------------------------------------

class TestProcessSingleV2(unittest.TestCase):

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    @patch.object(analyzer_v2, "_call_llm", return_value=EMAIL_QUOTING_FACT)
    def test_v2_path_produces_email_with_validation_metadata(
        self, _mock_llm, mock_fetch, _mock_ps
    ):
        from waa.cli import process_single

        mock_fetch.return_value = {
            "url": "https://ukarola.example", "status_code": 200,
            "html": HTML_RICH_SK, "error": None, "load_time_ms": 100,
            "final_url": "https://ukarola.example",
        }
        result = process_single(
            "https://ukarola.example", name="Reštaurácia U Karola",
            skip_pagespeed=True, sender_name="Tomas",
            audit_mode="v2", lang="sk",
            niche="restauracia", location="Bratislava",
        )
        self.assertIsNone(result.get("error"))
        self.assertIsNone(result.get("skipped_reason"))
        self.assertIsNotNone(result.get("email"))
        self.assertIn("Rezervovať", result["email"]["email_body"])
        self.assertEqual(result["analysis"]["audit_mode"], "v2")
        self.assertEqual(result["analysis"]["lang"], "sk")

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    def test_v2_skips_when_site_too_thin(self, mock_fetch, _ps):
        from waa.cli import process_single

        mock_fetch.return_value = {
            "url": "https://thin.example", "status_code": 200,
            "html": HTML_POOR, "error": None, "load_time_ms": 100,
            "final_url": "https://thin.example",
        }
        with patch.object(analyzer_v2, "_call_llm") as mock_llm:
            result = process_single(
                "https://thin.example", name="",
                skip_pagespeed=True, sender_name="Tomas",
                audit_mode="v2", lang="sk",
                niche="restauracia", location="Bratislava",
            )
        # Skip reason set, no LLM call made, no email sent
        self.assertIsNotNone(result.get("skipped_reason"))
        self.assertIn("insufficient_facts", result["skipped_reason"])
        mock_llm.assert_not_called()


class TestDashStripping(unittest.TestCase):
    """waa.analysis.analyzer.strip_ai_dashes removes the AI-tell dashes from emails."""

    def setUp(self):
        from waa.analysis.analyzer import strip_ai_dashes
        self.strip = strip_ai_dashes

    def test_em_dash_becomes_comma(self):
        self.assertEqual(self.strip("Krásny web — ale pomalý."),
                         "Krásny web, ale pomalý.")

    def test_en_dash_becomes_comma(self):
        self.assertEqual(self.strip("text – ďalší"), "text, ďalší")

    def test_spaced_hyphen_becomes_comma(self):
        self.assertEqual(self.strip("Rýchle - lacné"), "Rýchle, lacné")

    def test_intraword_hyphen_kept(self):
        self.assertEqual(self.strip("e-mail a Wi-Fi"), "e-mail a Wi-Fi")

    def test_dangling_punctuation_tidied(self):
        self.assertEqual(self.strip("Koniec — ."), "Koniec.")

    def test_empty(self):
        self.assertEqual(self.strip(""), "")

    def test_prompts_have_no_dashes(self):
        import waa.analysis.prompts_v2 as prompts_v2
        for p in (prompts_v2.EMAIL_PROMPT_SK, prompts_v2.EMAIL_PROMPT_EN):
            self.assertEqual(p.count("—"), 0)
            self.assertEqual(p.count("–"), 0)

    def test_prompts_ban_the_repeated_cta(self):
        # The line the user flagged must be explicitly forbidden.
        import waa.analysis.prompts_v2 as prompts_v2
        self.assertIn("Je to vedome takto", prompts_v2.EMAIL_PROMPT_SK)
        self.assertIn("ZAKÁZANÉ FRÁZY", prompts_v2.EMAIL_PROMPT_SK)


class TestRedesignFraming(unittest.TestCase):
    def test_sk_prompt_pushes_whole_site(self):
        import waa.analysis.prompts_v2 as prompts_v2
        body = prompts_v2.EMAIL_PROMPT_SK.lower()
        self.assertIn("celej stránky", body)
        self.assertIn("prerob", body)  # prerobiť / prerábali

    def test_en_prompt_pushes_whole_site(self):
        import waa.analysis.prompts_v2 as prompts_v2
        body = prompts_v2.EMAIL_PROMPT_EN.lower()
        self.assertIn("whole site", body)
        self.assertIn("redesign", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
