"""
Tests for the model-tiering layer (improvement #14).

Fully offline: LLMClient takes an injected transport, so no network. Also
asserts that the cheap-tier consumers (qualify gate, query generation) really
resolve to the CHEAP model.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from waa.core.llm import (  # noqa: E402
    ModelTier, ModelPolicy, LLMClient, default_llm_client,
)


class _RecordingTransport:
    """Captures (prompt, model, max_tokens) and returns a canned reply."""
    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls = []
    def __call__(self, prompt, model, max_tokens):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens})
        return self.reply


POLICY = ModelPolicy(cheap_model="cheap-x", premium_model="premium-y")


# ---------------------------------------------------------------------------
# ModelPolicy
# ---------------------------------------------------------------------------

class TestModelPolicy(unittest.TestCase):

    def test_resolves_each_tier(self):
        self.assertEqual(POLICY.model_for(ModelTier.CHEAP), "cheap-x")
        self.assertEqual(POLICY.model_for(ModelTier.PREMIUM), "premium-y")

    def test_from_config_maps_to_config_ids(self):
        from waa import config
        p = ModelPolicy.from_config()
        self.assertEqual(p.cheap_model, config.QUALIFY_MODEL)
        self.assertEqual(p.premium_model, config.LLM_MODEL)

    def test_is_frozen(self):
        with self.assertRaises(Exception):
            POLICY.cheap_model = "mutate"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

class TestLLMClient(unittest.TestCase):

    def test_default_tier_is_premium(self):
        t = _RecordingTransport()
        LLMClient(POLICY, t).complete("hi")
        self.assertEqual(t.calls[0]["model"], "premium-y")

    def test_cheap_tier_uses_cheap_model(self):
        t = _RecordingTransport()
        LLMClient(POLICY, t).complete("hi", tier=ModelTier.CHEAP)
        self.assertEqual(t.calls[0]["model"], "cheap-x")

    def test_passes_prompt_and_max_tokens(self):
        t = _RecordingTransport(reply="done")
        out = LLMClient(POLICY, t).complete("the prompt", tier=ModelTier.CHEAP, max_tokens=123)
        self.assertEqual(out, "done")
        self.assertEqual(t.calls[0]["prompt"], "the prompt")
        self.assertEqual(t.calls[0]["max_tokens"], 123)

    def test_default_client_constructs(self):
        self.assertIsInstance(default_llm_client(), LLMClient)


# ---------------------------------------------------------------------------
# Consumers resolve to the CHEAP tier
# ---------------------------------------------------------------------------

class TestCheapConsumers(unittest.TestCase):

    def test_qualifier_uses_cheap_tier(self):
        from waa.analysis.gating import AnthropicQualifier
        t = _RecordingTransport(reply='{"score": 7, "worth_contacting": true}')
        client = LLMClient(POLICY, t)
        AnthropicQualifier(client=client).complete("judge this lead")
        self.assertEqual(t.calls[0]["model"], "cheap-x")
        self.assertEqual(t.calls[0]["max_tokens"], 200)

    def test_search_with_llm_uses_cheap_tier(self):
        from waa.discovery import prospector
        t = _RecordingTransport(reply='["kaviaren bratislava", "kaviaren bratislava menu"]')
        client = LLMClient(POLICY, t)
        queries = prospector.search_with_llm("kaviaren", "Bratislava", count=2, client=client)
        self.assertEqual(queries, ["kaviaren bratislava", "kaviaren bratislava menu"])
        self.assertEqual(t.calls[0]["model"], "cheap-x")

    def test_search_with_llm_strips_markdown_fences(self):
        from waa.discovery import prospector
        t = _RecordingTransport(reply='```json\n["a", "b"]\n```')
        queries = prospector.search_with_llm("x", "Y", client=LLMClient(POLICY, t))
        self.assertEqual(queries, ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
