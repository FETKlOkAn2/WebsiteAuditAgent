"""
Turing critic (improvement #3).

The single biggest cause of "no reply" is an email that reads as AI-written.
After an email is generated, a second CHEAP pass scores how human it sounds.
Below threshold the pipeline regenerates once and, if it still fails, drops
the email rather than sending something that smells generated. This is what
lets the system run unattended — the critic replaces a human reading every
draft.

SOLID:
- `EmailCritic` is the abstraction (one `review()` method).
- `HumanToneCritic` depends on the `LLMClient` tiering layer (DIP), runs on
  the CHEAP tier, and fails OPEN on a flaky cheap call so a transient error
  never silently blocks an otherwise good send.
- A `NullCritic` (always-pass) lets callers disable critique without special
  casing None everywhere (Null Object pattern).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from waa.core.llm import LLMClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticVerdict:
    passed: bool
    score: float          # 0-10 human-ness; -1 when the critic errored
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class EmailCritic(ABC):
    """Scores how human a finished email reads."""

    @abstractmethod
    def review(self, email_body: str, *, lang: str = "sk") -> CriticVerdict:
        ...


class NullCritic(EmailCritic):
    """Always passes — used when critique is disabled."""

    def review(self, email_body: str, *, lang: str = "sk") -> CriticVerdict:
        return CriticVerdict(True, score=10.0, reason="critique disabled")


_CRITIC_PROMPT = """\
You are a skeptical small-business owner who gets dozens of cold emails a week
and instantly deletes anything that smells automated. Read the email below.

Score 0-10 how much it reads like a real, busy person typed it themselves:
  10 = unmistakably human, specific, a bit informal
   0 = obviously AI / template / mail-merge

Penalise hard: generic phrasing, over-politeness, marketing buzzwords, perfect
symmetry, "I hope this finds you", three-part lists, anything that feels generated
or could have been sent to a thousand businesses unchanged. The email may be in
Slovak; judge the tone, not the language.

Return ONLY JSON, nothing else:
{{"score": <0-10 integer>, "reason": "<max 15 words on what gives it away or makes it human>"}}

EMAIL:
\"\"\"
{email_body}
\"\"\"
"""


class HumanToneCritic(EmailCritic):
    """LLM critic on the CHEAP tier. Passes when score >= threshold."""

    def __init__(self, client: Optional["LLMClient"] = None,
                 threshold: float = 7.0, fail_open: bool = True) -> None:
        from waa.core.llm import default_llm_client
        self._client = client or default_llm_client()
        self._threshold = threshold
        self._fail_open = fail_open

    def review(self, email_body: str, *, lang: str = "sk") -> CriticVerdict:
        from waa.core.llm import ModelTier, parse_json

        if not (email_body or "").strip():
            return CriticVerdict(False, score=0.0, reason="empty email")

        prompt = _CRITIC_PROMPT.format(email_body=email_body.replace("\\n", "\n"))
        try:
            raw = self._client.complete(prompt, tier=ModelTier.CHEAP, max_tokens=200)
            data = parse_json(raw)
            score = float(data.get("score", 0))
            reason = str(data.get("reason", ""))[:200]
        except Exception as e:  # transient API / malformed output
            logger.warning(f"critic error: {e}")
            verdict = "open" if self._fail_open else "closed"
            return CriticVerdict(self._fail_open, score=-1.0,
                                 reason=f"critic error, failing {verdict}")
        return CriticVerdict(score >= self._threshold, score, reason)
