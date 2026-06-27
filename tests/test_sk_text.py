"""
Tests for the Slovak email polish (city locative + greeting). Pure /
deterministic — no tokens, no network.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.sk_text import (  # noqa: E402
    locative_phrase, proper_nominative, fix_city_phrases, ensure_greeting,
    polish_email,
)


# ---------------------------------------------------------------------------
# locative_phrase / proper_nominative
# ---------------------------------------------------------------------------

class TestLocative(unittest.TestCase):

    def test_known_cities(self):
        self.assertEqual(locative_phrase("Zilina"), "v Žiline")
        self.assertEqual(locative_phrase("Banska Bystrica"), "v Banskej Bystrici")
        self.assertEqual(locative_phrase("Kosice"), "v Košiciach")
        self.assertEqual(locative_phrase("Zvolen"), "vo Zvolene")

    def test_diacritic_insensitive(self):
        self.assertEqual(locative_phrase("Žilina"), "v Žiline")
        self.assertEqual(locative_phrase("  banská bystrica "), "v Banskej Bystrici")

    def test_unknown_city_none(self):
        self.assertIsNone(locative_phrase("Vrutky"))
        self.assertIsNone(locative_phrase(""))

    def test_proper_nominative(self):
        self.assertEqual(proper_nominative("zilina"), "Žilina")
        self.assertEqual(proper_nominative("Unknownville"), "Unknownville")


# ---------------------------------------------------------------------------
# fix_city_phrases
# ---------------------------------------------------------------------------

class TestFixCity(unittest.TestCase):

    def test_fixes_nominative_after_v(self):
        out = fix_city_phrases("advokát v Zilina ponúka", "Zilina")
        self.assertIn("v Žiline", out)
        self.assertNotIn("v Zilina", out)

    def test_fixes_truncated_multiword(self):
        out = fix_city_phrases("kancelária v Banska a okolie", "Banska Bystrica")
        # "Banska" alone is not the full city, so it should NOT be mis-rewritten
        self.assertIn("v Banska", out)  # only full-city match is corrected

    def test_fixes_full_multiword(self):
        out = fix_city_phrases("kancelária v Banska Bystrica dnes", "Banska Bystrica")
        self.assertIn("v Banskej Bystrici", out)

    def test_fixes_preposition_vo(self):
        out = fix_city_phrases("klinika v Zvolen", "Zvolen")
        self.assertIn("vo Zvolene", out)

    def test_leaves_correct_locative_untouched(self):
        out = fix_city_phrases("advokát v Žiline ponúka", "Zilina")
        self.assertEqual(out, "advokát v Žiline ponúka")

    def test_leaves_genitive_untouched(self):
        # "zo Žiliny" is genitive, not our "v <nominative>" error
        out = fix_city_phrases("advokát zo Žiliny", "Zilina")
        self.assertEqual(out, "advokát zo Žiliny")

    def test_unknown_city_noop(self):
        self.assertEqual(fix_city_phrases("v Vrutky", "Vrutky"), "v Vrutky")

    def test_diacritic_form_in_text(self):
        out = fix_city_phrases("notár v Žilina", "Zilina")  # wrong case, diacritic
        self.assertIn("v Žiline", out)

    def test_empty_inputs(self):
        self.assertEqual(fix_city_phrases("", "Zilina"), "")
        self.assertEqual(fix_city_phrases("text", ""), "text")


# ---------------------------------------------------------------------------
# ensure_greeting
# ---------------------------------------------------------------------------

class TestGreeting(unittest.TestCase):

    def test_prepends_when_missing_sk(self):
        out = ensure_greeting("Telefón na webe nefunguje.", lang="sk")
        self.assertTrue(out.startswith("Dobrý deň,"))
        self.assertIn("Telefón na webe", out)

    def test_uses_name_when_known(self):
        out = ensure_greeting("Telefón nefunguje.", owner_first_name="Mária", lang="sk")
        self.assertTrue(out.startswith("Dobrý deň, Mária,"))

    def test_idempotent_existing_greeting(self):
        body = "Dobrý deň,\n\nTelefón nefunguje."
        self.assertEqual(ensure_greeting(body, lang="sk"), body)

    def test_idempotent_ahoj(self):
        body = "Ahoj Peter,\n\nvšimol som si."
        self.assertEqual(ensure_greeting(body, owner_first_name="Peter", lang="sk"), body)

    def test_english(self):
        out = ensure_greeting("Your phone is not tappable.", lang="en")
        self.assertTrue(out.startswith("Hi,"))

    def test_english_with_name(self):
        out = ensure_greeting("Your phone.", owner_first_name="John", lang="en")
        self.assertTrue(out.startswith("Hi John,"))

    def test_empty_body_unchanged(self):
        self.assertEqual(ensure_greeting("", lang="sk"), "")


# ---------------------------------------------------------------------------
# polish_email
# ---------------------------------------------------------------------------

class TestPolishEmail(unittest.TestCase):

    def test_full_polish(self):
        res = polish_email(
            body="Telefón na webe nefunguje. Pre advokáta v Zilina to znamená problém.\n\nTomas",
            subject="Telefón v Zilina",
            follow_up_body="Ešte k webu v Zilina, zavoláme?",
            city="Zilina",
            owner_first_name=None,
            lang="sk",
        )
        self.assertTrue(res["body"].startswith("Dobrý deň,"))
        self.assertIn("v Žiline", res["body"])
        self.assertNotIn("v Zilina", res["body"])
        self.assertIn("v Žiline", res["subject"])
        # follow-up gets city fix but NO greeting (it's a reply in-thread)
        self.assertIn("v Žiline", res["follow_up_body"])
        self.assertFalse(res["follow_up_body"].startswith("Dobrý deň"))

    def test_polish_with_name(self):
        res = polish_email(body="Web nehovorí čo ponúkate.\n\nTomas",
                           city="Kosice", owner_first_name="Jana", lang="sk")
        self.assertTrue(res["body"].startswith("Dobrý deň, Jana,"))


# ---------------------------------------------------------------------------
# Integration: generate_email_v2 applies the polish
# ---------------------------------------------------------------------------

class TestGenerateEmailV2Polish(unittest.TestCase):

    def test_v2_output_is_polished(self):
        import json
        from unittest.mock import patch
        from waa.analysis import analyzer_v2
        from tests.test_v2 import HTML_RICH_SK

        # Email that lacks a greeting and uses the wrong city case.
        raw = json.dumps({
            "subject_line": "Telefón nefunguje",
            "email_body": "Tlačidlo \"Rezervovať stôl\" nikam nevedie. "
                          "Pre podnik v Bratislava to znamená problém.\\nTomas",
            "follow_up_subject": "Re: Telefón nefunguje",
            "follow_up_body": "Ešte k tomu, zavoláme?\\nTomas",
        })
        with patch.object(analyzer_v2, "_call_llm", return_value=raw):
            res = analyzer_v2.generate_email_v2(
                html=HTML_RICH_SK, url="https://x.sk", site_name="X",
                niche="restauracia", location="Bratislava", lang="sk")
        self.assertTrue(res["email_body"].startswith("Dobrý deň,"))
        self.assertIn("v Bratislave", res["email_body"])
        self.assertNotIn("v Bratislava", res["email_body"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
