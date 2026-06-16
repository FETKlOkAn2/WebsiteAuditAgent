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
from datetime import datetime, timedelta

from waa import config
from waa.core.storage import domain_of, JsonStore
from waa.discovery.scraper import analyze_website
from waa.analysis.analyzer import analyze_audit_data, generate_email
from waa.core.output import save_json, save_csv, print_summary
from waa.discovery.prospector import (
    prospect as run_prospect,
    save_prospects_csv,
    print_prospect_summary,
)
from waa.outreach.sender import (
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
                domain = domain_of(website)
                contacts[domain] = {
                    "email": email,
                    "name": row.get("name", row.get("contact_name", "")).strip(),
                    "website": website,
                }

    return contacts


# Maps a failed quality-gate check (waa.analysis.quality_gate) onto the
# pipeline's existing `skipped_reason` strings so downstream code, logs and
# tests keep their stable vocabulary.
_QUALITY_SKIP_REASON = {
    "content": "empty_email",
    "grounding": "v2_validation_failed",
    "human_tone": "critic_failed",
}


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
    qualify: bool = True,
    critique: bool = True,
) -> dict:
    """
    Full audit pipeline for a single URL.

    Args:
        audit_mode: "v1" (legacy PageSpeed-driven prompt) or
                    "v2" (fact-grounded conversion-audit prompt).
        lang:       "en" or "sk" — only meaningful for v2.
        niche/location: forwarded to the conversion auditor for personalization.
        require_email:  if True, skip LLM when no contact email is found.
        qualify:    if True (v2 only), run the cheap Haiku qualify gate before
                    the expensive email generation. Disable for max coverage.
        critique:   if True (v2 only), run the Turing critic on the generated
                    email and drop it if it still reads as AI after one rewrite.
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
        from waa.discovery.scraper import fetch_html
        from waa.analysis import personalization
        from waa.analysis.gating import LeadContext, build_lead_gate_chain

        fetch = fetch_html(url)
        html = fetch.get("html") or ""

        # Compute the conversion facts ONCE (free, no tokens) and reuse them
        # for both the gate chain and the email generator.
        facts = personalization.extract_facts(html, url, niche=niche, location=location)

        # Cheap-before-expensive gate (improvement #16): a free heuristic and
        # then the cheap Haiku qualifier decide whether this lead is worth the
        # expensive Sonnet email. We never reach Sonnet — nor even the owner
        # lookup — on a lead that's already been rejected here.
        decision = build_lead_gate_chain(qualify=qualify).evaluate(
            LeadContext(
                url=url, niche=niche,
                contact_emails=audit.get("contact_emails") or [],
                facts=facts,
            )
        )
        if not decision.passed:
            logger.info(f"Gated {url} at '{decision.stage}': {decision.reason}")
            audit["analysis"] = {
                "issues": [], "lead_score": 0,
                "overall_impression": f"gated:{decision.stage}: {decision.reason}",
                "facts": facts.to_dict(), "audit_mode": "v2", "lang": lang,
                "gate": {"stage": decision.stage, "reason": decision.reason,
                         "score": decision.score},
            }
            audit["email"] = None
            audit["skipped_reason"] = f"gated:{decision.stage}"
            return audit

        # Owner-name lookup (free, network only) — first-name greetings are
        # the single biggest cold-email reply-rate driver. Only worth doing
        # now that the lead has cleared the gate.
        owner_first_name = None
        try:
            from waa.analysis.owner_finder import find_owner_name
            owner_hit = find_owner_name(
                html=html, base_url=url,
                contact_emails=audit.get("contact_emails") or [],
                follow_about=True,
            )
            if owner_hit and owner_hit.get("confidence") in ("high", "medium"):
                owner_first_name = owner_hit["name"]
                logger.info(
                    f"Owner name for {url}: {owner_first_name} "
                    f"({owner_hit['confidence']}, via {owner_hit['source']})"
                )
        except Exception as e:
            logger.debug(f"Owner-name extraction failed for {url}: {e}")

        from waa.analysis.analyzer_v2 import generate_email_v2
        email_critic = None
        if critique:
            from waa.analysis.critic import HumanToneCritic
            email_critic = HumanToneCritic()
        v2 = generate_email_v2(
            html=html, url=url, site_name=site_name,
            niche=niche, location=location,
            sender_name=sender_name, lang=lang,
            owner_first_name=owner_first_name,
            facts=facts, critic=email_critic,
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
            "critic": v2.get("critic"),
            "audit_mode": "v2",
            "lang": lang,
        }

        audit["owner_first_name"] = owner_first_name

        if v2.get("skipped_reason"):
            # The generator never produced a draft (gated / thin / LLM error).
            audit["email"] = None
            audit["skipped_reason"] = v2["skipped_reason"]
        else:
            # Automated quality gate (improvement #10): no human reads this
            # draft — the gate decides whether it is send-worthy. It folds the
            # fact-grounding (#2) and Turing-critic (#3) verdicts into one
            # decision, replacing the manual `preview` eyeballing step. The
            # screenshot-correctness check runs later (at send time), once the
            # proof image exists.
            from waa.analysis.quality_gate import (
                EmailArtifact, build_output_quality_gate,
            )
            verdict = build_output_quality_gate(
                require_human=critique, check_screenshot=False,
            ).evaluate(EmailArtifact.from_v2(v2))
            email_payload = {
                "subject_line": v2["subject_line"],
                "email_body": v2["email_body"],
                "follow_up_subject": v2["follow_up_subject"],
                "follow_up_body": v2["follow_up_body"],
                "owner_first_name": owner_first_name,
            }
            if verdict.send_worthy:
                audit["email"] = email_payload
            else:
                failure = verdict.first_blocking_failure()
                reason = _QUALITY_SKIP_REASON.get(
                    failure.name if failure else "", "quality_gate_failed")
                audit["skipped_reason"] = reason
                # Keep the draft when only grounding failed (useful for QA);
                # drop it entirely when it reads as AI (existing behaviour).
                audit["email"] = (
                    email_payload if reason == "v2_validation_failed" else None
                )
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
    qualify: bool = True,
    critique: bool = True,
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
            qualify=qualify, critique=critique,
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


def _empty_registry() -> dict:
    return {"emails": {}, "domains": {}}


def _sent_registry_store() -> JsonStore:
    # Built per-call so tests can reassign the module-level path.
    # tolerate_corrupt=False: a corrupt registry must fail loud, never
    # silently reset — that would re-email everyone we've already contacted.
    return JsonStore(SENT_REGISTRY_FILE, _empty_registry, tolerate_corrupt=False)


def _load_sent_registry() -> dict:
    """Load the registry of already-contacted emails and domains."""
    return _sent_registry_store().load()


def _save_sent_registry(registry: dict):
    """Save the sent registry to disk."""
    _sent_registry_store().save(registry)


def _record_sent(
    registry: dict,
    email: str,
    website: str,
    subject: str,
    *,
    message_id: str | None = None,
    follow_up_subject: str = "",
    follow_up_body: str = "",
    follow_up_after_days: int = 4,
):
    """Record that we sent to this email/domain.

    Stores the Message-ID and the (already-generated) follow-up body so that
    the `monitor-replies` cron and the `send-followups` command have
    everything they need without re-running the audit.
    """
    domain = domain_of(website)
    now = datetime.now()
    now_iso = now.isoformat()

    followup_at = (
        (now + timedelta(days=follow_up_after_days)).isoformat()
        if follow_up_subject and follow_up_body else None
    )

    entry = {
        "website": website,
        "subject": subject,
        "sent_at": now_iso,
        "message_id": message_id,
        "follow_up_subject": follow_up_subject,
        "follow_up_body": follow_up_body,
        "followup_at": followup_at,
        "followup_sent_at": None,
        "reply_received_at": None,
    }
    registry["emails"][email.lower()] = entry
    registry["domains"][domain] = {
        "email": email,
        "sent_at": now_iso,
    }


def _persist_sent_results(send_list: list[dict], send_results: list[dict]) -> int:
    """
    Record every successfully-sent email into the sent registry, carrying its
    Message-ID and follow-up payload for the later `send-followups` step.

    `send_list` and `send_results` are positionally aligned (as returned by
    sender.send_batch). Returns the number of sends recorded.

    This is the shared tail of cmd_send / cmd_pipeline / cmd_campaign — each
    used to inline the same load → loop → save dance.
    """
    registry = _load_sent_registry()
    recorded = 0
    for sent, result in zip(send_list, send_results):
        if result.get("status") != "sent":
            continue
        _record_sent(
            registry, sent["to"], sent["website"], sent["subject"],
            message_id=result.get("message_id"),
            follow_up_subject=sent.get("follow_up_subject", ""),
            follow_up_body=sent.get("follow_up_body", ""),
        )
        recorded += 1
    _save_sent_registry(registry)
    return recorded


# ---------------------------------------------------------------------------
# Proof screenshots — annotate the prospect's own page with the problem
# ---------------------------------------------------------------------------

def _build_highlight_target(facts: dict, lang: str = "sk"):
    """
    Decide what single element on the page to circle in red, given the facts
    the audit extracted. Returns a screenshot.HighlightTarget or None when
    there's nothing visually circle-able (in which case a plain full-page
    shot is still useful, but it's weaker proof).

    Priority is "most visually obvious problem first": leftover placeholder
    text, then a stale copyright year, then the primary CTA.
    """
    import re
    from waa.proof.screenshot import HighlightTarget

    surprise = (facts.get("surprising_finding") or "")
    surprise_low = surprise.lower()
    cta = facts.get("primary_cta_text")
    sk = lang == "sk"

    # Most-compelling, highest-on-page first; the weak footer copyright is a
    # last resort so screenshots stop being a monotony of "© 20xx" footers.
    if "lorem ipsum" in surprise_low:
        return HighlightTarget(
            ["lorem ipsum"],
            "Lorem ipsum text na úvodnej stránke" if sk else "Lorem ipsum text on the homepage",
        )

    if "coming soon" in surprise_low:
        return HighlightTarget(
            ["coming soon"],
            "Sekcia 'Coming soon'" if sk else "'Coming soon' section",
        )

    # The primary CTA is a strong, above-the-fold visual anchor.
    if cta:
        return HighlightTarget(
            [cta],
            f"Tlačidlo „{cta}\"" if sk else f'The "{cta}" button',
        )

    # Footer copyright — weakest, only when there's nothing better to show.
    year_match = re.search(r"\b(20\d{2})\b", surprise)
    if year_match and ("copyright" in surprise_low or "©" in surprise):
        year = year_match.group(1)
        return HighlightTarget(
            [f"© {year}", year],
            f"Pätička stále uvádza © {year}" if sk else f"Footer still says © {year}",
        )

    return None


def attach_screenshots(results: list[dict], *, lang: str = "sk",
                       only_with_target: bool = True,
                       require_correct: bool = True,
                       design_critic: "DesignCritic | None" = None) -> int:
    """
    Capture an annotated "proof" screenshot for each audited prospect and
    store it on the result as result["screenshot"].

    Uses ONE headless browser for the whole batch (cheap per-page after
    launch). Best-effort: if Playwright isn't installed, or a page fails, the
    prospect simply gets no screenshot and the pipeline continues.

    Returns the number of screenshots successfully captured.

    Screenshot-correctness (improvement #10): with `require_correct=True`
    (default) a captured shot is only stored when its red box actually landed
    on the intended element (annotated AND target_found). A misaligned
    annotation is misleading "proof", so it is dropped rather than relying on a
    human to eyeball it. The `preview` QA view passes `require_correct=False`
    so a person can still SEE the near-misses.

    Vision design critique (improvement #6): pass a `design_critic` to also run
    a vision critique on each captured shot and store it on
    result["design_critique"]. Off unless a critic is supplied — it costs a
    vision call per screenshot — so callers opt in (CLI `--design-critique`).

    Deliverability reminder: the captured image is for the *reply / follow-up*,
    not the first cold email — see screenshot.py module docstring.
    """
    from waa.analysis.quality_gate import is_trustworthy_proof
    try:
        from waa.proof.screenshot import PageScreenshotter
    except ImportError:
        logger.warning("playwright not installed — skipping proof screenshots")
        return 0

    # Pair each result with its highlight target up front so we can skip the
    # browser entirely if nothing is circle-able.
    jobs = []
    for r in results:
        if r.get("error") or r.get("skipped_reason"):
            continue
        facts = (r.get("analysis") or {}).get("facts") or {}
        target = _build_highlight_target(facts, lang=lang)
        if target is None and only_with_target:
            continue
        jobs.append((r, target))

    if not jobs:
        return 0

    captured = 0
    try:
        with PageScreenshotter() as shot:
            for r, target in jobs:
                res = shot.capture(r["url"], target)
                if not res.ok():
                    logger.info(f"  no screenshot for {r['url']}: {res.error}")
                    continue
                if require_correct and not is_trustworthy_proof(
                    res.annotated, res.target_found
                ):
                    # The red box did not land on the claimed element — this
                    # image would be misleading proof, so don't keep it.
                    logger.info(
                        f"  dropping misaligned proof for {r['url']} "
                        f"(annotated={res.annotated}, target_found={res.target_found})"
                    )
                    continue
                r["screenshot"] = {
                    "path": res.path,
                    "annotated": res.annotated,
                    "target_found": res.target_found,
                    "caption": target.caption if target else "",
                }
                captured += 1

                # Vision design critique on the freshly captured page — the
                # screenshot already exists, so this adds only the vision call.
                if design_critic is not None:
                    niche = (r.get("analysis") or {}).get("facts", {}).get("niche") or ""
                    critique = design_critic.critique(res.path, niche=niche, lang=lang)
                    if critique.available:
                        r["design_critique"] = critique.to_dict()
                        logger.info(
                            f"  design critique for {r['url']}: "
                            f"score {critique.score:.0f}/10, "
                            f"{len(critique.findings)} finding(s)"
                        )
    except ImportError:
        logger.warning("playwright browser unavailable — skipping proof screenshots")
        return 0

    logger.info(f"Captured {captured} proof screenshot(s) for {len(jobs)} prospect(s)")
    return captured


def _build_design_critic(args):
    """Return a VisionDesignCritic when --design-critique is set, else None.

    Opt-in because it costs one vision call per captured screenshot
    (improvement #6). Built here so the three commands share one construction.
    """
    if not getattr(args, "design_critique", False):
        return None
    from waa.analysis.design_critic import VisionDesignCritic
    return VisionDesignCritic()


def _already_contacted(registry: dict, email: str, website: str) -> str | None:
    """Check if we've already contacted this email or domain. Returns reason or None."""
    email_lower = email.lower()
    if email_lower in registry.get("emails", {}):
        prev = registry["emails"][email_lower]
        return f"already emailed {email} on {prev.get('sent_at', '?')[:10]}"

    domain = domain_of(website)
    if domain in registry.get("domains", {}):
        prev = registry["domains"][domain]
        return f"already contacted {domain} via {prev.get('email', '?')} on {prev.get('sent_at', '?')[:10]}"

    return None


def _prepare_send_list(
    audit_results: list[dict],
    contacts: dict = None,
    *,
    validate_emails: bool = True,
    probe_from: str = "",
    keep_risky: bool = False,
) -> list[dict]:
    """
    Match audit results with contact emails and validate them.

    Order of operations per result:
      1. Pick recipient (contacts CSV → scraped emails → drop)
      2. Skip if already contacted (registry dedup)
      3. Validate the email — drop invalid + unknown
         (catch_all and risky are kept by default; risky is configurable)

    Args:
        validate_emails: run email_validator before sending. Default True.
            Disable only for offline tests or if dnspython is unavailable.
        probe_from: MAIL FROM used during SMTP probes. Pass the verifier
            address on a domain you control (e.g. `verify@emtdstudio.com`).
            If empty, falls back to `verify@<sender_domain_from_env>`.
        keep_risky: if True, role-account addresses (info@, support@…) are
            kept in the send list. Default False — they almost never reply.

    Returns list of {to, subject, body, website, validation: {...}} ready to send.
    """
    contacts = contacts or {}
    registry = _load_sent_registry()

    # Final automated quality gate (improvement #10): the single send-time
    # authority on send-worthiness. Re-affirms grounding + human tone (a
    # belt-and-braces check; these already gated in process_single) and is the
    # only place the proof screenshot's correctness is enforced before send.
    # NA-safe: v1 emails carry no v2 metadata, so those checks pass.
    from waa.analysis.quality_gate import EmailArtifact, build_output_quality_gate
    send_gate = build_output_quality_gate()

    # Profit-weighted lead scoring (improvement #19): rank send-worthy leads by
    # expected value to the agency so the daily email cap is spent best-first.
    from waa.analysis.lead_scoring import build_default_scorer, score_result
    scorer = build_default_scorer()

    # First pass: pick + dedup. Validation is a separate, slower pass.
    candidates = []
    for r in audit_results:
        if r.get("skipped_reason"):
            continue
        email_data = r.get("email") or {}
        subject = email_data.get("subject_line", "").strip().strip('"').strip("'")
        body = email_data.get("email_body", "").replace("\\n", "\n")
        url = r.get("url", "")

        if not subject or not body:
            continue

        verdict = send_gate.evaluate(EmailArtifact.from_result(r))
        if not verdict.send_worthy:
            logger.info(f"Quality gate blocked {url}: {verdict.reason()}")
            continue
        if r.get("screenshot") and not verdict.screenshot_ok():
            # Email still goes (the proof rides the reply, not the first
            # touch), but a misaligned shot must never be used as proof.
            logger.info(f"  proof screenshot for {url} not trustworthy — discarding it")
            r["screenshot"] = None

        domain = domain_of(url)
        contact = contacts.get(domain, {})
        to_email = contact.get("email", "")
        if not to_email:
            scraped_emails = r.get("contact_emails", [])
            if scraped_emails:
                to_email = scraped_emails[0]

        if not to_email:
            logger.warning(f"No contact email found for {url} — skipping send")
            continue

        reason = _already_contacted(registry, to_email, url)
        if reason:
            logger.info(f"Skipping {url} — {reason}")
            continue

        # Expected-profit score; stashed on the result for telemetry/preview
        # and carried on the candidate so we can rank the send list.
        lead_value = score_result(r, scorer).to_dict()
        r["lead_value"] = lead_value

        # Carry follow-up data through to the send pipeline so _record_sent
        # can persist it for the scheduled follow-up step.
        candidates.append({
            "to": to_email,
            "subject": subject,
            "body": body,
            "website": url,
            "contact_name": contact.get("name", ""),
            "follow_up_subject": (email_data.get("follow_up_subject") or "").strip(),
            "follow_up_body": (email_data.get("follow_up_body") or "").replace("\\n", "\n"),
            "owner_first_name": email_data.get("owner_first_name") or "",
            "lead_value": lead_value,
        })

    # Rank best-first so a downstream daily cap keeps the highest-value leads.
    # Stable sort preserves discovery order within an equal score.
    candidates.sort(key=lambda c: c["lead_value"]["value"], reverse=True)
    if candidates:
        logger.info(
            "Lead value: " + ", ".join(
                f"{c['website']}={c['lead_value']['value']}({c['lead_value']['tier']})"
                for c in candidates[:8]
            )
        )

    # Second pass: validate, attach validation metadata, drop bad ones.
    if not validate_emails or not candidates:
        return candidates

    if not probe_from:
        # Default: derive a verifier address from the configured SMTP_EMAIL
        # so probes ride on a domain we own (and not on a third-party domain).
        from waa.outreach.sender import ZOHO_EMAIL
        sender_domain = (ZOHO_EMAIL or "verify@validator.local").split("@", 1)[-1]
        probe_from = f"verify@{sender_domain}"

    try:
        from waa.outreach.email_validator import validate_emails as _do_validate
    except ImportError as e:
        logger.error(f"email_validator unavailable ({e}) — sending without validation")
        return candidates

    addresses = [c["to"] for c in candidates]
    logger.info(
        f"Validating {len(addresses)} email address(es) before send "
        f"(probe_from={probe_from})…"
    )
    results, stats = _do_validate(
        addresses, progress=False, probe_from=probe_from,
    )
    logger.info(f"  {stats.pretty()}")
    if stats.invalid_reasons:
        for reason, n in sorted(stats.invalid_reasons.items(),
                                 key=lambda x: -x[1])[:5]:
            logger.info(f"    – {n}× {reason}")

    safe = []
    for cand, vr in zip(candidates, results):
        cand["validation"] = vr.to_dict()
        if vr.status == "valid" or vr.status == "catch_all":
            safe.append(cand)
        elif vr.status == "risky":
            if keep_risky:
                safe.append(cand)
            else:
                logger.info(f"  Dropping risky address {vr.email}: {vr.reason}")
        else:
            # invalid or unknown — drop
            logger.info(f"  Dropping {vr.status} address {vr.email}: {vr.reason}")

    logger.info(f"Validation kept {len(safe)}/{len(candidates)} addresses")
    return safe


# ---------------------------------------------------------------------------
# preview — manual QA: see emails + proof screenshots before sending anything
# ---------------------------------------------------------------------------

def cmd_preview(args):
    """
    Prospect → v2 audit → screenshot for a handful of leads, then render a
    single self-contained HTML showing each email next to its proof shot.
    Sends NOTHING. This is the eyeball-it-first step.
    """
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY not set. Add it to .env file.")
        sys.exit(1)

    print(f"\n  Finding up to {args.count} '{args.niche}' prospects in {args.location}…\n")
    prospects = run_prospect(
        niche=args.niche, location=args.location or "",
        num_results=max(args.count * 2, args.count), min_score=args.min_score,
    )
    if not prospects:
        print("  No prospects found. Try a different niche/location.\n")
        sys.exit(0)

    urls = [{"url": p["url"], "name": p.get("name", "")} for p in prospects[:args.count]]
    print(f"  Auditing {len(urls)} and generating emails (v2/{args.lang})…\n")
    results = run_batch(
        urls=urls, skip_pagespeed=True,
        sender_name=args.sender, require_email=False,
        audit_mode="v2", lang=args.lang,
        niche=args.niche, location=args.location or "",
        # Preview is a QA view: show every generated email, don't let the
        # cost gate or critic hide marginal leads from the human.
        qualify=False, critique=False,
    )

    print("  Capturing proof screenshots…\n")
    # QA view: show every shot, including near-misses, so a person can see
    # what the screenshot-correctness gate would drop in a real send.
    attach_screenshots(results, lang=args.lang, only_with_target=False,
                       require_correct=False,
                       design_critic=_build_design_critic(args))

    # Profit-weighted lead value (improvement #19) so the QA view shows which
    # leads are worth the scarce daily sends, and order the cards best-first.
    from waa.analysis.lead_scoring import build_default_scorer, score_result
    _scorer = build_default_scorer()
    for r in results:
        if not r.get("skipped_reason"):
            r["lead_value"] = score_result(r, _scorer).to_dict()
    results.sort(key=lambda r: (r.get("lead_value") or {}).get("value", -1), reverse=True)

    from waa.proof.preview_report import render_preview
    html_doc = render_preview(results, niche=args.niche,
                              location=args.location or "", lang=args.lang)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(config.OUTPUT_DIR, f"preview_{stamp}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print(f"  Preview written: {out_path}")
    print(f"  Open it:  open {out_path}\n")
    if args.open:
        import webbrowser
        webbrowser.open(f"file://{os.path.abspath(out_path)}")


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
        qualify=not getattr(args, "no_qualify", False),
        critique=not getattr(args, "no_critic", False),
    )

    if getattr(args, "screenshots", False):
        attach_screenshots(results, lang=getattr(args, "lang", "en"),
                           design_critic=_build_design_critic(args))

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

    send_list = _prepare_send_list(
        audit_results, contacts,
        validate_emails=not getattr(args, "no_validate_emails", False),
        probe_from=getattr(args, "probe_from", "") or "",
        keep_risky=getattr(args, "keep_risky", False),
    )

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
        _persist_sent_results(send_list, results)

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
        qualify=not getattr(args, "no_qualify", False),
        critique=not getattr(args, "no_critic", False),
    )

    if getattr(args, "screenshots", False):
        attach_screenshots(results, lang=getattr(args, "lang", "en"),
                           design_critic=_build_design_critic(args))

    json_path = save_json(results)
    csv_path = save_csv(results)

    print_summary(results)

    # Step 3: Send (if --send flag)
    if args.send:
        print(f"\n{'='*60}")
        print(f" STEP 3: Sending emails")
        print(f"{'='*60}\n")

        contacts = load_contacts_csv(args.contacts) if args.contacts else {}
        send_list = _prepare_send_list(
            results, contacts,
            validate_emails=not getattr(args, "no_validate_emails", False),
            probe_from=getattr(args, "probe_from", "") or "",
            keep_risky=getattr(args, "keep_risky", False),
        )

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
                    _persist_sent_results(send_list, send_results)
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


def _empty_progress() -> dict:
    return {"completed": [], "daily_logs": {}}


def _campaign_progress_store() -> JsonStore:
    # tolerate_corrupt=False: corrupt progress must fail loud, not silently
    # reset (that would re-run every niche/location combo and re-email).
    return JsonStore(CAMPAIGN_PROGRESS_FILE, _empty_progress, tolerate_corrupt=False)


def _load_campaign_progress() -> dict:
    """Load campaign progress from disk."""
    return _campaign_progress_store().load()


def _save_campaign_progress(progress: dict):
    """Save campaign progress to disk."""
    _campaign_progress_store().save(progress)


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

    # Send-window guard: cold-email reply rate is highly sensitive to send
    # day. Weekend sends end up buried under Monday's inbox flood and
    # consistently underperform. Allow override for tests / one-offs.
    if args.send and args.confirm_send:
        allowed, reason = _is_good_send_window(
            allow_weekends=getattr(args, "allow_weekends", False),
        )
        if not allowed:
            print(f"\n  Send window guard: {reason}\n  "
                  "Audit step still runs — drafts will be ready for Monday.\n")
            # We could exit early; instead, downgrade to dry-run so the
            # campaign still generates drafts for later sending.
            args.confirm_send = False

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
                qualify=not getattr(args, "no_qualify", False),
                critique=not getattr(args, "no_critic", False),
            )

            today_usage["pagespeed_calls"] += top_n

            if getattr(args, "screenshots", False):
                attach_screenshots(results, lang=getattr(args, "lang", "en"),
                                   design_critic=_build_design_critic(args))

            json_path = save_json(results)
            save_csv(results)

            # Step 3: Send emails (if enabled)
            if args.send:
                contacts = {}
                send_list = _prepare_send_list(
                    results, contacts,
                    validate_emails=not getattr(args, "no_validate_emails", False),
                    probe_from=getattr(args, "probe_from", "") or "",
                    keep_risky=getattr(args, "keep_risky", False),
                )

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
                            # Record sent emails so we never contact them again
                            sent_count = _persist_sent_results(send_list, send_results)
                            today_usage["emails_sent"] += sent_count
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
# Send-window guard — cold email reply rates are highly sensitive to send time
# ---------------------------------------------------------------------------

def _is_good_send_window(now: datetime | None = None, *, allow_weekends: bool = False) -> tuple[bool, str]:
    """
    Industry data on cold-email reply rate by send time:
      - Tue–Thu 9–11 AM local: best
      - Mon, Fri: weaker
      - Sat–Sun: deadtime; mails get buried under Monday's flood
      - Holiday weeks: avoid

    Returns (allowed, reason). When `allow_weekends=True`, the weekend gate
    is disabled (useful for testing / one-off manual sends).
    """
    now = now or datetime.now()
    weekday = now.weekday()  # 0 = Monday … 6 = Sunday
    if not allow_weekends and weekday >= 5:
        return False, f"weekend send avoided ({now.strftime('%A')}) — pass --allow-weekends to override"
    return True, "ok"


# ---------------------------------------------------------------------------
# send-followups — drive the second touch in a cold-outreach sequence
# ---------------------------------------------------------------------------

def cmd_send_followups(args):
    """
    Send scheduled follow-ups for prospects who:
      • were emailed at least `--after-days` ago
      • haven't replied yet
      • have a follow-up body recorded in sent_registry.json
      • haven't already received a follow-up

    Follow-ups are threaded via In-Reply-To + References so they appear
    under the original email in the recipient's inbox.

    Industry data: follow-ups account for roughly half of all cold-email
    replies. Sending only the first email throws away most of the funnel.
    """
    from datetime import datetime, timedelta
    from waa.outreach.sender import send_batch, save_send_log, print_send_summary, ZOHO_PASSWORD

    allowed, reason = _is_good_send_window(allow_weekends=args.allow_weekends)
    if not allowed and not args.dry_run:
        print(f"  Skipping send: {reason}")
        sys.exit(0)

    registry = _load_sent_registry()
    emails_map = registry.get("emails", {}) or {}
    now = datetime.now()
    cutoff = now - timedelta(days=args.after_days)

    eligible = []
    for email, entry in emails_map.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("reply_received_at"):
            continue
        if entry.get("followup_sent_at"):
            continue
        if not entry.get("follow_up_subject") or not entry.get("follow_up_body"):
            continue
        try:
            sent_at = datetime.fromisoformat(entry.get("sent_at", ""))
        except (TypeError, ValueError):
            continue
        if sent_at > cutoff:
            continue
        eligible.append((email, entry))

    if not eligible:
        print("\n  No follow-ups due right now.\n")
        sys.exit(0)

    # Apply daily cap so we don't burn the whole list in one run
    eligible = eligible[: args.max_per_run]

    print(f"\n  {len(eligible)} follow-up(s) due (sent at least {args.after_days}d ago, no reply)\n")
    for email, entry in eligible:
        days_since = (now - datetime.fromisoformat(entry["sent_at"])).days
        print(f"    → {email}  ({days_since}d since first contact, original: {entry.get('subject', '')[:60]})")

    if args.dry_run:
        print("\n  DRY RUN — add --confirm-send to actually send\n")
        return

    if not args.confirm_send:
        print("\n  Add --confirm-send to actually send these follow-ups.\n")
        return

    if not ZOHO_PASSWORD:
        print("\nERROR: SMTP_PASSWORD not set in .env\n")
        sys.exit(1)

    # Build send list with threading headers
    send_list = []
    for email, entry in eligible:
        # Prefix subject with "Re:" if the LLM didn't already
        fu_subject = entry.get("follow_up_subject", "")
        if not fu_subject.lower().startswith("re:"):
            fu_subject = f"Re: {entry.get('subject', '')}"
        send_list.append({
            "to": email,
            "subject": fu_subject,
            "body": entry["follow_up_body"],
            "website": entry.get("website", ""),
            "in_reply_to": entry.get("message_id") or "",
            "references": entry.get("message_id") or "",
        })

    results = send_batch(
        emails=send_list,
        from_name=args.from_name,
        dry_run=False,
        delay=args.delay,
    )

    # Record follow-up sent timestamps
    now_iso = now.isoformat()
    for sl, sr in zip(send_list, results):
        if sr.get("status") == "sent":
            entry = emails_map.get(sl["to"].lower())
            if entry:
                entry["followup_sent_at"] = now_iso
                entry["followup_message_id"] = sr.get("message_id")
    _save_sent_registry(registry)

    save_send_log(results, filename=f"followup_send_log_{now.strftime('%Y%m%d_%H%M%S')}.json")
    print_send_summary(results)


# ---------------------------------------------------------------------------
# monitor-replies — IMAP poll all sender mailboxes and post replies to Discord
# ---------------------------------------------------------------------------

def cmd_monitor_replies(args):
    """Poll all configured Zoho mailboxes for replies, post to Discord webhook."""
    from waa.outreach.replies_monitor import run_once

    webhook = args.webhook_url or os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook and not args.dry_run:
        print("ERROR: DISCORD_WEBHOOK_URL not set in env, and --dry-run not specified.")
        print("Either export DISCORD_WEBHOOK_URL=... or pass --dry-run for a console-only test.")
        sys.exit(1)

    summary = run_once(
        webhook_url=webhook,
        lookback_days=args.lookback_days,
        dry_run=args.dry_run,
    )

    print(f"\n  examined  : {summary['examined']}")
    print(f"  posted    : {summary['posted']}")
    print(f"  ⤿ already seen : {summary['skipped_seen']}")
    print(f"  ⤿ noise        : {summary['skipped_noise']}")
    print(f"  ⤿ not a reply  : {summary['skipped_not_a_reply']}\n")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="python -m waa",
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

    # --- preview ---
    p_preview = subparsers.add_parser(
        "preview",
        help="QA: generate emails + proof screenshots into one HTML, send nothing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Eyeball what the agent would send BEFORE any real run.

Examples:
  python audit_agent.py preview --niche restauracia --location Bratislava
  python audit_agent.py preview --niche kaviaren --location Kosice --count 8 --open

Writes output/preview_<timestamp>.html — open it in a browser. Each card
shows the generated email next to the annotated screenshot of that
prospect's site. Nothing is sent.
        """,
    )
    p_preview.add_argument("--niche", required=True, help="Business niche (e.g. restauracia)")
    p_preview.add_argument("--location", default="", help="City (e.g. Bratislava)")
    p_preview.add_argument("--count", type=int, default=5, help="How many prospects to preview (default: 5)")
    p_preview.add_argument("--lang", choices=["en", "sk"], default="sk", help="Email language (default: sk)")
    p_preview.add_argument("--min-score", type=int, default=25, help="Min prospect score (default: 25)")
    p_preview.add_argument("--sender", default="Tomas", help="Sender first name")
    p_preview.add_argument("--design-critique", action="store_true",
                           help="Add a vision design critique under each screenshot (costs a vision call per page)")
    p_preview.add_argument("--open", action="store_true", help="Open the HTML in your browser when done")
    p_preview.set_defaults(func=cmd_preview)

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
    p_audit.add_argument("--screenshots", action="store_true",
                         help="Capture an annotated proof screenshot per prospect (needs Playwright)")
    p_audit.add_argument("--design-critique", action="store_true",
                         help="Run a vision design critique on each screenshot (needs --screenshots; costs a vision call per page)")
    p_audit.add_argument("--no-qualify", action="store_true",
                         help="Skip the cheap Haiku qualify gate (v2). Generates an "
                              "email for every prospect — more coverage, more tokens.")
    p_audit.add_argument("--no-critic", action="store_true",
                         help="Skip the Turing critic (v2). Keeps emails even if they read as AI.")
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
    p_send.add_argument("--no-validate-emails", action="store_true",
                        help="Skip email validation (NOT recommended — high bounce risk)")
    p_send.add_argument("--probe-from", default="",
                        help="MAIL FROM used during validation probes "
                             "(default: verify@<your sender domain>)")
    p_send.add_argument("--keep-risky", action="store_true",
                        help="Send to role accounts (info@, support@…) anyway")
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
    p_pipeline.add_argument("--no-validate-emails", action="store_true",
                            help="Skip email validation (NOT recommended)")
    p_pipeline.add_argument("--probe-from", default="",
                            help="MAIL FROM used during validation probes")
    p_pipeline.add_argument("--keep-risky", action="store_true",
                            help="Send to role accounts (info@, support@…) anyway")
    p_pipeline.add_argument("--screenshots", action="store_true",
                            help="Capture an annotated proof screenshot per prospect (needs Playwright)")
    p_pipeline.add_argument("--design-critique", action="store_true",
                            help="Run a vision design critique on each screenshot (needs --screenshots; costs a vision call per page)")
    p_pipeline.add_argument("--no-qualify", action="store_true",
                            help="Skip the cheap Haiku qualify gate (v2)")
    p_pipeline.add_argument("--no-critic", action="store_true",
                            help="Skip the Turing critic (v2)")
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
    p_campaign.add_argument("--input-csv", default="data/agent_input.csv", help="CSV with niche,location columns (default: data/agent_input.csv)")
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
    p_campaign.add_argument("--no-validate-emails", action="store_true",
                            help="Skip email validation (NOT recommended)")
    p_campaign.add_argument("--probe-from", default="",
                            help="MAIL FROM used during validation probes "
                                 "(default: verify@<your sender domain>)")
    p_campaign.add_argument("--keep-risky", action="store_true",
                            help="Send to role accounts (info@, support@…) anyway")
    p_campaign.add_argument("--allow-weekends", action="store_true",
                            help="Allow sending on Sat/Sun (default: blocked, "
                                 "weekend sends have low reply rates)")
    p_campaign.add_argument("--screenshots", action="store_true",
                            help="Capture an annotated proof screenshot per prospect "
                                 "(needs Playwright; recommended for the SK proof play)")
    p_campaign.add_argument("--design-critique", action="store_true",
                            help="Run a vision design critique on each screenshot (needs --screenshots; costs a vision call per page)")
    p_campaign.add_argument("--no-qualify", action="store_true",
                            help="Skip the cheap Haiku qualify gate. Default is to "
                                 "qualify (saves the expensive model on weak leads).")
    p_campaign.add_argument("--no-critic", action="store_true",
                            help="Skip the Turing critic. Default runs it so AI-sounding "
                                 "emails are dropped instead of sent.")
    p_campaign.set_defaults(func=cmd_campaign)

    # --- monitor-replies ---
    p_monitor = subparsers.add_parser(
        "monitor-replies",
        help="Poll all sender Zoho mailboxes for replies and post them to Discord",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # One-shot run (uses DISCORD_WEBHOOK_URL from env)
  python audit_agent.py monitor-replies

  # Dry run — print to console instead of posting
  python audit_agent.py monitor-replies --dry-run

  # Look back further (default 3 days)
  python audit_agent.py monitor-replies --lookback-days 7

Required env:
  DISCORD_WEBHOOK_URL   — channel webhook
  IMAP_HOST             — default imap.zoho.eu
  IMAP_PORT             — default 993
  SMTP_EMAIL / SMTP_PASSWORD                 — Tomas mailbox
  SMTP_EMAIL_2 / SMTP_PASSWORD_2             — Erik mailbox (optional)
  SMTP_EMAIL_3 / SMTP_PASSWORD_3             — Michal mailbox (optional)
        """,
    )
    p_monitor.add_argument("--lookback-days", type=int, default=3,
                           help="Look back this many days (default: 3)")
    p_monitor.add_argument("--dry-run", action="store_true",
                           help="Print to console instead of posting to Discord")
    p_monitor.add_argument("--webhook-url", default="",
                           help="Override DISCORD_WEBHOOK_URL")
    p_monitor.set_defaults(func=cmd_monitor_replies)

    # --- send-followups ---
    p_followups = subparsers.add_parser(
        "send-followups",
        help="Send the second-touch follow-up to prospects who didn't reply",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Preview what would go out today
  python audit_agent.py send-followups --dry-run

  # Actually send (default: prospects emailed >=4 days ago, capped at 20)
  python audit_agent.py send-followups --confirm-send

  # Custom timing + bigger batch
  python audit_agent.py send-followups --after-days 7 --max-per-run 40 --confirm-send

How it works:
  Reads sent_registry.json. For each prospect:
    - sent_at older than --after-days
    - no reply_received_at (set by monitor-replies when an IMAP reply arrives)
    - has follow_up_subject + follow_up_body recorded
    - no followup_sent_at yet
  Sends a threaded follow-up with In-Reply-To pointing at the original
  Message-ID so it lands in the same conversation thread on Gmail/Outlook.

  Industry data: ~40-60% of cold-email replies come from follow-ups.
  Without this step, you're throwing away most of your funnel.
        """,
    )
    p_followups.add_argument("--after-days", type=int, default=4,
                             help="Send follow-up if first email is at least N days old (default: 4)")
    p_followups.add_argument("--max-per-run", type=int, default=20,
                             help="Max follow-ups to send per run (default: 20)")
    p_followups.add_argument("--delay", type=float, default=30,
                             help="Seconds between sends (default: 30)")
    p_followups.add_argument("--from-name", default="Tomas Maxim",
                             help="Sender display name")
    p_followups.add_argument("--dry-run", action="store_true",
                             help="Preview only — don't send")
    p_followups.add_argument("--confirm-send", action="store_true",
                             help="Actually send (default is dry-run)")
    p_followups.add_argument("--allow-weekends", action="store_true",
                             help="Allow sending on Sat/Sun (default: blocked)")
    p_followups.set_defaults(func=cmd_send_followups)

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
