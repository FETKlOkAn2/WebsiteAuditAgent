"""
Tests for the Turing critic (improvement #3).

Offline: the critic uses an injected LLMClient (fake transport) so no network.
Also covers the generate_email_v2 integration (pass / regenerate / still-fail)
with a fake EmailCritic and mocked generation.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OUTPUT_DIR", "/tmp/critic_tests")

from waa.analysis.critic import (  # noqa: E402
    CriticVerdict, EmailCritic, HumanToneCritic, NullCritic,
)
from waa.core.llm import LLMClient, ModelPolicy, ModelTier  # noqa: E402
import waa.analysis.analyzer_v2 as analyzer_v2  # noqa: E402

POLICY = ModelPolicy(cheap_model="cheap-x", premium_model="premium-y")


class _RecordingTransport:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
    def __call__(self, prompt, model, max_tokens):
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return self.reply


# ---------------------------------------------------------------------------
# HumanToneCritic
# ---------------------------------------------------------------------------

class TestHumanToneCritic(unittest.TestCase):

    def _critic(self, reply, threshold=7.0, fail_open=True):
        t = _RecordingTransport(reply)
        return HumanToneCritic(client=LLMClient(POLICY, t), threshold=threshold,
                               fail_open=fail_open), t

    def test_passes_high_score(self):
        c, t = self._critic('{"score": 9, "reason": "specific, informal"}')
        v = c.review("Ahoj Peter, vsimol som si...")
        self.assertTrue(v.passed)
        self.assertEqual(v.score, 9)
        self.assertEqual(t.calls[0]["model"], "cheap-x")  # CHEAP tier

    def test_fails_low_score(self):
        c, _ = self._critic('{"score": 4, "reason": "sounds templated"}')
        v = c.review("Dear Sir or Madam, I hope this finds you well")
        self.assertFalse(v.passed)
        self.assertEqual(v.score, 4)

    def test_threshold_boundary(self):
        c, _ = self._critic('{"score": 7, "reason": "ok"}', threshold=7.0)
        self.assertTrue(c.review("x").passed)

    def test_empty_email_fails(self):
        c, t = self._critic('{"score": 9}')
        v = c.review("   ")
        self.assertFalse(v.passed)
        self.assertEqual(t.calls, [])  # no model call for empty input

    def test_malformed_json_fail_open(self):
        c, _ = self._critic("not json", fail_open=True)
        self.assertTrue(c.review("hello").passed)

    def test_malformed_json_fail_closed(self):
        c, _ = self._critic("not json", fail_open=False)
        self.assertFalse(c.review("hello").passed)

    def test_null_critic_always_passes(self):
        self.assertTrue(NullCritic().review("anything").passed)


# ---------------------------------------------------------------------------
# generate_email_v2 integration with a fake critic
# ---------------------------------------------------------------------------

HTML_RICH_SK = """
<html><head><title>Reštaurácia U Karola</title></head><body>
  <header><a href="tel:+421900111222">+421 900 111 222</a>
  <a class="cta primary" href="/rezervacia">Rezervovať stôl</a></header>
  <h1>Domáca slovenská kuchyňa v centre Bratislavy</h1>
  <p>Bratislava, Hviezdoslavovo námestie.</p>
  <footer>© 2019</footer>
</body></html>
"""

EMAIL_OK = json.dumps({
    "subject_line": "tlacidlo nikam",
    "email_body": "Vsimol som si \"Rezervovať stôl\" na webe. Mam sa pozriet?\\nTomas",
    "follow_up_subject": "Re: tlacidlo", "follow_up_body": "Bumping.\\nTomas",
})
EMAIL_OK2 = json.dumps({
    "subject_line": "tlacidlo nikam",
    "email_body": "Hej, to \"Rezervovať stôl\" nefunguje, vsimol som si. Pozriem?\\nTomas",
    "follow_up_subject": "Re: tlacidlo", "follow_up_body": "Tak co?\\nTomas",
})


class _FakeCritic(EmailCritic):
    """Returns canned verdicts in sequence."""
    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0
    def review(self, email_body, *, lang="sk"):
        self.calls += 1
        return self._verdicts.pop(0) if self._verdicts else CriticVerdict(True, 9, "ok")


class TestGenerateEmailV2WithCritic(unittest.TestCase):

    def test_no_critic_means_no_critic_block(self):
        with patch.object(analyzer_v2, "_call_llm", return_value=EMAIL_OK):
            res = analyzer_v2.generate_email_v2(
                HTML_RICH_SK, "https://x.sk", "X",
                niche="restauracia", location="Bratislava", lang="sk")
        self.assertNotIn("critic", res)

    def test_critic_pass_no_regeneration(self):
        critic = _FakeCritic([CriticVerdict(True, 9, "human")])
        with patch.object(analyzer_v2, "_call_llm", return_value=EMAIL_OK) as m:
            res = analyzer_v2.generate_email_v2(
                HTML_RICH_SK, "https://x.sk", "X",
                niche="restauracia", location="Bratislava", lang="sk",
                critic=critic)
        self.assertTrue(res["critic"]["passed"])
        self.assertFalse(res["critic"]["retried"])
        self.assertEqual(m.call_count, 1)   # only the first generation
        self.assertEqual(critic.calls, 1)

    def test_critic_fail_then_regenerate_to_pass(self):
        critic = _FakeCritic([CriticVerdict(False, 4, "templated"),
                              CriticVerdict(True, 8, "better")])
        with patch.object(analyzer_v2, "_call_llm", side_effect=[EMAIL_OK, EMAIL_OK2]) as m:
            res = analyzer_v2.generate_email_v2(
                HTML_RICH_SK, "https://x.sk", "X",
                niche="restauracia", location="Bratislava", lang="sk",
                critic=critic)
        self.assertTrue(res["critic"]["passed"])
        self.assertTrue(res["critic"]["retried"])
        self.assertEqual(m.call_count, 2)   # gen + humanize regen
        self.assertIn("nefunguje", res["email_body"])  # took the regenerated body

    def test_critic_fail_twice_stays_failed(self):
        critic = _FakeCritic([CriticVerdict(False, 4, "templated"),
                              CriticVerdict(False, 5, "still off")])
        with patch.object(analyzer_v2, "_call_llm", side_effect=[EMAIL_OK, EMAIL_OK2]):
            res = analyzer_v2.generate_email_v2(
                HTML_RICH_SK, "https://x.sk", "X",
                niche="restauracia", location="Bratislava", lang="sk",
                critic=critic)
        self.assertFalse(res["critic"]["passed"])
        self.assertTrue(res["critic"]["retried"])
        # original body kept (regeneration not accepted)
        self.assertIn("Mam sa pozriet", res["email_body"])


# ---------------------------------------------------------------------------
# process_single drops critic-failed emails
# ---------------------------------------------------------------------------

class TestProcessSingleCritic(unittest.TestCase):

    @patch("waa.discovery.scraper.fetch_pagespeed", return_value={"available": False})
    @patch("waa.discovery.scraper.fetch_html")
    def test_critic_failed_marks_skipped_and_no_email(self, mock_fetch, _ps):
        from waa.cli import process_single
        mock_fetch.return_value = {
            "url": "https://x.sk", "status_code": 200, "html": HTML_RICH_SK,
            "error": None, "load_time_ms": 100, "final_url": "https://x.sk",
        }
        # Patch the critic the pipeline builds to always fail, and the generator.
        from waa.analysis import critic as critic_mod
        always_fail = _FakeCritic([CriticVerdict(False, 3, "ai"),
                                   CriticVerdict(False, 3, "ai")])
        with patch.object(critic_mod, "HumanToneCritic", return_value=always_fail), \
             patch("waa.analysis.owner_finder.find_owner_name", return_value=None), \
             patch.object(analyzer_v2, "_call_llm", side_effect=[EMAIL_OK, EMAIL_OK2]):
            result = process_single(
                "https://x.sk", skip_pagespeed=True, audit_mode="v2", lang="sk",
                niche="restauracia", location="Bratislava",
                qualify=False, critique=True)
        self.assertEqual(result.get("skipped_reason"), "critic_failed")
        self.assertIsNone(result.get("email"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
