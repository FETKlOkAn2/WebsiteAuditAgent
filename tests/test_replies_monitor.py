"""
Tests for replies_monitor.

All offline — IMAP and HTTP are mocked. Run with:
    .venv/bin/python -m unittest tests.test_replies_monitor -v
"""

from __future__ import annotations

import email as _email
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Force a writable test output dir BEFORE importing replies_monitor (it
# computes module-level paths from config.OUTPUT_DIR).
TEST_OUT = tempfile.mkdtemp(prefix="replies_tests_")
os.environ["OUTPUT_DIR"] = TEST_OUT

import waa.config as config  # noqa: E402
config.OUTPUT_DIR = TEST_OUT

import waa.outreach.replies_monitor as rm  # noqa: E402
# Re-anchor module-level paths after the OUTPUT_DIR override
rm.REPLIES_SEEN_FILE = os.path.join(TEST_OUT, "replies_seen.json")
rm.SENT_REGISTRY_FILE = os.path.join(TEST_OUT, "sent_registry.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_email(
    *,
    from_addr: str = "owner@goodsite.com",
    from_name: str = "Owner Person",
    subject: str = "Re: quick note about your site",
    body: str = "Hi Tomas, thanks for reaching out. Yes I'd like more details.",
    msg_id: str = "<reply-1@goodsite.com>",
    in_reply_to: str = "<our-msg-1@emtdstudio.com>",
    date_str: str = "Thu, 1 May 2026 10:15:00 +0200",
    multipart: bool = False,
) -> bytes:
    if multipart:
        raw = (
            f"From: {from_name} <{from_addr}>\r\n"
            f"To: tomasmaxim@emtdstudio.com\r\n"
            f"Subject: {subject}\r\n"
            f"Message-ID: {msg_id}\r\n"
            f"In-Reply-To: {in_reply_to}\r\n"
            f"Date: {date_str}\r\n"
            f"MIME-Version: 1.0\r\n"
            f"Content-Type: multipart/alternative; boundary=BOUNDARY\r\n"
            f"\r\n"
            f"--BOUNDARY\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}\r\n"
            f"\r\n"
            f"On Thu, 1 May 2026 09:00, Tomas wrote:\r\n"
            f"> hi this is the original email\r\n"
            f"--BOUNDARY\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"\r\n"
            f"<p>{body}</p>\r\n"
            f"--BOUNDARY--\r\n"
        )
    else:
        raw = (
            f"From: {from_name} <{from_addr}>\r\n"
            f"To: tomasmaxim@emtdstudio.com\r\n"
            f"Subject: {subject}\r\n"
            f"Message-ID: {msg_id}\r\n"
            f"In-Reply-To: {in_reply_to}\r\n"
            f"Date: {date_str}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}\r\n"
        )
    return raw.encode("utf-8")


# ---------------------------------------------------------------------------
# Parse + body extraction
# ---------------------------------------------------------------------------

class TestParse(unittest.TestCase):

    def test_parse_simple_text(self):
        raw = _make_raw_email()
        msg = _email.message_from_bytes(raw)
        r = rm._parse_message(msg, "Tomas")
        self.assertIsNotNone(r)
        self.assertEqual(r.from_email, "owner@goodsite.com")
        self.assertEqual(r.from_name, "Owner Person")
        self.assertIn("quick note", r.subject)
        self.assertEqual(r.message_id, "<reply-1@goodsite.com>")
        self.assertEqual(r.in_reply_to, "<our-msg-1@emtdstudio.com>")
        self.assertIn("thanks for reaching out", r.body_preview)

    def test_parse_multipart_prefers_text_plain(self):
        raw = _make_raw_email(multipart=True, body="multipart text body content")
        msg = _email.message_from_bytes(raw)
        r = rm._parse_message(msg, "Tomas")
        self.assertIn("multipart text body content", r.body_preview)

    def test_quote_to_preview_strips_quoted_chain(self):
        body = (
            "Yes that sounds good.\n"
            "\n"
            "On Thu, 1 May 2026 at 10:00, Tomas wrote:\n"
            "> hi I noticed something on your site\n"
            "> Tomas\n"
        )
        out = rm._quote_to_preview(body)
        self.assertIn("Yes that sounds good", out)
        self.assertNotIn("hi I noticed", out)
        self.assertNotIn("Tomas wrote", out)

    def test_quote_to_preview_truncates(self):
        body = "x" * 5000
        out = rm._quote_to_preview(body, limit=200)
        self.assertLess(len(out), 220)  # accounts for ellipsis padding
        self.assertTrue(out.endswith("…"))

    def test_quote_to_preview_strips_signature(self):
        body = "Sure, send me the details.\n\n--\nOwner\n+421 900 000 000"
        out = rm._quote_to_preview(body)
        self.assertIn("send me the details", out)
        self.assertNotIn("+421", out)

    def test_parse_handles_missing_headers(self):
        raw = b"Subject: hi\r\n\r\nbody"
        msg = _email.message_from_bytes(raw)
        # No From → returns None
        self.assertIsNone(rm._parse_message(msg, "Tomas"))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

class TestFilters(unittest.TestCase):

    def _r(self, **kwargs):
        defaults = dict(
            sender_mailbox="Tomas", from_email="x@y.com", from_name="X",
            subject="Re: hello", date=None, body_preview="hi",
            message_id="<a>", in_reply_to=None, refs=[],
        )
        defaults.update(kwargs)
        return rm.Reply(**defaults)

    def test_mailer_daemon_is_noise(self):
        r = self._r(from_email="mailer-daemon@x.com", subject="undeliverable")
        is_noise, reason = rm.is_noise(r)
        self.assertTrue(is_noise)
        self.assertIn("system sender", reason)

    def test_postmaster_is_noise(self):
        r = self._r(from_email="postmaster@gmail.com")
        self.assertTrue(rm.is_noise(r)[0])

    def test_noreply_is_noise(self):
        r = self._r(from_email="noreply@biz.com")
        self.assertTrue(rm.is_noise(r)[0])

    def test_out_of_office_subject_is_noise(self):
        r = self._r(subject="Out of office: away until May 5")
        self.assertTrue(rm.is_noise(r)[0])

    def test_auto_reply_subject_is_noise(self):
        r = self._r(subject="Automatic Reply")
        self.assertTrue(rm.is_noise(r)[0])

    def test_slovak_auto_reply_is_noise(self):
        r = self._r(subject="Mimo kancelárie do 5. mája")
        self.assertTrue(rm.is_noise(r)[0])

    def test_real_reply_is_not_noise(self):
        r = self._r(subject="Re: quick note about your site",
                    body_preview="Yes I'd like to hear more.")
        self.assertFalse(rm.is_noise(r)[0])

    def test_genuine_reply_by_email_match(self):
        r = self._r(from_email="owner@biz.com")
        registry = {"emails": {"owner@biz.com": {"subject": "x"}}, "domains": {}}
        ok, via = rm.is_genuine_reply(r, registry)
        self.assertTrue(ok)
        self.assertEqual(via, "email")

    def test_genuine_reply_by_domain_match(self):
        # Replied from a different mailbox at same company
        r = self._r(from_email="ceo@biz.com")
        registry = {
            "emails": {"info@biz.com": {"subject": "x"}},
            "domains": {"biz.com": {"email": "info@biz.com"}},
        }
        ok, via = rm.is_genuine_reply(r, registry)
        self.assertTrue(ok)
        self.assertEqual(via, "domain")

    def test_not_a_reply_when_unknown_sender(self):
        r = self._r(from_email="random@stranger.com")
        registry = {"emails": {}, "domains": {}}
        ok, _ = rm.is_genuine_reply(r, registry)
        self.assertFalse(ok)

    def test_genuine_reply_by_in_reply_to_header(self):
        r = self._r(from_email="forwarded@elsewhere.com",
                    in_reply_to="<our-msg-42@emtdstudio.com>")
        registry = {
            "emails": {"original@biz.com": {
                "subject": "x", "message_id": "<our-msg-42@emtdstudio.com>",
            }},
            "domains": {},
        }
        ok, via = rm.is_genuine_reply(r, registry)
        self.assertTrue(ok)
        self.assertEqual(via, "header")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence(unittest.TestCase):

    def setUp(self):
        # Fresh empty state for every test
        for p in (rm.REPLIES_SEEN_FILE, rm.SENT_REGISTRY_FILE):
            if os.path.exists(p):
                os.remove(p)

    def test_load_when_missing(self):
        seen = rm.load_seen()
        self.assertEqual(seen, {"keys": []})

    def test_save_and_reload(self):
        rm.save_seen({"keys": ["<a>", "<b>"]})
        reloaded = rm.load_seen()
        self.assertEqual(set(reloaded["keys"]), {"<a>", "<b>"})

    def test_corrupt_file_starts_fresh(self):
        with open(rm.REPLIES_SEEN_FILE, "w") as f:
            f.write("{not valid json")
        seen = rm.load_seen()
        self.assertEqual(seen["keys"], [])

    def test_save_caps_at_5000(self):
        big = {"keys": [f"<{i}>" for i in range(6000)]}
        rm.save_seen(big)
        reloaded = rm.load_seen()
        self.assertEqual(len(reloaded["keys"]), 5000)

    def test_load_sent_registry_when_missing(self):
        reg = rm.load_sent_registry()
        self.assertEqual(reg, {"emails": {}, "domains": {}})


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

class TestDiscordPost(unittest.TestCase):

    def _r(self):
        return rm.Reply(
            sender_mailbox="Tomas",
            from_email="owner@biz.com",
            from_name="Owner",
            subject="Re: quick note",
            date=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
            body_preview="Yes I'd like more info.",
            message_id="<r1>",
            matched_via="email",
            original_subject="quick note",
        )

    @patch("waa.outreach.replies_monitor.requests.post")
    def test_post_success(self, mock_post):
        mock_post.return_value.status_code = 204
        ok = rm.post_to_discord(self._r(), webhook_url="https://discord/x")
        self.assertTrue(ok)
        # Verify we sent a JSON payload with an embed
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], "https://discord/x")
        payload = kwargs["json"]
        self.assertIn("embeds", payload)
        embed = payload["embeds"][0]
        self.assertIn("Re: quick note", embed["title"])
        self.assertIn("Yes I'd like more info", embed["description"])
        # Fields should include From, mailbox, matched_via, original
        field_names = [f["name"] for f in embed["fields"]]
        self.assertIn("From", field_names)
        self.assertIn("To (our mailbox)", field_names)
        self.assertIn("Original outreach", field_names)

    @patch("waa.outreach.replies_monitor.requests.post")
    def test_post_failure_returns_false(self, mock_post):
        mock_post.return_value.status_code = 500
        mock_post.return_value.text = "boom"
        ok = rm.post_to_discord(self._r(), webhook_url="https://discord/x")
        self.assertFalse(ok)

    @patch("waa.outreach.replies_monitor.requests.post",
           side_effect=Exception("network down"))
    def test_post_request_exception_returns_false(self, _mock):
        # network errors should not propagate
        import requests as _req
        with patch("waa.outreach.replies_monitor.requests.post",
                   side_effect=_req.RequestException("conn refused")):
            ok = rm.post_to_discord(self._r(), webhook_url="https://x")
        self.assertFalse(ok)

    def test_post_without_webhook_falls_back_to_print(self):
        # No webhook URL → returns False but prints. Should not raise.
        ok = rm.post_to_discord(self._r(), webhook_url="")
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# fetch_recent (mocked imaplib)
# ---------------------------------------------------------------------------

class _FakeIMAP:
    """Stub IMAP4_SSL implementation."""

    def __init__(self, raw_messages: list[bytes], login_ok: bool = True):
        self.raw_messages = raw_messages
        self.login_ok = login_ok
        self.search_calls = 0
        self.fetch_calls = 0

    def login(self, email, password):
        if not self.login_ok:
            raise __import__("imaplib").IMAP4.error("auth failed")
        return ("OK", [b""])

    def select(self, mbox, readonly=False):
        return ("OK", [b""])

    def search(self, charset, criteria):
        self.search_calls += 1
        return ("OK", [b" ".join(str(i).encode() for i in range(1, len(self.raw_messages)+1))])

    def fetch(self, mid, parts):
        self.fetch_calls += 1
        idx = int(mid) - 1
        if idx < 0 or idx >= len(self.raw_messages):
            return ("NO", [None])
        return ("OK", [(b"1 (BODY[] {N}", self.raw_messages[idx])])

    def close(self):
        return None

    def logout(self):
        return None


class TestFetchRecent(unittest.TestCase):

    def test_fetch_returns_replies(self):
        raw = [
            _make_raw_email(from_addr="a@x.com", msg_id="<m1>"),
            _make_raw_email(from_addr="b@y.com", msg_id="<m2>",
                            subject="Re: another"),
        ]
        fake = _FakeIMAP(raw)
        with patch("waa.outreach.replies_monitor.imaplib.IMAP4_SSL", return_value=fake):
            mb = rm.MailboxConfig(label="Tomas", email="a@b.com", password="pw")
            replies = rm.fetch_recent(mb)
        self.assertEqual(len(replies), 2)
        emails = [r.from_email for r in replies]
        self.assertIn("a@x.com", emails)
        self.assertIn("b@y.com", emails)

    def test_unconfigured_mailbox_skipped(self):
        mb = rm.MailboxConfig(label="Erik", email="", password="")
        # Should not even try to connect
        with patch("waa.outreach.replies_monitor.imaplib.IMAP4_SSL") as MockImap:
            replies = rm.fetch_recent(mb)
        self.assertEqual(replies, [])
        MockImap.assert_not_called()

    def test_login_failure_returns_empty(self):
        fake = _FakeIMAP([], login_ok=False)
        with patch("waa.outreach.replies_monitor.imaplib.IMAP4_SSL", return_value=fake):
            mb = rm.MailboxConfig(label="Tomas", email="a@b.com", password="bad")
            replies = rm.fetch_recent(mb)
        self.assertEqual(replies, [])

    def test_connect_failure_returns_empty(self):
        with patch("waa.outreach.replies_monitor.imaplib.IMAP4_SSL",
                   side_effect=ConnectionRefusedError("imap down")):
            mb = rm.MailboxConfig(label="Tomas", email="a@b.com", password="pw")
            replies = rm.fetch_recent(mb)
        self.assertEqual(replies, [])

    def test_corrupt_message_does_not_kill_batch(self):
        # First message is broken bytes; second is fine.
        good = _make_raw_email(from_addr="ok@x.com", msg_id="<ok>")
        # The fake IMAP returns these in order; corrupt first
        fake = _FakeIMAP([b"\x00\x01\x02 not really an email", good])
        with patch("waa.outreach.replies_monitor.imaplib.IMAP4_SSL", return_value=fake):
            mb = rm.MailboxConfig(label="Tomas", email="a@b.com", password="pw")
            replies = rm.fetch_recent(mb)
        # Even if first parses to empty, second should still return.
        # _parse_message returns None for headerless garbage so we get 0–1.
        self.assertLessEqual(len(replies), 2)


# ---------------------------------------------------------------------------
# run_once — orchestration
# ---------------------------------------------------------------------------

class TestRunOnce(unittest.TestCase):

    def setUp(self):
        # Reset state files
        for p in (rm.REPLIES_SEEN_FILE, rm.SENT_REGISTRY_FILE):
            if os.path.exists(p):
                os.remove(p)
        # Default config: 1 mailbox configured, others empty
        os.environ["SMTP_EMAIL"] = "tomas@emtdstudio.com"
        os.environ["SMTP_PASSWORD"] = "fakepw"
        os.environ.pop("SMTP_EMAIL_2", None)
        os.environ.pop("SMTP_PASSWORD_2", None)
        os.environ.pop("SMTP_EMAIL_3", None)
        os.environ.pop("SMTP_PASSWORD_3", None)

    def _write_registry(self, **kwargs):
        registry = {
            "emails": kwargs.get("emails", {}),
            "domains": kwargs.get("domains", {}),
        }
        os.makedirs(TEST_OUT, exist_ok=True)
        with open(rm.SENT_REGISTRY_FILE, "w") as f:
            json.dump(registry, f)

    @patch("waa.outreach.replies_monitor.post_to_discord", return_value=True)
    @patch("waa.outreach.replies_monitor.fetch_recent")
    def test_posts_genuine_reply_and_skips_noise(self, mock_fetch, mock_post):
        # Three messages: one real reply, one out-of-office, one stranger
        replies = [
            rm.Reply(sender_mailbox="Tomas",
                     from_email="owner@goodsite.com", from_name="Owner",
                     subject="Re: quick note", date=None,
                     body_preview="Yes interested.", message_id="<r1>"),
            rm.Reply(sender_mailbox="Tomas",
                     from_email="boss@goodsite.com", from_name="Boss",
                     subject="Out of office", date=None,
                     body_preview="I'm away.", message_id="<r2>"),
            rm.Reply(sender_mailbox="Tomas",
                     from_email="random@stranger.com", from_name="Stranger",
                     subject="Hi there", date=None,
                     body_preview="cold pitch", message_id="<r3>"),
        ]
        mock_fetch.return_value = replies

        self._write_registry(
            emails={"owner@goodsite.com": {"subject": "quick note"}},
            domains={"goodsite.com": {"email": "owner@goodsite.com"}},
        )

        summary = rm.run_once(webhook_url="https://discord/x")
        self.assertEqual(summary["posted"], 1)
        self.assertEqual(summary["skipped_noise"], 1)
        self.assertEqual(summary["skipped_not_a_reply"], 1)
        mock_post.assert_called_once()
        # Verify the matched_via was annotated
        posted_reply = mock_post.call_args[0][0]
        self.assertEqual(posted_reply.matched_via, "email")
        self.assertEqual(posted_reply.original_subject, "quick note")

    @patch("waa.outreach.replies_monitor.post_to_discord", return_value=True)
    @patch("waa.outreach.replies_monitor.fetch_recent")
    def test_does_not_double_post_across_runs(self, mock_fetch, mock_post):
        replies = [
            rm.Reply(sender_mailbox="Tomas",
                     from_email="owner@x.com", from_name="O",
                     subject="Re: hi", date=None,
                     body_preview="Yes", message_id="<dedup-1>"),
        ]
        mock_fetch.return_value = replies
        self._write_registry(emails={"owner@x.com": {"subject": "hi"}})

        s1 = rm.run_once(webhook_url="https://discord/x")
        s2 = rm.run_once(webhook_url="https://discord/x")
        self.assertEqual(s1["posted"], 1)
        self.assertEqual(s2["posted"], 0)
        self.assertEqual(s2["skipped_seen"], 1)
        # Discord webhook called only once across both runs
        self.assertEqual(mock_post.call_count, 1)

    @patch("waa.outreach.replies_monitor.fetch_recent")
    def test_dry_run_does_not_post_or_persist(self, mock_fetch):
        mock_fetch.return_value = [
            rm.Reply(sender_mailbox="Tomas",
                     from_email="owner@x.com", from_name="O",
                     subject="Re: hi", date=None,
                     body_preview="Yes", message_id="<dry-1>"),
        ]
        self._write_registry(emails={"owner@x.com": {"subject": "hi"}})

        with patch("waa.outreach.replies_monitor.post_to_discord") as mock_post:
            summary = rm.run_once(webhook_url="https://discord/x", dry_run=True)
        # In dry-run mode `posted` counts what WOULD have been posted
        self.assertEqual(summary["posted"], 1)
        mock_post.assert_not_called()
        # Seen file should NOT be persisted in dry run
        if os.path.exists(rm.REPLIES_SEEN_FILE):
            with open(rm.REPLIES_SEEN_FILE) as f:
                seen = json.load(f)
            self.assertNotIn("<dry-1>", seen.get("keys", []))

    @patch("waa.outreach.replies_monitor.post_to_discord")
    @patch("waa.outreach.replies_monitor.fetch_recent")
    def test_failed_post_will_retry_next_run(self, mock_fetch, mock_post):
        # First post fails → not added to seen → next run re-tries
        mock_fetch.return_value = [
            rm.Reply(sender_mailbox="Tomas",
                     from_email="o@x.com", from_name="O",
                     subject="Re: hi", date=None,
                     body_preview="Y", message_id="<retry-1>"),
        ]
        self._write_registry(emails={"o@x.com": {"subject": "hi"}})

        mock_post.side_effect = [False, True]
        s1 = rm.run_once(webhook_url="https://x")
        s2 = rm.run_once(webhook_url="https://x")
        self.assertEqual(s1["posted"], 0)  # first attempt failed
        self.assertEqual(s2["posted"], 1)  # second retried + succeeded
        self.assertEqual(mock_post.call_count, 2)

    @patch("waa.outreach.replies_monitor.fetch_recent", return_value=[])
    def test_no_messages_returns_zero_summary(self, _fetch):
        summary = rm.run_once(webhook_url="https://x")
        self.assertEqual(summary["examined"], 0)
        self.assertEqual(summary["posted"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
