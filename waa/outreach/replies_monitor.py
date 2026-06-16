"""
replies_monitor.py — IMAP polling + Discord webhook for campaign replies.

Why
---
We have three campaign senders (Tomas, Erik, Michal) on three Zoho mailboxes.
A reply lands in only ONE mailbox; the other two never see it. By the time
someone notices, the prospect has cooled. We need a single shared view.

What this does
--------------
Every run:
  1. Connects via IMAP to each configured mailbox (Tomas + Erik + Michal).
  2. Fetches recent INBOX messages (default last 3 days, UNSEEN preferred).
  3. Filters out noise: auto-replies, out-of-office, mailer-daemon, our own
     campaign sends, and anything from a sender we never emailed.
  4. Cross-references every "from" address against `sent_registry.json` so
     only genuine replies to our outreach get posted.
  5. Posts each new reply as a rich embed to a Discord webhook.
  6. Tracks Message-IDs in `replies_seen.json` so the same reply never
     gets posted twice across runs.

Running
-------
Standalone:
    python replies_monitor.py

Or via the campaign CLI:
    python audit_agent.py monitor-replies

In CI: GitHub Actions cron every 15 min. See .github/workflows/replies_monitor.yml.

Required env vars
-----------------
    DISCORD_WEBHOOK_URL   — channel webhook
    IMAP_HOST             — default imap.zoho.eu
    IMAP_PORT             — default 993
    SMTP_EMAIL / SMTP_PASSWORD             — Tomas
    SMTP_EMAIL_2 / SMTP_PASSWORD_2         — Erik (optional)
    SMTP_EMAIL_3 / SMTP_PASSWORD_3         — Michal (optional)
"""

from __future__ import annotations

import email as _email
import imaplib
import json
import logging
import os
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import requests

from waa import config
from waa.core.storage import domain_of, JsonStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults & paths
# ---------------------------------------------------------------------------

REPLIES_SEEN_FILE = os.path.join(config.OUTPUT_DIR, "replies_seen.json")
SENT_REGISTRY_FILE = os.path.join(config.OUTPUT_DIR, "sent_registry.json")

DEFAULT_IMAP_HOST = os.getenv("IMAP_HOST", "imap.zoho.eu")
DEFAULT_IMAP_PORT = int(os.getenv("IMAP_PORT") or "993")
DEFAULT_LOOKBACK_DAYS = int(os.getenv("REPLIES_LOOKBACK_DAYS", "3"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

AUTO_REPLY_PATTERNS = [
    re.compile(r"\bout of office\b", re.I),
    re.compile(r"\bautomatic(?:\s+|-)reply\b", re.I),
    re.compile(r"\bauto[-\s]?reply\b", re.I),
    re.compile(r"\bvacation(?:\s+reply|\s+responder)\b", re.I),
    re.compile(r"\bmimo kancel[áa]ri[eu]?\b", re.I),       # SK
    re.compile(r"\bna dovolen[ek]\w*\b", re.I),             # SK
    re.compile(r"\bautomatick[áeú]\s+odpoved\w*\b", re.I),  # SK
    re.compile(r"\bdelivery\s+status\s+notification\b", re.I),
    re.compile(r"\bundeliverable\b", re.I),
]

NOISE_FROM_PATTERNS = [
    re.compile(r"^mailer-daemon@", re.I),
    re.compile(r"^postmaster@", re.I),
    re.compile(r"^noreply@", re.I),
    re.compile(r"^no-reply@", re.I),
    re.compile(r"^donotreply@", re.I),
]

DISCORD_EMBED_BODY_LIMIT = 1500
DISCORD_EMBED_TITLE_LIMIT = 240


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Reply:
    sender_mailbox: str           # which of our mailboxes received it
    from_email: str
    from_name: str
    subject: str
    date: Optional[datetime]
    body_preview: str
    message_id: str
    in_reply_to: Optional[str] = None
    refs: list[str] = field(default_factory=list)
    matched_via: str = ""         # email | domain | header
    original_subject: str = ""    # what we'd sent that they're replying to

    def to_dict(self) -> dict:
        d = asdict(self)
        d["date"] = self.date.isoformat() if self.date else None
        return d

    def cache_key(self) -> str:
        # Use Message-ID if present; fall back to a composite that's stable
        # across runs (no timestamp drift).
        if self.message_id:
            return self.message_id.strip()
        return f"{self.sender_mailbox}|{self.from_email}|{self.subject}"


@dataclass
class MailboxConfig:
    label: str       # "Tomas", "Erik", "Michal"
    email: str
    password: str

    def is_configured(self) -> bool:
        return bool(self.email and self.password)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

# Both stores tolerate corruption: they're re-derivable from the mailbox,
# so a bad file should reset rather than block the monitor. (This is the
# opposite policy from audit_agent's stores — see storage.JsonStore docstring.)
# Built per-call so tests can reassign the module-level path variables.

def _seen_store() -> JsonStore:
    return JsonStore(REPLIES_SEEN_FILE, lambda: {"keys": []}, tolerate_corrupt=True)


def _registry_store() -> JsonStore:
    return JsonStore(
        SENT_REGISTRY_FILE, lambda: {"emails": {}, "domains": {}},
        tolerate_corrupt=True,
    )


def load_seen() -> dict:
    data = _seen_store().load()
    if not isinstance(data, dict):
        return {"keys": []}
    data.setdefault("keys", [])
    return data


def save_seen(seen: dict):
    # Cap the list so it doesn't grow forever. Keep most recent 5000.
    seen["keys"] = seen.get("keys", [])[-5000:]
    _seen_store().save(seen)


def load_sent_registry() -> dict:
    return _registry_store().load()


# ---------------------------------------------------------------------------
# IMAP fetch
# ---------------------------------------------------------------------------

def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: _email.message.Message) -> str:
    """Return the best text/plain body; fall back to text/html stripped."""
    if msg.is_multipart():
        # Prefer text/plain
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               "attachment" not in (part.get("Content-Disposition") or ""):
                try:
                    raw = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace")
                except Exception:
                    continue
        # Fallback: text/html, strip tags crudely
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    raw = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    html = raw.decode(charset, errors="replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    continue
        return ""
    # Single-part
    try:
        raw = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")
    except Exception:
        return ""


def _quote_to_preview(body: str, limit: int = DISCORD_EMBED_BODY_LIMIT) -> str:
    """
    Strip quoted reply chains and signatures so the preview shows what's
    actually new in this message.
    """
    if not body:
        return ""
    lines = body.replace("\r\n", "\n").split("\n")
    new = []
    for line in lines:
        s = line.strip()
        # Strip lines like "On Thu, ... wrote:"
        if re.match(r"^on\s.+\bwrote:\s*$", s, re.I):
            break
        # Strip lines starting with > (quoted)
        if s.startswith(">"):
            continue
        # Stop on a signature delimiter
        if s in ("--", "-- "):
            break
        new.append(line)
    cleaned = "\n".join(new).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rsplit(" ", 1)[0] + "…"
    return cleaned


def fetch_recent(
    mb: MailboxConfig,
    *,
    host: str = DEFAULT_IMAP_HOST,
    port: int = DEFAULT_IMAP_PORT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    only_unseen: bool = False,
    timeout: float = 30,
) -> list[Reply]:
    """Connect to a mailbox and fetch recent messages parsed as Reply objects."""
    if not mb.is_configured():
        logger.info(f"Skipping {mb.label}: no credentials")
        return []

    logger.info(f"[{mb.label}] connecting to {host}:{port}")
    try:
        imap = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    except Exception as e:
        logger.error(f"[{mb.label}] connect failed: {e}")
        return []

    try:
        imap.login(mb.email, mb.password)
    except imaplib.IMAP4.error as e:
        logger.error(f"[{mb.label}] login failed: {e}")
        try:
            imap.logout()
        except Exception:
            pass
        return []

    replies: list[Reply] = []
    try:
        imap.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        criteria = f'(SINCE "{since}")'
        if only_unseen:
            criteria = f'(UNSEEN SINCE "{since}")'
        status, data = imap.search(None, criteria)
        if status != "OK" or not data or not data[0]:
            logger.info(f"[{mb.label}] no recent messages ({criteria})")
            return []
        ids = data[0].split()
        # Newest first, cap to a sane number to avoid mega-fetches
        ids = list(reversed(ids))[:200]
        logger.info(f"[{mb.label}] examining {len(ids)} recent message(s)")

        for mid in ids:
            try:
                # PEEK so we don't mark as read in the user's inbox
                status, msg_data = imap.fetch(mid, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                msg = _email.message_from_bytes(raw)
                reply = _parse_message(msg, mb.label)
                if reply is not None:
                    replies.append(reply)
            except Exception as e:
                logger.debug(f"[{mb.label}] failed to parse a message: {e}")
                continue
    finally:
        try:
            imap.close()
        except Exception:
            pass
        try:
            imap.logout()
        except Exception:
            pass

    return replies


def _parse_message(msg: _email.message.Message, mailbox_label: str) -> Optional[Reply]:
    from_raw = _decode_header(msg.get("From"))
    from_name, from_email = parseaddr(from_raw)
    if not from_email:
        return None
    subject = _decode_header(msg.get("Subject"))
    msg_id = (msg.get("Message-ID") or "").strip()
    in_reply_to = (msg.get("In-Reply-To") or "").strip() or None
    refs_raw = (msg.get("References") or "").strip()
    refs = re.findall(r"<[^>]+>", refs_raw) if refs_raw else []
    body = _extract_body(msg)

    date = None
    try:
        date_hdr = msg.get("Date")
        if date_hdr:
            date = parsedate_to_datetime(date_hdr)
    except Exception:
        date = None

    return Reply(
        sender_mailbox=mailbox_label,
        from_email=from_email.lower().strip(),
        from_name=(from_name or "").strip(),
        subject=subject.strip(),
        date=date,
        body_preview=_quote_to_preview(body),
        message_id=msg_id,
        in_reply_to=in_reply_to,
        refs=refs,
    )


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def is_noise(r: Reply) -> tuple[bool, str]:
    """True if the message is auto-reply / bounce / system mail."""
    fe = (r.from_email or "").lower()
    for pat in NOISE_FROM_PATTERNS:
        if pat.match(fe):
            return True, f"system sender ({fe})"
    subject = r.subject or ""
    for pat in AUTO_REPLY_PATTERNS:
        if pat.search(subject):
            return True, f"auto-reply subject"
    # Body-level heuristic
    body = r.body_preview or ""
    for pat in AUTO_REPLY_PATTERNS[:5]:  # narrower set to avoid false positives
        if pat.search(body):
            return True, "auto-reply body"
    return False, ""


def is_genuine_reply(r: Reply, sent_registry: dict) -> tuple[bool, str]:
    """
    Did this come from someone we previously emailed?

    Returns (is_reply, matched_via). matched_via ∈ {email, domain, header, ""}.
    """
    emails = sent_registry.get("emails", {}) or {}
    domains = sent_registry.get("domains", {}) or {}

    fe = (r.from_email or "").lower()
    if fe in emails:
        return True, "email"

    # Domain match — same company replying from a different mailbox
    if "@" in fe:
        domain = domain_of(fe.split("@", 1)[1])
        if domain in domains:
            return True, "domain"

    # Header match — In-Reply-To or References pointing at one of our sent IDs
    sent_msg_ids = {
        v.get("message_id") for v in emails.values()
        if isinstance(v, dict) and v.get("message_id")
    }
    if sent_msg_ids:
        if r.in_reply_to and r.in_reply_to in sent_msg_ids:
            return True, "header"
        for ref in r.refs:
            if ref in sent_msg_ids:
                return True, "header"

    return False, ""


def annotate_with_original(r: Reply, sent_registry: dict):
    """Best-effort: which subject did we send that they're replying to?"""
    emails = sent_registry.get("emails", {}) or {}
    fe = (r.from_email or "").lower()
    info = emails.get(fe)
    if isinstance(info, dict):
        r.original_subject = info.get("subject", "")
        return
    # Try domain
    if "@" in fe:
        domain = domain_of(fe.split("@", 1)[1])
        domains = sent_registry.get("domains", {}) or {}
        info = domains.get(domain)
        if isinstance(info, dict):
            r.original_subject = info.get("subject", "")


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def post_to_discord(reply: Reply, *, webhook_url: str = DISCORD_WEBHOOK_URL) -> bool:
    if not webhook_url:
        logger.warning("No DISCORD_WEBHOOK_URL set — printing instead")
        print("=" * 60)
        print(f"REPLY → {reply.sender_mailbox}")
        print(f"From: {reply.from_name} <{reply.from_email}>")
        print(f"Subject: {reply.subject}")
        if reply.original_subject:
            print(f"In reply to: {reply.original_subject}")
        print()
        print(reply.body_preview[:500])
        print("=" * 60)
        return False

    title = (reply.subject or "(no subject)")[:DISCORD_EMBED_TITLE_LIMIT]
    body = reply.body_preview or "(empty body)"

    fields = [
        {"name": "From", "value": f"{reply.from_name or '—'} `<{reply.from_email}>`", "inline": True},
        {"name": "To (our mailbox)", "value": reply.sender_mailbox, "inline": True},
        {"name": "Matched via", "value": reply.matched_via or "—", "inline": True},
    ]
    if reply.original_subject:
        fields.append({
            "name": "Original outreach",
            "value": f"`{reply.original_subject[:200]}`",
            "inline": False,
        })

    embed = {
        "title": "📬  " + title,
        "description": body,
        "color": 0x38BDF8,  # match the marketing pipeline diagram cyan
        "fields": fields,
        "timestamp": (reply.date or datetime.now(timezone.utc)).isoformat(),
        "footer": {"text": f"Website Audit Agent · reply detection"},
    }

    payload = {"embeds": [embed]}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        if 200 <= resp.status_code < 300:
            return True
        logger.error(f"Discord webhook {resp.status_code}: {resp.text[:200]}")
        return False
    except requests.RequestException as e:
        logger.error(f"Discord webhook request failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def configured_mailboxes() -> list[MailboxConfig]:
    """Pull all 1–3 senders from env. Skip any that don't have credentials."""
    return [
        MailboxConfig(
            label="Tomas",
            email=os.getenv("SMTP_EMAIL", ""),
            password=os.getenv("SMTP_PASSWORD", ""),
        ),
        MailboxConfig(
            label="Erik",
            email=os.getenv("SMTP_EMAIL_2", ""),
            password=os.getenv("SMTP_PASSWORD_2", ""),
        ),
        MailboxConfig(
            label="Michal",
            email=os.getenv("SMTP_EMAIL_3", ""),
            password=os.getenv("SMTP_PASSWORD_3", ""),
        ),
    ]


def _mark_reply_in_registry(sent_registry: dict, from_email: str, when: str):
    """Annotate the sent_registry entry so the follow-up scheduler skips
    prospects who have already replied. Saves only if we actually changed
    something."""
    emails = sent_registry.get("emails", {}) or {}
    fe = (from_email or "").lower()
    if fe in emails and isinstance(emails[fe], dict):
        if not emails[fe].get("reply_received_at"):
            emails[fe]["reply_received_at"] = when
            return True
    # Domain-level fallback — we may have emailed `info@x.com` and the
    # CEO replied from `ceo@x.com`. Mark the domain's primary contact.
    if "@" in fe:
        domain = domain_of(fe.split("@", 1)[1])
        domains = sent_registry.get("domains", {}) or {}
        contact = (domains.get(domain) or {}).get("email")
        if contact and contact in emails and isinstance(emails[contact], dict):
            if not emails[contact].get("reply_received_at"):
                emails[contact]["reply_received_at"] = when
                return True
    return False


def run_once(
    *,
    webhook_url: str = "",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
) -> dict:
    """
    One full polling cycle. Returns a small stats dict for logging / tests.
    """
    webhook_url = webhook_url or DISCORD_WEBHOOK_URL
    sent_registry = load_sent_registry()
    seen = load_seen()
    seen_keys: set[str] = set(seen.get("keys", []))
    registry_dirty = False

    posted = 0
    examined = 0
    skipped_seen = 0
    skipped_noise = 0
    skipped_not_a_reply = 0
    posted_keys: list[str] = []

    for mb in configured_mailboxes():
        if not mb.is_configured():
            continue
        replies = fetch_recent(mb, lookback_days=lookback_days)
        examined += len(replies)

        for r in replies:
            key = r.cache_key()
            if key in seen_keys:
                skipped_seen += 1
                continue

            noise, reason = is_noise(r)
            if noise:
                logger.info(f"  filtered noise [{mb.label}] {r.from_email}: {reason}")
                seen_keys.add(key)  # so we don't re-evaluate every cycle
                skipped_noise += 1
                continue

            ok, matched_via = is_genuine_reply(r, sent_registry)
            if not ok:
                logger.debug(f"  not a reply [{mb.label}] {r.from_email}")
                # Don't add to seen: tomorrow they might match (registry grows)
                skipped_not_a_reply += 1
                continue

            r.matched_via = matched_via
            annotate_with_original(r, sent_registry)

            if dry_run:
                logger.info(
                    f"  [DRY] would post [{mb.label}] {r.from_email}: {r.subject[:80]}"
                )
                posted += 1
                continue

            if post_to_discord(r, webhook_url=webhook_url):
                logger.info(
                    f"  posted [{mb.label}] {r.from_email}: {r.subject[:80]}"
                )
                seen_keys.add(key)
                posted_keys.append(key)
                posted += 1
                # Mark in the sent_registry so the follow-up scheduler skips
                # this prospect. Persisted in the next save_sent_registry call.
                now_iso = datetime.now(timezone.utc).isoformat()
                if _mark_reply_in_registry(sent_registry, r.from_email, now_iso):
                    registry_dirty = True
            else:
                logger.warning(f"  failed to post — will retry next run")

    if not dry_run:
        seen["keys"] = sorted(seen_keys)
        save_seen(seen)
        if registry_dirty:
            # Persist updated reply_received_at flags into sent_registry
            # so cmd_send_followups can skip already-replied prospects.
            _registry_store().save(sent_registry)
            logger.info("Marked reply timestamps in sent_registry.json")

    summary = {
        "examined": examined,
        "posted": posted,
        "skipped_seen": skipped_seen,
        "skipped_noise": skipped_noise,
        "skipped_not_a_reply": skipped_not_a_reply,
    }
    logger.info(f"Replies monitor done: {summary}")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Poll IMAP for replies and post to Discord.")
    p.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                   help="Look back this many days (default: %(default)s)")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would be posted; don't hit Discord")
    p.add_argument("--webhook-url", default="",
                   help="Override DISCORD_WEBHOOK_URL")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    run_once(
        webhook_url=args.webhook_url,
        lookback_days=args.lookback_days,
        dry_run=args.dry_run,
    )
