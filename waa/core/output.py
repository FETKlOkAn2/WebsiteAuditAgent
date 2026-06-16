"""
Output layer: save results as JSON and CSV.
"""

import csv
import json
import os
import logging
from datetime import datetime

from waa import config

logger = logging.getLogger(__name__)


def ensure_output_dir():
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)


def save_json(results: list[dict], filename: str = None) -> str:
    """Save full results as JSON."""
    ensure_output_dir()
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"audit_results_{ts}.json"

    path = os.path.join(config.OUTPUT_DIR, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    logger.info(f"JSON saved: {path}")
    return path


def save_csv(results: list[dict], filename: str = None) -> str:
    """Save a flat CSV suitable for CRM import or email tools."""
    ensure_output_dir()
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"audit_results_{ts}.csv"

    path = os.path.join(config.OUTPUT_DIR, filename)

    rows = []
    for r in results:
        analysis = r.get("analysis") or {}
        email_data = r.get("email") or {}
        tech = r.get("tech") or {}
        issues = analysis.get("issues", [])
        issues_text = " | ".join(
            f"[{i.get('category', '')}] {i.get('problem', '')}" for i in issues
        )

        rows.append({
            "website": r.get("url", ""),
            "status": "skipped" if r.get("skipped_reason") else ("error" if r.get("error") else "ok"),
            "performance_score_mobile": _get_ps(r, "mobile", "performance_score"),
            "performance_score_desktop": _get_ps(r, "desktop", "performance_score"),
            "seo_score": _get_ps(r, "mobile", "seo_score"),
            "cms": tech.get("cms", ""),
            "lead_score": analysis.get("lead_score", ""),
            "overall_impression": analysis.get("overall_impression", ""),
            "issues_summary": issues_text,
            "email_subject": email_data.get("subject_line", ""),
            "email_body": email_data.get("email_body", ""),
            "followup_subject": email_data.get("follow_up_subject", ""),
            "followup_body": email_data.get("follow_up_body", ""),
        })

    if rows:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    logger.info(f"CSV saved: {path}")
    return path


def _get_ps(result: dict, strategy: str, field: str):
    """Safely extract a PageSpeed field."""
    ps = result.get("pagespeed")
    if not ps or not ps.get("available"):
        return ""
    data = ps.get(strategy, {})
    if not data or "error" in data:
        return ""
    return data.get(field, "")


def print_summary(results: list[dict]):
    """Print a quick summary to stdout."""
    print(f"\n{'='*60}")
    print(f" AUDIT COMPLETE — {len(results)} sites processed")
    print(f"{'='*60}\n")

    for r in results:
        url = r.get("url", "unknown")
        if r.get("error"):
            print(f"  x {url} — ERROR: {r['error']}")
            continue
        if r.get("skipped_reason"):
            print(f"  - {url} — skipped ({r['skipped_reason']})")
            continue

        analysis = r.get("analysis") or {}
        email_data = r.get("email") or {}
        score = analysis.get("lead_score", "?")
        issues = analysis.get("issues", [])
        subject = email_data.get("subject_line", "")

        print(f"  + {url}")
        print(f"    Lead Score: {score}/10")
        print(f"    Issues Found: {len(issues)}")
        for issue in issues:
            print(f"      - [{issue.get('category', '')}] {issue.get('problem', '')}")
        if subject:
            print(f"    Email Subject: {subject}")
        print()
