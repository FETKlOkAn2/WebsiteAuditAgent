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
from datetime import datetime
from email.mime.text import MIMEText

import config

logger = logging.getLogger(__name__)

# Zoho SMTP settings
ZOHO_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.zoho.com")
ZOHO_SMTP_PORT = int(os.getenv("SMTP_PORT") or "465")
ZOHO_EMAIL = os.getenv("SMTP_EMAIL", "tomasmaxim@emtdstudio.com")
ZOHO_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SEND_DELAY = float(os.getenv("SEND_DELAY_SECONDS", "30"))


def _connect_smtp():
    """Establish SMTP connection to Zoho."""
    server = smtplib.SMTP_SSL(ZOHO_SMTP_HOST, ZOHO_SMTP_PORT, timeout=30)
    server.login(ZOHO_EMAIL, ZOHO_PASSWORD)
    return server


def send_single(
    to_email: str,
    subject: str,
    body: str,
    from_name: str = "Tomas Maxim",
    reply_to: str = "",
    dry_run: bool = True,
) -> dict:
    """Send a single email. Returns status dict."""
    result = {
        "to": to_email,
        "subject": subject,
        "status": None,
        "error": None,
        "timestamp": datetime.now().isoformat(),
    }

    if dry_run:
        result["status"] = "dry_run"
        logger.info(f"[DRY RUN] Would send to {to_email}: {subject}")
        return result

    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = f"{from_name} <{ZOHO_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    try:
        server = _connect_smtp()
        server.sendmail(ZOHO_EMAIL, to_email, msg.as_string())
        server.quit()
        result["status"] = "sent"
        logger.info(f"Sent to {to_email}: {subject}")
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

        result = {
            "to": to,
            "subject": subject,
            "status": None,
            "error": None,
            "timestamp": datetime.now().isoformat(),
        }

        if dry_run:
            result["status"] = "dry_run"
            logger.info(f"[DRY RUN] [{i}/{total}] → {to}: {subject}")
        else:
            try:
                msg = MIMEText(body, "plain", "utf-8")
                msg["From"] = f"{from_name} <{ZOHO_EMAIL}>"
                msg["To"] = to
                msg["Subject"] = subject

                server.sendmail(ZOHO_EMAIL, to, msg.as_string())
                result["status"] = "sent"
                logger.info(f"[{i}/{total}] Sent → {to}: {subject}")
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
        email_data = r.get("email", {})
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
