"""
Profit-weighted lead scoring (improvement #19).

The send channel is volume-capped (Zoho free tier is ~40-50 emails/day), so the
single biggest lever on revenue is WHICH leads get those scarce sends. Two
qualified leads are not equal: a dental practice or law firm with an obviously
dated site is worth many times a small cafe with a tidy one. This module ranks
leads by EXPECTED PROFIT to the agency so the daily quota is spent best-first.

Expected profit is composed, deterministically and with no tokens, from three
signals already produced upstream:

  - niche value   : how much a client in this market is worth (deal size /
                    budget) — a professional service outranks a low-margin shop;
  - design need   : how badly the site needs us (low design score from #7,
                    missing niche elements, weak first impression) — more need
                    means a higher chance the owner buys a redesign;
  - reachability  : can we actually reach and personalise to them (contact
                    email present, enough grounding facts).

SOLID:
- `LeadScorer` is the abstraction (one `score()` method).
- `ProfitWeightedScorer` depends only on a `ProfitSignals` value object (DIP),
  so it is trivially unit-tested and the weighting can change without touching
  the signal extraction. New scoring strategies are new `LeadScorer`s (OCP).
- `ProfitSignals` / `LeadScore` are immutable value objects; the `from_*`
  adapters isolate the messy projection off audit dicts in one place (SRP).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from waa.analysis.personalization import SiteFacts


# ---------------------------------------------------------------------------
# Niche value — relative worth (0..1) of winning one client in this market.
# Driven by typical deal size + budget for web work, not by volume of leads.
# ---------------------------------------------------------------------------

NICHE_VALUE: dict[str, float] = {
    # high-LTV professional services / big-ticket bookings
    "advokatska kancelaria": 1.0,
    "uctovnik": 0.9,
    "realitna kancelaria": 0.95,
    "zubar": 0.95,
    "zubna ambulancia": 0.95,
    "fyzioterapia": 0.8,
    "autoservis": 0.8,
    "hotel": 0.9,
    "notar": 0.95,
    "ortodoncia": 0.95,
    "financny poradca": 0.85,
    "poistovaci agent": 0.75,
    "chiropraktik": 0.75,
    "instalater": 0.7,
    "elektrikar": 0.65,
    "pneuservis": 0.65,
    "karoseria": 0.7,
    "jazykova skola": 0.6,
    "detska skolka": 0.6,
    "penzion": 0.7,
    "wellness": 0.7,
    "veterina": 0.75,
    "optika": 0.7,
    "autoskola": 0.7,
    "svadobny fotograf": 0.7,
    # mid: appointment-based local services
    "fitness centrum": 0.6,
    "joga studio": 0.55,
    "kadernictvo": 0.55,
    "barber shop": 0.55,
    "kozmeticky salon": 0.6,
    "nechtove studio": 0.5,
    "masaze": 0.55,
    "tetovacie studio": 0.55,
    "fotograf": 0.6,
    "kvetinarstvo": 0.5,
    "restauracia": 0.5,
    # low-margin / small budgets
    "kaviaren": 0.4,
    "cukraren": 0.4,
    "pekaren": 0.35,
}

DEFAULT_NICHE_VALUE = 0.5


def niche_value(niche: str) -> float:
    return NICHE_VALUE.get((niche or "").lower().strip(), DEFAULT_NICHE_VALUE)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfitSignals:
    """The three 0..1 signals that compose expected profit."""
    niche_value: float        # deal size / budget for this market
    design_need: float        # how badly the site needs a redesign
    reachability: float       # can we reach + personalise to them

    @staticmethod
    def _clamp(x: float) -> float:
        return max(0.0, min(1.0, x))

    def __post_init__(self):
        object.__setattr__(self, "niche_value", self._clamp(self.niche_value))
        object.__setattr__(self, "design_need", self._clamp(self.design_need))
        object.__setattr__(self, "reachability", self._clamp(self.reachability))

    # -- adapters ----------------------------------------------------------

    @classmethod
    def from_facts(cls, facts: "SiteFacts", *, has_contact: bool) -> "ProfitSignals":
        return cls(
            niche_value=niche_value(facts.niche or ""),
            design_need=_design_need_from_facts(
                design_score=facts.design_score,
                missing=len(facts.niche_specific_missing or []),
                has_clear_h1=facts.has_clear_h1,
                has_phone_clickable=facts.has_phone_clickable,
                surprising=bool(facts.surprising_finding),
            ),
            reachability=_reachability(has_contact, facts.fact_count()),
        )

    @classmethod
    def from_result(cls, result: dict) -> "ProfitSignals":
        """Project off a full audit-result dict (analysis.facts + contacts)."""
        facts = (result.get("analysis") or {}).get("facts") or {}
        has_contact = bool(result.get("contact_emails"))
        design = (result.get("analysis") or {}).get("facts", {})
        return cls(
            niche_value=niche_value(facts.get("niche") or ""),
            design_need=_design_need_from_facts(
                design_score=design.get("design_score"),
                missing=len(facts.get("niche_specific_missing") or []),
                has_clear_h1=facts.get("has_clear_h1"),
                has_phone_clickable=facts.get("has_phone_clickable"),
                surprising=bool(facts.get("surprising_finding")),
            ),
            reachability=_reachability(has_contact, _fact_count_from_dict(facts)),
        )


@dataclass(frozen=True)
class LeadScore:
    value: int                       # 0-100 expected-profit score
    tier: str                        # high | medium | low
    breakdown: dict = field(default_factory=dict)  # per-signal contribution

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------

class LeadScorer(ABC):
    @abstractmethod
    def score(self, signals: ProfitSignals) -> LeadScore:
        ...


@dataclass(frozen=True)
class ScoreWeights:
    niche_value: float = 0.45
    design_need: float = 0.40
    reachability: float = 0.15

    def total(self) -> float:
        return self.niche_value + self.design_need + self.reachability


class ProfitWeightedScorer(LeadScorer):
    """Weighted blend of the three signals into a 0-100 expected-profit score."""

    def __init__(self, weights: Optional[ScoreWeights] = None,
                 high: int = 70, medium: int = 45) -> None:
        self._w = weights or ScoreWeights()
        self._high = high
        self._medium = medium

    def score(self, signals: ProfitSignals) -> LeadScore:
        w = self._w
        total_w = w.total() or 1.0
        parts = {
            "niche_value": w.niche_value * signals.niche_value,
            "design_need": w.design_need * signals.design_need,
            "reachability": w.reachability * signals.reachability,
        }
        raw = sum(parts.values()) / total_w  # 0..1
        value = round(raw * 100)
        tier = ("high" if value >= self._high
                else "medium" if value >= self._medium else "low")
        breakdown = {k: round(v / total_w * 100) for k, v in parts.items()}
        return LeadScore(value=value, tier=tier, breakdown=breakdown)


def build_default_scorer() -> ProfitWeightedScorer:
    return ProfitWeightedScorer()


def score_result(result: dict, scorer: Optional[LeadScorer] = None) -> LeadScore:
    """Convenience: score a full audit-result dict."""
    scorer = scorer or build_default_scorer()
    return scorer.score(ProfitSignals.from_result(result))


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _design_need_from_facts(*, design_score, missing: int, has_clear_h1,
                            has_phone_clickable, surprising: bool) -> float:
    """How badly the site needs a redesign, 0..1 (higher = more need)."""
    if design_score is not None:
        # design_score is 0-10 health (#7); need is its inverse.
        need = (10 - float(design_score)) / 10.0
    else:
        # Fallback when the smell scanner didn't run: assemble from facts.
        need = 0.0
        if has_clear_h1 is False:
            need += 0.35
        if has_phone_clickable is False:
            need += 0.25
        if surprising:
            need += 0.20
        need += min(missing, 3) * 0.10
    return max(0.0, min(1.0, need))


def _reachability(has_contact: bool, fact_count: int) -> float:
    """Can we reach + personalise: contact email dominates, facts refine."""
    base = 1.0 if has_contact else 0.3
    detail = 0.6 + 0.4 * (min(fact_count, 5) / 5.0)  # 0.6..1.0
    return base * detail


def _fact_count_from_dict(facts: dict) -> int:
    """Mirror SiteFacts.fact_count() over a plain dict (for from_result)."""
    n = 0
    if facts.get("h1"):
        n += 1
    if facts.get("primary_cta_text"):
        n += 1
    if facts.get("city_or_area"):
        n += 1
    if facts.get("niche_specific_present") or facts.get("niche_specific_missing"):
        n += 1
    if facts.get("surprising_finding"):
        n += 1
    if facts.get("high_confidence_finding"):
        n += 1
    return n
