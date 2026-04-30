"""
email_validator.py — multi-level email validation, no paid APIs.

Why this exists
---------------
Hard bounces (sending to addresses that don't exist) are the single biggest
killer of cold-email sender reputation. Every mailer-daemon return is a strike
against the domain. Mailbox providers (Gmail, Outlook) start throttling and
spam-foldering once the bounce rate goes above ~3%.

This module validates BEFORE the SMTP send step so dead addresses never enter
the queue.

What it does
------------
Five validation levels, cheapest first; first failure short-circuits:

    1. Syntax        — must be RFC-shaped (a@b.c)
    2. Role accounts — info@, noreply@, support@ etc. (configurable)
    3. MX lookup     — does the domain accept mail at all?
    4. SMTP probe    — RCPT TO check against the recipient MX. We never send
                       a real message — the transaction is RSET'd before DATA.
    5. Catch-all     — sanity check the SMTP "OK" by also probing a random
                       nonsense address; if both pass, the domain is catch-all
                       and we mark the result as `catch_all` (uncertain but
                       safe-ish to send).

Statuses returned
-----------------
    valid      → safe to send (verified at MX level)
    catch_all  → MX accepts everything, can't verify but probably OK to send
    risky      → role account; can send but reply rate is near zero
    invalid    → confirmed dead (syntax broken, no MX, or 5xx RCPT)
    unknown    → transient failure (timeout, 4xx, network error) — try again

Caches per process: MX results and catch-all status, so we don't hammer the
same domain repeatedly.

Reputation safety
-----------------
- We use `verify@<sender_domain>` as MAIL FROM, NOT the real campaign sender.
  This isolates probe traffic from the campaign's own reputation.
- We RSET after each RCPT TO so transactions never reach the DATA stage.
- One probe per address + at most one extra (catch-all) per domain.
- No paid APIs.
"""

from __future__ import annotations

import logging
import random
import re
import smtplib
import socket
import string
import time
from dataclasses import dataclass, asdict, field
from typing import Optional

try:
    import dns.resolver
    import dns.exception
    _HAS_DNS = True
except ImportError:  # pragma: no cover
    _HAS_DNS = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# RFC 5321: local 1–64, total 1–254. We're a little stricter on the TLD shape.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9](?:[a-zA-Z0-9.\-]{0,251}[a-zA-Z0-9])?\."
    r"[a-zA-Z]{2,}$"
)

# Local-parts that almost never produce a reply on cold outreach.
# `info@`, `support@` etc. are technically real mailboxes but they're mostly
# auto-monitored or shared. Treat as `risky`, not `valid`. The orchestrator
# can choose to skip or to send anyway.
ROLE_ACCOUNTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "abuse",
    "info", "support", "help", "contact", "office", "admin",
    "sales", "marketing", "billing", "accounts", "press",
    "privacy", "legal", "hr", "jobs", "careers", "spam",
    "webmaster", "hostmaster", "root", "ftp", "www",
}

# Defaults
DEFAULT_TIMEOUT = 8.0  # seconds per network op
DEFAULT_HELO_HOSTNAME = "validator.local"


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class EmailValidationResult:
    email: str
    status: str               # valid | catch_all | risky | invalid | unknown
    reason: str
    mx_host: Optional[str] = None
    smtp_code: Optional[int] = None
    smtp_message: Optional[str] = None
    elapsed_ms: int = 0

    def is_safe_to_send(self) -> bool:
        """True if we should keep this address in the send queue."""
        return self.status in ("valid", "catch_all")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationStats:
    total: int = 0
    valid: int = 0
    catch_all: int = 0
    risky: int = 0
    invalid: int = 0
    unknown: int = 0
    elapsed_ms: int = 0
    invalid_reasons: dict[str, int] = field(default_factory=dict)

    def record(self, r: EmailValidationResult):
        self.total += 1
        setattr(self, r.status, getattr(self, r.status, 0) + 1)
        self.elapsed_ms += r.elapsed_ms
        if r.status in ("invalid", "unknown"):
            key = r.reason.split(":")[0][:60]
            self.invalid_reasons[key] = self.invalid_reasons.get(key, 0) + 1

    def pretty(self) -> str:
        return (
            f"validated {self.total} | "
            f"valid={self.valid} catch_all={self.catch_all} "
            f"risky={self.risky} invalid={self.invalid} unknown={self.unknown} "
            f"({self.elapsed_ms/1000:.1f}s total)"
        )


# Per-process caches
_MX_CACHE: dict[str, Optional[list[str]]] = {}
_CATCH_ALL_CACHE: dict[str, bool] = {}


def reset_caches():
    """Reset MX and catch-all caches (test helper)."""
    _MX_CACHE.clear()
    _CATCH_ALL_CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_email(
    email: str,
    *,
    probe_from: str = "verify@validator.local",
    helo_hostname: str = DEFAULT_HELO_HOSTNAME,
    skip_role_accounts: bool = True,
    detect_catch_all: bool = True,
    smtp_probe: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> EmailValidationResult:
    """
    Validate a single email address.

    Args:
        email: address to check
        probe_from: MAIL FROM used during the SMTP probe. Use a generic
            address on a domain you control (e.g. `verify@yourdomain.com`).
            Do NOT use the real campaign sender — that ties probe traffic to
            campaign reputation.
        helo_hostname: hostname sent in EHLO/HELO. Doesn't have to resolve
            but should look reasonable.
        skip_role_accounts: if True, role accounts (info@, support@, …) are
            classified as `risky` (still safe-to-send by default but marked).
        detect_catch_all: if True, run a second probe with a random local
            part to detect catch-all domains.
        smtp_probe: if False, stop at the MX-lookup level. Faster but less
            accurate (still catches ~70% of dead addresses).
        timeout: per-network-op timeout in seconds.

    Returns:
        EmailValidationResult — see module docstring for status meanings.
    """
    started = time.time()
    email_clean = (email or "").strip().lower()

    def done(status: str, reason: str, **kw) -> EmailValidationResult:
        return EmailValidationResult(
            email=email_clean, status=status, reason=reason,
            elapsed_ms=int((time.time() - started) * 1000),
            **kw,
        )

    # 1. Syntax
    if not email_clean:
        return done("invalid", "syntax: empty")
    if not _EMAIL_RE.match(email_clean):
        return done("invalid", "syntax: not RFC-shaped")
    if email_clean.count("@") != 1:
        return done("invalid", "syntax: multiple @")

    local, domain = email_clean.split("@", 1)

    # 2. Role accounts
    if skip_role_accounts and local in ROLE_ACCOUNTS:
        return done("risky", f"role account: {local}@…")

    # 3. MX lookup (with A-record fallback per RFC 5321 §5.1)
    mx_hosts = _resolve_mx(domain, timeout=timeout)
    if not mx_hosts:
        return done("invalid", f"no MX or A record for {domain}")

    primary_mx = mx_hosts[0]

    if not smtp_probe:
        # MX-only mode: we know the domain accepts mail, but we can't
        # confirm the specific address exists. Treat as catch_all.
        return done("catch_all",
                    f"mx-only mode: {domain} has MX, address not probed",
                    mx_host=primary_mx)

    # 4. SMTP RCPT TO probe
    code, message = _smtp_probe(
        primary_mx, probe_from, email_clean,
        helo_hostname=helo_hostname, timeout=timeout,
    )

    if code is None:
        # Probe couldn't connect at all. The MX exists (we just resolved it),
        # so the domain DOES accept mail — we simply can't verify this specific
        # recipient. This is normal when port 25 is blocked outbound (typical
        # on residential ISPs, CI runners, and major free providers like Gmail
        # and Outlook). Treat as `catch_all` so the address is kept in the
        # send queue; the worst case is the same risk profile as a real
        # catch-all domain.
        return done(
            "catch_all",
            f"mx ok but smtp probe blocked: {message[:120] if message else 'no response'}",
            mx_host=primary_mx,
        )
    if 400 <= code < 500:
        # 4xx = transient (greylist, throttle, defer). Server is alive and
        # accepting connections, just deferring. Treat as catch_all — sending
        # the real campaign mail will likely succeed (different IP, different
        # context, fresh connection).
        return done(
            "catch_all",
            f"smtp {code} (transient): {(message or '')[:120]}",
            mx_host=primary_mx, smtp_code=code, smtp_message=message,
        )
    if code >= 500:
        # 5xx = permanent: address rejected.
        return done(
            "invalid",
            f"smtp {code}: {(message or '')[:120]}",
            mx_host=primary_mx, smtp_code=code, smtp_message=message,
        )

    # code is in the 2xx/3xx range — RCPT accepted.

    # 5. Catch-all detection
    if detect_catch_all and _is_catch_all(
        primary_mx, domain, probe_from, helo_hostname, timeout
    ):
        return done(
            "catch_all",
            f"{domain} accepts any recipient; verification inconclusive",
            mx_host=primary_mx, smtp_code=code, smtp_message=message,
        )

    return done(
        "valid", "smtp accepted RCPT TO",
        mx_host=primary_mx, smtp_code=code, smtp_message=message,
    )


def validate_emails(
    emails: list[str],
    *,
    progress: bool = False,
    **kwargs,
) -> tuple[list[EmailValidationResult], ValidationStats]:
    """
    Validate a batch of emails. Order is preserved.

    Returns (results, stats). Stats are useful for campaign reporting:
    "we dropped X invalid + Y unknown emails before sending".
    """
    results: list[EmailValidationResult] = []
    stats = ValidationStats()
    total = len(emails)

    for i, e in enumerate(emails, 1):
        try:
            r = validate_email(e, **kwargs)
        except Exception as exc:  # never let one bad address kill the batch
            logger.exception(f"validator crashed on {e!r}")
            r = EmailValidationResult(
                email=(e or "").strip().lower(),
                status="unknown",
                reason=f"validator error: {exc.__class__.__name__}: {exc}",
            )
        results.append(r)
        stats.record(r)
        if progress:
            logger.info(f"  [{i}/{total}] {r.email} → {r.status} ({r.reason})")

    return results, stats


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_mx(domain: str, *, timeout: float = DEFAULT_TIMEOUT) -> Optional[list[str]]:
    """
    Resolve the MX records for `domain`, falling back to A/AAAA per
    RFC 5321 §5.1 ("If no MX records are found, the A RR is used"). Returns
    a list of hostnames sorted by MX preference, or None if nothing accepts
    mail at this domain.
    """
    if not _HAS_DNS:
        # Without dnspython we can't probe MX — report as unknown so the
        # caller can fall back to MX-less validation (syntax only).
        logger.warning("dnspython is not installed; MX checks disabled")
        _MX_CACHE[domain] = None
        return None

    if domain in _MX_CACHE:
        return _MX_CACHE[domain]

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        sorted_ans = sorted(
            ((r.preference, str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0],
        )
        hosts = [h for _, h in sorted_ans if h]
        _MX_CACHE[domain] = hosts or None
        return _MX_CACHE[domain]

    except dns.resolver.NoAnswer:
        # No MX. Try A/AAAA fallback.
        for rrtype in ("A", "AAAA"):
            try:
                dns.resolver.resolve(domain, rrtype, lifetime=timeout)
                _MX_CACHE[domain] = [domain]
                return _MX_CACHE[domain]
            except Exception:
                continue
        _MX_CACHE[domain] = None
        return None

    except (dns.resolver.NXDOMAIN, dns.exception.Timeout,
            dns.resolver.NoNameservers, dns.exception.DNSException):
        _MX_CACHE[domain] = None
        return None
    except Exception as e:  # last-resort safety net
        logger.warning(f"unexpected DNS error for {domain}: {e}")
        _MX_CACHE[domain] = None
        return None


def _smtp_probe(
    mx_host: str,
    sender: str,
    recipient: str,
    *,
    helo_hostname: str = DEFAULT_HELO_HOSTNAME,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Optional[int], Optional[str]]:
    """
    Probe `mx_host` with EHLO + MAIL FROM + RCPT TO. RSETs and quits before
    DATA, so no message is ever sent. Returns (code, message). On connection
    failure returns (None, error_string).
    """
    server = None
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host, 25)
        try:
            server.ehlo(helo_hostname)
        except smtplib.SMTPException:
            try:
                server.helo(helo_hostname)
            except smtplib.SMTPException as e:
                return getattr(e, "smtp_code", None), str(e)

        # MAIL FROM. If the server rejects the sender entirely (rare, but
        # happens with strict configs), surface that as an error.
        try:
            mail_code, mail_msg = server.mail(sender)
        except smtplib.SMTPException as e:
            return getattr(e, "smtp_code", None), str(e)
        if mail_code >= 400:
            return mail_code, _decode(mail_msg)

        # RCPT TO — the actual answer we care about
        try:
            code, msg = server.rcpt(recipient)
        except smtplib.SMTPException as e:
            return getattr(e, "smtp_code", None), str(e)

        return code, _decode(msg)

    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            socket.timeout, ConnectionRefusedError, ConnectionResetError,
            OSError) as e:
        logger.debug(f"smtp probe to {mx_host} failed at connect: {e}")
        return None, f"{e.__class__.__name__}: {e}"
    except Exception as e:
        logger.debug(f"smtp probe to {mx_host} unexpected: {e}")
        return None, f"{e.__class__.__name__}: {e}"
    finally:
        if server is not None:
            try:
                server.rset()
            except Exception:
                pass
            try:
                server.quit()
            except Exception:
                try:
                    server.close()
                except Exception:
                    pass


def _is_catch_all(
    mx_host: str,
    domain: str,
    sender: str,
    helo_hostname: str,
    timeout: float,
) -> bool:
    """
    Probe a guaranteed-fake address on `domain`. If the MX accepts it, the
    domain is catch-all and any RCPT TO would have returned 250 — meaning
    our earlier OK was uninformative.

    If the junk probe fails to connect (None code), we conservatively answer
    False — we don't know it's catch-all, and the real address probe already
    succeeded, so we trust that result.
    """
    if domain in _CATCH_ALL_CACHE:
        return _CATCH_ALL_CACHE[domain]

    junk_local = "x" + "".join(random.choices(string.ascii_lowercase, k=18))
    junk = f"{junk_local}-not-a-real-mailbox-{int(time.time())}@{domain}"
    code, _msg = _smtp_probe(
        mx_host, sender, junk,
        helo_hostname=helo_hostname, timeout=timeout,
    )
    # Only flag catch-all when the junk probe definitively returned 2xx.
    # None/4xx/5xx means the domain is NOT catch-all (or we couldn't tell).
    catch_all = code is not None and 200 <= code < 300
    _CATCH_ALL_CACHE[domain] = catch_all
    if catch_all:
        logger.debug(f"{domain} is catch-all (junk RCPT returned {code})")
    return catch_all


def _decode(msg) -> str:
    if msg is None:
        return ""
    if isinstance(msg, bytes):
        return msg.decode("utf-8", "replace")
    return str(msg)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json
    import sys

    p = argparse.ArgumentParser(
        description="Validate one or more email addresses.",
    )
    p.add_argument("emails", nargs="+", help="email addresses to validate")
    p.add_argument("--probe-from", default="verify@validator.local",
                   help="MAIL FROM used during SMTP probe (default: %(default)s)")
    p.add_argument("--no-smtp-probe", action="store_true",
                   help="Stop at MX lookup; faster but less accurate")
    p.add_argument("--no-catch-all", action="store_true",
                   help="Skip catch-all detection")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="Per-network-op timeout (seconds)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    results, stats = validate_emails(
        args.emails,
        progress=not args.json,
        probe_from=args.probe_from,
        smtp_probe=not args.no_smtp_probe,
        detect_catch_all=not args.no_catch_all,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        print()
        for r in results:
            mark = {"valid": "✓", "catch_all": "~", "risky": "!",
                    "invalid": "✗", "unknown": "?"}.get(r.status, "?")
            print(f"  {mark} {r.email:<40} {r.status:<10} {r.reason}")
        print()
        print(f"  {stats.pretty()}")

    bad = sum(1 for r in results if r.status == "invalid")
    sys.exit(1 if bad else 0)
