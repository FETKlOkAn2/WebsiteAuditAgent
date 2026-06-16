"""
Tests for the automated output-quality gate (improvement #10). Pure /
deterministic — no tokens, no network, no browser.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.quality_gate import (  # noqa: E402
    EmailArtifact, CheckResult, QualityVerdict,
    OutputCheck, ContentCheck, FactGroundingCheck, HumanToneCheck,
    ScreenshotCorrectnessCheck, QualityGate, build_output_quality_gate,
    is_trustworthy_proof,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _good() -> EmailArtifact:
    return EmailArtifact(
        subject="Rychla otazka", body="Vsimol som si rezervacny formular",
        grounded=True, quoted_facts=("Rezervovat",),
        critic_passed=True, critic_score=8.0,
    )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

class TestContentCheck(unittest.TestCase):

    def test_passes_with_subject_and_body(self):
        self.assertTrue(ContentCheck().check(_good()).passed)

    def test_fails_empty_body(self):
        r = ContentCheck().check(EmailArtifact(subject="x", body=""))
        self.assertFalse(r.passed)
        self.assertIn("body", r.reason)

    def test_fails_empty_subject(self):
        r = ContentCheck().check(EmailArtifact(subject="", body="y"))
        self.assertFalse(r.passed)
        self.assertIn("subject", r.reason)


class TestGroundingCheck(unittest.TestCase):

    def test_passes_when_grounded(self):
        self.assertTrue(FactGroundingCheck().check(_good()).passed)

    def test_fails_when_not_grounded(self):
        art = EmailArtifact(subject="x", body="y", grounded=False)
        self.assertFalse(FactGroundingCheck().check(art).passed)

    def test_passes_when_not_applicable(self):
        # v1 email: no grounding metadata -> None -> not applicable -> pass
        art = EmailArtifact(subject="x", body="y", grounded=None)
        self.assertTrue(FactGroundingCheck().check(art).passed)


class TestHumanToneCheck(unittest.TestCase):

    def test_passes_when_human(self):
        self.assertTrue(HumanToneCheck().check(_good()).passed)

    def test_fails_when_ai(self):
        art = EmailArtifact(subject="x", body="y", critic_passed=False, critic_score=3.0)
        r = HumanToneCheck().check(art)
        self.assertFalse(r.passed)
        self.assertIn("3", r.reason)  # score surfaced

    def test_passes_when_critic_not_run(self):
        art = EmailArtifact(subject="x", body="y", critic_passed=None)
        self.assertTrue(HumanToneCheck().check(art).passed)


class TestScreenshotCheck(unittest.TestCase):

    def test_advisory_by_default(self):
        self.assertFalse(ScreenshotCorrectnessCheck().blocking)

    def test_no_screenshot_passes_when_optional(self):
        art = EmailArtifact(subject="x", body="y", has_screenshot=False)
        self.assertTrue(ScreenshotCorrectnessCheck().check(art).passed)

    def test_no_screenshot_fails_when_required(self):
        art = EmailArtifact(subject="x", body="y", has_screenshot=False)
        self.assertFalse(ScreenshotCorrectnessCheck(require=True).check(art).passed)

    def test_correct_screenshot_passes(self):
        art = EmailArtifact(subject="x", body="y", has_screenshot=True,
                            screenshot_annotated=True, screenshot_target_found=True)
        self.assertTrue(ScreenshotCorrectnessCheck().check(art).passed)

    def test_misaligned_screenshot_fails(self):
        art = EmailArtifact(subject="x", body="y", has_screenshot=True,
                            screenshot_annotated=True, screenshot_target_found=False)
        self.assertFalse(ScreenshotCorrectnessCheck().check(art).passed)

    def test_is_trustworthy_proof(self):
        self.assertTrue(is_trustworthy_proof(True, True))
        self.assertFalse(is_trustworthy_proof(True, False))
        self.assertFalse(is_trustworthy_proof(False, True))


# ---------------------------------------------------------------------------
# The gate / verdict
# ---------------------------------------------------------------------------

class TestQualityGate(unittest.TestCase):

    def test_good_email_is_send_worthy(self):
        verdict = build_output_quality_gate().evaluate(_good())
        self.assertTrue(verdict.send_worthy)
        self.assertEqual(verdict.failures(), [])
        self.assertEqual(verdict.reason(), "all checks passed")

    def test_grounding_failure_blocks(self):
        art = EmailArtifact(subject="x", body="y", grounded=False, critic_passed=True)
        verdict = build_output_quality_gate().evaluate(art)
        self.assertFalse(verdict.send_worthy)
        self.assertEqual(verdict.first_blocking_failure().name, "grounding")

    def test_tone_failure_blocks(self):
        art = EmailArtifact(subject="x", body="y", grounded=True, critic_passed=False)
        verdict = build_output_quality_gate().evaluate(art)
        self.assertFalse(verdict.send_worthy)
        self.assertEqual(verdict.first_blocking_failure().name, "human_tone")

    def test_bad_screenshot_does_not_block_send(self):
        # Advisory: a misaligned proof is recorded as a failure but the
        # otherwise-good email is still send-worthy.
        art = EmailArtifact(subject="x", body="y", grounded=True, critic_passed=True,
                            has_screenshot=True, screenshot_annotated=True,
                            screenshot_target_found=False)
        verdict = build_output_quality_gate().evaluate(art)
        self.assertTrue(verdict.send_worthy)
        self.assertFalse(verdict.screenshot_ok())
        self.assertEqual(len(verdict.failures()), 1)

    def test_require_human_flag_drops_check(self):
        art = EmailArtifact(subject="x", body="y", grounded=True, critic_passed=False)
        # With the human check disabled, an AI-sounding email passes.
        verdict = build_output_quality_gate(require_human=False).evaluate(art)
        self.assertTrue(verdict.send_worthy)
        self.assertIsNone(verdict.result_for("human_tone"))

    def test_check_screenshot_flag_drops_check(self):
        verdict = build_output_quality_gate(check_screenshot=False).evaluate(_good())
        self.assertIsNone(verdict.result_for("screenshot"))

    def test_gate_runs_all_checks_not_short_circuit(self):
        art = EmailArtifact(subject="", body="", grounded=False, critic_passed=False)
        verdict = build_output_quality_gate().evaluate(art)
        names = {r.name for r in verdict.failures()}
        self.assertEqual(names, {"content", "grounding", "human_tone"})

    def test_custom_gate_composition(self):
        gate = QualityGate([ContentCheck()])
        self.assertTrue(gate.evaluate(_good()).send_worthy)


# ---------------------------------------------------------------------------
# Artifact adapters
# ---------------------------------------------------------------------------

class TestEmailArtifactAdapters(unittest.TestCase):

    def test_from_v2(self):
        v2 = {
            "subject_line": " Ahoj ", "email_body": " telo ",
            "validation": {"passed": True, "quoted_facts": ["a", "b"]},
            "critic": {"passed": False, "score": 4.0},
        }
        art = EmailArtifact.from_v2(v2)
        self.assertEqual(art.subject, "Ahoj")
        self.assertEqual(art.body, "telo")
        self.assertTrue(art.grounded)
        self.assertEqual(art.quoted_facts, ("a", "b"))
        self.assertIs(art.critic_passed, False)
        self.assertEqual(art.critic_score, 4.0)
        self.assertFalse(art.has_screenshot)

    def test_from_v2_no_critic(self):
        v2 = {"subject_line": "s", "email_body": "b",
              "validation": {"passed": True, "quoted_facts": []}}
        art = EmailArtifact.from_v2(v2)
        self.assertIsNone(art.critic_passed)

    def test_from_result_with_screenshot(self):
        result = {
            "email": {"subject_line": "s", "email_body": "b"},
            "analysis": {"validation": {"passed": True, "quoted_facts": ["x"]},
                         "critic": {"passed": True, "score": 9.0}},
            "screenshot": {"path": "/tmp/x.png", "annotated": True,
                           "target_found": True},
        }
        art = EmailArtifact.from_result(result)
        self.assertTrue(art.grounded)
        self.assertTrue(art.has_screenshot)
        self.assertTrue(art.screenshot_target_found)

    def test_from_result_v1_is_na_safe(self):
        # A v1 result has no validation/critic/screenshot metadata; the gate
        # must treat it as send-worthy rather than blocking everything.
        result = {"email": {"subject_line": "s", "email_body": "b"}, "analysis": {}}
        art = EmailArtifact.from_result(result)
        self.assertIsNone(art.grounded)
        self.assertIsNone(art.critic_passed)
        self.assertFalse(art.has_screenshot)
        self.assertTrue(build_output_quality_gate().evaluate(art).send_worthy)


# ---------------------------------------------------------------------------
# Pipeline integration: _prepare_send_list + attach_screenshots
# ---------------------------------------------------------------------------

class TestPrepareSendListGate(unittest.TestCase):

    def _result(self, **over):
        r = {
            "url": "https://x.sk",
            "contact_emails": ["owner@x.sk"],
            "email": {"subject_line": "Ahoj", "email_body": "telo"},
            "analysis": {"validation": {"passed": True, "quoted_facts": ["x"]},
                         "critic": {"passed": True, "score": 9.0}},
        }
        r.update(over)
        return r

    def test_send_worthy_email_kept(self):
        from waa.cli import _prepare_send_list
        with _patched_registry():
            out = _prepare_send_list([self._result()], validate_emails=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["to"], "owner@x.sk")

    def test_ai_email_blocked_at_send(self):
        from waa.cli import _prepare_send_list
        r = self._result(analysis={"validation": {"passed": True, "quoted_facts": ["x"]},
                                    "critic": {"passed": False, "score": 3.0}})
        with _patched_registry():
            out = _prepare_send_list([r], validate_emails=False)
        self.assertEqual(out, [])

    def test_misaligned_screenshot_discarded_but_email_sent(self):
        from waa.cli import _prepare_send_list
        r = self._result(screenshot={"path": "/tmp/x.png", "annotated": True,
                                     "target_found": False})
        with _patched_registry():
            out = _prepare_send_list([r], validate_emails=False)
        self.assertEqual(len(out), 1)          # email still goes
        self.assertIsNone(r["screenshot"])     # bad proof discarded

    def test_correct_screenshot_kept(self):
        from waa.cli import _prepare_send_list
        r = self._result(screenshot={"path": "/tmp/x.png", "annotated": True,
                                     "target_found": True})
        with _patched_registry():
            out = _prepare_send_list([r], validate_emails=False)
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(r["screenshot"])

    def test_v1_email_not_blocked(self):
        from waa.cli import _prepare_send_list
        r = {"url": "https://v1.sk", "contact_emails": ["a@v1.sk"],
             "email": {"subject_line": "Hi", "email_body": "body"},
             "analysis": {"issues": [], "lead_score": 50}}  # no v2 metadata
        with _patched_registry():
            out = _prepare_send_list([r], validate_emails=False)
        self.assertEqual(len(out), 1)


class _patched_registry:
    """Context manager: point the sent-registry at an empty in-memory dict so
    the dedup step never reads/writes disk during the test."""

    def __enter__(self):
        import waa.cli as cli
        self._orig = cli._load_sent_registry
        cli._load_sent_registry = lambda: {"emails": {}, "domains": {}}
        return self

    def __exit__(self, *exc):
        import waa.cli as cli
        cli._load_sent_registry = self._orig
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
