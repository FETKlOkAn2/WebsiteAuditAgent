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

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


def parse_json(text: str) -> dict:
    """Parse a JSON object from LLM output, tolerating markdown fences,
    surrounding prose, and the common LLM mistakes that break json.loads
    (trailing commas, unescaped double quotes inside a string value, raw
    newlines/tabs inside strings). Shared by the cheap-tier consumers
    (gating, critic) and the v2 email generator.

    Raises json.JSONDecodeError if nothing parses, so callers can still retry.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()

    # Narrow to the outermost { ... } object if there's surrounding prose.
    m = re.search(r"\{.*\}", text, re.S)
    candidates = [text]
    if m:
        candidates.append(m.group(0))

    last_err: Optional[json.JSONDecodeError] = None
    for cand in candidates:
        for attempt in (cand, _repair_json(cand)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError as e:
                last_err = e
    raise last_err if last_err else json.JSONDecodeError("no JSON found", text or "", 0)


def _repair_json(text: str) -> str:
    """Best-effort repair of the JSON mistakes LLMs make most often.

    - removes trailing commas before } or ]
    - escapes raw control characters (newline/tab/CR) that appear INSIDE string
      literals, and escapes stray double quotes inside a string value (the
      "...the "Book" button..." case that produces 'Expecting , delimiter').

    A tiny state machine walks the text tracking whether we're inside a string;
    a double quote inside a string is treated as literal (and escaped) unless
    the next non-space character closes the value (`,` `}` `]` `:`).
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            if not in_string:
                in_string = True
                out.append(ch)
            else:
                # Does this quote actually close the string? Look ahead past
                # whitespace for a structural character.
                nxt = _next_nonspace(text, i + 1)
                if nxt in {",", "}", "]", ":", ""}:
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')  # stray inner quote -> escape it
            continue
        if in_string and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
    repaired = "".join(out)
    # Trailing commas: {"a":1,} or [1,2,]
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return repaired


def _next_nonspace(text: str, start: int) -> str:
    for j in range(start, len(text)):
        if not text[j].isspace():
            return text[j]
    return ""


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
