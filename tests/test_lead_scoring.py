"""
Tests for profit-weighted lead scoring (improvement #19). Pure / deterministic
— no tokens, no network.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.lead_scoring import (  # noqa: E402
    ProfitSignals, LeadScore, LeadScorer, ProfitWeightedScorer, ScoreWeights,
    build_default_scorer, score_result, niche_value, NICHE_VALUE,
    DEFAULT_NICHE_VALUE,
)
from waa.analysis.personalization import SiteFacts  # noqa: E402


# ---------------------------------------------------------------------------
# Niche value
# ---------------------------------------------------------------------------

class TestNicheValue(unittest.TestCase):

    def test_professional_outranks_cafe(self):
        self.assertGreater(niche_value("advokatska kancelaria"), niche_value("kaviaren"))
        self.assertGreater(niche_value("zubar"), niche_value("pekaren"))

    def test_unknown_niche_default(self):
        self.assertEqual(niche_value("space-elevator"), DEFAULT_NICHE_VALUE)

    def test_case_insensitive(self):
        self.assertEqual(niche_value("  ZUBAR "), NICHE_VALUE["zubar"])


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class TestProfitSignals(unittest.TestCase):

    def test_clamped_to_unit_interval(self):
        s = ProfitSignals(niche_value=5, design_need=-2, reachability=0.5)
        self.assertEqual(s.niche_value, 1.0)
        self.assertEqual(s.design_need, 0.0)
        self.assertEqual(s.reachability, 0.5)

    def test_from_facts_uses_design_score(self):
        facts = SiteFacts(url="x", niche="zubar", design_score=2,
                          h1="A", primary_cta_text="Book", city_or_area="BA")
        s = ProfitSignals.from_facts(facts, has_contact=True)
        # design_score 2/10 -> need 0.8
        self.assertAlmostEqual(s.design_need, 0.8, places=5)
        self.assertEqual(s.niche_value, NICHE_VALUE["zubar"])

    def test_from_facts_fallback_without_design_score(self):
        facts = SiteFacts(url="x", niche="restauracia", has_clear_h1=False,
                          has_phone_clickable=False, surprising_finding="old year")
        s = ProfitSignals.from_facts(facts, has_contact=True)
        # 0.35 + 0.25 + 0.20 = 0.80
        self.assertAlmostEqual(s.design_need, 0.8, places=5)

    def test_reachability_drops_without_contact(self):
        facts = SiteFacts(url="x", niche="zubar", h1="A", primary_cta_text="B",
                          city_or_area="BA", design_score=5)
        with_contact = ProfitSignals.from_facts(facts, has_contact=True)
        without = ProfitSignals.from_facts(facts, has_contact=False)
        self.assertGreater(with_contact.reachability, without.reachability)

    def test_from_result_projection(self):
        result = {
            "contact_emails": ["a@x.sk"],
            "analysis": {"facts": {
                "niche": "hotel", "h1": "Hotel", "primary_cta_text": "Book",
                "city_or_area": "BA", "design_score": 3,
                "niche_specific_missing": ["online booking"],
            }},
        }
        s = ProfitSignals.from_result(result)
        self.assertEqual(s.niche_value, NICHE_VALUE["hotel"])
        self.assertAlmostEqual(s.design_need, 0.7, places=5)
        self.assertGreater(s.reachability, 0.5)


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class TestScorer(unittest.TestCase):

    def setUp(self):
        self.scorer = build_default_scorer()

    def test_high_value_lead_scores_high(self):
        s = ProfitSignals(niche_value=1.0, design_need=1.0, reachability=1.0)
        score = self.scorer.score(s)
        self.assertEqual(score.value, 100)
        self.assertEqual(score.tier, "high")

    def test_low_value_lead_scores_low(self):
        s = ProfitSignals(niche_value=0.1, design_need=0.1, reachability=0.3)
        self.assertEqual(self.scorer.score(s).tier, "low")

    def test_dentist_outranks_cafe_same_state(self):
        dentist = ProfitSignals.from_facts(
            SiteFacts(url="x", niche="zubar", h1="A", primary_cta_text="B",
                      city_or_area="BA", design_score=3), has_contact=True)
        cafe = ProfitSignals.from_facts(
            SiteFacts(url="y", niche="kaviaren", h1="A", primary_cta_text="B",
                      city_or_area="BA", design_score=3), has_contact=True)
        self.assertGreater(self.scorer.score(dentist).value,
                           self.scorer.score(cafe).value)

    def test_worse_site_outranks_tidy_site_same_niche(self):
        bad = ProfitSignals.from_facts(
            SiteFacts(url="x", niche="zubar", h1="A", primary_cta_text="B",
                      city_or_area="BA", design_score=1), has_contact=True)
        tidy = ProfitSignals.from_facts(
            SiteFacts(url="y", niche="zubar", h1="A", primary_cta_text="B",
                      city_or_area="BA", design_score=9), has_contact=True)
        self.assertGreater(self.scorer.score(bad).value,
                           self.scorer.score(tidy).value)

    def test_breakdown_sums_to_value(self):
        s = ProfitSignals(niche_value=0.8, design_need=0.6, reachability=0.5)
        score = self.scorer.score(s)
        self.assertEqual(sum(score.breakdown.values()), score.value)

    def test_custom_weights(self):
        # All weight on niche_value -> a top-niche, no-need lead still scores high.
        scorer = ProfitWeightedScorer(ScoreWeights(niche_value=1.0, design_need=0.0,
                                                    reachability=0.0))
        s = ProfitSignals(niche_value=1.0, design_need=0.0, reachability=0.0)
        self.assertEqual(scorer.score(s).value, 100)

    def test_is_a_lead_scorer(self):
        self.assertIsInstance(self.scorer, LeadScorer)

    def test_score_result_convenience(self):
        result = {"contact_emails": ["a@x.sk"],
                  "analysis": {"facts": {"niche": "zubar", "h1": "A",
                                         "primary_cta_text": "B", "city_or_area": "BA",
                                         "design_score": 2}}}
        score = score_result(result)
        self.assertIsInstance(score, LeadScore)
        self.assertEqual(score.tier, "high")


# ---------------------------------------------------------------------------
# Integration: _prepare_send_list ranks best-first
# ---------------------------------------------------------------------------

class _patched_registry:
    def __enter__(self):
        import waa.cli as cli
        self._orig = cli._load_sent_registry
        cli._load_sent_registry = lambda: {"emails": {}, "domains": {}}
        return self

    def __exit__(self, *exc):
        import waa.cli as cli
        cli._load_sent_registry = self._orig
        return False


def _result(url, niche, design_score, email):
    return {
        "url": url,
        "contact_emails": [email],
        "email": {"subject_line": "Ahoj", "email_body": "telo"},
        "analysis": {"validation": {"passed": True, "quoted_facts": ["x"]},
                     "critic": {"passed": True, "score": 9.0},
                     "facts": {"niche": niche, "h1": "A", "primary_cta_text": "B",
                               "city_or_area": "BA", "design_score": design_score}},
    }


class TestPrepareSendListRanking(unittest.TestCase):

    def test_high_value_lead_sorted_first(self):
        from waa.cli import _prepare_send_list
        results = [
            _result("https://cafe.sk", "kaviaren", 8, "a@cafe.sk"),     # low value
            _result("https://law.sk", "advokatska kancelaria", 1, "b@law.sk"),  # high
        ]
        with _patched_registry():
            out = _prepare_send_list(results, validate_emails=False)
        self.assertEqual(out[0]["website"], "https://law.sk")
        self.assertGreater(out[0]["lead_value"]["value"], out[1]["lead_value"]["value"])

    def test_lead_value_attached_to_result(self):
        from waa.cli import _prepare_send_list
        results = [_result("https://law.sk", "zubar", 2, "b@law.sk")]
        with _patched_registry():
            _prepare_send_list(results, validate_emails=False)
        self.assertIn("lead_value", results[0])
        self.assertEqual(results[0]["lead_value"]["tier"], "high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
