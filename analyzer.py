"""
LLM analysis layer.
Takes raw audit data, identifies issues, generates emails.
"""

import json
import logging
import anthropic

import config
from prompts import AUDIT_ANALYSIS_PROMPT, EMAIL_GENERATION_PROMPT

logger = logging.getLogger(__name__)


def _call_llm(prompt: str) -> str:
    """Call Anthropic API and return the text response."""
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

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
        agency_name=agency_name,
        sender_name=sender_name,
        sender_title=sender_title,
    )

    logger.info(f"Generating email for {url}")

    try:
        response = _call_llm(prompt)
        result = _parse_json_response(response)
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
