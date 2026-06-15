"""
Tests for preview_report.render_preview — pure HTML rendering, no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import preview_report  # noqa: E402


def _result(**kw):
    base = {
        "url": "https://restauracia.sk",
        "analysis": {"facts": {"h1": "Reštaurácia U Karola"},
                     "validation": {"passed": True}},
        "email": {
            "subject_line": "Tlačidlo Rezervovať",
            "email_body": "Ahoj Peter,\\nvšimol som si...\\nTomas",
            "follow_up_subject": "Re: Tlačidlo Rezervovať",
            "follow_up_body": "Ešte k tomu...\\nTomas",
            "owner_first_name": "Peter",
        },
    }
    base.update(kw)
    return base


class TestRenderPreview(unittest.TestCase):

    def test_basic_document(self):
        out = preview_report.render_preview([_result()], niche="restauracia",
                                            location="Bratislava")
        self.assertIn("<!DOCTYPE html>", out)
        self.assertIn("restauracia", out)
        self.assertIn("Bratislava", out)
        self.assertIn("NOTHING was sent", out)

    def test_email_rendered_with_newlines_and_owner(self):
        out = preview_report.render_preview([_result()])
        self.assertIn("Tlačidlo Rezervovať", out)
        # \n placeholders become real newlines in the <pre> body
        self.assertIn("všimol som si", out)
        self.assertIn("Peter", out)
        self.assertIn("grounded", out)

    def test_skipped_result(self):
        out = preview_report.render_preview([
            _result(skipped_reason="insufficient_facts (2/3)", email=None),
        ])
        self.assertIn("Skipped", out)
        self.assertIn("insufficient_facts", out)

    def test_no_screenshot_placeholder(self):
        out = preview_report.render_preview([_result()])  # no screenshot key
        self.assertIn("no screenshot", out)

    def test_screenshot_embedded_as_base64(self):
        # Write a tiny fake PNG and ensure it gets embedded as a data URI.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
            png_path = f.name
        try:
            r = _result(screenshot={"path": png_path, "caption": "Tlačidlo",
                                    "annotated": True})
            out = preview_report.render_preview([r])
            self.assertIn("data:image/png;base64,", out)
            self.assertIn("Tlačidlo", out)
        finally:
            os.unlink(png_path)

    def test_html_is_escaped(self):
        r = _result()
        r["email"]["subject_line"] = "<script>alert(1)</script>"
        out = preview_report.render_preview([r])
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_missing_screenshot_file_falls_back(self):
        r = _result(screenshot={"path": "/nonexistent/x.png", "caption": ""})
        out = preview_report.render_preview([r])
        self.assertIn("no screenshot", out)

    def test_sendable_count(self):
        out = preview_report.render_preview([
            _result(),
            _result(skipped_reason="x", email=None),
        ])
        self.assertIn("1 with a sendable email", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
