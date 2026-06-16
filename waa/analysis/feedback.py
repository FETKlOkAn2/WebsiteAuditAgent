"""
Feedback loop / A-B tracking (improvement #20).

Cold outreach only improves if you can see WHICH choices earn replies. Every
send already lands in `sent_registry.json`, and the replies monitor stamps a
`reply_received_at` on the ones that answered. This module joins those two
facts and computes the reply rate per segment — by niche, by sender, by lead
value tier, by subject length — so the next campaign can lean into what works
and drop what doesn't.

It is pure and deterministic (no tokens, no network): it reads the registry
dict and returns a report. The send path records the experiment "dimensions"
on each entry so there is something to segment by.

SOLID:
- `Dimension` is a small strategy object (name + a function that buckets one
  registry entry); new things to A-B test plug in as new Dimensions (OCP)
  without touching the analyzer.
- `SegmentStat` / `FeedbackReport` are immutable value objects.
- `FeedbackAnalyzer` has one job: registry -> report (SRP), and depends on the
  Dimension abstraction, not on any concrete key (DIP).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Callable, Optional, Sequence


# ---------------------------------------------------------------------------
# Dimensions — each buckets one registry entry into a segment label
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Dimension:
    name: str
    extract: Callable[[dict], str]


def _dims(entry: dict) -> dict:
    d = entry.get("dimensions")
    return d if isinstance(d, dict) else {}


def _niche(entry: dict) -> str:
    return (_dims(entry).get("niche") or "unknown").strip().lower() or "unknown"


def _sender(entry: dict) -> str:
    return (_dims(entry).get("sender") or "unknown").strip() or "unknown"


def _lead_tier(entry: dict) -> str:
    return (_dims(entry).get("lead_tier") or "unknown").strip().lower() or "unknown"


def _subject_length(entry: dict) -> str:
    """Bucket the subject by word count — short subjects often reply better."""
    n = len((entry.get("subject") or "").split())
    if n == 0:
        return "unknown"
    if n <= 3:
        return "1-3 words"
    if n <= 6:
        return "4-6 words"
    return "7+ words"


def _followed_up(entry: dict) -> str:
    return "followed-up" if entry.get("followup_sent_at") else "first-touch only"


DEFAULT_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("niche", _niche),
    Dimension("sender", _sender),
    Dimension("lead_tier", _lead_tier),
    Dimension("subject_length", _subject_length),
    Dimension("follow_up", _followed_up),
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentStat:
    dimension: str
    segment: str
    sent: int
    replied: int

    @property
    def reply_rate(self) -> float:
        return self.replied / self.sent if self.sent else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["reply_rate"] = round(self.reply_rate, 4)
        return d


@dataclass(frozen=True)
class FeedbackReport:
    total_sent: int
    total_replied: int
    segments: tuple[SegmentStat, ...] = field(default_factory=tuple)

    @property
    def overall_reply_rate(self) -> float:
        return self.total_replied / self.total_sent if self.total_sent else 0.0

    def by_dimension(self, name: str, *, min_sent: int = 1) -> list[SegmentStat]:
        """Segments for one dimension, best reply rate first (then by volume)."""
        rows = [s for s in self.segments
                if s.dimension == name and s.sent >= min_sent]
        rows.sort(key=lambda s: (-s.reply_rate, -s.sent))
        return rows

    def dimensions(self) -> list[str]:
        seen, out = set(), []
        for s in self.segments:
            if s.dimension not in seen:
                seen.add(s.dimension)
                out.append(s.dimension)
        return out

    def to_dict(self) -> dict:
        return {
            "total_sent": self.total_sent,
            "total_replied": self.total_replied,
            "overall_reply_rate": round(self.overall_reply_rate, 4),
            "segments": [s.to_dict() for s in self.segments],
        }

    def summary(self, *, min_sent: int = 1) -> str:
        if not self.total_sent:
            return "No sends recorded yet — nothing to learn from."
        pct = self.overall_reply_rate * 100
        lines = [
            f"Feedback over {self.total_sent} send(s): "
            f"{self.total_replied} replied ({pct:.1f}% overall)"
        ]
        for dim in self.dimensions():
            rows = self.by_dimension(dim, min_sent=min_sent)
            if not rows:
                continue
            lines.append(f"\n  by {dim}:")
            for s in rows:
                lines.append(
                    f"    {s.segment:<18} {s.replied}/{s.sent} "
                    f"({s.reply_rate * 100:.0f}%)"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class FeedbackAnalyzer:
    """Turns a sent-registry into reply-rate stats per segment."""

    def __init__(self, dimensions: Optional[Sequence[Dimension]] = None) -> None:
        self._dimensions = tuple(dimensions) if dimensions else DEFAULT_DIMENSIONS

    @staticmethod
    def _replied(entry: dict) -> bool:
        return bool(entry.get("reply_received_at"))

    def analyze(self, registry: dict) -> FeedbackReport:
        emails = (registry or {}).get("emails", {}) or {}
        entries = [e for e in emails.values() if isinstance(e, dict)]

        total_sent = len(entries)
        total_replied = sum(1 for e in entries if self._replied(e))

        # (dimension, segment) -> [sent, replied]
        buckets: dict[tuple[str, str], list[int]] = {}
        for entry in entries:
            replied = self._replied(entry)
            for dim in self._dimensions:
                try:
                    seg = dim.extract(entry) or "unknown"
                except Exception:
                    seg = "unknown"
                slot = buckets.setdefault((dim.name, seg), [0, 0])
                slot[0] += 1
                if replied:
                    slot[1] += 1

        segments = tuple(
            SegmentStat(dim, seg, sent, replied)
            for (dim, seg), (sent, replied) in buckets.items()
        )
        return FeedbackReport(total_sent, total_replied, segments)
