"""
Per-run cost budget + telemetry (improvement #18).

Every Anthropic call funnels through `analyzer._call_llm` / `_call_llm_vision`.
This module is the single place that knows what a call costs: it records the
real token usage off each response, prices it per model, accumulates a per-run
total, and (optionally) enforces a hard USD cap so an unattended campaign can
never run away with the bill.

SOLID:
- `ModelPricing` is the single source of truth for $/token; new models plug in
  via the `MODEL_PRICING` map without touching any call site (OCP).
- `UsageRecord` is an immutable value object; `CostMeter` has one job — collect
  records and total them (SRP).
- `Budget` depends only on a meter's totals, so it is trivially unit-tested and
  independent of where the spend came from (DIP).
- The module-level meter is a deliberate process singleton (the bill is global)
  but every consumer reads it through `get_meter()`, so tests reset it cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Pricing — USD per million tokens (Anthropic list prices, approximate).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000 * self.input_per_mtok
                + output_tokens / 1_000_000 * self.output_per_mtok)


# Keyed by a substring of the model id so minor version suffixes still match.
MODEL_PRICING: dict[str, ModelPricing] = {
    "sonnet": ModelPricing(3.0, 15.0),
    "haiku": ModelPricing(1.0, 5.0),
    "opus": ModelPricing(15.0, 75.0),
}

# Unknown models are priced as Sonnet — conservative, never under-reports.
DEFAULT_PRICING = ModelPricing(3.0, 15.0)


def pricing_for(model: str) -> ModelPricing:
    m = (model or "").lower()
    for key, pricing in MODEL_PRICING.items():
        if key in m:
            return pricing
    return DEFAULT_PRICING


# ---------------------------------------------------------------------------
# Usage records + meter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageRecord:
    model: str
    input_tokens: int
    output_tokens: int
    cost: float
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class CostMeter:
    """Accumulates per-call usage and totals it for one run."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []

    def record(self, model: str, input_tokens: int, output_tokens: int,
               label: str = "") -> UsageRecord:
        cost = pricing_for(model).cost(input_tokens, output_tokens)
        rec = UsageRecord(model, int(input_tokens), int(output_tokens), cost, label)
        self._records.append(rec)
        return rec

    @property
    def records(self) -> list[UsageRecord]:
        return list(self._records)

    @property
    def calls(self) -> int:
        return len(self._records)

    def total_cost(self) -> float:
        return sum(r.cost for r in self._records)

    def total_tokens(self) -> tuple[int, int]:
        return (sum(r.input_tokens for r in self._records),
                sum(r.output_tokens for r in self._records))

    def by_model(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for r in self._records:
            slot = out.setdefault(r.model, {"calls": 0, "input_tokens": 0,
                                            "output_tokens": 0, "cost": 0.0})
            slot["calls"] += 1
            slot["input_tokens"] += r.input_tokens
            slot["output_tokens"] += r.output_tokens
            slot["cost"] += r.cost
        return out

    def to_dict(self) -> dict:
        cin, cout = self.total_tokens()
        return {
            "calls": self.calls,
            "input_tokens": cin,
            "output_tokens": cout,
            "total_cost": round(self.total_cost(), 4),
            "by_model": self.by_model(),
        }

    def summary(self) -> str:
        if not self._records:
            return "LLM cost this run: no calls"
        cin, cout = self.total_tokens()
        lines = [
            f"LLM cost this run: ${self.total_cost():.4f} over {self.calls} call(s) "
            f"({cin:,} in / {cout:,} out tokens)"
        ]
        for model, s in sorted(self.by_model().items(),
                               key=lambda kv: -kv[1]["cost"]):
            short = _short_model(model)
            lines.append(
                f"  {short}: {s['calls']} call(s), "
                f"{s['input_tokens']:,} in / {s['output_tokens']:,} out, "
                f"${s['cost']:.4f}"
            )
        return "\n".join(lines)


def _short_model(model: str) -> str:
    m = (model or "").lower()
    for key in MODEL_PRICING:
        if key in m:
            return key
    return model or "unknown"


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Raised when a run hits its configured USD cap."""


@dataclass(frozen=True)
class Budget:
    """A hard ceiling on a single run's spend. `max_usd <= 0` means no cap."""
    max_usd: float = 0.0

    def is_active(self) -> bool:
        return self.max_usd and self.max_usd > 0

    def exceeded_by(self, meter: CostMeter) -> bool:
        return self.is_active() and meter.total_cost() >= self.max_usd

    def check(self, meter: CostMeter) -> None:
        if self.exceeded_by(meter):
            raise BudgetExceeded(
                f"cost budget reached: ${meter.total_cost():.4f} "
                f"spent of ${self.max_usd:.2f} cap"
            )


# ---------------------------------------------------------------------------
# Process-global meter + helpers used by the SDK chokepoints
# ---------------------------------------------------------------------------

_METER = CostMeter()


def get_meter() -> CostMeter:
    return _METER


def reset_meter() -> CostMeter:
    """Start a fresh per-run meter (call at the top of each command)."""
    global _METER
    _METER = CostMeter()
    return _METER


def active_budget() -> Budget:
    """Build the budget from config (COST_BUDGET_USD). 0/absent => no cap."""
    try:
        from waa import config
        cap = float(getattr(config, "COST_BUDGET_USD", 0) or 0)
    except (ValueError, TypeError):
        cap = 0.0
    return Budget(max_usd=cap)


def enforce_budget() -> None:
    """Raise BudgetExceeded if the run has already hit its cap. Called before
    each LLM request so spend stops within one call of the limit."""
    active_budget().check(get_meter())


def record_message_usage(message, model: str, label: str = "") -> Optional[UsageRecord]:
    """Defensively read `message.usage` off an Anthropic response and record it.
    Never raises — telemetry must not break a real call."""
    try:
        usage = getattr(message, "usage", None)
        if usage is None:
            return None
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        return get_meter().record(model, in_tok, out_tok, label=label)
    except Exception:
        return None
