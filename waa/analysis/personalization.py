"""
Personalization layer.

The whole reason cold emails get 0% reply: they read like templates.
This module extracts a small set of CONCRETE FACTS from a site so the
email generator can ground every sentence in something specific.

Strategy:
- We extract 5–7 facts. If we have fewer than 3, the prospect is NOT
  personalizable and the pipeline should skip them. This saves Anthropic
  tokens AND prevents generic emails from going out.
- Every fact must be quotable verbatim — the prompt forces the LLM to
  reference at least one fact directly. Any output that doesn't is rejected
  and regenerated once.

The output of this module is a `SiteFacts` dataclass that downstream
prompts/generators can rely on without re-parsing HTML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field
from typing import Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from waa.analysis import conversion_audit


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class SiteFacts:
    """Concrete, quotable facts about a site that can anchor an email."""

    url: str
    h1: Optional[str] = None
    primary_cta_text: Optional[str] = None
    primary_cta_destination: Optional[str] = None
    city_or_area: Optional[str] = None  # from location hint or page copy
    niche: Optional[str] = None
    niche_specific_present: list[str] = field(default_factory=list)
    niche_specific_missing: list[str] = field(default_factory=list)
    surprising_finding: Optional[str] = None
    high_confidence_finding: Optional[str] = None  # one Finding to anchor on
    business_case: Optional[str] = None  # money-framed angle for the email lead
    design_smells: list[str] = field(default_factory=list)  # heuristic dated-design labels (#7)
    design_score: Optional[int] = None  # 0-10 heuristic design health (#7)
    booking_field_count: Optional[int] = None
    has_phone_clickable: Optional[bool] = None
    has_clear_h1: Optional[bool] = None

    def fact_count(self) -> int:
        """How many distinct facts can the email actually reference?"""
        n = 0
        if self.h1:
            n += 1
        if self.primary_cta_text:
            n += 1
        if self.city_or_area:
            n += 1
        if self.niche_specific_present or self.niche_specific_missing:
            n += 1
        if self.surprising_finding:
            n += 1
        if self.high_confidence_finding:
            n += 1
        return n

    def is_personalizable(self) -> bool:
        """
        Need at least 3 distinct facts for a non-generic email.
        Lower threshold and you get templates. Higher and you skip too much.
        """
        return self.fact_count() >= 3

    def to_dict(self) -> dict:
        return asdict(self)

    def quotable_strings(self) -> list[str]:
        """
        Strings the email is allowed to reference verbatim, used by the
        validator to confirm grounding.

        ONLY genuinely on-page, language-neutral anchors belong here: the H1,
        the CTA label, the city, the booking-field count. Our own finding
        prose (surprising_finding, high_confidence_finding) and the English
        niche labels are deliberately excluded — forcing a Slovak email to
        quote an English sentence like "Phone number is shown but not
        tappable on mobile" verbatim produced broken, mixed-language copy.
        Those findings still reach the LLM via the prompt's {surprise} /
        {hi_finding} fields, which it paraphrases naturally in the target
        language.
        """
        out = []
        if self.h1:
            out.append(self.h1)
        if self.primary_cta_text:
            out.append(self.primary_cta_text)
        if self.city_or_area:
            out.append(self.city_or_area)
        if self.booking_field_count is not None:
            out.append(str(self.booking_field_count))
        return [s for s in out if s and len(s.strip()) >= 2]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _clean(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = re.sub(r"\s+", " ", text).strip()
    return t or None


def _short_enough(text: Optional[str], max_words: int = 12) -> Optional[str]:
    """Trim a candidate to at most N words, return None if empty."""
    if not text:
        return None
    words = text.split()
    if not words:
        return None
    return " ".join(words[:max_words])


def extract_facts(
    html: str,
    url: str,
    niche: str = "",
    location: str = "",
) -> SiteFacts:
    """
    Run the conversion audit and project its output onto a SiteFacts shape
    that prompts can reason about easily.
    """
    facts = SiteFacts(url=url, niche=(niche or None))

    if not html:
        return facts

    soup = BeautifulSoup(html, "lxml")
    audit = conversion_audit.audit_conversion(html, url, niche=niche, location=location)

    # H1 (the visitor's first signal of value prop)
    h1 = audit.above_fold.get("h1")
    facts.h1 = _short_enough(_clean(h1), max_words=15)
    facts.has_clear_h1 = audit.above_fold.get("has_clear_value_prop")

    # Primary CTA
    cta_text = audit.primary_cta.get("primary_cta_text")
    facts.primary_cta_text = _short_enough(_clean(cta_text), max_words=6)
    facts.primary_cta_destination = audit.primary_cta.get("primary_cta_destination")

    # Location / city
    if location:
        # Use the city portion of "Bratislava, SK" or "Scottsdale AZ"
        city = location.split(",")[0].split()[0]
        facts.city_or_area = city
    else:
        # Fall back to whatever showed up on the page
        city = _detect_city_from_page(soup)
        facts.city_or_area = city

    # Niche signals
    facts.niche_specific_present = list(audit.niche_check.get("present", []))[:3]
    facts.niche_specific_missing = list(audit.niche_check.get("missing", []))[:3]

    # Surprises
    surprises = audit.niche_check.get("surprises") or []
    facts.surprising_finding = surprises[0] if surprises else None

    # High-confidence anchor finding (preferred when we have one)
    hi_findings = audit.high_confidence_findings()
    if hi_findings:
        # Pick the most specific one, prefer non-niche surprises
        ordered = sorted(
            hi_findings,
            key=lambda f: (
                0 if f.category == "surprise" else
                1 if f.category == "local" else
                2 if f.category == "above_fold" else 3
            ),
        )
        facts.high_confidence_finding = ordered[0].detail

    # Booking friction (numeric reference)
    booking = audit.niche_check.get("booking") or {}
    facts.booking_field_count = booking.get("field_count")

    # Local signals
    facts.has_phone_clickable = audit.local.get("has_phone_clickable")

    # Business case (improvement #8): the most commercially compelling,
    # market-aware money angle for the email to lead with.
    from waa.analysis.business_case import BusinessCaseBuilder
    facts.business_case = BusinessCaseBuilder().summary_for_prompt(audit, niche) or None

    # Heuristic design smells (#7): free dated-design signals for the preview.
    design = audit.design or {}
    facts.design_score = design.get("score")
    facts.design_smells = [s.get("label", "") for s in design.get("smells", []) if s.get("label")]

    return facts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CITY_LIST_SK = [
    "Bratislava", "Košice", "Kosice", "Žilina", "Zilina", "Prešov", "Presov",
    "Banská Bystrica", "Banska Bystrica", "Trnava", "Nitra", "Trenčín", "Trencin",
    "Martin", "Poprad", "Liptov", "Tatry", "Piešťany", "Piestany", "Levice",
    "Senec", "Pezinok", "Malacky", "Dunajská Streda", "Dunajska Streda",
]


def _detect_city_from_page(soup: BeautifulSoup) -> Optional[str]:
    """Best-effort: scan visible text for a known city name."""
    text = soup.get_text(" ", strip=True)
    for city in _CITY_LIST_SK:
        if city in text:
            return city
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json
    from waa.discovery.scraper import fetch_html

    if len(sys.argv) < 2:
        print("Usage: python personalization.py <url> [niche] [location]")
        sys.exit(1)

    url = sys.argv[1]
    niche = sys.argv[2] if len(sys.argv) > 2 else ""
    location = sys.argv[3] if len(sys.argv) > 3 else ""
    if not url.startswith("http"):
        url = "https://" + url

    fetch = fetch_html(url)
    if not fetch.get("html"):
        print(f"ERROR: could not fetch {url} ({fetch.get('error')})")
        sys.exit(1)

    facts = extract_facts(fetch["html"], url, niche=niche, location=location)
    print(json.dumps(facts.to_dict(), indent=2, ensure_ascii=False))
    print(f"\nfact_count = {facts.fact_count()}")
    print(f"is_personalizable = {facts.is_personalizable()}")
