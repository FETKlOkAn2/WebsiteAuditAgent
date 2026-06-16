"""
Tests for the lead-qualification gates (improvement #16, cheap-before-expensive).

Fully offline: the Haiku qualifier is a fake, no network. Covers each gate,
the chain's cheapest-first short-circuit, the builder, and the fail-open
behaviour of the LLM gate.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from waa.analysis.gating import (  # noqa: E402
    GateDecision, LeadContext, LeadGate, GateChain,
    ContactEmailGate, PersonalizableGate, HaikuQualifyGate,
    build_lead_gate_chain,
)
from waa.analysis.personalization import SiteFacts  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _facts(**kw) -> SiteFacts:
    base = dict(
        url="https://x.sk", h1="Domáca kuchyňa v centre",
        primary_cta_text="Rezervovať", city_or_area="Bratislava",
        niche="restauracia", niche_specific_missing=["online rezervácia"],
    )
    base.update(kw)
    return SiteFacts(**base)


def _ctx(facts=None, emails=("info@x.sk",), niche="restauracia") -> LeadContext:
    return LeadContext(url="https://x.sk", niche=niche,
                       contact_emails=list(emails), facts=facts or _facts())


class _FakeQualifier:
    """Stand-in QualifyModel: returns a canned JSON (or raises)."""
    def __init__(self, response=None, raises=None):
        self._response = response
        self._raises = raises
        self.calls = 0
    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


# ---------------------------------------------------------------------------
# Free gates
# ---------------------------------------------------------------------------

class TestFreeGates(unittest.TestCase):

    def test_contact_email_pass(self):
        d = ContactEmailGate().evaluate(_ctx(emails=("owner@x.sk",)))
        self.assertTrue(d.passed)

    def test_contact_email_fail(self):
        d = ContactEmailGate().evaluate(_ctx(emails=()))
        self.assertFalse(d.passed)
        self.assertEqual(d.stage, "contact_email")

    def test_contact_email_ignores_blanks(self):
        d = ContactEmailGate().evaluate(_ctx(emails=("", "   ")))
        self.assertFalse(d.passed)

    def test_personalizable_pass(self):
        # _facts() has h1 + cta + city + niche_missing => >=3 facts
        d = PersonalizableGate(min_facts=3).evaluate(_ctx())
        self.assertTrue(d.passed)

    def test_personalizable_fail(self):
        thin = SiteFacts(url="https://x.sk")  # 0 facts
        d = PersonalizableGate(min_facts=3).evaluate(_ctx(facts=thin))
        self.assertFalse(d.passed)
        self.assertIn("0/3", d.reason)


# ---------------------------------------------------------------------------
# Haiku qualify gate
# ---------------------------------------------------------------------------

class TestHaikuQualifyGate(unittest.TestCase):

    def test_passes_high_score(self):
        q = _FakeQualifier('{"score": 8, "worth_contacting": true, "reason": "broken booking"}')
        d = HaikuQualifyGate(q, threshold=6).evaluate(_ctx())
        self.assertTrue(d.passed)
        self.assertEqual(d.score, 8)
        self.assertEqual(q.calls, 1)

    def test_rejects_low_score(self):
        q = _FakeQualifier('{"score": 3, "worth_contacting": false, "reason": "fine site"}')
        d = HaikuQualifyGate(q, threshold=6).evaluate(_ctx())
        self.assertFalse(d.passed)
        self.assertEqual(d.score, 3)

    def test_rejects_when_worth_false_even_if_score_high(self):
        q = _FakeQualifier('{"score": 9, "worth_contacting": false, "reason": "wont pay"}')
        d = HaikuQualifyGate(q, threshold=6).evaluate(_ctx())
        self.assertFalse(d.passed)

    def test_handles_markdown_fenced_json(self):
        q = _FakeQualifier('```json\n{"score": 7, "worth_contacting": true, "reason": "x"}\n```')
        d = HaikuQualifyGate(q, threshold=6).evaluate(_ctx())
        self.assertTrue(d.passed)

    def test_fail_open_on_error(self):
        q = _FakeQualifier(raises=RuntimeError("api down"))
        d = HaikuQualifyGate(q, threshold=6, fail_open=True).evaluate(_ctx())
        self.assertTrue(d.passed)
        self.assertIn("failing open", d.reason)

    def test_fail_closed_on_error(self):
        q = _FakeQualifier(raises=RuntimeError("api down"))
        d = HaikuQualifyGate(q, threshold=6, fail_open=False).evaluate(_ctx())
        self.assertFalse(d.passed)
        self.assertIn("failing closed", d.reason)

    def test_malformed_json_fails_open(self):
        q = _FakeQualifier("not json at all")
        d = HaikuQualifyGate(q, threshold=6, fail_open=True).evaluate(_ctx())
        self.assertTrue(d.passed)


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

class _StubGate(LeadGate):
    def __init__(self, name, passed):
        self.name = name
        self._passed = passed
        self.evaluated = False
    def evaluate(self, lead):
        self.evaluated = True
        return GateDecision(self._passed, self.name, "stub")


class TestGateChain(unittest.TestCase):

    def test_all_pass(self):
        chain = GateChain([_StubGate("a", True), _StubGate("b", True)])
        d = chain.evaluate(_ctx())
        self.assertTrue(d.passed)
        self.assertEqual(d.stage, "all")

    def test_short_circuits_on_first_failure(self):
        g1 = _StubGate("first", False)
        g2 = _StubGate("second", True)
        d = GateChain([g1, g2]).evaluate(_ctx())
        self.assertFalse(d.passed)
        self.assertEqual(d.stage, "first")
        self.assertTrue(g1.evaluated)
        self.assertFalse(g2.evaluated, "later gate must not run after a rejection")

    def test_cheapest_first_order_preserved(self):
        # The expensive gate should never run if a cheap one rejects first.
        cheap = _StubGate("cheap", False)
        expensive = _StubGate("expensive", True)
        GateChain([cheap, expensive]).evaluate(_ctx())
        self.assertFalse(expensive.evaluated)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class TestBuilder(unittest.TestCase):

    def test_no_qualify_omits_llm_gate(self):
        chain = build_lead_gate_chain(qualify=False)
        kinds = [type(g).__name__ for g in chain._gates]
        self.assertIn("PersonalizableGate", kinds)
        self.assertNotIn("HaikuQualifyGate", kinds)

    def test_contact_gate_off_by_default_on_by_request(self):
        default_kinds = [type(g).__name__ for g in build_lead_gate_chain(qualify=False)._gates]
        self.assertNotIn("ContactEmailGate", default_kinds)
        with_contact = [type(g).__name__ for g in
                        build_lead_gate_chain(qualify=False, require_contact=True)._gates]
        self.assertIn("ContactEmailGate", with_contact)

    def test_qualify_includes_llm_gate_with_injected_fake(self):
        q = _FakeQualifier('{"score": 9, "worth_contacting": true, "reason": "x"}')
        chain = build_lead_gate_chain(qualify=True, qualifier=q, threshold=6)
        kinds = [type(g).__name__ for g in chain._gates]
        self.assertIn("HaikuQualifyGate", kinds)
        # End-to-end through the chain with a healthy lead -> passes, fake called.
        d = chain.evaluate(_ctx())
        self.assertTrue(d.passed)
        self.assertEqual(q.calls, 1)

    def test_chain_never_calls_qualifier_when_free_gate_rejects(self):
        # The whole point of #16: a free rejection must save the (cheap, but
        # still costed) LLM call.
        q = _FakeQualifier('{"score": 9, "worth_contacting": true, "reason": "x"}')
        chain = build_lead_gate_chain(qualify=True, qualifier=q, threshold=6)
        thin = SiteFacts(url="https://x.sk")  # fails PersonalizableGate
        d = chain.evaluate(_ctx(facts=thin))
        self.assertFalse(d.passed)
        self.assertEqual(d.stage, "personalizable")
        self.assertEqual(q.calls, 0, "qualifier must not be called after a free rejection")


if __name__ == "__main__":
    unittest.main(verbosity=2)
