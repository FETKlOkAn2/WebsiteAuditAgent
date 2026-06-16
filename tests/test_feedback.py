"""
Tests for the feedback / A-B tracking layer (improvement #20). Pure /
deterministic — no tokens, no network.
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.analysis.feedback import (  # noqa: E402
    Dimension, SegmentStat, FeedbackReport, FeedbackAnalyzer, DEFAULT_DIMENSIONS,
)


def _entry(niche="restauracia", sender="Tomas", tier="high", subject="rychla otazka",
           replied=False, followed_up=False):
    return {
        "subject": subject,
        "sent_at": "2026-06-10T10:00:00",
        "reply_received_at": "2026-06-11T09:00:00" if replied else None,
        "followup_sent_at": "2026-06-14T10:00:00" if followed_up else None,
        "dimensions": {"niche": niche, "sender": sender, "lead_tier": tier},
    }


def _registry(*entries):
    return {"emails": {f"a{i}@x.sk": e for i, e in enumerate(entries)},
            "domains": {}}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

class TestSegmentStat(unittest.TestCase):

    def test_reply_rate(self):
        self.assertAlmostEqual(SegmentStat("niche", "x", 4, 1).reply_rate, 0.25)

    def test_reply_rate_zero_sent(self):
        self.assertEqual(SegmentStat("niche", "x", 0, 0).reply_rate, 0.0)

    def test_to_dict_rounds(self):
        d = SegmentStat("niche", "x", 3, 1).to_dict()
        self.assertEqual(d["reply_rate"], round(1 / 3, 4))


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class TestFeedbackAnalyzer(unittest.TestCase):

    def test_empty_registry(self):
        report = FeedbackAnalyzer().analyze({"emails": {}})
        self.assertEqual(report.total_sent, 0)
        self.assertIn("No sends", report.summary())

    def test_overall_counts(self):
        reg = _registry(_entry(replied=True), _entry(replied=False),
                        _entry(replied=True))
        report = FeedbackAnalyzer().analyze(reg)
        self.assertEqual(report.total_sent, 3)
        self.assertEqual(report.total_replied, 2)
        self.assertAlmostEqual(report.overall_reply_rate, 2 / 3)

    def test_by_niche_segmentation(self):
        reg = _registry(
            _entry(niche="zubar", replied=True),
            _entry(niche="zubar", replied=False),
            _entry(niche="kaviaren", replied=False),
        )
        report = FeedbackAnalyzer().analyze(reg)
        rows = {s.segment: s for s in report.by_dimension("niche")}
        self.assertEqual(rows["zubar"].sent, 2)
        self.assertEqual(rows["zubar"].replied, 1)
        self.assertEqual(rows["kaviaren"].replied, 0)

    def test_by_dimension_sorted_best_first(self):
        reg = _registry(
            _entry(sender="Erik", replied=False),
            _entry(sender="Tomas", replied=True),
            _entry(sender="Tomas", replied=True),
        )
        report = FeedbackAnalyzer().analyze(reg)
        senders = report.by_dimension("sender")
        self.assertEqual(senders[0].segment, "Tomas")  # 100% first
        self.assertEqual(senders[0].reply_rate, 1.0)

    def test_subject_length_bucketing(self):
        reg = _registry(
            _entry(subject="ahoj", replied=True),                 # 1-3 words
            _entry(subject="jedna dva tri styri", replied=False), # 4-6 words
        )
        report = FeedbackAnalyzer().analyze(reg)
        segs = {s.segment for s in report.by_dimension("subject_length")}
        self.assertIn("1-3 words", segs)
        self.assertIn("4-6 words", segs)

    def test_follow_up_dimension(self):
        reg = _registry(_entry(followed_up=True, replied=True),
                        _entry(followed_up=False, replied=False))
        report = FeedbackAnalyzer().analyze(reg)
        segs = {s.segment for s in report.by_dimension("follow_up")}
        self.assertEqual(segs, {"followed-up", "first-touch only"})

    def test_missing_dimensions_bucket_unknown(self):
        reg = {"emails": {"a@x.sk": {"subject": "hi", "reply_received_at": None}},
               "domains": {}}
        report = FeedbackAnalyzer().analyze(reg)
        niche_rows = report.by_dimension("niche")
        self.assertEqual(niche_rows[0].segment, "unknown")

    def test_min_sent_filter(self):
        reg = _registry(
            _entry(niche="zubar", replied=True),
            _entry(niche="kaviaren", replied=False),
            _entry(niche="kaviaren", replied=False),
        )
        report = FeedbackAnalyzer().analyze(reg)
        rows = report.by_dimension("niche", min_sent=2)
        self.assertEqual([r.segment for r in rows], ["kaviaren"])

    def test_custom_dimensions(self):
        dim = Dimension("lang", lambda e: e.get("dimensions", {}).get("lang", "sk"))
        reg = _registry(_entry(replied=True))
        report = FeedbackAnalyzer([dim]).analyze(reg)
        self.assertEqual(report.dimensions(), ["lang"])

    def test_non_dict_entries_ignored(self):
        reg = {"emails": {"a@x.sk": _entry(replied=True), "b": "garbage"},
               "domains": {}}
        report = FeedbackAnalyzer().analyze(reg)
        self.assertEqual(report.total_sent, 1)

    def test_to_dict_shape(self):
        report = FeedbackAnalyzer().analyze(_registry(_entry(replied=True)))
        d = report.to_dict()
        self.assertEqual(d["total_sent"], 1)
        self.assertEqual(d["total_replied"], 1)
        self.assertTrue(d["segments"])

    def test_summary_renders(self):
        report = FeedbackAnalyzer().analyze(_registry(_entry(replied=True),
                                                      _entry(replied=False)))
        s = report.summary()
        self.assertIn("Feedback over 2", s)
        self.assertIn("by niche", s)


# ---------------------------------------------------------------------------
# Integration: dimensions recorded at send time, read back by the analyzer
# ---------------------------------------------------------------------------

class TestRecordingIntegration(unittest.TestCase):

    def test_persist_records_dimensions(self):
        import waa.cli as cli
        saved = {}

        def _fake_load():
            return {"emails": {}, "domains": {}}

        def _fake_save(reg):
            saved.update(reg)

        orig_load, orig_save = cli._load_sent_registry, cli._save_sent_registry
        cli._load_sent_registry = _fake_load
        cli._save_sent_registry = _fake_save
        try:
            send_list = [{
                "to": "owner@zubar.sk", "subject": "rychla otazka",
                "website": "https://zubar.sk",
                "lead_value": {"tier": "high", "value": 88},
                "niche": "zubar",
            }]
            results = [{"status": "sent", "message_id": "<m1>"}]
            cli._persist_sent_results(send_list, results, sender="Erik")
        finally:
            cli._load_sent_registry, cli._save_sent_registry = orig_load, orig_save

        entry = saved["emails"]["owner@zubar.sk"]
        dims = entry["dimensions"]
        self.assertEqual(dims["niche"], "zubar")
        self.assertEqual(dims["sender"], "Erik")
        self.assertEqual(dims["lead_tier"], "high")

        # The analyzer can immediately segment by what we just recorded.
        report = FeedbackAnalyzer().analyze(saved)
        self.assertEqual(report.by_dimension("sender")[0].segment, "Erik")


if __name__ == "__main__":
    unittest.main(verbosity=2)
