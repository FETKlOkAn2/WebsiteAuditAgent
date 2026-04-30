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
import os
import sys
import time
from datetime import datetime

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

import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

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
    require_email: bool = False,
    audit_mode: str = "v1",
    lang: str = "en",
    niche: str = "",
    location: str = "",
) -> dict:
    """
    Full audit pipeline for a single URL.

    Args:
        audit_mode: "v1" (legacy PageSpeed-driven prompt) or
                    "v2" (fact-grounded conversion-audit prompt).
        lang:       "en" or "sk" — only meaningful for v2.
        niche/location: forwarded to the conversion auditor for personalization.
        require_email:  if True, skip LLM when no contact email is found.
    """
    audit = analyze_website(url, skip_pagespeed=skip_pagespeed)

    if audit.get("error"):
        logger.error(f"Failed to analyze {url}: {audit['error']}")
        return audit

    # Check for contact email before spending tokens on LLM
    if require_email and not audit.get("contact_emails"):
        logger.info(f"No contact email found for {url} — skipping LLM analysis (saving tokens)")
        audit["analysis"] = None
        audit["email"] = None
        audit["skipped_reason"] = "no_contact_email"
        return audit

    site_name = name or ""
    if not site_name and audit.get("seo", {}).get("title"):
        site_name = audit["seo"]["title"].split("|")[0].split("-")[0].strip()
    if not site_name:
        from urllib.parse import urlparse
        site_name = urlparse(url).netloc.replace("www.", "")

    if audit_mode == "v2":
        # Fact-grounded path. Needs the raw HTML — re-fetch only if scraper
        # didn't keep it. analyze_website doesn't currently store html on the
        # result, so we fetch once via the scraper.
        from scraper import fetch_html
        fetch = fetch_html(url)
        html = fetch.get("html") or ""

        from analyzer_v2 import generate_email_v2
        v2 = generate_email_v2(
            html=html, url=url, site_name=site_name,
            niche=niche, location=location,
            sender_name=sender_name, lang=lang,
        )

        # Map v2 output onto the existing schema so the rest of the pipeline
        # (output, sender, registry) doesn't change.
        audit["analysis"] = {
            "issues": [],  # v2 doesn't produce v1-style issues; left empty
            "overall_impression": "v2: " + (
                v2.get("skipped_reason") or
                f"validation passed={v2['validation']['passed']}, "
                f"quoted={len(v2['validation']['quoted_facts'])}"
            ),
            "lead_score": 0,
            "facts": v2.get("facts"),
            "validation": v2.get("validation"),
            "audit_mode": "v2",
            "lang": lang,
        }

        if v2.get("skipped_reason"):
            # Mark this prospect as skipped. _prepare_send_list will drop it.
            audit["email"] = None
            audit["skipped_reason"] = v2["skipped_reason"]
        elif not v2["validation"]["passed"]:
            # We got an email but it isn't grounded — still return it but flag
            audit["email"] = {
                "subject_line": v2["subject_line"],
                "email_body": v2["email_body"],
                "follow_up_subject": v2["follow_up_subject"],
                "follow_up_body": v2["follow_up_body"],
            }
            audit["skipped_reason"] = "v2_validation_failed"
        else:
            audit["email"] = {
                "subject_line": v2["subject_line"],
                "email_body": v2["email_body"],
                "follow_up_subject": v2["follow_up_subject"],
                "follow_up_body": v2["follow_up_body"],
            }
        return audit

    # ---- legacy v1 path (unchanged) ----
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
    require_email: bool = False,
    audit_mode: str = "v1",
    lang: str = "en",
    niche: str = "",
    location: str = "",
) -> list[dict]:
    """Process a batch of URLs with rate limiting."""
    results = []
    total = len(urls)
    skipped = 0

    for i, entry in enumerate(urls, 1):
        url = entry["url"]
        name = entry.get("name", "")

        logger.info(f"[{i}/{total}] Processing: {url}")

        result = process_single(
            url=url, name=name, skip_pagespeed=skip_pagespeed,
            agency_name=agency_name, sender_name=sender_name,
            sender_title=sender_title, require_email=require_email,
            audit_mode=audit_mode, lang=lang, niche=niche, location=location,
        )
        results.append(result)

        if result.get("skipped_reason"):
            skipped += 1

        if i < total:
            time.sleep(config.SCRAPE_DELAY)

    if skipped:
        logger.info(f"Skipped LLM analysis for {skipped}/{total} sites")

    return results


# ---------------------------------------------------------------------------
# Sent emails registry — never email the same address or domain twice
# ---------------------------------------------------------------------------

SENT_REGISTRY_FILE = os.path.join(config.OUTPUT_DIR, "sent_registry.json")


def _load_sent_registry() -> dict:
    """Load the registry of already-contacted emails and domains."""
    if os.path.exists(SENT_REGISTRY_FILE):
        with open(SENT_REGISTRY_FILE, "r") as f:
            return json.load(f)
    return {"emails": {}, "domains": {}}


def _save_sent_registry(registry: dict):
    """Save the sent registry to disk."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(SENT_REGISTRY_FILE, "w") as f:
        json.dump(registry, f, indent=2)


def _record_sent(registry: dict, email: str, website: str, subject: str):
    """Record that we sent to this email/domain."""
    from urllib.parse import urlparse
    domain = urlparse(website).netloc.lower().replace("www.", "")
    now = datetime.now().isoformat()

    registry["emails"][email.lower()] = {
        "website": website,
        "subject": subject,
        "sent_at": now,
    }
    registry["domains"][domain] = {
        "email": email,
        "sent_at": now,
    }


def _already_contacted(registry: dict, email: str, website: str) -> str | None:
    """Check if we've already contacted this email or domain. Returns reason or None."""
    from urllib.parse import urlparse

    email_lower = email.lower()
    if email_lower in registry.get("emails", {}):
        prev = registry["emails"][email_lower]
        return f"already emailed {email} on {prev.get('sent_at', '?')[:10]}"

    domain = urlparse(website).netloc.lower().replace("www.", "")
    if domain in registry.get("domains", {}):
        prev = registry["domains"][domain]
        return f"already contacted {domain} via {prev.get('email', '?')} on {prev.get('sent_at', '?')[:10]}"

    return None


def _prepare_send_list(audit_results: list[dict], contacts: dict = None) -> list[dict]:
    """
    Match audit results with contact emails.
    Uses contacts CSV if provided, otherwise uses auto-extracted emails from scraping.
    Skips emails/domains we've already contacted.
    Returns list of {to, subject, body, website} ready to send.
    """
    from urllib.parse import urlparse
    contacts = contacts or {}
    send_list = []
    registry = _load_sent_registry()

    for r in audit_results:
        # Skip results that had no LLM analysis (no contact email)
        if r.get("skipped_reason"):
            continue
        email_data = r.get("email") or {}
        subject = email_data.get("subject_line", "").strip().strip('"').strip("'")
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
            # Check if already contacted
            reason = _already_contacted(registry, to_email, url)
            if reason:
                logger.info(f"Skipping {url} — {reason}")
                continue

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
        audit_mode=getattr(args, "audit_mode", "v1"),
        lang=getattr(args, "lang", "en"),
        niche=getattr(args, "niche", "") or "",
        location=getattr(args, "location", "") or "",
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

    # Record sent emails so we never contact them again
    if not dry_run:
        registry = _load_sent_registry()
        for sl, sr in zip(send_list, results):
            if sr.get("status") == "sent":
                _record_sent(registry, sl["to"], sl["website"], sl["subject"])
        _save_sent_registry(registry)

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
        sender_title=args.title, require_email=True,
        audit_mode=getattr(args, "audit_mode", "v1"),
        lang=getattr(args, "lang", "en"),
        niche=args.niche, location=args.location or "",
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
                # Record sent emails so we never contact them again
                if not dry_run:
                    registry = _load_sent_registry()
                    for sl, sr in zip(send_list, send_results):
                        if sr.get("status") == "sent":
                            _record_sent(registry, sl["to"], sl["website"], sl["subject"])
                    _save_sent_registry(registry)
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
# Campaign — run through agent_input.csv until daily limits are hit
# ---------------------------------------------------------------------------

CAMPAIGN_PROGRESS_FILE = os.path.join(config.OUTPUT_DIR, "campaign_progress.json")

# Free tier limits
DEFAULT_SERPER_DAILY_LIMIT = 80      # ~2500/month ÷ 31 days = ~80/day to spread evenly
DEFAULT_EMAIL_DAILY_LIMIT = 40       # Zoho free = 50/day, keep 10 buffer
DEFAULT_PAGESPEED_DAILY_LIMIT = 400  # 25k/day but no need to hog it


def _load_campaign_progress() -> dict:
    """Load campaign progress from disk."""
    if os.path.exists(CAMPAIGN_PROGRESS_FILE):
        with open(CAMPAIGN_PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"completed": [], "daily_logs": {}}


def _save_campaign_progress(progress: dict):
    """Save campaign progress to disk."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(CAMPAIGN_PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def _get_today_usage(progress: dict) -> dict:
    """Get today's usage counters."""
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in progress.get("daily_logs", {}):
        progress.setdefault("daily_logs", {})[today] = {
            "serper_queries": 0,
            "emails_sent": 0,
            "pagespeed_calls": 0,
            "combos_processed": 0,
        }
    return progress["daily_logs"][today]


def cmd_campaign(args):
    """
    Handle the 'campaign' subcommand.
    Reads niche/location pairs from agent_input.csv and runs the pipeline
    for each one, stopping when daily API limits are reached.
    Tracks progress so you can resume the next day.
    """
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env file.")
        sys.exit(1)

    # Load the input CSV
    input_file = args.input_csv
    if not os.path.exists(input_file):
        print(f"ERROR: Input file not found: {input_file}")
        sys.exit(1)

    combos = []
    with open(input_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            niche = row.get("niche", "").strip()
            location = row.get("location", "").strip()
            if niche:
                combos.append(f"{niche}|{location}")

    if not combos:
        print(f"ERROR: No niche/location pairs found in {input_file}")
        sys.exit(1)

    # Load progress
    progress = _load_campaign_progress()
    completed = set(progress.get("completed", []))
    today_usage = _get_today_usage(progress)

    # Always flush both files immediately so upload-artifact never fails on missing files
    _save_campaign_progress(progress)
    registry = _load_sent_registry()
    _save_sent_registry(registry)

    # Filter out already-completed combos (unless --reset)
    if args.reset:
        completed = set()
        progress["completed"] = []
        _save_campaign_progress(progress)
        print("  Progress reset — starting from scratch.\n")

    remaining = [c for c in combos if c not in completed]

    if not remaining:
        print(f"\n  All {len(combos)} niche/location combos have been processed!")
        print(f"  Use --reset to start over.\n")
        sys.exit(0)

    # Limits
    serper_limit = args.serper_limit
    email_limit = args.email_limit

    # Estimate: each pipeline run uses ~8 Serper queries (4 base + ~4 LLM-generated)
    QUERIES_PER_RUN = 8

    print(f"\n{'='*60}")
    print(f" CAMPAIGN MODE")
    print(f"{'='*60}")
    print(f"  Total combos: {len(combos)}")
    print(f"  Already done:  {len(completed)}")
    print(f"  Remaining:     {len(remaining)}")
    print(f"  Today's usage: {today_usage['serper_queries']} Serper queries, "
          f"{today_usage['emails_sent']} emails sent")
    print(f"  Daily limits:  {serper_limit} Serper queries, {email_limit} emails")
    print(f"  Send emails:   {'YES' if args.send else 'NO (dry run)'}")
    print(f"{'='*60}\n")

    processed_this_session = 0

    for combo in remaining:
        niche, location = combo.split("|", 1)

        # Check if we'd exceed Serper limit
        if today_usage["serper_queries"] + QUERIES_PER_RUN > serper_limit:
            print(f"\n  STOPPING — Serper daily limit approaching "
                  f"({today_usage['serper_queries']}/{serper_limit} queries used)")
            print(f"  Resume tomorrow: python audit_agent.py campaign\n")
            break

        # Check email limit
        if args.send and today_usage["emails_sent"] >= email_limit:
            print(f"\n  STOPPING — Email daily limit reached "
                  f"({today_usage['emails_sent']}/{email_limit} emails sent)")
            print(f"  Resume tomorrow: python audit_agent.py campaign --send\n")
            break

        processed_this_session += 1
        print(f"\n{'─'*60}")
        print(f" [{processed_this_session}] {niche} in {location}")
        print(f" (Serper: {today_usage['serper_queries']}/{serper_limit} | "
              f"Emails: {today_usage['emails_sent']}/{email_limit})")
        print(f"{'─'*60}\n")

        try:
            # Step 1: Prospect
            prospects = run_prospect(
                niche=niche,
                location=location,
                num_results=args.count,
                min_score=args.min_score,
            )

            # Count Serper queries used (estimate)
            today_usage["serper_queries"] += QUERIES_PER_RUN

            if not prospects:
                logger.info(f"No prospects found for {niche} in {location}")
                completed.add(combo)
                progress["completed"] = list(completed)
                today_usage["combos_processed"] += 1
                _save_campaign_progress(progress)
                continue

            save_prospects_csv(prospects)

            # Step 2: Audit top prospects
            top_n = min(args.audit_top, len(prospects))
            urls = [
                {"url": p["url"], "name": p.get("name", "")}
                for p in prospects[:top_n]
            ]

            results = run_batch(
                urls=urls,
                skip_pagespeed=args.skip_pagespeed,
                agency_name=args.agency,
                sender_name=args.sender,
                sender_title=args.title,
                require_email=True,
                audit_mode=getattr(args, "audit_mode", "v1"),
                lang=getattr(args, "lang", "en"),
                niche=niche,
                location=location,
            )

            today_usage["pagespeed_calls"] += top_n

            json_path = save_json(results)
            save_csv(results)

            # Step 3: Send emails (if enabled)
            if args.send:
                contacts = {}
                send_list = _prepare_send_list(results, contacts)

                if send_list:
                    # Trim to stay within daily email limit
                    remaining_emails = email_limit - today_usage["emails_sent"]
                    send_list = send_list[:remaining_emails]

                    if send_list:
                        dry_run = not args.confirm_send
                        send_results = send_batch(
                            emails=send_list,
                            from_name=args.sender_full,
                            dry_run=dry_run,
                        )

                        if not dry_run:
                            sent_count = sum(1 for r in send_results if r.get("status") == "sent")
                            today_usage["emails_sent"] += sent_count
                            # Record sent emails so we never contact them again
                            registry = _load_sent_registry()
                            for sl, sr in zip(send_list, send_results):
                                if sr.get("status") == "sent":
                                    _record_sent(registry, sl["to"], sl["website"], sl["subject"])
                            _save_sent_registry(registry)
                        else:
                            today_usage["emails_sent"] += len(send_list)

                        save_send_log(send_results)

            # Mark combo as done
            completed.add(combo)
            progress["completed"] = list(completed)
            today_usage["combos_processed"] += 1
            _save_campaign_progress(progress)

            logger.info(f"Completed: {niche} in {location} "
                       f"({len(prospects)} prospects, {top_n} audited)")

        except KeyboardInterrupt:
            print(f"\n\n  Campaign interrupted. Progress saved — resume anytime.\n")
            _save_campaign_progress(progress)
            sys.exit(0)
        except Exception as e:
            logger.error(f"Error processing {niche} in {location}: {e}")
            # Don't mark as completed so it retries next time
            _save_campaign_progress(progress)
            continue

    # Final summary
    print(f"\n{'='*60}")
    print(f" CAMPAIGN SESSION COMPLETE")
    print(f"{'='*60}")
    print(f"  Processed this session: {processed_this_session}")
    print(f"  Total completed:        {len(completed)}/{len(combos)}")
    print(f"  Serper queries today:   {today_usage['serper_queries']}")
    print(f"  Emails sent today:      {today_usage['emails_sent']}")
    remaining_count = len(combos) - len(completed)
    if remaining_count > 0:
        print(f"  Remaining:              {remaining_count}")
        print(f"\n  Resume tomorrow: python audit_agent.py campaign"
              f"{' --send --confirm-send' if args.send else ''}\n")
    else:
        print(f"\n  All combos processed! Use --reset to start over.\n")


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
    p_audit.add_argument("--audit-mode", choices=["v1", "v2"], default="v1",
                         help="v1=legacy PageSpeed prompt, v2=fact-grounded conversion audit")
    p_audit.add_argument("--lang", choices=["en", "sk"], default="en",
                         help="Email language (only used in v2 mode)")
    p_audit.add_argument("--niche", default="", help="Niche hint (used in v2 mode)")
    p_audit.add_argument("--location", default="", help="Location hint (used in v2 mode)")
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
    p_pipeline.add_argument("--audit-mode", choices=["v1", "v2"], default="v1",
                            help="v1=legacy, v2=fact-grounded (recommended)")
    p_pipeline.add_argument("--lang", choices=["en", "sk"], default="en",
                            help="Email language (only used in v2 mode)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    # --- campaign ---
    p_campaign = subparsers.add_parser(
        "campaign",
        help="Run through agent_input.csv until daily limits are hit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Reads niche/location pairs from agent_input.csv (or custom CSV).
Runs the full pipeline for each pair, tracking progress and stopping
when free-tier API limits are approached. Resume the next day.

Free tier limits (daily):
  Serper:     ~80/day (2500/month spread evenly)
  Zoho email: 50/day (we use 40 as buffer)
  PageSpeed:  25,000/day (not a concern)

Examples:
  # Dry run (no emails sent)
  python audit_agent.py campaign

  # Actually send emails
  python audit_agent.py campaign --send --confirm-send

  # Custom input file and limits
  python audit_agent.py campaign --input my_niches.csv --serper-limit 100

  # Reset progress and start over
  python audit_agent.py campaign --reset
        """,
    )
    p_campaign.add_argument("--input-csv", default="agent_input.csv", help="CSV with niche,location columns (default: agent_input.csv)")
    p_campaign.add_argument("--serper-limit", type=int, default=DEFAULT_SERPER_DAILY_LIMIT, help=f"Max Serper queries per day (default: {DEFAULT_SERPER_DAILY_LIMIT})")
    p_campaign.add_argument("--email-limit", type=int, default=DEFAULT_EMAIL_DAILY_LIMIT, help=f"Max emails per day (default: {DEFAULT_EMAIL_DAILY_LIMIT})")
    p_campaign.add_argument("--count", type=int, default=20, help="Max URLs to prospect per combo (default: 20)")
    p_campaign.add_argument("--min-score", type=int, default=25, help="Min prospect score (default: 25)")
    p_campaign.add_argument("--audit-top", type=int, default=5, help="Top N prospects to audit per combo (default: 5)")
    p_campaign.add_argument("--skip-pagespeed", action="store_true", help="Skip PageSpeed API")
    p_campaign.add_argument("--agency", default="EMTD Studio", help="Agency name")
    p_campaign.add_argument("--sender", default="Tomas", help="Sender first name")
    p_campaign.add_argument("--sender-full", default="Tomas Maxim", help="Sender full name (for emails)")
    p_campaign.add_argument("--title", default="Founder", help="Sender title")
    p_campaign.add_argument("--send", action="store_true", help="Enable email sending")
    p_campaign.add_argument("--confirm-send", action="store_true", help="Actually send (default is dry-run)")
    p_campaign.add_argument("--reset", action="store_true", help="Reset progress and start from scratch")
    p_campaign.add_argument("--audit-mode", choices=["v1", "v2"], default="v2",
                            help="v1=legacy PageSpeed prompt, v2=fact-grounded (default)")
    p_campaign.add_argument("--lang", choices=["en", "sk"], default="sk",
                            help="Email language (default: sk for the SK pipeline)")
    p_campaign.set_defaults(func=cmd_campaign)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        print("\nQuick start:")
        print('  python audit_agent.py pipeline --niche "plumber" --location "Austin TX"')
        print('  python audit_agent.py campaign                    # run through all niches')
        print('  python audit_agent.py audit --url https://example.com')
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
