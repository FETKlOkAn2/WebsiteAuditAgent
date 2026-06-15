"""
Tests for the proof-screenshot step.

The browser-driving part of screenshot.py is exercised manually via its CLI
(launching chromium in unit tests is slow and flaky). Here we cover the
pure logic that decides WHAT to highlight, and the batch orchestration in
audit_agent.attach_screenshots — with the screenshotter fully mocked, so no
real browser launches.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("OUTPUT_DIR", "/tmp/audit_screenshot_tests")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import audit_agent  # noqa: E402
from screenshot import HighlightTarget, ScreenshotResult  # noqa: E402


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class TestValueObjects(unittest.TestCase):

    def test_highlight_target_is_empty(self):
        self.assertTrue(HighlightTarget([], "").is_empty())
        self.assertTrue(HighlightTarget(["", "  "], "x").is_empty())
        self.assertFalse(HighlightTarget(["Book now"], "x").is_empty())

    def test_screenshot_result_ok(self):
        self.assertTrue(ScreenshotResult("u", path="/tmp/a.png").ok())
        self.assertFalse(ScreenshotResult("u", error="boom").ok())
        self.assertFalse(ScreenshotResult("u").ok())


# ---------------------------------------------------------------------------
# _build_highlight_target — what do we circle?
# ---------------------------------------------------------------------------

class TestBuildHighlightTarget(unittest.TestCase):

    def test_lorem_ipsum_wins(self):
        facts = {
            "surprising_finding": "Lorem ipsum placeholder text is still on the homepage.",
            "primary_cta_text": "Rezervovať",
        }
        t = audit_agent._build_highlight_target(facts, lang="sk")
        self.assertIsNotNone(t)
        self.assertIn("lorem ipsum", [c.lower() for c in t.text_candidates])

    def test_copyright_year(self):
        facts = {"surprising_finding": "Footer copyright still says 2019."}
        t = audit_agent._build_highlight_target(facts, lang="sk")
        self.assertIsNotNone(t)
        self.assertIn("© 2019", t.text_candidates)
        self.assertIn("2019", t.text_candidates)

    def test_cta_fallback(self):
        facts = {"surprising_finding": "", "primary_cta_text": "Book a table"}
        t = audit_agent._build_highlight_target(facts, lang="en")
        self.assertIsNotNone(t)
        self.assertEqual(t.text_candidates, ["Book a table"])
        self.assertIn("Book a table", t.caption)

    def test_nothing_to_circle_returns_none(self):
        facts = {"surprising_finding": "", "primary_cta_text": None}
        self.assertIsNone(audit_agent._build_highlight_target(facts, lang="sk"))

    def test_caption_language(self):
        facts = {"surprising_finding": "Lorem ipsum on page"}
        sk = audit_agent._build_highlight_target(facts, lang="sk")
        en = audit_agent._build_highlight_target(facts, lang="en")
        self.assertIn("úvodnej", sk.caption)
        self.assertIn("homepage", en.caption)

    def test_cta_beats_copyright(self):
        # Regression: copyright used to flood every screenshot. Now a CTA
        # (stronger, higher-on-page) wins over a stale-copyright surprise.
        facts = {"surprising_finding": "Footer copyright still says 2019.",
                 "primary_cta_text": "Rezervovať"}
        t = audit_agent._build_highlight_target(facts, lang="sk")
        self.assertEqual(t.text_candidates, ["Rezervovať"])

    def test_coming_soon_beats_cta(self):
        facts = {"surprising_finding": "There's a 'Coming soon' section on the homepage.",
                 "primary_cta_text": "Book"}
        t = audit_agent._build_highlight_target(facts, lang="en")
        self.assertIn("coming soon", [c.lower() for c in t.text_candidates])

    def test_copyright_only_when_nothing_better(self):
        # No CTA, no other surprise -> copyright is the last-resort anchor.
        facts = {"surprising_finding": "Footer copyright still says 2019.",
                 "primary_cta_text": None}
        t = audit_agent._build_highlight_target(facts, lang="sk")
        self.assertIn("© 2019", t.text_candidates)

    def test_random_year_without_copyright_does_not_match(self):
        # A year mentioned in non-copyright context shouldn't trigger the
        # footer rule; it should fall through to CTA (or None).
        facts = {"surprising_finding": "Established 2019 banner in hero",
                 "primary_cta_text": "Call us"}
        t = audit_agent._build_highlight_target(facts, lang="en")
        self.assertEqual(t.text_candidates, ["Call us"])


# ---------------------------------------------------------------------------
# attach_screenshots — batch orchestration (mocked browser)
# ---------------------------------------------------------------------------

class _FakeShot:
    """Stand-in for screenshot.PageScreenshotter used as a context manager."""
    def __init__(self, results_by_url):
        self._results_by_url = results_by_url
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def capture(self, url, target=None, **kw):
        return self._results_by_url[url]


class TestAttachScreenshots(unittest.TestCase):

    def _results(self):
        return [
            {  # has a lorem-ipsum surprise -> circle-able
                "url": "https://good.example",
                "analysis": {"facts": {
                    "surprising_finding": "Lorem ipsum placeholder text is still on the homepage.",
                    "primary_cta_text": "Rezervovať",
                }},
            },
            {  # nothing to circle
                "url": "https://thin.example",
                "analysis": {"facts": {"surprising_finding": "", "primary_cta_text": None}},
            },
            {  # errored audit -> skipped entirely
                "url": "https://dead.example",
                "error": "fetch failed",
            },
        ]

    def test_captures_only_circle_able_by_default(self):
        results = self._results()
        fake = _FakeShot({
            "https://good.example": ScreenshotResult(
                "https://good.example", path="/tmp/good.png",
                annotated=True, target_found=True),
        })
        with patch("screenshot.PageScreenshotter", return_value=fake):
            n = audit_agent.attach_screenshots(results, lang="sk")
        self.assertEqual(n, 1)
        self.assertIn("screenshot", results[0])
        self.assertEqual(results[0]["screenshot"]["path"], "/tmp/good.png")
        self.assertTrue(results[0]["screenshot"]["annotated"])
        # thin + errored prospects untouched
        self.assertNotIn("screenshot", results[1])
        self.assertNotIn("screenshot", results[2])

    def test_only_with_target_false_captures_everything_auditable(self):
        results = self._results()
        fake = _FakeShot({
            "https://good.example": ScreenshotResult("https://good.example", path="/tmp/a.png"),
            "https://thin.example": ScreenshotResult("https://thin.example", path="/tmp/b.png"),
        })
        with patch("screenshot.PageScreenshotter", return_value=fake):
            n = audit_agent.attach_screenshots(results, lang="sk", only_with_target=False)
        self.assertEqual(n, 2)  # errored one still skipped

    def test_failed_capture_leaves_no_screenshot_key(self):
        results = self._results()
        fake = _FakeShot({
            "https://good.example": ScreenshotResult(
                "https://good.example", error="TimeoutError"),
        })
        with patch("screenshot.PageScreenshotter", return_value=fake):
            n = audit_agent.attach_screenshots(results, lang="sk")
        self.assertEqual(n, 0)
        self.assertNotIn("screenshot", results[0])

    def test_missing_playwright_degrades_gracefully(self):
        # Simulate ImportError on `from screenshot import PageScreenshotter`
        results = self._results()
        with patch.dict(sys.modules, {"screenshot": None}):
            # `from screenshot import PageScreenshotter` will raise ImportError
            n = audit_agent.attach_screenshots(results, lang="sk")
        self.assertEqual(n, 0)

    def test_no_jobs_returns_zero_without_launching_browser(self):
        results = [{"url": "https://thin.example",
                    "analysis": {"facts": {"surprising_finding": "", "primary_cta_text": None}}}]
        # PageScreenshotter must never be constructed when there are no jobs.
        with patch("screenshot.PageScreenshotter") as MockShot:
            n = audit_agent.attach_screenshots(results, lang="sk")
        self.assertEqual(n, 0)
        MockShot.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
