"""
Tests for the vision design critic (improvement #6). Fully offline: a fake
vision transport stands in for the Anthropic call, and a tiny temp file stands
in for the screenshot — no network, no real image decoding required.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.design_critic import (  # noqa: E402
    DesignFinding, DesignCritique, DesignCritic, NullDesignCritic,
    VisionDesignCritic, _media_type_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png(tmp) -> str:
    p = os.path.join(tmp, "shot.png")
    with open(p, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n fake bytes")
    return p


_GOOD_JSON = json.dumps({
    "score": 4,
    "looks_dated": True,
    "summary": "Cluttered hero with no clear focus loses bookings.",
    "findings": [
        {"aspect": "hierarchy", "severity": "high",
         "observation": "Three competing buttons fight for attention above the fold",
         "redesign_rationale": "A single clear action lifts bookings"},
        {"aspect": "dated", "severity": "medium",
         "observation": "2012 era gradient buttons and stock photos",
         "redesign_rationale": "A modern look builds trust with new diners"},
    ],
})


class _FakeTransport:
    """Records the call and returns a canned completion."""
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, prompt, image_b64, media_type, model, max_tokens):
        self.calls.append({
            "prompt": prompt, "image_b64": image_b64,
            "media_type": media_type, "model": model, "max_tokens": max_tokens,
        })
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class TestValueObjects(unittest.TestCase):

    def test_top_orders_by_severity(self):
        c = DesignCritique(
            available=True,
            findings=(
                DesignFinding("spacing", "low", "a", "x"),
                DesignFinding("hierarchy", "high", "b", "y"),
                DesignFinding("color", "medium", "c", "z"),
            ),
        )
        order = [f.severity for f in c.top(3)]
        self.assertEqual(order, ["high", "medium", "low"])

    def test_top_limits_count(self):
        c = DesignCritique(available=True, findings=tuple(
            DesignFinding("dated", "high", str(i), "r") for i in range(5)))
        self.assertEqual(len(c.top(2)), 2)

    def test_summary_for_prompt_empty_when_unavailable(self):
        self.assertEqual(DesignCritique(available=False).summary_for_prompt(), "")

    def test_summary_for_prompt_builds_block(self):
        c = DesignCritique(
            available=True, summary="Looks dated.",
            findings=(DesignFinding("dated", "high", "old buttons", "trust"),))
        block = c.summary_for_prompt()
        self.assertIn("Looks dated.", block)
        self.assertIn("old buttons", block)

    def test_to_dict_roundtrips_findings(self):
        c = DesignCritique(available=True, score=5.0,
                           findings=(DesignFinding("color", "low", "o", "r"),))
        d = c.to_dict()
        self.assertEqual(d["findings"][0]["aspect"], "color")
        self.assertEqual(d["score"], 5.0)

    def test_media_type_detection(self):
        self.assertEqual(_media_type_for("a.png"), "image/png")
        self.assertEqual(_media_type_for("a.jpg"), "image/jpeg")
        self.assertEqual(_media_type_for("a.JPEG"), "image/jpeg")
        self.assertEqual(_media_type_for("a.webp"), "image/webp")
        self.assertEqual(_media_type_for("a.unknown"), "image/png")


# ---------------------------------------------------------------------------
# NullDesignCritic
# ---------------------------------------------------------------------------

class TestNullCritic(unittest.TestCase):

    def test_always_unavailable(self):
        c = NullDesignCritic().critique("whatever.png", niche="x")
        self.assertFalse(c.available)
        self.assertFalse(c.has_findings())


# ---------------------------------------------------------------------------
# VisionDesignCritic
# ---------------------------------------------------------------------------

class TestVisionCritic(unittest.TestCase):

    def test_happy_path_parses_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = _FakeTransport(_GOOD_JSON)
            crit = VisionDesignCritic(transport=t).critique(_png(tmp), niche="restauracia")
        self.assertTrue(crit.available)
        self.assertEqual(crit.score, 4.0)
        self.assertTrue(crit.looks_dated)
        self.assertEqual(len(crit.findings), 2)
        self.assertEqual(crit.top(1)[0].aspect, "hierarchy")
        # niche made it into the prompt
        self.assertIn("restauracia", t.calls[0]["prompt"])
        # image was base64-encoded and media type detected
        self.assertTrue(t.calls[0]["image_b64"])
        self.assertEqual(t.calls[0]["media_type"], "image/png")

    def test_missing_file_is_unavailable_no_call(self):
        t = _FakeTransport(_GOOD_JSON)
        crit = VisionDesignCritic(transport=t).critique("/no/such/file.png")
        self.assertFalse(crit.available)
        self.assertIn("unreadable", crit.error)
        self.assertEqual(t.calls, [])  # never hit the model

    def test_empty_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "empty.png")
            open(p, "wb").close()
            t = _FakeTransport(_GOOD_JSON)
            crit = VisionDesignCritic(transport=t).critique(p)
        self.assertFalse(crit.available)
        self.assertEqual(t.calls, [])

    def test_fails_open_on_transport_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = _FakeTransport(RuntimeError("boom"))
            crit = VisionDesignCritic(transport=t).critique(_png(tmp))
        self.assertFalse(crit.available)
        self.assertIn("critic error", crit.error)

    def test_fails_open_on_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = _FakeTransport("not json at all")
            crit = VisionDesignCritic(transport=t).critique(_png(tmp))
        self.assertFalse(crit.available)

    def test_unknown_aspect_normalised(self):
        reply = json.dumps({"score": 6, "findings": [
            {"aspect": "vibes", "severity": "high", "observation": "o", "redesign_rationale": "r"}]})
        with tempfile.TemporaryDirectory() as tmp:
            crit = VisionDesignCritic(transport=_FakeTransport(reply)).critique(_png(tmp))
        self.assertEqual(crit.findings[0].aspect, "other")

    def test_bad_severity_defaults_medium(self):
        reply = json.dumps({"score": 6, "findings": [
            {"aspect": "color", "severity": "catastrophic", "observation": "o", "redesign_rationale": "r"}]})
        with tempfile.TemporaryDirectory() as tmp:
            crit = VisionDesignCritic(transport=_FakeTransport(reply)).critique(_png(tmp))
        self.assertEqual(crit.findings[0].severity, "medium")

    def test_findings_without_observation_dropped(self):
        reply = json.dumps({"score": 7, "findings": [
            {"aspect": "color", "severity": "low", "observation": "", "redesign_rationale": "r"}]})
        with tempfile.TemporaryDirectory() as tmp:
            crit = VisionDesignCritic(transport=_FakeTransport(reply)).critique(_png(tmp))
        self.assertEqual(crit.findings, ())

    def test_caps_findings_at_four(self):
        reply = json.dumps({"score": 3, "findings": [
            {"aspect": "color", "severity": "low", "observation": f"o{i}", "redesign_rationale": "r"}
            for i in range(8)]})
        with tempfile.TemporaryDirectory() as tmp:
            crit = VisionDesignCritic(transport=_FakeTransport(reply)).critique(_png(tmp))
        self.assertLessEqual(len(crit.findings), 4)

    def test_is_a_design_critic(self):
        self.assertIsInstance(VisionDesignCritic(transport=_FakeTransport("{}")), DesignCritic)


# ---------------------------------------------------------------------------
# Pipeline integration: attach_screenshots runs the critic on captured shots
# ---------------------------------------------------------------------------

class TestAttachScreenshotsDesignCritique(unittest.TestCase):

    def test_critique_stored_on_result(self):
        from unittest.mock import patch
        from waa.proof.screenshot import ScreenshotResult
        import waa.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            shot_path = _png(tmp)

            class _FakeShot:
                def __enter__(self): return self
                def __exit__(self, *exc): return False
                def capture(self, url, target=None, **kw):
                    return ScreenshotResult(url, path=shot_path,
                                            annotated=True, target_found=True)

            results = [{
                "url": "https://x.sk",
                "analysis": {"facts": {"niche": "restauracia",
                                       "surprising_finding": "Lorem ipsum on homepage",
                                       "primary_cta_text": "Rezervovat"}},
            }]
            critic = VisionDesignCritic(transport=_FakeTransport(_GOOD_JSON))
            with patch("waa.proof.screenshot.PageScreenshotter", return_value=_FakeShot()):
                n = cli.attach_screenshots(results, lang="sk", design_critic=critic)
        self.assertEqual(n, 1)
        self.assertIn("design_critique", results[0])
        self.assertTrue(results[0]["design_critique"]["available"])
        self.assertEqual(len(results[0]["design_critique"]["findings"]), 2)

    def test_no_critic_means_no_critique_key(self):
        from unittest.mock import patch
        from waa.proof.screenshot import ScreenshotResult
        import waa.cli as cli

        class _FakeShot:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def capture(self, url, target=None, **kw):
                return ScreenshotResult(url, path="/tmp/x.png",
                                        annotated=True, target_found=True)

        results = [{
            "url": "https://x.sk",
            "analysis": {"facts": {"surprising_finding": "Lorem ipsum on homepage",
                                   "primary_cta_text": "Rezervovat"}},
        }]
        with patch("waa.proof.screenshot.PageScreenshotter", return_value=_FakeShot()):
            cli.attach_screenshots(results, lang="sk")  # no critic
        self.assertNotIn("design_critique", results[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
