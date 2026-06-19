"""
Lead qualification gates — cheap-before-expensive (improvement #16).

The expensive step in the pipeline is the Sonnet email generation. This
module puts a chain of progressively-more-expensive gates in front of it, so
we never spend Sonnet tokens on a prospect that a free heuristic — or a cheap
Haiku judgement — already rejected. At ~10x the price of Haiku, every Sonnet
call we avoid is the single biggest lever on cost.

Design (SOLID):
- `LeadGate` is the abstraction; each gate has ONE reason to change (SRP).
- New gates drop into the chain without touching existing ones (OCP).
- Every gate returns the same `GateDecision` and is interchangeable (LSP).
- The interface is a single `evaluate()` method (ISP).
- `HaikuQualifyGate` depends on the `QualifyModel` abstraction, not the
  Anthropic SDK, so it is trivial to test with a fake (DIP).

The chain runs cheapest-first and short-circuits on the first rejection.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from waa.analysis.personalization import SiteFacts
    from waa.core.llm import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateDecision:
    """The outcome of one gate (or the whole chain)."""
    passed: bool
    stage: str
    reason: str
    score: Optional[float] = None


@dataclass
class LeadContext:
    """
    Everything the gates need to judge a prospect, computed once (for free)
    by the orchestrator and reused by every gate so nothing is recomputed.
    """
    url: str
    niche: str
    contact_emails: Sequence[str]
    facts: "SiteFacts"


# ---------------------------------------------------------------------------
# Gate abstraction + free (deterministic) gates
# ---------------------------------------------------------------------------

class LeadGate(ABC):
    """A single yes/no check on a prospect."""

    name: str = "gate"

    @abstractmethod
    def evaluate(self, lead: LeadContext) -> GateDecision:
        ...


class ContactEmailGate(LeadGate):
    """Free. No reachable address => nothing to send, drop immediately."""

    name = "contact_email"

    def evaluate(self, lead: LeadContext) -> GateDecision:
        if any((e or "").strip() for e in lead.contact_emails):
            return GateDecision(True, self.name, "contact email present")
        return GateDecision(False, self.name, "no contact email on the site")


class PersonalizableGate(LeadGate):
    """Free. Too few concrete facts => the email would be generic; drop."""

    name = "personalizable"

    def __init__(self, min_facts: int = 3) -> None:
        self._min_facts = min_facts

    def evaluate(self, lead: LeadContext) -> GateDecision:
        n = lead.facts.fact_count()
        if n >= self._min_facts:
            return GateDecision(True, self.name, f"{n} grounding facts")
        return GateDecision(
            False, self.name,
            f"only {n}/{self._min_facts} facts to personalise on",
        )


# ---------------------------------------------------------------------------
# Cheap-LLM (Haiku) qualify gate
# ---------------------------------------------------------------------------

@runtime_checkable
class QualifyModel(Protocol):
    """Anything that can answer a qualify prompt. Implemented by
    AnthropicQualifier in prod and by a fake in tests."""

    def complete(self, prompt: str) -> str:
        ...


_QUALIFY_PROMPT = """\
You screen cold-outreach prospects for a web-design studio that sells FULL
WEBSITE REDESIGNS to local businesses. The pitch is more customers, bookings and
revenue from a modern, higher-converting site — NOT fixing isolated bugs. A site
that merely "works" is still a strong prospect if it looks dated, generic, or
under-converts.

Score 0-10 how good a REDESIGN prospect this is and set worth_contacting.

Lean YES (score 6-9) when BOTH hold:
- it's a real local business that plausibly pays for web work (clinic, dentist,
  law/notary office, salon, gym, hotel, trades, services...), AND
- there is room to improve: dated or generic design, a low design score,
  weak conversion path, thin trust signals, or poor mobile.

Say NO (score 0-3) only for: big national brands or chains, directories /
aggregators / social pages (not a single business's own site), or a genuinely
modern, polished, high-converting site with nothing meaningful to improve.

Do NOT penalise a site just for lacking an obvious "bug" — a tidy but dated or
plain site is exactly who we redesign.

Return ONLY JSON, nothing else:
{{"score": <0-10 integer>, "worth_contacting": <true|false>, "reason": "<max 15 words>"}}

FACTS
niche: {niche}
city: {city}
H1: "{h1}"
main button: "{cta}"
phone tappable: {phone}
design quality (0-10, lower = more redesign upside): {design_score}
dated-design signals: {design_smells}
niche elements present: {present}
niche elements missing: {missing}
notable issue: {surprise}
top finding: {hi}
"""


class HaikuQualifyGate(LeadGate):
    """
    Cheap LLM judgement: is this lead worth an expensive personalised email?

    Depends on a `QualifyModel`, not the SDK (DIP). On any model/parse error
    it fails OPEN by default (lets the lead through) so a flaky cheap call
    never silently drops a potentially good prospect; flip `fail_open=False`
    to prioritise cost over coverage.
    """

    name = "qualify"

    def __init__(self, model: QualifyModel, threshold: float = 6.0,
                 fail_open: bool = True) -> None:
        self._model = model
        self._threshold = threshold
        self._fail_open = fail_open

    def evaluate(self, lead: LeadContext) -> GateDecision:
        prompt = self._build_prompt(lead)
        try:
            raw = self._model.complete(prompt)
            data = _parse_json(raw)
            score = float(data.get("score", 0))
            worth = bool(data.get("worth_contacting", score >= self._threshold))
            reason = str(data.get("reason", ""))[:200]
        except Exception as e:  # transient API / malformed output
            logger.warning(f"qualify gate error for {lead.url}: {e}")
            verdict = "open" if self._fail_open else "closed"
            return GateDecision(self._fail_open, self.name,
                                f"qualifier error, failing {verdict}")
        passed = worth and score >= self._threshold
        return GateDecision(passed, self.name,
                            f"score {score:.0f}/10: {reason}", score=score)

    def _build_prompt(self, lead: LeadContext) -> str:
        f = lead.facts
        # Translate the Slovak niche slug to an English market label the cheap
        # model actually understands. Passing the raw slug (e.g. "zubar") made
        # Haiku answer "Unknown niche" and tank the score, dropping good leads.
        from waa.analysis.business_case import profile_for
        niche_en = profile_for(lead.niche).niche_en if lead.niche else "local business"
        design_score = getattr(f, "design_score", None)
        design_smells = getattr(f, "design_smells", None) or []
        return _QUALIFY_PROMPT.format(
            niche=niche_en,
            city=f.city_or_area or "(unknown)",
            h1=(f.h1 or "(none)"),
            cta=(f.primary_cta_text or "(none)"),
            phone="yes" if f.has_phone_clickable else "no",
            design_score=("(unknown)" if design_score is None else design_score),
            design_smells=", ".join(design_smells) or "(none)",
            present=", ".join(f.niche_specific_present) or "(none)",
            missing=", ".join(f.niche_specific_missing) or "(none)",
            surprise=f.surprising_finding or "(none)",
            hi=f.high_confidence_finding or "(none)",
        )


class AnthropicQualifier:
    """`QualifyModel` backed by the CHEAP model tier (improvement #14).

    Depends on an LLMClient so the concrete model lives in one place
    (ModelPolicy), not hardcoded here.
    """

    def __init__(self, client: Optional["LLMClient"] = None) -> None:
        from waa.core.llm import default_llm_client
        self._client = client or default_llm_client()

    def complete(self, prompt: str) -> str:
        from waa.core.llm import ModelTier
        return self._client.complete(prompt, tier=ModelTier.CHEAP, max_tokens=200)


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

class GateChain:
    """Runs gates in order, short-circuiting on the first rejection."""

    def __init__(self, gates: Sequence[LeadGate]) -> None:
        self._gates = list(gates)

    def evaluate(self, lead: LeadContext) -> GateDecision:
        for gate in self._gates:
            decision = gate.evaluate(lead)
            if not decision.passed:
                return decision
        return GateDecision(True, "all", "passed all gates")


def build_lead_gate_chain(
    *,
    qualify: bool = True,
    require_contact: bool = False,
    min_facts: int = 3,
    qualifier: Optional[QualifyModel] = None,
    threshold: Optional[float] = None,
) -> GateChain:
    """
    Default chain, cheapest-first: personalizable -> (Haiku qualify).

    `ContactEmailGate` is NOT in the default chain because having a contact
    address is a SEND-time concern (handled by the caller's require_email
    check and by _prepare_send_list); audit/preview should still generate an
    email even when no address was scraped. Set `require_contact=True` to
    prepend it. The Haiku gate is added only when `qualify` is True; inject a
    fake `qualifier` in tests to avoid any network call.
    """
    gates: list[LeadGate] = []
    if require_contact:
        gates.append(ContactEmailGate())
    gates.append(PersonalizableGate(min_facts))
    if qualify:
        from waa import config
        model = qualifier or AnthropicQualifier()
        thr = config.QUALIFY_THRESHOLD if threshold is None else threshold
        gates.append(HaikuQualifyGate(model, thr))
    return GateChain(gates)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> dict:
    """Parse JSON from LLM output (shared helper in waa.core.llm)."""
    from waa.core.llm import parse_json
    return parse_json(text)
