"""
Model tiering (improvement #14).

One place decides which model each kind of task uses, so cheap high-volume
work (search-query generation, lead qualification, future critics) runs on
the cheap model, and only the low-volume final copywriting for qualified
leads pays for the premium model. That split is the main lever on token cost.

SOLID:
- `ModelTier` names the *intent* (CHEAP / PREMIUM), never a vendor id, so call
  sites read by purpose, not by model string.
- `ModelPolicy` is the single source of truth mapping tier -> concrete model
  id (SRP). Swap models in config without touching any call site (OCP).
- `LLMClient` depends on an injectable transport callable, not the Anthropic
  SDK directly (DIP), so it is trivially unit-tested with a fake — no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class ModelTier(str, Enum):
    """The intent of a call, decoupled from any concrete model id."""
    CHEAP = "cheap"      # high-volume, low-stakes: queries, qualify, critics
    PREMIUM = "premium"  # low-volume, high-stakes: final email copywriting


@dataclass(frozen=True)
class ModelPolicy:
    """Single source of truth: which concrete model backs each tier."""
    cheap_model: str
    premium_model: str

    def model_for(self, tier: ModelTier) -> str:
        if tier is ModelTier.CHEAP:
            return self.cheap_model
        if tier is ModelTier.PREMIUM:
            return self.premium_model
        raise ValueError(f"unknown model tier: {tier!r}")

    @classmethod
    def from_config(cls) -> "ModelPolicy":
        from waa import config
        return cls(cheap_model=config.QUALIFY_MODEL,
                   premium_model=config.LLM_MODEL)


# A transport turns (prompt, model, max_tokens) into completion text. The
# default hits Anthropic via analyzer._call_llm; tests inject a fake.
Transport = Callable[[str, str, int], str]


def _default_transport(prompt: str, model: str, max_tokens: int) -> str:
    from waa.analysis.analyzer import _call_llm
    return _call_llm(prompt, model=model, max_tokens=max_tokens)


class LLMClient:
    """
    Tier-aware LLM caller. Callers choose a TIER, never a model id; the policy
    resolves it. Inject a `transport` (and/or `policy`) to test without any
    network call.
    """

    def __init__(self, policy: Optional[ModelPolicy] = None,
                 transport: Optional[Transport] = None) -> None:
        self._policy = policy or ModelPolicy.from_config()
        self._transport = transport or _default_transport

    def complete(self, prompt: str, *, tier: ModelTier = ModelTier.PREMIUM,
                 max_tokens: int = 2000) -> str:
        model = self._policy.model_for(tier)
        return self._transport(prompt, model, max_tokens)


def default_llm_client() -> LLMClient:
    """A client wired to config + the real Anthropic transport."""
    return LLMClient()
