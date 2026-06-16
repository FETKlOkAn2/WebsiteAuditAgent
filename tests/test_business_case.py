"""
Tests for the business-case layer (improvement #8). Pure / deterministic,
no tokens, no network.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.business_case import (  # noqa: E402
    NicheProfile, profile_for, BusinessCase, BusinessCaseBuilder,
    DEFAULT_PROFILE,
)
from waa.analysis.conversion_audit import Finding, ConversionAudit  # noqa: E402


def _audit(findings, niche="restauracia") -> ConversionAudit:
    a = ConversionAudit(url="https://x.sk", niche=niche)
    a.findings = findings
    return a


def _f(category, label="x", confidence="medium") -> Finding:
    return Finding(category=category, label=label, detail="d", confidence=confidence)


class TestNicheProfile(unittest.TestCase):

    def test_known_niche(self):
        p = profile_for("restauracia")
        self.assertEqual(p.niche_en, "restaurant")
        self.assertIn("local", p.priority_categories)

    def test_unknown_niche_falls_back(self):
        self.assertIs(profile_for("space-elevator"), DEFAULT_PROFILE)

    def test_case_insensitive(self):
        self.assertEqual(profile_for("  Restauracia ").niche_en, "restaurant")


class TestBuilder(unittest.TestCase):

    def setUp(self):
        self.b = BusinessCaseBuilder()

    def test_build_maps_category_with_market_context(self):
        case = self.b.build(_f("local"), "restauracia")
        self.assertIsInstance(case, BusinessCase)
        self.assertIn("restaurant", case.reasoning)   # niche context injected
        self.assertTrue(case.headline)
        self.assertFalse(case.is_design)               # 'local' is a hard bug

    def test_design_category_flagged(self):
        self.assertTrue(self.b.build(_f("above_fold"), "kaviaren").is_design)

    def test_unknown_category_returns_none(self):
        self.assertIsNone(self.b.build(_f("totally_unknown"), "restauracia"))

    def test_niche_boost_changes_ranking(self):
        # For a restaurant, 'local' (phone) is a priority category and should
        # outrank a 'surprise' (stale copyright) even though both exist.
        audit = _audit([_f("surprise"), _f("local")])
        top = self.b.top(audit, "restauracia")
        self.assertEqual(top.finding_label, "x")
        cases = self.b.build_all(audit, "restauracia")
        local = next(c for c in cases if "tap" in c.headline)
        surprise = next(c for c in cases if "unfinished" in c.headline)
        self.assertGreater(local.priority, surprise.priority)

    def test_high_confidence_bonus(self):
        hi = self.b.build(_f("cta", confidence="high"), "restauracia")
        med = self.b.build(_f("cta", confidence="medium"), "restauracia")
        self.assertGreater(hi.priority, med.priority)

    def test_summary_for_prompt_returns_top(self):
        audit = _audit([_f("local")])
        s = self.b.summary_for_prompt(audit, "restauracia")
        self.assertIn("tap", s)
        self.assertIn("—", s)  # headline — reasoning

    def test_summary_empty_audit(self):
        self.assertEqual(self.b.summary_for_prompt(_audit([]), "restauracia"), "")

    def test_default_profile_for_unmapped_niche_still_builds(self):
        case = self.b.build(_f("cta"), "no-such-niche")
        self.assertIn("local business", case.reasoning)


class TestSiteFactsIntegration(unittest.TestCase):

    def test_extract_facts_sets_business_case(self):
        from waa.analysis import personalization
        html = ('<html><body><h1>Vitajte</h1><p>Volajte 0905 123 456</p>'
                '<a href="#">Domov</a><footer>© 2018</footer></body></html>')
        facts = personalization.extract_facts(
            html, "https://x.sk", niche="restauracia", location="Bratislava")
        self.assertTrue(facts.business_case)
        # business_case must NOT leak into the verbatim-quotable set
        self.assertNotIn(facts.business_case, facts.quotable_strings())


if __name__ == "__main__":
    unittest.main(verbosity=2)
