"""
Business-case layer (improvement #8).

A finding like "missing H1" or "phone not tappable" does not sell a redesign.
A *business case* does: it ties the finding to money, in the language of the
prospect's own market ("restaurant customers on phones call rather than type;
a non-tappable number quietly loses you calls every day"). That reasoning is
what wins the deal and what the designer then delivers against.

This turns the raw conversion-audit `Finding`s into ranked, market-aware
business cases — deterministically, with no LLM tokens. The top case becomes
the angle the email leads with (money first, bug second).

SOLID:
- `NicheProfile` holds the few market facts that change the framing (how
  customers convert, mobile-heavy?, trust-critical?). One source of truth.
- `BusinessCase` is the output value object.
- `BusinessCaseBuilder` has a single job: Finding (+ niche) -> BusinessCase,
  and ranks them. New finding categories or niches plug in via the maps
  without touching the builder logic (OCP).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from waa.analysis.conversion_audit import ConversionAudit, Finding


# ---------------------------------------------------------------------------
# Niche market profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NicheProfile:
    """The handful of market facts that change how a finding costs money."""
    niche_en: str               # readable singular, e.g. "restaurant"
    channel: str                # how customers convert: "call", "online booking", "order", "visit"
    mobile_heavy: bool          # most visitors arrive on a phone
    trust_critical: bool        # choice is dominated by trust/reputation
    # finding categories this market cares about most (get a priority boost)
    priority_categories: tuple[str, ...] = ()


# Keyed by the niche slug used across the app (see prompts_v2.NICHE_TRANSLATIONS_SK).
NICHE_PROFILES: dict[str, NicheProfile] = {
    "restauracia": NicheProfile("restaurant", "call or reservation", True, False, ("local", "cta", "niche")),
    "kaviaren": NicheProfile("cafe", "visit", True, False, ("above_fold", "trust", "local")),
    "fitness centrum": NicheProfile("gym", "online sign-up", True, True, ("cta", "niche", "trust")),
    "joga studio": NicheProfile("yoga studio", "online booking", True, True, ("cta", "niche")),
    "kadernictvo": NicheProfile("hair salon", "online booking", True, True, ("niche", "trust", "cta")),
    "barber shop": NicheProfile("barber shop", "online booking", True, True, ("niche", "cta")),
    "nechtove studio": NicheProfile("nail salon", "online booking", True, True, ("niche", "trust")),
    "kozmeticky salon": NicheProfile("beauty salon", "online booking", True, True, ("niche", "trust")),
    "masaze": NicheProfile("massage studio", "online booking", True, True, ("niche", "cta")),
    "zubar": NicheProfile("dental practice", "call or request", True, True, ("trust", "cta", "niche")),
    "zubna ambulancia": NicheProfile("dental practice", "call or request", True, True, ("trust", "cta", "niche")),
    "fyzioterapia": NicheProfile("physio clinic", "call or booking", True, True, ("trust", "cta")),
    "optika": NicheProfile("optician", "visit", True, False, ("niche", "trust")),
    "veterina": NicheProfile("vet clinic", "call", True, True, ("local", "trust")),
    "hotel": NicheProfile("hotel", "online booking", True, True, ("cta", "trust", "above_fold")),
    "penzion": NicheProfile("guesthouse", "online booking", True, True, ("cta", "trust")),
    "wellness": NicheProfile("wellness centre", "online booking", True, True, ("cta", "trust")),
    "autoservis": NicheProfile("auto repair shop", "call", True, True, ("local", "trust")),
    "kvetinarstvo": NicheProfile("florist", "order", True, False, ("cta", "niche")),
    "advokatska kancelaria": NicheProfile("law firm", "call or consultation", False, True, ("trust", "above_fold")),
    "uctovnik": NicheProfile("accountant", "call or consultation", False, True, ("trust", "above_fold")),
    "realitna kancelaria": NicheProfile("estate agency", "call", True, True, ("trust", "above_fold")),
    "fotograf": NicheProfile("photographer", "enquiry", True, True, ("niche", "trust")),
    "svadobny fotograf": NicheProfile("wedding photographer", "enquiry", True, True, ("niche", "trust")),
    "cukraren": NicheProfile("patisserie", "order or visit", True, False, ("above_fold", "niche")),
    "pekaren": NicheProfile("bakery", "visit", True, False, ("above_fold",)),
    "autoskola": NicheProfile("driving school", "call or sign-up", True, True, ("cta", "trust")),
    "tetovacie studio": NicheProfile("tattoo studio", "enquiry", True, True, ("niche", "trust")),
}

DEFAULT_PROFILE = NicheProfile("local business", "contact", True, True, ("cta", "trust"))


def profile_for(niche: str) -> NicheProfile:
    return NICHE_PROFILES.get((niche or "").lower().strip(), DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
# Business case
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BusinessCase:
    finding_label: str
    headline: str       # money-framed, one line
    reasoning: str      # why it costs money, in this market's terms
    priority: int       # higher = more commercially compelling for this niche
    is_design: bool     # design/credibility issue vs a hard technical bug

    def to_dict(self) -> dict:
        return asdict(self)


# Per finding-category: base priority, whether it's a design issue, and the
# (headline, reasoning) templates. Templates may reference {niche}, {channel}.
_CATEGORY_CASES: dict[str, dict] = {
    "local": {
        "base": 60, "design": False,
        "headline": "mobile visitors can't reach you in one tap",
        "reasoning": "a {niche}'s customers on phones {channel} rather than type; "
                     "a number they can't tap quietly loses you contacts every day",
    },
    "cta": {
        "base": 58, "design": True,
        "headline": "there's no single obvious next step",
        "reasoning": "visitors to a {niche} decide in seconds; with no clear "
                     "way to {channel}, they leave instead of converting",
    },
    "trust": {
        "base": 55, "design": True,
        "headline": "nothing on the page builds trust",
        "reasoning": "reviews and proof are the deciding factor when people pick "
                     "a {niche}; without them they choose a competitor they trust",
    },
    "niche": {
        "base": 52, "design": False,
        "headline": "the site is missing what customers expect",
        "reasoning": "people expect this from a {niche} before they commit; its "
                     "absence makes them hesitate and look elsewhere",
    },
    "above_fold": {
        "base": 50, "design": True,
        "headline": "visitors can't tell what you offer in 3 seconds",
        "reasoning": "a weak first impression for a {niche} means people bounce "
                     "before they understand why they'd choose you",
    },
    "social_proof": {
        "base": 45, "design": True,
        "headline": "the social proof is thin or generic",
        "reasoning": "vague testimonials read as fake; for a {niche} that can "
                     "hurt trust more than having none at all",
    },
    "surprise": {
        "base": 40, "design": True,
        "headline": "the site looks unfinished or dated",
        "reasoning": "leftover placeholder text or an old year makes a new "
                     "customer wonder if the {niche} is even still operating",
    },
    "design": {
        "base": 48, "design": True,
        "headline": "the design looks years out of date",
        "reasoning": "a dated, non-responsive look makes a {niche} seem less "
                     "credible than competitors before anyone reads the offer, "
                     "so visitors who would {channel} bounce instead",
    },
}


class BusinessCaseBuilder:
    """Turns conversion-audit findings into ranked, market-aware business cases."""

    def build(self, finding: "Finding", niche: str) -> Optional[BusinessCase]:
        spec = _CATEGORY_CASES.get(finding.category)
        if not spec:
            return None
        prof = profile_for(niche)
        ctx = {"niche": prof.niche_en, "channel": prof.channel}
        priority = spec["base"]
        if finding.category in prof.priority_categories:
            priority += 15
        if finding.confidence == "high":
            priority += 5
        return BusinessCase(
            finding_label=finding.label,
            headline=spec["headline"].format(**ctx),
            reasoning=spec["reasoning"].format(**ctx),
            priority=priority,
            is_design=spec["design"],
        )

    def build_all(self, audit: "ConversionAudit", niche: str) -> list[BusinessCase]:
        cases = [self.build(f, niche) for f in audit.findings]
        cases = [c for c in cases if c is not None]
        cases.sort(key=lambda c: -c.priority)
        return cases

    def top(self, audit: "ConversionAudit", niche: str) -> Optional[BusinessCase]:
        cases = self.build_all(audit, niche)
        return cases[0] if cases else None

    def summary_for_prompt(self, audit: "ConversionAudit", niche: str) -> str:
        """One short money-framed angle for the email to lead with, or ''."""
        case = self.top(audit, niche)
        if not case:
            return ""
        return f"{case.headline} — {case.reasoning}"
