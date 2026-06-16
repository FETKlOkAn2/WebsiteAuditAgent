"""
Email sender — sends generated cold emails via Zoho Mail SMTP.

Safety features:
  - Dry-run mode (default) — prints emails without sending
  - Confirmation prompt before sending
  - Configurable delay between emails (avoid spam flags)
  - Sends as plain text (better deliverability for cold email)
  - Logs every send with timestamp
"""

import csv
import json
import logging
import os
import smtplib
import time
import uuid
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from waa import config

logger = logging.getLogger(__name__)

# Zoho SMTP settings
ZOHO_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
ZOHO_SMTP_PORT = int(os.getenv("SMTP_PORT") or "465")
ZOHO_EMAIL = os.getenv("SMTP_EMAIL", "tomasmaxim@emtdstudio.com")
ZOHO_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SEND_DELAY = float(os.getenv("SEND_DELAY_SECONDS", "30"))
# Optional: set REPLY_TO_EMAIL to redirect replies to a different inbox
# (e.g. tomas.maxim33@gmail.com) when the From address is a campaign domain.
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL", "")


def _connect_smtp():
    """Establish SMTP connection to Zoho."""
    server = smtplib.SMTP_SSL(ZOHO_SMTP_HOST, ZOHO_SMTP_PORT, timeout=30)
    server.login(ZOHO_EMAIL, ZOHO_PASSWORD)
    return server


def _build_message(
    *,
    to_email: str,
    subject: str,
    body: str,
    from_name: str,
    reply_to: str = "",
    in_reply_to: str = "",
    references: str = "",
) -> tuple[MIMEText, str]:
    """
    Build an MIME message with all the headers a thread-aware client
    expects. Returns (msg, message_id) — the Message-ID is generated here
    so callers can persist it for later follow-up threading.
    """
    sender_domain = ZOHO_EMAIL.split("@", 1)[-1] or "localhost"
    message_id = make_msgid(domain=sender_domain)

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{from_name} <{ZOHO_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id

    # Reply-To: if explicitly given, use it; otherwise fall back to global
    # REPLY_TO_EMAIL env var. If both empty, replies go to the From inbox.
    chosen_reply_to = reply_to or REPLY_TO_EMAIL
    if chosen_reply_to:
        msg["Reply-To"] = chosen_reply_to

    # Threading headers — for follow-ups, populate In-Reply-To and
    # References so Gmail/Outlook show the email under the original
    # conversation rather than as a new thread.
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    elif in_reply_to:
        msg["References"] = in_reply_to

    # Minimal hygiene headers that make us look less bot-y to spam filters
    msg["MIME-Version"] = "1.0"
    msg["X-Mailer"] = "WebsiteAuditAgent/2.0"

    return msg, message_id


def send_single(
    to_email: str,
    subject: str,
    body: str,
    from_name: str = "Tomas Maxim",
    reply_to: str = "",
    in_reply_to: str = "",
    references: str = "",
    dry_run: bool = True,
) -> dict:
    """Send a single email. Returns status dict.

    For follow-ups, pass `in_reply_to` (the original Message-ID we stored)
    so the new mail threads under the same conversation in the recipient's
    inbox.
    """
    result = {
        "to": to_email,
        "subject": subject,
        "status": None,
        "error": None,
        "message_id": None,
        "timestamp": datetime.now().isoformat(),
    }

    msg, message_id = _build_message(
        to_email=to_email, subject=subject, body=body,
        from_name=from_name, reply_to=reply_to,
        in_reply_to=in_reply_to, references=references,
    )
    result["message_id"] = message_id

    if dry_run:
        result["status"] = "dry_run"
        logger.info(f"[DRY RUN] Would send to {to_email}: {subject}")
        return result

    try:
        server = _connect_smtp()
        server.sendmail(ZOHO_EMAIL, to_email, msg.as_string())
        server.quit()
        result["status"] = "sent"
        logger.info(f"Sent to {to_email}: {subject} (msg-id {message_id})")
    except smtplib.SMTPAuthenticationError as e:
        result["status"] = "auth_error"
        result["error"] = str(e)
        logger.error(f"SMTP auth failed — check SMTP_PASSWORD in .env: {e}")
    except smtplib.SMTPException as e:
        result["status"] = "error"
        result["error"] = str(e)
        logger.error(f"SMTP error sending to {to_email}: {e}")

    return result


def send_batch(
    emails: list[dict],
    from_name: str = "Tomas Maxim",
    dry_run: bool = True,
    delay: float = None,
) -> list[dict]:
    """
    Send a batch of emails with delay between sends.

    Each email dict should have:
      - to: recipient email address
      - subject: email subject
      - body: email body text
    """
    if delay is None:
        delay = SEND_DELAY

    results = []
    total = len(emails)

    # For real sends, use a single SMTP connection
    server = None
    if not dry_run:
        if not ZOHO_PASSWORD:
            logger.error("SMTP_PASSWORD not set in .env — cannot send emails")
            return [{"status": "error", "error": "No SMTP password configured"}]

        try:
            server = _connect_smtp()
        except Exception as e:
            logger.error(f"Could not connect to SMTP: {e}")
            return [{"status": "error", "error": str(e)}]

    for i, email in enumerate(emails, 1):
        to = email.get("to", "")
        subject = email.get("subject", "")
        body = email.get("body", "")

        if not to or not subject or not body:
            logger.warning(f"Skipping incomplete email entry: {email}")
            continue

        msg, message_id = _build_message(
            to_email=to, subject=subject, body=body,
            from_name=from_name,
            reply_to=email.get("reply_to", ""),
            in_reply_to=email.get("in_reply_to", ""),
            references=email.get("references", ""),
        )

        result = {
            "to": to,
            "subject": subject,
            "status": None,
            "error": None,
            "message_id": message_id,
            "website": email.get("website", ""),
            "timestamp": datetime.now().isoformat(),
        }

        if dry_run:
            result["status"] = "dry_run"
            logger.info(f"[DRY RUN] [{i}/{total}] → {to}: {subject}")
        else:
            try:
                server.sendmail(ZOHO_EMAIL, to, msg.as_string())
                result["status"] = "sent"
                logger.info(f"[{i}/{total}] Sent → {to}: {subject} (msg-id {message_id})")
            except smtplib.SMTPException as e:
                result["status"] = "error"
                result["error"] = str(e)
                logger.error(f"[{i}/{total}] Failed → {to}: {e}")

        results.append(result)

        # Delay between sends (avoid spam triggers)
        if i < total:
            if not dry_run:
                logger.info(f"Waiting {delay}s before next send...")
            time.sleep(delay if not dry_run else 0.1)

    if server:
        try:
            server.quit()
        except Exception:
            pass

    return results


def load_emails_from_audit_csv(filepath: str) -> list[dict]:
    """
    Load emails from an audit results CSV.
    NOTE: This only loads the generated email content — you still need
    recipient emails. Use --recipient-col or pair with a contacts CSV.
    """
    emails = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            subject = row.get("email_subject", "").strip()
            body = row.get("email_body", "").strip()
            website = row.get("website", "").strip()

            if subject and body:
                emails.append({
                    "website": website,
                    "subject": subject,
                    "body": body,
                    "to": row.get("recipient_email", "").strip(),
                })

    return emails


def load_emails_from_audit_json(filepath: str) -> list[dict]:
    """Load emails from an audit results JSON file."""
    with open(filepath, "r", encoding="utf-8") as f:
        results = json.load(f)

    emails = []
    for r in results:
        # Use `or {}` so explicit None entries (from skipped/errored audits)
        # don't crash on .get(). r.get("email", {}) returns None when the key
        # is present with value None — silent foot-gun in cold-outreach data.
        email_data = r.get("email") or {}
        subject = email_data.get("subject_line", "")
        body = email_data.get("email_body", "")

        if subject and body:
            emails.append({
                "website": r.get("url", ""),
                "subject": subject,
                "body": body.replace("\\n", "\n"),
                "to": "",  # needs to be filled in
            })

    return emails


def save_send_log(results: list[dict], filename: str = None) -> str:
    """Save send log as JSON."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"send_log_{ts}.json"

    path = os.path.join(config.OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    logger.info(f"Send log saved: {path}")
    return path


def print_send_summary(results: list[dict]):
    """Print summary of send results."""
    sent = sum(1 for r in results if r.get("status") == "sent")
    dry = sum(1 for r in results if r.get("status") == "dry_run")
    errors = sum(1 for r in results if r.get("status") == "error")

    print(f"\n{'='*60}")
    if dry > 0:
        print(f" DRY RUN COMPLETE — {dry} emails previewed")
    else:
        print(f" SEND COMPLETE — {sent} sent, {errors} failed")
    print(f"{'='*60}\n")

    for r in results:
        status = r.get("status", "?")
        icon = {"sent": "✓", "dry_run": "~", "error": "✗"}.get(status, "?")
        print(f"  {icon} {r.get('to', 'no-email')} — {r.get('subject', '')}")
        if r.get("error"):
            print(f"    Error: {r['error']}")

    print()
