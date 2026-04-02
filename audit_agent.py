#!/usr/bin/env python3
"""
Website Audit Agent — Cold Outreach Pipeline
=============================================

Four modes:
  prospect  — Find business websites that need agency services
  audit     — Deep-analyze sites + generate cold emails
  send      — Send generated emails via Zoho Mail
  pipeline  — Find → Qualify → Audit → Email → Send (all-in-one)

Usage:
  python audit_agent.py prospect --niche "plumber" --location "Austin TX"
  python audit_agent.py audit sites.csv
  python audit_agent.py send output/audit_results_*.json --contacts contacts.csv
  python audit_agent.py pipeline --niche "dentist" --location "Miami FL" --send
"""

import argparse
import csv
import json
import logging
import sys
import time

import config
from scraper import analyze_website
from analyzer import analyze_audit_data, generate_email
from output import save_json, save_csv, print_summary
from prospector import (
    prospect as run_prospect,
    save_prospects_csv,
    print_prospect_summary,
)
from sender import (
    send_batch,
    load_emails_from_audit_json,
    save_send_log,
    print_send_summary,
    ZOHO_EMAIL,
    ZOHO_PASSWORD,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_urls_from_csv(filepath: str) -> list[dict]:
    """Load URLs from a CSV file. Expects a 'website_url' column (or first column)."""
    urls = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []

        url_col = None
        for candidate in ["website_url", "url", "website", "domain", "site"]:
            if candidate in fieldnames:
                url_col = candidate
                break

        if url_col is None and fieldnames:
            url_col = fieldnames[0]

        for row in reader:
            url = row.get(url_col, "").strip()
            if url:
                name = row.get("name", row.get("company", "")).strip()
                urls.append({"url": url, "name": name})

    return urls


def load_contacts_csv(filepath: str) -> dict:
    """
    Load a contacts CSV that maps website URLs to recipient emails.
    Returns dict: {website_domain: {email, name, ...}}
    """
    from urllib.parse import urlparse
    contacts = {}
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Find email column
            email = (
                row.get("email", "") or row.get("Email", "") or
                row.get("recipient_email", "") or row.get("contact_email", "")
            ).strip()
            # Find website column
            website = (
                row.get("website", "") or row.get("website_url", "") or
                row.get("url", "") or row.get("domain", "")
            ).strip()

            if email and website:
                if not website.startswith("http"):
                    website = "https://" + website
                domain = urlparse(website).netloc.lower().replace("www.", "")
                contacts[domain] = {
                    "email": email,
                    "name": row.get("name", row.get("contact_name", "")).strip(),
                    "website": website,
                }

    return contacts


def process_single(
    url: str,
    name: str = "",
    skip_pagespeed: bool = False,
    agency_name: str = "Our Agency",
    sender_name: str = "Tomas",
    sender_title: str = "Founder",
) -> dict:
    """Full audit pipeline for a single URL."""
    audit = analyze_website(url, skip_pagespeed=skip_pagespeed)

    if audit.get("error"):
        logger.error(f"Failed to analyze {url}: {audit['error']}")
        return audit

    site_name = name or ""
    if not site_name and audit.get("seo", {}).get("title"):
        site_name = audit["seo"]["title"].split("|")[0].split("-")[0].strip()
    if not site_name:
        from urllib.parse import urlparse
        site_name = urlparse(url).netloc.replace("www.", "")

    analysis = analyze_audit_data(audit)
    audit["analysis"] = analysis

    email_result = generate_email(
        url=url,
        site_name=site_name,
        findings=analysis,
        agency_name=agency_name,
        sender_name=sender_name,
        sender_title=sender_title,
    )
    audit["email"] = email_result

    return audit


def run_batch(
    urls: list[dict],
    skip_pagespeed: bool = False,
    agency_name: str = "Our Agency",
    sender_name: str = "Tomas",
    sender_title: str = "Founder",
) -> list[dict]:
    """Process a batch of URLs with rate limiting."""
    results = []
    total = len(urls)

    for i, entry in enumerate(urls, 1):
        url = entry["url"]
        name = entry.get("name", "")

        logger.info(f"[{i}/{total}] Processing: {url}")

        result = process_single(
            url=url, name=name, skip_pagespeed=skip_pagespeed,
            agency_name=agency_name, sender_name=sender_name,
            sender_title=sender_title,
        )
        results.append(result)

        if i < total:
            time.sleep(config.SCRAPE_DELAY)

    return results


def _prepare_send_list(audit_results: list[dict], contacts: dict = None) -> list[dict]:
    """
    Match audit results with contact emails.
    Uses contacts CSV if provided, otherwise uses auto-extracted emails from scraping.
    Returns list of {to, subject, body, website} ready to send.
    """
    from urllib.parse import urlparse
    contacts = contacts or {}
    send_list = []

    for r in audit_results:
        email_data = r.get("email", {})
        subject = email_data.get("subject_line", "")
        body = email_data.get("email_body", "").replace("\\n", "\n")
        url = r.get("url", "")

        if not subject or not body:
            continue

        # Try contacts CSV first
        domain = urlparse(url).netloc.lower().replace("www.", "")
        contact = contacts.get(domain, {})
        to_email = contact.get("email", "")

        # Fall back to auto-extracted emails from scraping
        if not to_email:
            scraped_emails = r.get("contact_emails", [])
            if scraped_emails:
                to_email = scraped_emails[0]  # best match (already sorted by priority)

        if to_email:
            send_list.append({
                "to": to_email,
                "subject": subject,
                "body": body,
                "website": url,
                "contact_name": contact.get("name", ""),
            })
        else:
            logger.warning(f"No contact email found for {url} — skipping send")

    return send_list


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_prospect(args):
    """Handle the 'prospect' subcommand."""
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env file.")
        sys.exit(1)

    prospects = run_prospect(
        niche=args.niche,
        location=args.location or "",
        num_results=args.count,
        min_score=args.min_score,
    )

    if not prospects:
        print("\nNo qualified prospects found. Try a different niche or location.")
        sys.exit(0)

    csv_path = save_prospects_csv(prospects)
    print_prospect_summary(prospects)
    print(f"  Prospects CSV saved to: {csv_path}")
    print(f"  Run audit on these: python audit_agent.py audit {csv_path}\n")


def cmd_audit(args):
    """Handle the 'audit' subcommand."""
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env file.")
        sys.exit(1)

    if not args.input_csv and not args.url:
        print("ERROR: Provide either a CSV file or --url")
        sys.exit(1)

    if args.url:
        urls = [{"url": args.url, "name": ""}]
    else:
        urls = load_urls_from_csv(args.input_csv)
        if not urls:
            print(f"ERROR: No URLs found in {args.input_csv}")
            sys.exit(1)

    logger.info(f"Starting audit of {len(urls)} website(s)")

    if not config.PAGESPEED_API_KEY and not args.skip_pagespeed:
        logger.warning(
            "No PAGESPEED_API_KEY set — PageSpeed analysis will be skipped. "
            "Get a free key at https://developers.google.com/speed/docs/insights/v5/get-started"
        )

    results = run_batch(
        urls=urls, skip_pagespeed=args.skip_pagespeed,
        agency_name=args.agency, sender_name=args.sender,
        sender_title=args.title,
    )

    json_path = save_json(results, args.output_json)
    csv_path = save_csv(results, args.output_csv)

    print_summary(results)
    print(f"  Results saved to:")
    print(f"    JSON: {json_path}")
    print(f"    CSV:  {csv_path}\n")


def cmd_send(args):
    """Handle the 'send' subcommand."""
    # Load audit results
    if args.audit_json.endswith(".json"):
        emails = load_emails_from_audit_json(args.audit_json)
    else:
        print("ERROR: --audit-json must be a .json file from the audit step")
        sys.exit(1)

    if not emails:
        print("ERROR: No emails found in audit results")
        sys.exit(1)

    # Load contacts (optional — can also use auto-extracted emails from JSON)
    contacts = {}
    if args.contacts:
        contacts = load_contacts_csv(args.contacts)

    # Load full audit data to get auto-extracted emails
    with open(args.audit_json, "r") as f:
        audit_results = json.load(f)

    send_list = _prepare_send_list(audit_results, contacts)

    if not send_list:
        print("ERROR: No emails could be matched to contacts")
        sys.exit(1)

    # Confirm
    dry_run = not args.confirm_send

    if dry_run:
        print(f"\n  DRY RUN — previewing {len(send_list)} emails")
        print(f"  Add --confirm-send to actually send\n")
    else:
        if not ZOHO_PASSWORD:
            print("ERROR: SMTP_PASSWORD not set in .env")
            sys.exit(1)

        print(f"\n  SENDING {len(send_list)} emails from {ZOHO_EMAIL}")
        print(f"  Delay between sends: {args.delay}s")
        resp = input(f"  Type 'yes' to confirm: ")
        if resp.strip().lower() != "yes":
            print("  Aborted.")
            sys.exit(0)

    # Send
    results = send_batch(
        emails=send_list,
        from_name=args.from_name,
        dry_run=dry_run,
        delay=args.delay,
    )

    log_path = save_send_log(results)
    print_send_summary(results)
    print(f"  Send log saved to: {log_path}\n")


def cmd_pipeline(args):
    """Handle the 'pipeline' subcommand — prospect → audit → optionally send."""
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env file.")
        sys.exit(1)

    # Step 1: Prospect
    print(f"\n{'='*60}")
    print(f" STEP 1: Finding prospects — {args.niche} in {args.location or 'any location'}")
    print(f"{'='*60}\n")

    prospects = run_prospect(
        niche=args.niche,
        location=args.location or "",
        num_results=args.count,
        min_score=args.min_score,
    )

    if not prospects:
        print("\nNo qualified prospects found. Try a different niche or location.")
        sys.exit(0)

    prospect_csv = save_prospects_csv(prospects)
    print_prospect_summary(prospects)

    # Step 2: Audit top prospects
    top_n = min(args.audit_top, len(prospects))
    print(f"\n{'='*60}")
    print(f" STEP 2: Deep audit of top {top_n} prospects")
    print(f"{'='*60}\n")

    urls = [
        {"url": p["url"], "name": p.get("name", "")}
        for p in prospects[:top_n]
    ]

    results = run_batch(
        urls=urls, skip_pagespeed=args.skip_pagespeed,
        agency_name=args.agency, sender_name=args.sender,
        sender_title=args.title,
    )

    json_path = save_json(results)
    csv_path = save_csv(results)

    print_summary(results)

    # Step 3: Send (if --send flag)
    if args.send:
        print(f"\n{'='*60}")
        print(f" STEP 3: Sending emails")
        print(f"{'='*60}\n")

        contacts = load_contacts_csv(args.contacts) if args.contacts else {}
        send_list = _prepare_send_list(results, contacts)

        if send_list:
            dry_run = not args.confirm_send
            if dry_run:
                print(f"  DRY RUN — {len(send_list)} emails ready to send:")
                for s in send_list:
                    print(f"    → {s['to']} ({s['website']})")
                print(f"\n  Add --confirm-send to actually send\n")
            else:
                if not ZOHO_PASSWORD:
                    print("  ERROR: SMTP_PASSWORD not set in .env")
                else:
                    print(f"  About to send {len(send_list)} emails from {ZOHO_EMAIL}:")
                    for s in send_list:
                        print(f"    → {s['to']} — {s['subject']}")
                    print()
                    resp = input(f"  Type 'yes' to confirm: ")
                    if resp.strip().lower() != "yes":
                        print("  Send skipped.")
                        send_list = []

            if send_list:
                send_results = send_batch(
                    emails=send_list, from_name=args.sender, dry_run=dry_run,
                )
                log_path = save_send_log(send_results)
                print_send_summary(send_results)
                print(f"  Send log: {log_path}")
        else:
            print("  No contact emails found on any audited sites — skipping send")
            print("  You can provide emails manually:")
            print(f"    python audit_agent.py send {json_path} --contacts contacts.csv\n")

    print(f"  Results saved to:")
    print(f"    Prospects: {prospect_csv}")
    print(f"    Audit JSON: {json_path}")
    print(f"    Audit CSV:  {csv_path}\n")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Website Audit Agent — Find leads, audit sites, send cold emails",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- prospect ---
    p_prospect = subparsers.add_parser(
        "prospect",
        help="Find business websites that need agency services",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python audit_agent.py prospect --niche "plumber" --location "Austin TX"
  python audit_agent.py prospect --niche "dentist" --location "Miami" --count 50
        """,
    )
    p_prospect.add_argument("--niche", required=True, help="Business niche")
    p_prospect.add_argument("--location", default="", help="City/region")
    p_prospect.add_argument("--count", type=int, default=30, help="Max URLs to check (default: 30)")
    p_prospect.add_argument("--min-score", type=int, default=25, help="Min qualification score (default: 25)")
    p_prospect.set_defaults(func=cmd_prospect)

    # --- audit ---
    p_audit = subparsers.add_parser(
        "audit",
        help="Deep-analyze websites and generate cold emails",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python audit_agent.py audit sites.csv
  python audit_agent.py audit --url https://example.com
  python audit_agent.py audit sites.csv --agency "WebPro" --sender "Sarah"
        """,
    )
    p_audit.add_argument("input_csv", nargs="?", help="CSV file with website URLs")
    p_audit.add_argument("--url", help="Audit a single URL")
    p_audit.add_argument("--skip-pagespeed", action="store_true", help="Skip PageSpeed API")
    p_audit.add_argument("--agency", default="EMTD Studio", help="Agency name")
    p_audit.add_argument("--sender", default="Tomas", help="Sender name")
    p_audit.add_argument("--title", default="Founder", help="Sender title")
    p_audit.add_argument("--output-json", help="Custom JSON output filename")
    p_audit.add_argument("--output-csv", help="Custom CSV output filename")
    p_audit.set_defaults(func=cmd_audit)

    # --- send ---
    p_send = subparsers.add_parser(
        "send",
        help="Send generated emails via Zoho Mail",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview emails (dry run, default)
  python audit_agent.py send output/audit_results.json --contacts contacts.csv

  # Actually send
  python audit_agent.py send output/audit_results.json --contacts contacts.csv --confirm-send

contacts.csv format:
  website,email,name
  https://example.com,owner@example.com,John Smith
        """,
    )
    p_send.add_argument("audit_json", help="Audit results JSON file")
    p_send.add_argument("--contacts", help="CSV mapping websites to recipient emails (optional — uses auto-extracted emails if not provided)")
    p_send.add_argument("--confirm-send", action="store_true", help="Actually send (default is dry-run)")
    p_send.add_argument("--from-name", default="Tomas Maxim", help="Sender display name")
    p_send.add_argument("--delay", type=float, default=30, help="Seconds between sends (default: 30)")
    p_send.set_defaults(func=cmd_send)

    # --- pipeline ---
    p_pipeline = subparsers.add_parser(
        "pipeline",
        help="Full pipeline: prospect → audit → send",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python audit_agent.py pipeline --niche "plumber" --location "Austin TX"
  python audit_agent.py pipeline --niche "dentist" --location "Miami" --send --contacts contacts.csv
  python audit_agent.py pipeline --niche "restaurant" --audit-top 5 --send --contacts c.csv --confirm-send
        """,
    )
    p_pipeline.add_argument("--niche", required=True, help="Business niche")
    p_pipeline.add_argument("--location", default="", help="City/region")
    p_pipeline.add_argument("--count", type=int, default=30, help="Max URLs to prospect (default: 30)")
    p_pipeline.add_argument("--min-score", type=int, default=25, help="Min prospect score (default: 25)")
    p_pipeline.add_argument("--audit-top", type=int, default=10, help="Top N prospects to audit (default: 10)")
    p_pipeline.add_argument("--skip-pagespeed", action="store_true", help="Skip PageSpeed API")
    p_pipeline.add_argument("--agency", default="EMTD Studio", help="Agency name")
    p_pipeline.add_argument("--sender", default="Tomas", help="Sender name")
    p_pipeline.add_argument("--title", default="Founder", help="Sender title")
    p_pipeline.add_argument("--send", action="store_true", help="Enable send step")
    p_pipeline.add_argument("--contacts", help="Contacts CSV (required with --send)")
    p_pipeline.add_argument("--confirm-send", action="store_true", help="Actually send (default is dry-run)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print("\nQuick start:")
        print('  python audit_agent.py pipeline --niche "plumber" --location "Austin TX"')
        print('  python audit_agent.py audit --url https://example.com')
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
