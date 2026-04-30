"""
Tests for email_validator.

Strategy: every test mocks DNS and SMTP. No packets leave the machine.
Run with:
    .venv/bin/python -m unittest tests.test_email_validator -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import email_validator as ev  # noqa: E402


# Helper to build fake DNS answer objects
def _fake_mx(*pairs):
    """pairs of (preference, hostname)"""
    answers = []
    for pref, host in pairs:
        m = MagicMock()
        m.preference = pref
        m.exchange = MagicMock(__str__=lambda s, h=host: h + ".")
        answers.append(m)
    return answers


def _setup_mx(mock_resolver, hosts):
    """Configure dns.resolver.resolve to return given MX hosts."""
    mock_resolver.return_value = _fake_mx(*[(10 + i*10, h) for i, h in enumerate(hosts)])


# -----------------------------------------------------------------------------
# Syntax + role-account checks (no network)
# -----------------------------------------------------------------------------

class TestSyntaxAndRoleChecks(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()

    def test_empty_string(self):
        r = ev.validate_email("", smtp_probe=False)
        self.assertEqual(r.status, "invalid")
        self.assertIn("empty", r.reason)

    def test_no_at_sign(self):
        r = ev.validate_email("not-an-email", smtp_probe=False)
        self.assertEqual(r.status, "invalid")

    def test_double_at(self):
        r = ev.validate_email("a@@b.com", smtp_probe=False)
        self.assertEqual(r.status, "invalid")

    def test_no_tld(self):
        r = ev.validate_email("user@host", smtp_probe=False)
        self.assertEqual(r.status, "invalid")

    def test_short_tld(self):
        # Single-letter TLDs don't exist
        r = ev.validate_email("user@host.x", smtp_probe=False)
        self.assertEqual(r.status, "invalid")

    def test_whitespace_trimmed(self):
        with patch("email_validator.dns.resolver.resolve") as mock_resolve:
            _setup_mx(mock_resolve, ["mx.x.com"])
            r = ev.validate_email("  USER@X.COM  ", smtp_probe=False)
        self.assertEqual(r.email, "user@x.com")
        self.assertEqual(r.status, "catch_all")  # mx-only mode

    def test_role_account_marked_risky(self):
        r = ev.validate_email("info@example.com", smtp_probe=False)
        self.assertEqual(r.status, "risky")
        self.assertIn("role account", r.reason)

    def test_role_account_disabled(self):
        with patch("email_validator.dns.resolver.resolve") as mock_resolve:
            _setup_mx(mock_resolve, ["mx.example.com"])
            r = ev.validate_email("info@example.com",
                                   skip_role_accounts=False, smtp_probe=False)
        self.assertEqual(r.status, "catch_all")  # mx-only mode


# -----------------------------------------------------------------------------
# MX lookup
# -----------------------------------------------------------------------------

class TestMXLookup(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()

    @patch("email_validator.dns.resolver.resolve")
    def test_mx_found(self, mock_resolve):
        _setup_mx(mock_resolve, ["mx.example.com"])
        hosts = ev._resolve_mx("example.com")
        self.assertEqual(hosts, ["mx.example.com"])

    @patch("email_validator.dns.resolver.resolve")
    def test_mx_sorted_by_preference(self, mock_resolve):
        # Higher pref number = lower priority. Should come last.
        mock_resolve.return_value = _fake_mx((50, "mx3"), (10, "mx1"), (20, "mx2"))
        hosts = ev._resolve_mx("example.com")
        self.assertEqual(hosts, ["mx1", "mx2", "mx3"])

    @patch("email_validator.dns.resolver.resolve")
    def test_no_mx_a_fallback(self, mock_resolve):
        # First call (MX) raises NoAnswer, second (A) returns OK
        mock_resolve.side_effect = [
            ev.dns.resolver.NoAnswer(),
            [MagicMock()],  # A record present
        ]
        hosts = ev._resolve_mx("only-a-record.com")
        self.assertEqual(hosts, ["only-a-record.com"])

    @patch("email_validator.dns.resolver.resolve")
    def test_no_mx_no_a_returns_none(self, mock_resolve):
        mock_resolve.side_effect = [
            ev.dns.resolver.NoAnswer(),
            ev.dns.resolver.NoAnswer(),  # A
            ev.dns.resolver.NoAnswer(),  # AAAA
        ]
        hosts = ev._resolve_mx("nothing-here.example")
        self.assertIsNone(hosts)

    def test_nxdomain_returns_none(self):
        with patch("email_validator.dns.resolver.resolve",
                   side_effect=ev.dns.resolver.NXDOMAIN()):
            self.assertIsNone(ev._resolve_mx("nx.invalid"))

    def test_dns_timeout_returns_none(self):
        with patch("email_validator.dns.resolver.resolve",
                   side_effect=ev.dns.exception.Timeout()):
            self.assertIsNone(ev._resolve_mx("slow.invalid"))

    @patch("email_validator.dns.resolver.resolve")
    def test_mx_cache_hits_only_once(self, mock_resolve):
        _setup_mx(mock_resolve, ["mx.cached.com"])
        ev._resolve_mx("cached.com")
        ev._resolve_mx("cached.com")
        ev._resolve_mx("cached.com")
        self.assertEqual(mock_resolve.call_count, 1)


# -----------------------------------------------------------------------------
# SMTP probe behavior
# -----------------------------------------------------------------------------

class TestSMTPProbe(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()

    def _mock_smtp(self, rcpt_code=250, rcpt_msg=b"OK", mail_code=250,
                   ehlo_raises=False):
        m = MagicMock()
        m.connect.return_value = (220, b"hello")
        if ehlo_raises:
            m.ehlo.side_effect = ev.smtplib.SMTPException("ehlo failed")
            m.helo.return_value = (250, b"helo OK")
        else:
            m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (mail_code, b"OK")
        m.rcpt.return_value = (rcpt_code, rcpt_msg)
        return m

    def test_probe_returns_rcpt_code(self):
        m = self._mock_smtp(rcpt_code=250, rcpt_msg=b"recipient OK")
        with patch("email_validator.smtplib.SMTP", return_value=m):
            code, msg = ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        self.assertEqual(code, 250)
        self.assertIn("OK", msg)

    def test_probe_returns_550_for_unknown_user(self):
        m = self._mock_smtp(rcpt_code=550, rcpt_msg=b"User unknown")
        with patch("email_validator.smtplib.SMTP", return_value=m):
            code, msg = ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        self.assertEqual(code, 550)
        self.assertIn("unknown", msg)

    def test_probe_falls_back_to_helo_when_ehlo_fails(self):
        m = self._mock_smtp(rcpt_code=250, ehlo_raises=True)
        with patch("email_validator.smtplib.SMTP", return_value=m):
            code, _ = ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        self.assertEqual(code, 250)
        m.helo.assert_called_once()

    def test_probe_handles_connection_refused(self):
        with patch("email_validator.smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.connect.side_effect = ConnectionRefusedError()
            code, msg = ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        self.assertIsNone(code)
        self.assertIn("ConnectionRefusedError", msg)

    def test_probe_handles_timeout(self):
        with patch("email_validator.smtplib.SMTP") as MockSMTP:
            import socket
            MockSMTP.return_value.connect.side_effect = socket.timeout()
            code, _ = ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        self.assertIsNone(code)

    def test_probe_always_calls_quit_or_close(self):
        m = self._mock_smtp()
        with patch("email_validator.smtplib.SMTP", return_value=m):
            ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        # quit OR close must have been attempted
        self.assertTrue(m.quit.called or m.close.called)

    def test_probe_resets_after_rcpt(self):
        m = self._mock_smtp()
        with patch("email_validator.smtplib.SMTP", return_value=m):
            ev._smtp_probe("mx.x.com", "v@x.com", "u@x.com")
        # rset is best-effort — should be invoked but tolerate failure
        m.rset.assert_called()


# -----------------------------------------------------------------------------
# Top-level validate_email
# -----------------------------------------------------------------------------

class TestValidateEmail(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_valid_address(self, mock_resolve, mock_smtp):
        _setup_mx(mock_resolve, ["mx.example.com"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        # Real address: 250 OK. Catch-all probe: 550 (so NOT catch-all)
        m.rcpt.side_effect = [(250, b"recipient OK"), (550, b"user unknown")]
        mock_smtp.return_value = m

        r = ev.validate_email("real@example.com")
        self.assertEqual(r.status, "valid")
        self.assertEqual(r.smtp_code, 250)
        self.assertTrue(r.is_safe_to_send())

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_invalid_address_5xx(self, mock_resolve, mock_smtp):
        _setup_mx(mock_resolve, ["mx.example.com"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        m.rcpt.return_value = (550, b"User unknown")
        mock_smtp.return_value = m

        r = ev.validate_email("ghost@example.com")
        self.assertEqual(r.status, "invalid")
        self.assertEqual(r.smtp_code, 550)
        self.assertFalse(r.is_safe_to_send())

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_4xx_greylist_treated_as_catch_all(self, mock_resolve, mock_smtp):
        # 4xx is transient (greylist, throttle). Server is alive. Treat as
        # catch_all so we keep the address; a real send will likely succeed.
        _setup_mx(mock_resolve, ["mx.example.com"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        m.rcpt.return_value = (450, b"Try again later")
        mock_smtp.return_value = m

        r = ev.validate_email("greylisted@example.com")
        self.assertEqual(r.status, "catch_all")
        self.assertEqual(r.smtp_code, 450)
        self.assertTrue(r.is_safe_to_send())

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_smtp_blocked_with_mx_treated_as_catch_all(self, mock_resolve, mock_smtp):
        # Common case: Gmail/Outlook block port 25 from random IPs. We resolve
        # MX successfully but can't probe. Should NOT drop the address.
        _setup_mx(mock_resolve, ["gmail-smtp-in.l.google.com"])
        mock_smtp.return_value.connect.side_effect = TimeoutError("port 25 blocked")

        r = ev.validate_email("user@gmail.com")
        self.assertEqual(r.status, "catch_all")
        self.assertTrue(r.is_safe_to_send())
        self.assertIn("smtp probe blocked", r.reason)

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_catch_all_domain_marked(self, mock_resolve, mock_smtp):
        _setup_mx(mock_resolve, ["mx.catchall.com"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        # Both real address AND junk address get 250 → catch-all
        m.rcpt.return_value = (250, b"OK")
        mock_smtp.return_value = m

        r = ev.validate_email("real@catchall.com")
        self.assertEqual(r.status, "catch_all")
        self.assertTrue(r.is_safe_to_send())

    @patch("email_validator.dns.resolver.resolve",
           side_effect=ev.dns.resolver.NXDOMAIN())
    def test_no_mx_marks_invalid(self, _resolve):
        r = ev.validate_email("nobody@nowhere.invalid", smtp_probe=False)
        self.assertEqual(r.status, "invalid")
        self.assertIn("no MX", r.reason)

    @patch("email_validator.dns.resolver.resolve")
    def test_mx_only_mode(self, mock_resolve):
        _setup_mx(mock_resolve, ["mx.example.com"])
        r = ev.validate_email("anyone@example.com", smtp_probe=False)
        self.assertEqual(r.status, "catch_all")
        self.assertIn("mx-only", r.reason)

    @patch("email_validator.dns.resolver.resolve")
    def test_role_account_short_circuits_before_mx(self, mock_resolve):
        r = ev.validate_email("noreply@example.com")
        self.assertEqual(r.status, "risky")
        # No DNS call should have been made
        mock_resolve.assert_not_called()

    @patch("email_validator.smtplib.SMTP")
    @patch("email_validator.dns.resolver.resolve")
    def test_elapsed_ms_recorded(self, mock_resolve, mock_smtp):
        _setup_mx(mock_resolve, ["mx.example.com"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        m.rcpt.return_value = (250, b"OK")
        mock_smtp.return_value = m

        r = ev.validate_email("a@example.com")
        self.assertGreaterEqual(r.elapsed_ms, 0)


# -----------------------------------------------------------------------------
# Batch validation
# -----------------------------------------------------------------------------

class TestValidateBatch(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()

    def test_batch_preserves_order(self):
        emails = ["a@b.com", "INVALID", "c@d.com"]
        with patch("email_validator.dns.resolver.resolve") as mock_resolve:
            _setup_mx(mock_resolve, ["mx"])
            results, _ = ev.validate_emails(emails, smtp_probe=False)
        self.assertEqual([r.email for r in results],
                          ["a@b.com", "invalid", "c@d.com"])

    def test_batch_records_stats(self):
        with patch("email_validator.dns.resolver.resolve") as mock_resolve:
            _setup_mx(mock_resolve, ["mx"])
            results, stats = ev.validate_emails(
                ["good@example.com", "noreply@example.com", "bad-syntax"],
                smtp_probe=False,
            )
        self.assertEqual(stats.total, 3)
        self.assertEqual(stats.catch_all, 1)  # mx-only mode → catch_all
        self.assertEqual(stats.risky, 1)
        self.assertEqual(stats.invalid, 1)

    def test_batch_survives_validator_crash(self):
        # Inject a TypeError mid-flight; the batch must keep going.
        with patch("email_validator.validate_email",
                   side_effect=[TypeError("boom"),
                                ev.EmailValidationResult(
                                    email="ok@x.com", status="valid",
                                    reason="")]):
            results, stats = ev.validate_emails(["a@b.com", "ok@x.com"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, "unknown")  # graceful fallback
        self.assertEqual(results[1].status, "valid")

    def test_pretty_stats_output(self):
        s = ev.ValidationStats(total=10, valid=4, catch_all=2, risky=1,
                                 invalid=2, unknown=1, elapsed_ms=15000)
        out = s.pretty()
        self.assertIn("validated 10", out)
        self.assertIn("valid=4", out)
        self.assertIn("invalid=2", out)


# -----------------------------------------------------------------------------
# Integration with audit_agent._prepare_send_list
# -----------------------------------------------------------------------------

class TestPrepareSendListWithValidator(unittest.TestCase):

    def setUp(self):
        ev.reset_caches()
        os.environ.setdefault("OUTPUT_DIR", "/tmp/audit_validator_tests")
        os.makedirs("/tmp/audit_validator_tests", exist_ok=True)

    def _audit_results(self):
        return [
            {
                "url": "https://goodsite.com",
                "email": {"subject_line": "x", "email_body": "y"},
                "contact_emails": ["real@goodsite.com"],
            },
            {
                "url": "https://badsite.com",
                "email": {"subject_line": "x", "email_body": "y"},
                "contact_emails": ["dead@badsite.com"],
            },
            {
                "url": "https://rolesite.com",
                "email": {"subject_line": "x", "email_body": "y"},
                "contact_emails": ["info@rolesite.com"],
            },
        ]

    @patch("email_validator.dns.resolver.resolve")
    @patch("email_validator.smtplib.SMTP")
    def test_validator_drops_dead_addresses(self, mock_smtp, mock_resolve):
        # All three domains have MX. Only one accepts the recipient.
        _setup_mx(mock_resolve, ["mx.fake"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        # Order matters: validate_email is called per address. With catch-all
        # detection ON, each address triggers a real probe + a junk probe.
        # The junk probe returns 550 so no catch-all is detected.
        # Sequence: real(real@goodsite)=250, junk(goodsite)=550,
        #           real(dead@badsite)=550, real(info@rolesite) — never
        #           reached because role account short-circuits.
        m.rcpt.side_effect = [
            (250, b"OK"),       # real@goodsite — valid
            (550, b"unknown"),  # junk@goodsite — confirms not catch-all
            (550, b"unknown"),  # dead@badsite — invalid
        ]
        mock_smtp.return_value = m

        # Override sent registry to a fresh empty file
        from audit_agent import _save_sent_registry, _prepare_send_list
        _save_sent_registry({"emails": {}, "domains": {}})

        send_list = _prepare_send_list(
            self._audit_results(), {},
            validate_emails=True,
            probe_from="verify@me.com",
            keep_risky=False,
        )

        addrs = [s["to"] for s in send_list]
        self.assertIn("real@goodsite.com", addrs)
        self.assertNotIn("dead@badsite.com", addrs)
        self.assertNotIn("info@rolesite.com", addrs)

    def test_validator_disabled_keeps_everything(self):
        from audit_agent import _save_sent_registry, _prepare_send_list
        _save_sent_registry({"emails": {}, "domains": {}})

        send_list = _prepare_send_list(
            self._audit_results(), {},
            validate_emails=False,
        )
        addrs = [s["to"] for s in send_list]
        self.assertEqual(set(addrs), {
            "real@goodsite.com",
            "dead@badsite.com",
            "info@rolesite.com",
        })

    @patch("email_validator.dns.resolver.resolve")
    @patch("email_validator.smtplib.SMTP")
    def test_keep_risky_keeps_role_accounts(self, mock_smtp, mock_resolve):
        _setup_mx(mock_resolve, ["mx.fake"])
        m = MagicMock()
        m.connect.return_value = (220, b"hi")
        m.ehlo.return_value = (250, b"OK")
        m.mail.return_value = (250, b"OK")
        m.rcpt.side_effect = [
            (250, b"OK"), (550, b"unknown"),  # real@goodsite + catch-all probe
            (550, b"unknown"),                # dead@badsite
        ]
        mock_smtp.return_value = m

        from audit_agent import _save_sent_registry, _prepare_send_list
        _save_sent_registry({"emails": {}, "domains": {}})

        send_list = _prepare_send_list(
            self._audit_results(), {},
            validate_emails=True,
            probe_from="verify@me.com",
            keep_risky=True,
        )
        addrs = [s["to"] for s in send_list]
        self.assertIn("info@rolesite.com", addrs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
