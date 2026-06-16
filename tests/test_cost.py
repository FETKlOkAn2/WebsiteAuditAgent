"""
Tests for per-run cost telemetry + budget (improvement #18). Pure /
deterministic — no tokens, no network. The SDK instrumentation is tested by
mocking the Anthropic client so a fake response with a usage object flows
through _call_llm.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.core import cost  # noqa: E402
from waa.core.cost import (  # noqa: E402
    ModelPricing, pricing_for, DEFAULT_PRICING, MODEL_PRICING,
    UsageRecord, CostMeter, Budget, BudgetExceeded,
    get_meter, reset_meter, active_budget, enforce_budget, record_message_usage,
)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

class TestPricing(unittest.TestCase):

    def test_cost_math(self):
        p = ModelPricing(3.0, 15.0)
        # 1M input @ $3 + 1M output @ $15 = $18
        self.assertAlmostEqual(p.cost(1_000_000, 1_000_000), 18.0)

    def test_pricing_for_matches_substring(self):
        self.assertEqual(pricing_for("claude-sonnet-4-6"), MODEL_PRICING["sonnet"])
        self.assertEqual(pricing_for("claude-haiku-4-5"), MODEL_PRICING["haiku"])

    def test_unknown_model_defaults_conservative(self):
        self.assertEqual(pricing_for("some-future-model"), DEFAULT_PRICING)

    def test_haiku_cheaper_than_sonnet(self):
        in_t, out_t = 10_000, 2_000
        self.assertLess(pricing_for("haiku").cost(in_t, out_t),
                        pricing_for("sonnet").cost(in_t, out_t))


# ---------------------------------------------------------------------------
# Meter
# ---------------------------------------------------------------------------

class TestCostMeter(unittest.TestCase):

    def test_records_accumulate(self):
        m = CostMeter()
        m.record("claude-haiku-4-5", 1000, 200)
        m.record("claude-sonnet-4-6", 2000, 500)
        self.assertEqual(m.calls, 2)
        self.assertEqual(m.total_tokens(), (3000, 700))
        self.assertGreater(m.total_cost(), 0)

    def test_by_model_breakdown(self):
        m = CostMeter()
        m.record("claude-haiku-4-5", 1000, 200)
        m.record("claude-haiku-4-5", 500, 100)
        bm = m.by_model()
        self.assertEqual(bm["claude-haiku-4-5"]["calls"], 2)
        self.assertEqual(bm["claude-haiku-4-5"]["input_tokens"], 1500)

    def test_cost_matches_pricing(self):
        m = CostMeter()
        rec = m.record("claude-sonnet-4-6", 1_000_000, 0)
        self.assertAlmostEqual(rec.cost, 3.0)
        self.assertAlmostEqual(m.total_cost(), 3.0)

    def test_empty_summary(self):
        self.assertIn("no calls", CostMeter().summary())

    def test_summary_lists_models(self):
        m = CostMeter()
        m.record("claude-haiku-4-5", 1000, 200)
        s = m.summary()
        self.assertIn("haiku", s)
        self.assertIn("$", s)

    def test_to_dict_shape(self):
        m = CostMeter()
        m.record("claude-haiku-4-5", 1000, 200)
        d = m.to_dict()
        self.assertEqual(d["calls"], 1)
        self.assertEqual(d["input_tokens"], 1000)
        self.assertIn("by_model", d)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class TestBudget(unittest.TestCase):

    def test_no_cap_never_exceeds(self):
        m = CostMeter()
        m.record("claude-sonnet-4-6", 10_000_000, 10_000_000)  # huge
        b = Budget(max_usd=0)
        self.assertFalse(b.is_active())
        self.assertFalse(b.exceeded_by(m))
        b.check(m)  # must not raise

    def test_cap_triggers_when_over(self):
        m = CostMeter()
        m.record("claude-sonnet-4-6", 1_000_000, 0)  # $3
        b = Budget(max_usd=1.0)
        self.assertTrue(b.exceeded_by(m))
        with self.assertRaises(BudgetExceeded):
            b.check(m)

    def test_cap_ok_when_under(self):
        m = CostMeter()
        m.record("claude-haiku-4-5", 1000, 200)  # tiny
        Budget(max_usd=100.0).check(m)  # must not raise


# ---------------------------------------------------------------------------
# Process-global meter + config-driven budget
# ---------------------------------------------------------------------------

class TestGlobalMeter(unittest.TestCase):

    def tearDown(self):
        reset_meter()

    def test_reset_clears(self):
        get_meter().record("claude-haiku-4-5", 100, 50)
        self.assertEqual(get_meter().calls, 1)
        reset_meter()
        self.assertEqual(get_meter().calls, 0)

    def test_active_budget_from_config(self):
        import waa.config as cfg
        with patch.object(cfg, "COST_BUDGET_USD", 5.0):
            self.assertTrue(active_budget().is_active())
        with patch.object(cfg, "COST_BUDGET_USD", 0):
            self.assertFalse(active_budget().is_active())

    def test_enforce_budget_raises_when_over(self):
        import waa.config as cfg
        reset_meter()
        get_meter().record("claude-sonnet-4-6", 1_000_000, 0)  # $3
        with patch.object(cfg, "COST_BUDGET_USD", 1.0):
            with self.assertRaises(BudgetExceeded):
                enforce_budget()

    def test_enforce_budget_noop_without_cap(self):
        import waa.config as cfg
        reset_meter()
        get_meter().record("claude-sonnet-4-6", 1_000_000, 0)
        with patch.object(cfg, "COST_BUDGET_USD", 0):
            enforce_budget()  # must not raise


# ---------------------------------------------------------------------------
# record_message_usage helper
# ---------------------------------------------------------------------------

class TestRecordMessageUsage(unittest.TestCase):

    def tearDown(self):
        reset_meter()

    def test_reads_usage_off_message(self):
        reset_meter()
        msg = MagicMock()
        msg.usage.input_tokens = 1234
        msg.usage.output_tokens = 567
        rec = record_message_usage(msg, "claude-haiku-4-5", label="text")
        self.assertEqual(rec.input_tokens, 1234)
        self.assertEqual(get_meter().calls, 1)

    def test_missing_usage_is_safe(self):
        reset_meter()
        msg = MagicMock(spec=[])  # no .usage attribute
        self.assertIsNone(record_message_usage(msg, "claude-haiku-4-5"))
        self.assertEqual(get_meter().calls, 0)


# ---------------------------------------------------------------------------
# Instrumentation: _call_llm records usage + enforces budget
# ---------------------------------------------------------------------------

class TestCallLlmInstrumentation(unittest.TestCase):

    def tearDown(self):
        reset_meter()

    def _fake_message(self, text, in_tok, out_tok):
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        msg.usage.input_tokens = in_tok
        msg.usage.output_tokens = out_tok
        return msg

    def test_call_llm_records_usage(self):
        reset_meter()
        from waa.analysis import analyzer
        fake_client = MagicMock()
        fake_client.messages.create.return_value = self._fake_message("hi", 800, 120)
        with patch.object(analyzer.anthropic, "Anthropic", return_value=fake_client):
            out = analyzer._call_llm("prompt", model="claude-haiku-4-5")
        self.assertEqual(out, "hi")
        self.assertEqual(get_meter().calls, 1)
        self.assertEqual(get_meter().total_tokens(), (800, 120))

    def test_call_llm_enforces_budget_before_calling(self):
        import waa.config as cfg
        from waa.analysis import analyzer
        reset_meter()
        get_meter().record("claude-sonnet-4-6", 1_000_000, 0)  # already $3
        fake_client = MagicMock()
        with patch.object(cfg, "COST_BUDGET_USD", 1.0):
            with patch.object(analyzer.anthropic, "Anthropic", return_value=fake_client):
                with self.assertRaises(BudgetExceeded):
                    analyzer._call_llm("prompt")
        # the SDK was never hit
        fake_client.messages.create.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
