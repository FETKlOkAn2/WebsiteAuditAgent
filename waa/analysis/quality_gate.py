"""
Automated output-quality gate (improvement #10).

For the system to run unattended there must be NO human reading every draft
before it goes out. Three things make a generated cold email send-worthy:

  1. it actually contains content (subject + body),
  2. it is fact-grounded — it quotes something real from the prospect's site
     (improvement #2 / the v2 validator),
  3. it reads like a human wrote it — it cleared the Turing critic (#3),

and one thing makes its *proof screenshot* trustworthy:

  4. screenshot-correctness — the red box actually landed on the element we
     claim is the problem (otherwise the image is misleading and must not be
     used as proof).

This module formalises those four judgements into one composable gate so the
"is this send-worthy?" decision lives in ONE place instead of being scattered
through `process_single` / `attach_screenshots` / `_prepare_send_list`. It
replaces the manual `preview` HTML eyeballing step.

SOLID:
- `OutputCheck` is the abstraction; each check has ONE reason to change (SRP)
  and a single `check()` method (ISP).
- New checks drop into the gate without touching existing ones (OCP); they are
  interchangeable because they all return a `CheckResult` (LSP).
- `EmailArtifact` is an immutable value object built FROM the pipeline's dicts,
  so the checks depend on a small stable shape, not on the audit schema (DIP).
- A check may be `blocking` (its failure blocks the send) or advisory (its
  failure is recorded but the email still goes — used for the screenshot, which
  is delivered in the reply, not the first touch).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Sequence


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EmailArtifact:
    """
    Everything the quality checks need to judge one generated email, projected
    off the pipeline dicts so checks never re-parse the audit schema.

    `grounded` / `critic_passed` are tri-state: ``None`` means "not applicable
    / not evaluated" (e.g. a v1 email has no v2 grounding metadata, or the
    critic was disabled). A check only fails on an explicit ``False`` — never on
    a missing value — so the gate can run safely over mixed v1/v2 results.
    """

    subject: str = ""
    body: str = ""
    grounded: Optional[bool] = None
    quoted_facts: Sequence[str] = field(default_factory=tuple)
    critic_passed: Optional[bool] = None
    critic_score: Optional[float] = None
    has_screenshot: bool = False
    screenshot_annotated: bool = False
    screenshot_target_found: bool = False

    # -- adapters ----------------------------------------------------------

    @classmethod
    def from_v2(cls, v2: dict) -> "EmailArtifact":
        """Build from a `generate_email_v2` result (pre-send, no screenshot)."""
        validation = v2.get("validation") or {}
        critic = v2.get("critic")
        return cls(
            subject=(v2.get("subject_line") or "").strip(),
            body=(v2.get("email_body") or "").strip(),
            grounded=bool(validation.get("passed")) if validation else None,
            quoted_facts=tuple(validation.get("quoted_facts") or ()),
            critic_passed=(None if not critic else bool(critic.get("passed"))),
            critic_score=(critic.get("score") if critic else None),
        )

    @classmethod
    def from_result(cls, result: dict) -> "EmailArtifact":
        """Build from a full audit-result dict (at send time, with screenshot)."""
        email = result.get("email") or {}
        analysis = result.get("analysis") or {}
        validation = analysis.get("validation") or {}
        critic = analysis.get("critic")
        shot = result.get("screenshot") or {}
        has_shot = bool(shot.get("path") or shot.get("annotated"))
        return cls(
            subject=(email.get("subject_line") or "").strip(),
            body=(email.get("email_body") or "").strip(),
            grounded=bool(validation.get("passed")) if validation else None,
            quoted_facts=tuple(validation.get("quoted_facts") or ()),
            critic_passed=(None if not critic else bool(critic.get("passed"))),
            critic_score=(critic.get("score") if critic else None),
            has_screenshot=has_shot,
            screenshot_annotated=bool(shot.get("annotated")),
            screenshot_target_found=bool(shot.get("target_found")),
        )


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    reason: str
    blocking: bool = True


@dataclass(frozen=True)
class QualityVerdict:
    """Aggregate outcome of every check in the gate."""

    results: tuple[CheckResult, ...]

    @property
    def send_worthy(self) -> bool:
        """True when every BLOCKING check passed. Advisory failures don't block."""
        return all(r.passed for r in self.results if r.blocking)

    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def blocking_failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed and r.blocking]

    def result_for(self, name: str) -> Optional[CheckResult]:
        for r in self.results:
            if r.name == name:
                return r
        return None

    def first_blocking_failure(self) -> Optional[CheckResult]:
        blocking = self.blocking_failures()
        return blocking[0] if blocking else None

    def screenshot_ok(self) -> bool:
        """Did the proof screenshot clear its correctness check? True when there
        is no screenshot check, or it passed."""
        r = self.result_for(ScreenshotCorrectnessCheck.name)
        return r is None or r.passed

    def reason(self) -> str:
        fails = self.failures()
        if not fails:
            return "all checks passed"
        return "; ".join(f"{r.name}: {r.reason}" for r in fails)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class OutputCheck(ABC):
    """A single send-worthiness judgement on a generated email."""

    name: str = "check"
    blocking: bool = True

    @abstractmethod
    def check(self, art: EmailArtifact) -> CheckResult:
        ...

    def _ok(self, reason: str) -> CheckResult:
        return CheckResult(self.name, True, reason, self.blocking)

    def _fail(self, reason: str) -> CheckResult:
        return CheckResult(self.name, False, reason, self.blocking)


class ContentCheck(OutputCheck):
    """The email must have both a subject and a body — nothing empty goes out."""

    name = "content"

    def check(self, art: EmailArtifact) -> CheckResult:
        if art.subject and art.body:
            return self._ok("subject and body present")
        missing = []
        if not art.subject:
            missing.append("subject")
        if not art.body:
            missing.append("body")
        return self._fail(f"missing {', '.join(missing)}")


class FactGroundingCheck(OutputCheck):
    """The email must quote a real, on-page fact (the v2 validator's verdict).

    Tri-state: passes when grounding is True OR not applicable (``None``);
    fails only on an explicit ``False``."""

    name = "grounding"

    def check(self, art: EmailArtifact) -> CheckResult:
        if art.grounded is False:
            return self._fail("email does not quote any verified site fact")
        if art.grounded is None:
            return self._ok("grounding not applicable")
        return self._ok(f"quotes {len(art.quoted_facts)} verified fact(s)")


class HumanToneCheck(OutputCheck):
    """The email must read as human-written (the Turing critic's verdict).

    Passes when the critic passed OR did not run (``None``); fails on ``False``."""

    name = "human_tone"

    def check(self, art: EmailArtifact) -> CheckResult:
        if art.critic_passed is False:
            score = "" if art.critic_score is None else f" (score {art.critic_score:.0f}/10)"
            return self._fail(f"reads as AI-written{score}")
        if art.critic_passed is None:
            return self._ok("critic not run")
        return self._ok("reads as human-written")


class ScreenshotCorrectnessCheck(OutputCheck):
    """The proof screenshot must point at the right element.

    A screenshot whose red box did not land on the claimed problem is
    misleading and must not be used as proof. Advisory by default (``blocking
    = False``): the proof image is delivered in the reply/follow-up, not the
    first cold email, so a bad shot is dropped rather than blocking the send.
    Set ``require=True`` to also block when no usable screenshot exists.
    """

    name = "screenshot"

    def __init__(self, require: bool = False, blocking: bool = False) -> None:
        self._require = require
        self.blocking = blocking

    def check(self, art: EmailArtifact) -> CheckResult:
        if not art.has_screenshot:
            if self._require:
                return self._fail("no proof screenshot captured")
            return self._ok("no screenshot (optional)")
        if is_trustworthy_proof(art.screenshot_annotated, art.screenshot_target_found):
            return self._ok("red box landed on the claimed element")
        return self._fail("annotation did not land on the intended element")


def is_trustworthy_proof(annotated: bool, target_found: bool) -> bool:
    """A proof screenshot is only trustworthy when we drew the highlight AND it
    resolved to the intended element. Shared by the check and the capture loop
    so 'what counts as a usable proof' is defined once."""
    return bool(annotated and target_found)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class QualityGate:
    """Runs every check and aggregates them into a single verdict.

    Unlike the lead gate chain (which short-circuits to save tokens), this gate
    runs ALL checks so the verdict can report every reason a draft was held —
    the checks are cheap (no LLM calls; the critic already ran upstream)."""

    def __init__(self, checks: Sequence[OutputCheck]) -> None:
        self._checks = list(checks)

    def evaluate(self, art: EmailArtifact) -> QualityVerdict:
        return QualityVerdict(tuple(c.check(art) for c in self._checks))


def build_output_quality_gate(
    *,
    require_grounding: bool = True,
    require_human: bool = True,
    check_screenshot: bool = True,
    require_screenshot: bool = False,
) -> QualityGate:
    """
    Default send-worthiness gate: content -> grounding -> human tone ->
    screenshot correctness.

    Flags let callers tailor the gate per stage: the send path uses the full
    gate; a caller that has already enforced grounding/tone upstream can drop
    those, and the screenshot check is advisory unless `require_screenshot`.
    """
    checks: list[OutputCheck] = [ContentCheck()]
    if require_grounding:
        checks.append(FactGroundingCheck())
    if require_human:
        checks.append(HumanToneCheck())
    if check_screenshot:
        checks.append(ScreenshotCorrectnessCheck(require=require_screenshot))
    return QualityGate(checks)
