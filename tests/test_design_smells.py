"""
Tests for heuristic design smells (improvement #7). Pure / deterministic —
no tokens, no network, no browser.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.design_smells import (  # noqa: E402
    DesignSmell, DesignSmellReport, SmellContext,
    NoViewportMeta, DeprecatedTags, LayoutTables, InlineStyleHeavy,
    DatedFonts, OutdatedLibraries, FixedPixelWidth,
    DesignSmellScanner, build_default_scanner,
)

# A modern, clean page: responsive, no obsolete markup.
CLEAN = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css?family=Inter" rel="stylesheet">
</head><body><header><h1>Welcome</h1><a class="btn">Book now</a></header></body></html>"""

# A dated page hitting several smells.
DATED = """<html><head><title>Old</title></head><body>
<font face="Comic Sans MS">Welcome!</font>
<center><marquee>Best plumber in town</marquee></center>
<table width="960"><tr><td><table><tr><td>nested layout</td></tr></table></td></tr></table>
<script src="https://code.jquery.com/jquery-1.7.2.min.js"></script>
</body></html>"""


def _ctx(html):
    return SmellContext.build(html)


# ---------------------------------------------------------------------------
# Individual detectors
# ---------------------------------------------------------------------------

class TestDetectors(unittest.TestCase):

    def test_no_viewport_fires_when_missing(self):
        self.assertIsNotNone(NoViewportMeta().detect(_ctx("<html><body>x</body></html>")))

    def test_no_viewport_silent_when_present(self):
        self.assertIsNone(NoViewportMeta().detect(_ctx(CLEAN)))

    def test_no_viewport_silent_requires_content(self):
        # an empty viewport content still counts as missing
        html = '<html><head><meta name="viewport" content=""></head><body>x</body></html>'
        self.assertIsNotNone(NoViewportMeta().detect(_ctx(html)))

    def test_deprecated_tags(self):
        smell = DeprecatedTags().detect(_ctx("<body><marquee>hi</marquee><center>x</center></body>"))
        self.assertIsNotNone(smell)
        self.assertEqual(smell.severity, "high")
        self.assertIn("marquee", smell.evidence)

    def test_deprecated_tags_silent_on_clean(self):
        self.assertIsNone(DeprecatedTags().detect(_ctx(CLEAN)))

    def test_layout_tables_nested(self):
        html = "<body><table><tr><td><table><tr><td>x</td></tr></table></td></tr></table></body>"
        self.assertIsNotNone(LayoutTables().detect(_ctx(html)))

    def test_layout_tables_presentation_role(self):
        html = '<body><table role="presentation"><tr><td>x</td></tr></table></body>'
        self.assertIsNotNone(LayoutTables().detect(_ctx(html)))

    def test_layout_tables_silent_on_data_table(self):
        # a single, non-nested table without presentation role isn't flagged
        html = "<body><table><tr><th>A</th></tr><tr><td>1</td></tr></table></body>"
        self.assertIsNone(LayoutTables().detect(_ctx(html)))

    def test_inline_style_heavy(self):
        spans = "".join(f'<span style="color:red">{i}</span>' for i in range(20))
        self.assertIsNotNone(InlineStyleHeavy().detect(_ctx(f"<body>{spans}</body>")))

    def test_inline_style_under_threshold_silent(self):
        spans = "".join(f'<span style="color:red">{i}</span>' for i in range(3))
        self.assertIsNone(InlineStyleHeavy().detect(_ctx(f"<body>{spans}</body>")))

    def test_dated_fonts_comic_sans(self):
        smell = DatedFonts().detect(_ctx('<body><font face="Comic Sans MS">x</font></body>'))
        self.assertIsNotNone(smell)
        self.assertIn("Comic Sans", smell.evidence)

    def test_dated_fonts_silent_on_clean(self):
        self.assertIsNone(DatedFonts().detect(_ctx(CLEAN)))

    def test_outdated_libraries_jquery(self):
        html = '<body><script src="/js/jquery-1.7.2.min.js"></script></body>'
        self.assertIsNotNone(OutdatedLibraries().detect(_ctx(html)))

    def test_outdated_libraries_silent_on_modern(self):
        html = '<body><script src="/js/jquery-3.6.0.min.js"></script></body>'
        self.assertIsNone(OutdatedLibraries().detect(_ctx(html)))

    def test_fixed_pixel_width(self):
        self.assertIsNotNone(FixedPixelWidth().detect(_ctx('<body><table width="960"><tr><td>x</td></tr></table></body>')))

    def test_fixed_pixel_width_silent_on_small(self):
        # a 40px icon width is not a layout container
        self.assertIsNone(FixedPixelWidth().detect(_ctx('<body><img width="40"></body>')))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

class TestReport(unittest.TestCase):

    def test_score_full_when_no_smells(self):
        self.assertEqual(DesignSmellReport().design_score(), 10)

    def test_score_drops_with_severity(self):
        r = DesignSmellReport((
            DesignSmell("a", "A", "high", "e", "r"),
            DesignSmell("b", "B", "low", "e", "r"),
        ))
        self.assertEqual(r.design_score(), 10 - 3 - 1)

    def test_score_floors_at_zero(self):
        smells = tuple(DesignSmell(f"c{i}", "x", "high", "e", "r") for i in range(5))
        self.assertEqual(DesignSmellReport(smells).design_score(), 0)

    def test_top_orders_by_severity(self):
        r = DesignSmellReport((
            DesignSmell("a", "low one", "low", "e", "r"),
            DesignSmell("b", "high one", "high", "e", "r"),
        ))
        self.assertEqual(r.top(1)[0].code, "b")

    def test_as_findings_category_and_confidence(self):
        r = DesignSmellReport((DesignSmell("a", "not mobile", "high", "no viewport", "loses mobile"),))
        findings = r.as_findings()
        self.assertEqual(findings[0].category, "design")
        self.assertEqual(findings[0].confidence, "high")
        self.assertEqual(findings[0].impact, "loses mobile")

    def test_as_findings_capped_at_three(self):
        smells = tuple(DesignSmell(f"c{i}", f"l{i}", "medium", "e", "r") for i in range(6))
        self.assertEqual(len(DesignSmellReport(smells).as_findings()), 3)

    def test_to_dict_shape(self):
        r = DesignSmellReport((DesignSmell("a", "A", "high", "e", "r"),))
        d = r.to_dict()
        self.assertEqual(d["score"], 7)
        self.assertEqual(d["smells"][0]["code"], "a")

    def test_summary_for_prompt(self):
        r = DesignSmellReport((DesignSmell("a", "not mobile", "high", "no viewport", "r"),))
        self.assertIn("not mobile", r.summary_for_prompt())


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class TestScanner(unittest.TestCase):

    def test_clean_page_has_no_smells(self):
        report = build_default_scanner().scan(CLEAN)
        self.assertFalse(report.has_smells())
        self.assertEqual(report.design_score(), 10)

    def test_dated_page_lights_up(self):
        report = build_default_scanner().scan(DATED)
        codes = {s.code for s in report.smells}
        self.assertIn("no_viewport", codes)
        self.assertIn("deprecated_tags", codes)
        self.assertIn("dated_fonts", codes)
        self.assertIn("outdated_libs", codes)
        self.assertIn("layout_tables", codes)
        self.assertLess(report.design_score(), 5)

    def test_empty_html_safe(self):
        self.assertFalse(build_default_scanner().scan("").has_smells())

    def test_detector_exception_is_isolated(self):
        class _Boom(NoViewportMeta):
            def detect(self, ctx):
                raise RuntimeError("boom")
        scanner = DesignSmellScanner([_Boom(), DeprecatedTags()])
        # The boom detector is skipped; the good one still runs.
        report = scanner.scan("<body><marquee>x</marquee></body>")
        self.assertEqual({s.code for s in report.smells}, {"deprecated_tags"})


# ---------------------------------------------------------------------------
# Integration: conversion_audit + business_case + personalization
# ---------------------------------------------------------------------------

class TestIntegration(unittest.TestCase):

    def test_audit_appends_design_findings(self):
        from waa.analysis.conversion_audit import audit_conversion
        audit = audit_conversion(DATED, "https://x.sk", niche="restauracia")
        design_findings = [f for f in audit.findings if f.category == "design"]
        self.assertTrue(design_findings)
        self.assertTrue(audit.design["smells"])
        self.assertLess(audit.design["score"], 10)

    def test_clean_audit_has_no_design_findings(self):
        from waa.analysis.conversion_audit import audit_conversion
        audit = audit_conversion(CLEAN, "https://x.sk", niche="restauracia")
        self.assertEqual([f for f in audit.findings if f.category == "design"], [])
        self.assertEqual(audit.design["score"], 10)

    def test_business_case_builds_for_design(self):
        from waa.analysis.business_case import BusinessCaseBuilder
        from waa.analysis.conversion_audit import Finding, ConversionAudit
        audit = ConversionAudit(url="https://x.sk", niche="zubar")
        audit.findings = [Finding("design", "not mobile-responsive", "no viewport", "high")]
        case = BusinessCaseBuilder().top(audit, "zubar")
        self.assertIsNotNone(case)
        self.assertTrue(case.is_design)
        self.assertIn("dental practice", case.reasoning)

    def test_extract_facts_surfaces_smells(self):
        from waa.analysis import personalization
        facts = personalization.extract_facts(DATED, "https://x.sk",
                                               niche="restauracia", location="Bratislava")
        self.assertTrue(facts.design_smells)
        self.assertIsNotNone(facts.design_score)
        # smells must NOT leak into the verbatim-quotable set
        for label in facts.design_smells:
            self.assertNotIn(label, facts.quotable_strings())


if __name__ == "__main__":
    unittest.main(verbosity=2)
