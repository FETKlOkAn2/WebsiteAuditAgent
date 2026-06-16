"""
Vision design critic (improvement #6).

The prospect already employs a designer, so an email that points at an obvious
bug ("your copyright says 2019") is easy to wave away. What actually justifies
a paid redesign is a credible, specific critique of the *visual design itself* —
hierarchy, spacing, dated styling, mobile layout, trust cues. A heuristic HTML
audit cannot see any of that; only looking at the rendered page can.

This module feeds the proof screenshot we already capture to a vision model and
gets back a structured, money-framed design critique: a 0-10 visual-quality
score, a "looks dated" flag, and a handful of `DesignFinding`s, each tying a
visible design weakness to a commercial consequence. That critique strengthens
the warm reply/follow-up (where the screenshot is shown anyway) with concrete
observations a designer's client will recognise.

SOLID:
- `DesignCritic` is the abstraction (one `critique()` method).
- `VisionDesignCritic` depends on an injectable `VisionTransport` callable, not
  the Anthropic SDK (DIP), so it is unit-tested with a fake — no network, no
  real image decoding required.
- It runs on the CHEAP vision tier and FAILS OPEN: any model / parse / IO error
  yields an `available=False` critique rather than blocking the pipeline.
- `NullDesignCritic` (Null Object) lets callers disable critique without None
  checks everywhere.
- `DesignFinding` / `DesignCritique` are immutable value objects.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

# Aspects we recognise; anything else from the model is normalised to "other".
_KNOWN_ASPECTS = {
    "hierarchy", "spacing", "color", "typography",
    "mobile", "dated", "trust", "imagery", "other",
}


@dataclass(frozen=True)
class DesignFinding:
    """One visible design weakness tied to a commercial consequence."""
    aspect: str               # hierarchy | spacing | color | typography | mobile | dated | trust | imagery | other
    severity: str             # low | medium | high
    observation: str          # what is visually wrong (what the eye sees)
    redesign_rationale: str   # why fixing it (a redesign) makes the owner money

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, 0)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DesignCritique:
    """The vision model's verdict on a rendered page."""
    available: bool
    score: float = -1.0                 # 0-10 visual quality; lower => more redesign upside
    looks_dated: bool = False
    findings: tuple[DesignFinding, ...] = field(default_factory=tuple)
    summary: str = ""                   # one-line, money-framed redesign hook
    error: Optional[str] = None

    def has_findings(self) -> bool:
        return bool(self.findings)

    def top(self, n: int = 2) -> list[DesignFinding]:
        """Highest-severity findings first."""
        return sorted(self.findings, key=lambda f: f.rank, reverse=True)[:n]

    def summary_for_prompt(self) -> str:
        """A compact, language-neutral block the email/follow-up prompt can
        paraphrase (mirrors business_case). Empty when unavailable."""
        if not self.available or not self.has_findings():
            return ""
        lines = [f"- {f.observation} ({f.redesign_rationale})" for f in self.top(3)]
        head = self.summary or "Design weakens conversion and looks dated."
        return head + "\n" + "\n".join(lines)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


# ---------------------------------------------------------------------------
# Critic abstraction
# ---------------------------------------------------------------------------

class DesignCritic(ABC):
    """Critiques the visual design of a rendered page screenshot."""

    @abstractmethod
    def critique(self, image_path: str, *, niche: str = "",
                 lang: str = "sk") -> DesignCritique:
        ...


class NullDesignCritic(DesignCritic):
    """Always returns an unavailable critique — used when vision is disabled."""

    def critique(self, image_path: str, *, niche: str = "",
                 lang: str = "sk") -> DesignCritique:
        return DesignCritique(available=False, error="design critique disabled")


# A vision transport turns (prompt, image_b64, media_type, model, max_tokens)
# into completion text. The default hits Anthropic; tests inject a fake.
VisionTransport = Callable[[str, str, str, Optional[str], int], str]


def _default_vision_transport(prompt: str, image_b64: str, media_type: str,
                              model: Optional[str], max_tokens: int) -> str:
    from waa.analysis.analyzer import _call_llm_vision
    return _call_llm_vision(prompt, image_b64, media_type,
                            model=model, max_tokens=max_tokens)


_CRITIQUE_PROMPT = """\
You are a senior web designer giving a brutally honest, specific critique of the
screenshot of a small {niche} business homepage. Judge the VISUAL DESIGN only
(layout, hierarchy, spacing, typography, colour, imagery, mobile-readiness,
trust cues) — ignore copy quality and technical bugs.

The owner already has a designer, so be concrete and credible: name what a
trained eye sees, and tie each weakness to lost customers or revenue. Do not
invent problems; if the design is genuinely strong, say so with a high score and
few findings.

Score 0-10 where 10 is a polished, modern, conversion-focused design and 0 is an
amateur, dated page that costs the owner trust and customers.

Return ONLY JSON, nothing else:
{{
  "score": <0-10 integer>,
  "looks_dated": <true|false>,
  "summary": "<one sentence, max 18 words, on the single biggest design problem and its business cost>",
  "findings": [
    {{
      "aspect": "<hierarchy|spacing|color|typography|mobile|dated|trust|imagery>",
      "severity": "<low|medium|high>",
      "observation": "<what the eye sees, max 18 words>",
      "redesign_rationale": "<why fixing it makes the owner money, max 18 words>"
    }}
  ]
}}

Return 2-4 findings, most important first. Keep every string short and free of
dashes."""


class VisionDesignCritic(DesignCritic):
    """Vision-model design critic on the CHEAP tier. Fails open on any error."""

    def __init__(self, transport: Optional[VisionTransport] = None,
                 model: Optional[str] = None, max_tokens: int = 1024) -> None:
        self._transport = transport or _default_vision_transport
        self._model = model
        self._max_tokens = max_tokens

    def critique(self, image_path: str, *, niche: str = "",
                 lang: str = "sk") -> DesignCritique:
        loaded = self._load_image(image_path)
        if loaded is None:
            return DesignCritique(available=False,
                                  error=f"unreadable image: {image_path}")
        image_b64, media_type = loaded

        prompt = _CRITIQUE_PROMPT.format(niche=niche or "local")
        try:
            raw = self._transport(prompt, image_b64, media_type,
                                  self._model, self._max_tokens)
            data = self._parse(raw)
        except Exception as e:  # transient API / malformed output / decode
            logger.warning(f"design critic error for {image_path}: {e}")
            return DesignCritique(available=False, error=f"critic error: {e}")

        return self._build(data)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _load_image(image_path: str) -> Optional[tuple[str, str]]:
        if not image_path or not os.path.isfile(image_path):
            return None
        try:
            with open(image_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            return None
        if not raw:
            return None
        return base64.b64encode(raw).decode("ascii"), _media_type_for(image_path)

    @staticmethod
    def _parse(raw: str) -> dict:
        from waa.core.llm import parse_json
        return parse_json(raw)

    @staticmethod
    def _build(data: dict) -> DesignCritique:
        findings = []
        for item in (data.get("findings") or [])[:4]:
            if not isinstance(item, dict):
                continue
            aspect = str(item.get("aspect", "other")).strip().lower()
            if aspect not in _KNOWN_ASPECTS:
                aspect = "other"
            severity = str(item.get("severity", "medium")).strip().lower()
            if severity not in _SEVERITY_RANK:
                severity = "medium"
            obs = str(item.get("observation", "")).strip()[:200]
            rat = str(item.get("redesign_rationale", "")).strip()[:200]
            if obs:
                findings.append(DesignFinding(aspect, severity, obs, rat))

        try:
            score = float(data.get("score", -1))
        except (TypeError, ValueError):
            score = -1.0

        return DesignCritique(
            available=True,
            score=score,
            looks_dated=bool(data.get("looks_dated", False)),
            findings=tuple(findings),
            summary=str(data.get("summary", "")).strip()[:200],
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _media_type_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    if ext == ".gif":
        return "image/gif"
    return "image/png"
