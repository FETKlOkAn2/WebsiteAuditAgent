"""
Analyzer v2 — fact-grounded, language-aware orchestration.

Pipeline:
    HTML  ─►  conversion_audit (signals)
              │
              ▼
              personalization (SiteFacts)
              │
              ▼
              prompts_v2 (filled with facts)
              │
              ▼
              LLM call
              │
              ▼
              validate: must quote a fact verbatim
              │ (if not — retry once with stronger constraint)
              ▼
              cleaned email dict

Why this exists:
- v1 emails read like templates. v2 emails are forced to reference at least
  one verbatim fact from the site. If the LLM ignores that, we reject and
  retry once.
- Bilingual: same orchestrator handles SK and EN by switching the prompt.
- Skip-if-not-personalizable: when SiteFacts has <3 facts, we don't burn
  Anthropic tokens — we mark the prospect as skipped.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

import anthropic

from waa import config
from waa.analysis import personalization
from waa.analysis.personalization import SiteFacts
from waa.analysis import prompts_v2
from waa.analysis.analyzer import _call_llm, _parse_json_response, _clean_email_body, strip_ai_dashes

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_email_v2(
    html: str,
    url: str,
    site_name: str,
    niche: str = "",
    location: str = "",
    sender_name: str = "Tomas",
    lang: str = "sk",
    owner_first_name: Optional[str] = None,
    facts: Optional[SiteFacts] = None,
) -> dict:
    """
    Top-level v2 entry point. Extracts facts, builds the prompt, calls the
    LLM, validates, and returns a cleaned email dict.

    Returns:
        {
            "facts": SiteFacts.to_dict(),
            "skipped_reason": str | None,    # set if we didn't call the LLM
            "subject_line": str,
            "email_body": str,
            "follow_up_subject": str,
            "follow_up_body": str,
            "validation": {"quoted_facts": [...], "passed": bool, "retried": bool},
            "lang": str,
        }
    """
    # Reuse facts computed by the caller (e.g. the gate chain) if provided,
    # so the conversion audit isn't run twice.
    if facts is None:
        facts = personalization.extract_facts(html, url, niche=niche, location=location)
    base = {
        "facts": facts.to_dict(),
        "owner_first_name": owner_first_name,
        "skipped_reason": None,
        "subject_line": "",
        "email_body": "",
        "follow_up_subject": "",
        "follow_up_body": "",
        "validation": {"quoted_facts": [], "passed": False, "retried": False},
        "lang": lang,
    }

    if not facts.is_personalizable():
        base["skipped_reason"] = (
            f"insufficient_facts ({facts.fact_count()}/3) — "
            "site does not give the email anything specific to anchor on"
        )
        logger.info(f"Skipping {url}: {base['skipped_reason']}")
        return base

    prompt = _build_prompt(
        facts,
        site_name=site_name,
        sender_name=sender_name,
        lang=lang,
        owner_first_name=owner_first_name,
    )

    # First attempt
    try:
        response = _call_llm(prompt)
        result = _parse_json_response(response)
    except (json.JSONDecodeError, anthropic.APIError) as e:
        logger.error(f"v2 LLM call failed for {url}: {e}")
        base["skipped_reason"] = f"llm_error: {e}"
        return base

    body = result.get("email_body", "")
    body = _clean_email_body(body, sender_name)
    quoted = _facts_quoted(body, facts)
    passed = len(quoted) > 0

    if not passed:
        # One retry with explicit corrective instruction
        logger.info(f"v2 first pass for {url} did not quote any fact — retrying")
        retry_prompt = (
            prompt + "\n\n## CRITICAL RETRY INSTRUCTION\n"
            "Your previous output failed because it did not quote ANY of the "
            "facts from `quotable_facts` verbatim. You MUST include at least "
            "one verbatim quote (in quotes is fine, or as part of a sentence). "
            "Pick the most specific fact and reference it directly."
        )
        try:
            response = _call_llm(retry_prompt)
            result = _parse_json_response(response)
            body = _clean_email_body(result.get("email_body", ""), sender_name)
            quoted = _facts_quoted(body, facts)
            passed = len(quoted) > 0
        except (json.JSONDecodeError, anthropic.APIError) as e:
            logger.error(f"v2 retry failed for {url}: {e}")

        base["validation"]["retried"] = True

    base["subject_line"] = strip_ai_dashes((result.get("subject_line") or "").strip())
    base["email_body"] = body
    base["follow_up_subject"] = strip_ai_dashes((result.get("follow_up_subject") or "").strip())
    base["follow_up_body"] = _clean_email_body(
        result.get("follow_up_body", ""), sender_name
    )
    base["validation"]["quoted_facts"] = quoted
    base["validation"]["passed"] = passed

    if not passed:
        # We still return the email but mark it as not personalized.
        # The caller (audit_agent) can decide whether to send or skip.
        logger.warning(f"v2 email for {url} still not grounded after retry")

    return base


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(
    facts: SiteFacts,
    site_name: str,
    sender_name: str,
    lang: str,
    owner_first_name: Optional[str] = None,
) -> str:
    """Render the appropriate v2 prompt with the SiteFacts data filled in."""
    template = prompts_v2.EMAIL_PROMPT_SK if lang == "sk" else prompts_v2.EMAIL_PROMPT_EN
    quotable = facts.quotable_strings()
    # Keep the list short; LLM does better with focused choices
    quotable = quotable[:6]
    quotable_str = " | ".join(f'"{q}"' for q in quotable) if quotable else "(none)"

    niche_for_lang = prompts_v2.translate_niche(facts.niche or "", lang)

    # If we don't have a first name, pass a sentinel the prompt knows to
    # interpret as "skip the greeting" rather than confabulating a salutation.
    owner_value = owner_first_name if owner_first_name else (
        "neznáme" if lang == "sk" else "unknown"
    )

    return template.format(
        url=facts.url,
        site_name=site_name or "(unknown)",
        niche=facts.niche or "businesses",
        niche_sk=niche_for_lang,
        city=facts.city_or_area or ("daný región" if lang == "sk" else "your area"),
        h1=(facts.h1 or "(none)").replace('"', "'"),
        primary_cta=(facts.primary_cta_text or "(none)").replace('"', "'"),
        phone_clickable="áno" if facts.has_phone_clickable else (
            "no" if lang == "en" else "nie"
        ),
        niche_missing=", ".join(facts.niche_specific_missing) or "(none)",
        niche_present=", ".join(facts.niche_specific_present) or "(none)",
        surprise=facts.surprising_finding or "(none)",
        hi_finding=facts.high_confidence_finding or "(none)",
        quotable_facts=quotable_str,
        sender_name=sender_name,
        owner_first_name=owner_value,
    )


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for fuzzy substring matching."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _facts_quoted(body: str, facts: SiteFacts) -> list[str]:
    """
    Return the list of facts that appear (case-insensitive, whitespace-tolerant)
    verbatim in the email body. The LLM is allowed to drop trailing
    punctuation, so we match on a normalized form.
    """
    if not body:
        return []
    body_norm = _normalize(body.replace("\\n", " "))
    matched = []
    for q in facts.quotable_strings():
        qn = _normalize(q)
        if not qn or len(qn) < 3:
            continue
        # short factual tokens (numbers, single words) are matched as words
        if len(qn.split()) == 1 and qn.isdigit():
            if re.search(rf"\b{re.escape(qn)}\b", body_norm):
                matched.append(q)
            continue
        # multi-word facts: substring match is enough
        if qn in body_norm:
            matched.append(q)
    return matched
