"""
LLM analysis layer.
Takes raw audit data, identifies issues, generates emails.
"""

import json
import logging
import re
import anthropic

from waa import config
from waa.analysis.prompts import AUDIT_ANALYSIS_PROMPT, EMAIL_GENERATION_PROMPT

logger = logging.getLogger(__name__)


def strip_ai_dashes(text: str) -> str:
    """
    Remove em/en dashes and dash-as-punctuation, which are a strong "this was
    written by AI" tell. Intra-word hyphens (e-mail, Wi-Fi, tel:) are kept.

        "Krásny web — ale pomalý."  -> "Krásny web, ale pomalý."
        "Rýchle - a lacné"          -> "Rýchle, a lacné"
    """
    if not text:
        return text
    # em/en dash (with any surrounding whitespace) -> comma
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    # a hyphen used as a dash (surrounded by spaces) -> comma
    text = re.sub(r"\s+-\s+", ", ", text)
    # tidy artefacts left behind
    text = re.sub(r",\s*([.;:!?])", r"\1", text)   # ", ." -> "."
    text = re.sub(r",\s*,", ",", text)              # ", ," -> ","
    text = re.sub(r"\s+,", ",", text)               # " ,"  -> ","
    return text


def _call_llm(prompt: str, *, model: str | None = None, max_tokens: int = 2000) -> str:
    """Call Anthropic API and return the text response.

    `model` defaults to config.LLM_MODEL (the expensive synthesis model).
    Pass a cheaper model (e.g. config.QUALIFY_MODEL) for high-volume,
    low-stakes calls like lead qualification.
    """
    from waa.core import cost
    cost.enforce_budget()  # raises BudgetExceeded if the run hit its cap (#18)

    used_model = model or config.LLM_MODEL
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=used_model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

    cost.record_message_usage(message, used_model, label="text")
    return message.content[0].text


def _call_llm_vision(
    prompt: str,
    image_b64: str,
    media_type: str = "image/png",
    *,
    model: str | None = None,
    max_tokens: int = 1024,
) -> str:
    """Call Anthropic with an image + text prompt and return the text response.

    Mirrors `_call_llm` but sends a vision message. Kept separate (not folded
    into `_call_llm`) so the text path stays simple — the vision capability is
    only needed by the design critic (improvement #6). `model` defaults to the
    cheap vision-capable model so per-lead design critiques stay inexpensive.
    """
    from waa.core import cost
    cost.enforce_budget()  # raises BudgetExceeded if the run hit its cap (#18)

    used_model = model or config.VISION_MODEL
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=used_model,
        max_tokens=max_tokens,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": image_b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )

    cost.record_message_usage(message, used_model, label="vision")
    return message.content[0].text


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown code blocks."""
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    return json.loads(text)


def analyze_audit_data(audit_data: dict) -> dict:
    """Send audit data to LLM for analysis. Returns structured issues."""
    # Format audit data as readable text for the LLM
    formatted = _format_audit_for_llm(audit_data)
    prompt = AUDIT_ANALYSIS_PROMPT.format(audit_data=formatted)

    logger.info(f"Analyzing audit data for {audit_data.get('url', 'unknown')}")

    try:
        response = _call_llm(prompt)
        result = _parse_json_response(response)
        return result
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}")
        return {
            "issues": [],
            "overall_impression": "Analysis failed — could not parse LLM response",
            "lead_score": 0,
            "raw_response": response,
        }
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return {
            "issues": [],
            "overall_impression": f"Analysis failed — API error: {e}",
            "lead_score": 0,
        }


def generate_email(
    url: str,
    site_name: str,
    findings: dict,
    agency_name: str = "Our Agency",
    sender_name: str = "Alex",
    sender_title: str = "Founder",
) -> dict:
    """Generate personalized cold email based on audit findings."""
    prompt = EMAIL_GENERATION_PROMPT.format(
        url=url,
        site_name=site_name,
        findings=json.dumps(findings, indent=2),
        sender_name=sender_name,
        niche_placeholder="{niche}",
    )

    logger.info(f"Generating email for {url}")

    try:
        response = _call_llm(prompt)
        result = _parse_json_response(response)

        # Validate and fix common LLM issues
        body = result.get("email_body", "")
        body = _clean_email_body(body, sender_name)
        result["email_body"] = body

        word_count = len(body.replace("\\n", " ").split())
        if word_count > 110:
            logger.warning(f"Email for {url} is {word_count} words (limit 90) — regenerating")
            response = _call_llm(prompt + "\n\nCRITICAL: Your previous attempt was too long. This MUST be under 90 words.")
            result = _parse_json_response(response)
            result["email_body"] = _clean_email_body(result.get("email_body", ""), sender_name)

        return result
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse email response as JSON: {e}")
        return {
            "subject_line": "",
            "email_body": "",
            "error": f"Parse error: {e}",
            "raw_response": response,
        }
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error during email generation: {e}")
        return {
            "subject_line": "",
            "email_body": "",
            "error": str(e),
        }


def _clean_email_body(body: str, sender_name: str) -> str:
    """Remove common LLM additions that hurt deliverability."""
    lines = body.replace("\\n", "\n").split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip().lower()
        # Remove title/company from sign-off (e.g. "Founder, EMTD Studio")
        if any(kw in stripped for kw in ["founder", "ceo", "owner", "director", "emtd", "studio", "agency"]):
            if len(stripped.split()) <= 5:  # only skip short sign-off lines
                continue
        # Remove "no strings attached" and similar
        if "no strings attached" in stripped:
            line = line.replace("no strings attached", "").replace("No strings attached", "")
            line = line.replace(" — .", ".").replace(" — ,", ",").replace("  ", " ").strip()
            if not line or line in (".", ",", "—"):
                continue
        cleaned.append(line)
    result = "\n".join(cleaned)
    result = strip_ai_dashes(result)
    # Re-encode for JSON storage
    return result.replace("\n", "\\n")


def _format_audit_for_llm(audit: dict) -> str:
    """Format raw audit data into a readable summary for the LLM."""
    lines = []
    lines.append(f"URL: {audit.get('url', 'N/A')}")
    lines.append(f"HTTP Status: {audit.get('status_code', 'N/A')}")
    lines.append(f"Server Response Time: {audit.get('load_time_ms', 'N/A')}ms")

    # SEO signals
    seo = audit.get("seo")
    if seo:
        lines.append("\n--- SEO & Content ---")
        lines.append(f"Title: {seo.get('title', 'MISSING')} ({seo.get('title_length', 0)} chars)")
        lines.append(f"Meta Description: {seo.get('meta_description', 'MISSING')} ({seo.get('meta_description_length', 0)} chars)")
        lines.append(f"H1 Tags ({seo.get('h1_count', 0)}): {seo.get('h1_tags', [])}")
        lines.append(f"H2 Tags: {seo.get('h2_tags', [])}")
        lines.append(f"Images Total: {seo.get('images_total', 0)}")
        lines.append(f"Images Missing Alt Text: {seo.get('images_missing_alt', 0)}")
        lines.append(f"Has Viewport Meta (Mobile): {seo.get('has_viewport', False)}")
        lines.append(f"Has Open Graph Tags: {seo.get('has_og_tags', False)}")
        lines.append(f"Has Canonical URL: {seo.get('canonical') is not None}")
        lines.append(f"Has Schema/Structured Data: {seo.get('has_schema', False)}")
        lines.append(f"Uses HTTPS: {seo.get('uses_https', False)}")
        lines.append(f"CTAs Found: {seo.get('ctas_found', [])}")
        lines.append(f"Internal Links: {seo.get('internal_links', 0)}")
        lines.append(f"External Links: {seo.get('external_links', 0)}")
        lines.append(f"Word Count: {seo.get('word_count', 0)}")

    # Tech stack
    tech = audit.get("tech")
    if tech:
        lines.append("\n--- Technology Stack ---")
        lines.append(f"CMS: {tech.get('cms', 'Not detected')}")
        lines.append(f"Technologies: {', '.join(tech.get('technologies', [])) or 'None detected'}")
        lines.append(f"Server: {tech.get('server', 'Not detected')}")

    # PageSpeed
    ps = audit.get("pagespeed")
    if ps and ps.get("available"):
        for strategy in ["mobile", "desktop"]:
            data = ps.get(strategy, {})
            if data and "error" not in data:
                lines.append(f"\n--- PageSpeed ({strategy.title()}) ---")
                lines.append(f"Performance Score: {data.get('performance_score', 'N/A')}/100")
                lines.append(f"SEO Score: {data.get('seo_score', 'N/A')}/100")
                lines.append(f"Best Practices: {data.get('best_practices_score', 'N/A')}/100")
                lines.append(f"Accessibility: {data.get('accessibility_score', 'N/A')}/100")
                lines.append(f"LCP: {data.get('lcp_ms', 'N/A')}ms")
                lines.append(f"FCP: {data.get('fcp_ms', 'N/A')}ms")
                lines.append(f"CLS: {data.get('cls', 'N/A')}")
                lines.append(f"Speed Index: {data.get('speed_index_ms', 'N/A')}ms")
                lines.append(f"TTI: {data.get('tti_ms', 'N/A')}ms")
                lines.append(f"Total Blocking Time: {data.get('total_blocking_time_ms', 'N/A')}ms")
    else:
        lines.append("\n--- PageSpeed ---")
        lines.append("PageSpeed data not available (no API key or fetch failed)")

    return "\n".join(lines)
