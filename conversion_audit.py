"""
Conversion-grade audit.

Replaces the developer-focused signals (PageSpeed score, missing meta tags) with
findings a business owner actually cares about: where customers leave, what's
missing above the fold, how many steps to book, whether trust signals exist.

Each finding includes a `confidence` so the email generator can prefer
high-confidence references — never invent or stretch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup, Tag


# ---------------------------------------------------------------------------
# Niche signatures — what a well-built site in this niche should/might have
# ---------------------------------------------------------------------------

NICHE_SIGNATURES: dict[str, dict] = {
    "medspa": {
        "expected": [
            ("before/after gallery", ["before", "after", "results", "gallery"]),
            ("treatment menu", ["botox", "filler", "laser", "facial", "treatment"]),
            ("online booking", ["book", "schedule", "appointment", "consultation"]),
            ("pricing or financing", ["pricing", "financing", "cherry", "afterpay", "klarna"]),
            ("provider credentials", ["MD", "RN", "PA-C", "NP", "board certified"]),
        ],
    },
    "dentist": {
        "expected": [
            ("insurance accepted", ["insurance", "accept", "ppo", "delta dental"]),
            ("emergency/same-day", ["emergency", "same day", "urgent"]),
            ("online booking", ["book", "schedule", "appointment", "request"]),
            ("financing", ["financing", "carecredit", "payment plan"]),
            ("doctor bio", ["dr.", "doctor", "DDS", "DMD", "about our"]),
        ],
    },
    "plumber": {
        "expected": [
            ("emergency hotline", ["24/7", "emergency", "same day"]),
            ("service area", ["serving", "service area", "we cover"]),
            ("free estimate", ["free estimate", "free quote", "no obligation"]),
            ("licensed/bonded", ["licensed", "bonded", "insured"]),
            ("phone clickable", ["tel:"]),
        ],
    },
    "restaurant": {
        "expected": [
            ("menu visible", ["menu", "view menu"]),
            ("reservations", ["reserve", "book a table", "opentable", "resy"]),
            ("hours", ["hours", "open", "monday", "sunday"]),
            ("delivery", ["delivery", "doordash", "grubhub", "ubereats"]),
        ],
    },
    "lawyer": {
        "expected": [
            ("free consultation", ["free consultation", "free case review"]),
            ("practice areas", ["practice areas", "areas of practice"]),
            ("results/case wins", ["verdict", "settlement", "results", "won"]),
            ("attorney bio", ["attorney", "esq", "j.d.", "about"]),
        ],
    },
    "gym": {
        "expected": [
            ("free trial", ["free trial", "free pass", "first class free"]),
            ("class schedule", ["schedule", "classes", "timetable"]),
            ("pricing", ["pricing", "membership", "join"]),
            ("trainer bios", ["trainers", "coaches", "team"]),
        ],
    },
    "salon": {
        "expected": [
            ("online booking", ["book", "schedule", "vagaro", "booksy"]),
            ("services + pricing", ["services", "pricing", "haircut", "color"]),
            ("stylist team", ["stylists", "team", "our staff"]),
            ("portfolio", ["gallery", "portfolio", "instagram"]),
        ],
    },
    "chiropractor": {
        "expected": [
            ("first visit special", ["first visit", "new patient", "special"]),
            ("conditions treated", ["back pain", "neck pain", "sciatica", "conditions"]),
            ("online booking", ["book", "schedule", "appointment"]),
            ("doctor bio", ["dr.", "doctor", "DC", "chiropractor"]),
        ],
    },
    "optometrist": {
        "expected": [
            ("insurance accepted", ["insurance", "vsp", "eyemed"]),
            ("eyewear brands", ["frames", "ray-ban", "oakley", "designer"]),
            ("online booking", ["book", "schedule", "exam"]),
        ],
    },
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    """One specific observation about the site."""
    category: str          # above_fold | cta | trust | social_proof | niche | local | surprise
    label: str             # short human label, e.g. "primary CTA below the fold"
    detail: str            # specific evidence, owner-readable
    confidence: str        # high | medium | low
    impact: str = ""       # business impact, plain English
    quote: Optional[str] = None  # verbatim text from the site, if any

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConversionAudit:
    url: str
    niche: str
    findings: list[Finding] = field(default_factory=list)
    above_fold: dict = field(default_factory=dict)
    primary_cta: dict = field(default_factory=dict)
    trust: dict = field(default_factory=dict)
    niche_check: dict = field(default_factory=dict)
    local: dict = field(default_factory=dict)

    def high_confidence_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.confidence == "high"]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "niche": self.niche,
            "findings": [f.to_dict() for f in self.findings],
            "above_fold": self.above_fold,
            "primary_cta": self.primary_cta,
            "trust": self.trust,
            "niche_check": self.niche_check,
            "local": self.local,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACTION_VERBS = {
    # English
    "book", "schedule", "request", "reserve", "get", "call", "contact",
    "start", "begin", "try", "apply", "join", "shop", "buy", "order",
    "claim", "download", "subscribe", "sign up", "consult", "appoint",

    "rezervovať", "rezervovat", "objednať", "objednat", "kontakt",
    "kontaktovať", "kontaktovat", "zavolať", "zavolat", "napísať",
    "napisat", "požiadať", "poziadat", "prihlásiť", "prihlasit",
    "registrovať", "registrovat", "zaregistrovať", "zaregistrovat",
    "kúpiť", "kupit", "vyžiadať", "vyziadat", "dohodnúť", "dohodnut",
    "objednávka", "objednavka", "rezervácia", "rezervacia", "stiahnuť",
    "stiahnut",

    "buchen", "anfragen", "termin",
}

GENERIC_REVIEW_PATTERNS = [
    re.compile(r"^great (service|experience|place)!?$", re.I),
    re.compile(r"^highly recommend!?$", re.I),
    re.compile(r"^amazing!?$", re.I),
    re.compile(r"^[a-z]\.\s*$", re.I), 
]


def _visible_text(el: Tag) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _first_meaningful_section(soup: BeautifulSoup, char_budget: int = 1500) -> str:
    """
    Approximate what's 'above the fold' from a flat HTML perspective.
    We can't know real viewport without rendering, so we use the first
    ~1500 chars of visible body text as a proxy.
    """
    body = soup.find("body")
    if not body:
        return ""
    text = body.get_text(" ", strip=True)
    return text[:char_budget]


def _is_action_text(text: str) -> bool:
    text = (text or "").strip().lower()
    if not text or len(text) > 40:
        return False
    return any(verb in text for verb in ACTION_VERBS)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_above_fold(soup: BeautifulSoup) -> dict:
    """
    Identify what a visitor sees first: H1, hero subheadline, primary CTA.
    """
    above = _first_meaningful_section(soup, 1500)
    h1 = soup.find("h1")
    h1_text = _visible_text(h1)

    # Hero CTAs — buttons/links in the first ~2000 chars of HTML source
    html_str = str(soup)[:8000]  # source-order proxy for "above fold"
    early_soup = BeautifulSoup(html_str, "lxml")
    early_buttons = early_soup.find_all(["a", "button"])
    early_actions = [
        _visible_text(b) for b in early_buttons
        if _is_action_text(_visible_text(b))
    ]

    has_value_prop = bool(h1_text) and len(h1_text.split()) >= 3
    cta_visible_early = len(early_actions) > 0

    return {
        "h1": h1_text or None,
        "h1_word_count": len(h1_text.split()) if h1_text else 0,
        "has_clear_value_prop": has_value_prop,
        "early_actions": early_actions[:3],
        "cta_visible_above_fold": cta_visible_early,
        "above_fold_text_sample": above[:300],
    }


def detect_primary_cta(soup: BeautifulSoup, base_url: str) -> dict:
    """
    Find the most prominent action element on the page.
    Score candidates by: action-verb in text, presence in <header>, position
    in source, link to booking/contact target.
    """
    candidates: list[tuple[int, dict]] = []
    header = soup.find("header")

    for i, el in enumerate(soup.find_all(["a", "button"])[:60]):
        text = _visible_text(el)
        if not _is_action_text(text):
            continue

        score = 0
        score += max(0, 30 - i)  # earlier = higher
        if header and el in header.descendants:
            score += 25
        href = el.get("href", "") if el.name == "a" else ""
        target_lower = (href or "").lower()
        if any(k in target_lower for k in [
            "book", "schedule", "appoint", "contact", "consult", "reserve",
        ]):
            score += 15
        # Visual hint heuristics from class names
        cls = " ".join(el.get("class", [])).lower()
        if any(k in cls for k in ["primary", "cta", "btn-primary", "main-button"]):
            score += 10

        candidates.append((score, {
            "text": text,
            "href": urljoin(base_url, href) if href else None,
            "in_header": header is not None and el in header.descendants,
        }))

    candidates.sort(key=lambda x: -x[0])
    primary = candidates[0][1] if candidates else {
        "text": None, "href": None, "in_header": False,
    }
    return {
        "primary_cta_text": primary["text"],
        "primary_cta_destination": primary["href"],
        "primary_cta_in_header": primary["in_header"],
        "all_candidates": [c[1] for c in candidates[:5]],
    }


def detect_booking_friction(soup: BeautifulSoup) -> dict:
    """
    If there's a visible booking/contact form on the page, count its fields.
    Industry rule of thumb: under 4 fields = low friction.
    """
    forms = soup.find_all("form")
    if not forms:
        return {"form_present": False, "field_count": None, "verdict": None}

    best = None
    for form in forms:
        inputs = form.find_all(["input", "select", "textarea"])
        meaningful = [
            i for i in inputs
            if (i.get("type") or "").lower() not in ("hidden", "submit", "button", "reset")
        ]
        count = len(meaningful)
        # Prefer forms that look like booking/contact (heuristic)
        action = (form.get("action") or "").lower()
        text = _visible_text(form).lower()
        is_booking = any(k in (action + " " + text) for k in [
            "book", "schedule", "contact", "appointment", "consult", "reserve",
        ])
        score = count + (10 if is_booking else 0)
        if not best or score > best[0]:
            best = (score, count, is_booking)

    field_count = best[1]
    verdict = (
        "low" if field_count <= 3 else
        "medium" if field_count <= 6 else
        "high"
    )
    return {
        "form_present": True,
        "field_count": field_count,
        "looks_like_booking": best[2],
        "verdict": verdict,  # friction level
    }


def detect_trust_signals(soup: BeautifulSoup) -> dict:
    """
    Reviews, ratings, awards, credentials, years-in-business, real photos.
    """
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()

    # Years in business
    since_match = re.search(r"(since|established|founded)\s+(?:in\s+)?(\d{4})", text_lower)
    since_year = int(since_match.group(2)) if since_match else None

    # Star/rating mentions
    rating_match = re.search(r"(\d\.\d)\s*(?:out of\s*)?(?:/|stars?|★)", text_lower)
    rating_value = float(rating_match.group(1)) if rating_match else None

    # Review widgets
    has_google_reviews = any(s in str(soup).lower() for s in [
        "google reviews", "g.page", "google.com/maps",
    ])
    has_yelp = "yelp.com" in str(soup).lower()

    # Awards / "best of"
    award_match = re.search(r"(best of \w+|top \d+|award[- ]winning)", text_lower)
    award = award_match.group(0) if award_match else None

    # Credentials (medical/legal)
    creds = re.findall(
        r"\b(MD|DDS|DMD|DC|DO|RN|NP|PA-C|J\.D\.|Esq\.?)\b",
        text,
    )

    return {
        "since_year": since_year,
        "rating_value": rating_value,
        "has_google_reviews_embed": has_google_reviews,
        "has_yelp_link": has_yelp,
        "award_mention": award,
        "credentials_found": list(set(creds)),
    }


def detect_social_proof_quality(soup: BeautifulSoup) -> dict:
    """
    Are testimonials specific (real names + details) or generic?
    """
    # Find testimonial-ish blocks
    candidates = soup.find_all(
        attrs={"class": re.compile(r"testimonial|review|quote", re.I)}
    )
    if not candidates:
        # Try blockquotes too
        candidates = soup.find_all("blockquote")

    if not candidates:
        return {"present": False, "quality": None, "samples": []}

    samples = []
    generic_count = 0
    for c in candidates[:6]:
        txt = _visible_text(c)
        if not txt or len(txt) < 20:
            continue
        is_generic = any(p.match(txt) for p in GENERIC_REVIEW_PATTERNS) or len(txt.split()) < 6
        if is_generic:
            generic_count += 1
        samples.append({"text": txt[:200], "generic": is_generic})

    if not samples:
        return {"present": False, "quality": None, "samples": []}

    quality = (
        "specific" if generic_count == 0 else
        "mixed" if generic_count < len(samples) else
        "generic"
    )
    return {"present": True, "quality": quality, "samples": samples[:3]}


def detect_niche_elements(soup: BeautifulSoup, niche: str) -> dict:
    """
    For the given niche, check which expected elements are present/missing.
    """
    sig = NICHE_SIGNATURES.get((niche or "").lower().strip())
    if not sig:
        return {"checked": False, "present": [], "missing": []}

    text_lower = soup.get_text(" ", strip=True).lower()
    href_blob = " ".join((a.get("href") or "").lower() for a in soup.find_all("a"))
    haystack = text_lower + " " + href_blob

    present, missing = [], []
    for label, keywords in sig["expected"]:
        if any(kw in haystack for kw in keywords):
            present.append(label)
        else:
            missing.append(label)

    return {
        "checked": True,
        "niche": niche,
        "present": present,
        "missing": missing,
    }


def detect_local_relevance(soup: BeautifulSoup, hint: str = "") -> dict:
    """
    Is the city/service area visible? NAP — name, address, phone present?
    `hint` = optional location string (e.g. from prospector CSV).
    """
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()

    has_phone = bool(re.search(r"\+?\d[\d\-\s\(\)]{8,}\d", text))
    has_phone_clickable = bool(soup.find("a", href=re.compile(r"^tel:", re.I)))
    has_address = bool(soup.find(attrs={"itemtype": re.compile("PostalAddress", re.I)})) or \
                  bool(re.search(r"\d{1,5}\s+\w+\s+(street|st\.?|avenue|ave\.?|road|rd\.?|blvd)\b", text, re.I))

    city_in_copy = False
    city_match = None
    if hint:
        # Extract first word of hint (e.g. "Scottsdale AZ" -> "Scottsdale")
        first = hint.strip().split(",")[0].split()[0]
        if first and first.lower() in text_lower:
            city_in_copy = True
            city_match = first

    return {
        "has_phone": has_phone,
        "has_phone_clickable": has_phone_clickable,
        "has_address": has_address,
        "city_hint": hint,
        "city_appears_on_page": city_in_copy,
        "city_matched": city_match,
    }


def detect_surprises(soup: BeautifulSoup) -> list[str]:
    """
    Things worth pointing out in an email — unexpected, low-effort observations.
    Each returned string is owner-readable.
    """
    # Ordered most-compelling-first: downstream uses surprises[0] as the
    # email/screenshot anchor, so the weakest signal (stale copyright) goes
    # last and only surfaces when nothing better was found.
    surprises = []
    text = soup.get_text(" ", strip=True)
    text_lower = text.lower()

    # Lorem ipsum still on the page — devastating, rare
    if "lorem ipsum" in text_lower:
        surprises.append("Lorem ipsum placeholder text is still on the homepage.")

    # "Coming soon" — reads as "not really operating"
    if "coming soon" in text_lower:
        surprises.append("There's a 'Coming soon' section on the homepage.")

    # Phone shown but not tappable — direct lost-call cost on mobile
    has_phone = bool(re.search(r"\+?\d[\d\-\s\(\)]{8,}\d", text))
    has_phone_clickable = bool(soup.find("a", href=re.compile(r"^tel:", re.I)))
    if has_phone and not has_phone_clickable:
        surprises.append("Phone number is shown but not tappable on mobile.")

    # Empty alt text on hero images
    imgs = soup.find_all("img")
    no_alt_in_first_5 = sum(1 for img in imgs[:5] if not (img.get("alt") or "").strip())
    if no_alt_in_first_5 >= 3:
        surprises.append("The first images on the homepage have no alt text or descriptions.")

    # Outdated copyright — only if GENUINELY stale (3+ years), and LAST,
    # because a footer year is weak proof and was flooding every prospect.
    year_match = re.search(r"©\s*(\d{4})", text)
    if year_match:
        year = int(year_match.group(1))
        from datetime import datetime
        if year <= datetime.now().year - 3:
            surprises.append(f"Footer copyright still says {year}.")

    return surprises[:3]


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def audit_conversion(html: str, url: str, niche: str = "", location: str = "") -> ConversionAudit:
    """
    Run a conversion-grade audit on an HTML page.

    Returns a ConversionAudit with structured findings AND owner-readable
    Finding objects that can be referenced in cold emails.
    """
    soup = BeautifulSoup(html, "lxml")
    audit = ConversionAudit(url=url, niche=niche)

    audit.above_fold = detect_above_fold(soup)
    audit.primary_cta = detect_primary_cta(soup, url)
    audit.trust = detect_trust_signals(soup)
    audit.niche_check = detect_niche_elements(soup, niche)
    audit.local = detect_local_relevance(soup, location)

    booking = detect_booking_friction(soup)
    social = detect_social_proof_quality(soup)
    surprises = detect_surprises(soup)

    # ---- Build findings (the email can reference these) ----

    # Above-fold clarity
    if not audit.above_fold.get("has_clear_value_prop"):
        audit.findings.append(Finding(
            category="above_fold",
            label="weak or missing headline",
            detail=(
                "There's no clear H1 above the fold — the visitor lands "
                "and isn't immediately told what you do."
            ),
            confidence="high" if not audit.above_fold.get("h1") else "medium",
            impact="Most visitors decide whether to stay in 3-5 seconds.",
            quote=audit.above_fold.get("h1"),
        ))

    if not audit.above_fold.get("cta_visible_above_fold"):
        audit.findings.append(Finding(
            category="above_fold",
            label="no action button above the fold",
            detail="No book/schedule/contact action visible without scrolling.",
            confidence="medium",
            impact="Visitors who don't see a button on first view bounce twice as often.",
        ))

    # Primary CTA
    cta_text = audit.primary_cta.get("primary_cta_text")
    if cta_text:
        audit.primary_cta["cta_quote"] = cta_text  # for personalization

    # Booking friction
    if booking.get("form_present") and booking.get("verdict") == "high":
        audit.findings.append(Finding(
            category="cta",
            label="booking form has too many fields",
            detail=(
                f"The form on the homepage has {booking['field_count']} fields. "
                "Most high-converting sites use 2-3."
            ),
            confidence="high",
            impact="Each extra field cuts completion rate by roughly 10%.",
        ))

    # Trust signals
    has_any_review_signal = any([
        audit.trust.get("rating_value"),
        audit.trust.get("has_google_reviews_embed"),
        audit.trust.get("has_yelp_link"),
        social.get("present"),
    ])
    if not has_any_review_signal:
        audit.findings.append(Finding(
            category="trust",
            label="no visible reviews or social proof",
            detail="No star ratings, Google reviews embed, or testimonials found on the homepage.",
            confidence="medium",
            impact="Reviews are the #1 factor in local-business decisions.",
        ))
    elif social.get("present") and social.get("quality") == "generic":
        audit.findings.append(Finding(
            category="social_proof",
            label="testimonials feel generic",
            detail="The reviews on the page are short and unspecific (e.g. 'Great service!').",
            confidence="medium",
            impact="Vague reviews can hurt trust more than no reviews.",
        ))

    # Niche-specific gaps
    missing_elements = audit.niche_check.get("missing", [])
    if missing_elements:
        # Only the top 2 most-impactful as findings
        for m in missing_elements[:2]:
            audit.findings.append(Finding(
                category="niche",
                label=f"missing: {m}",
                detail=f"Most {niche or 'businesses in this category'} sites have {m} on the homepage. Yours doesn't.",
                confidence="medium",
                impact="Visitors expect to see this before they trust you.",
            ))

    # Local relevance
    if not audit.local.get("has_phone_clickable") and audit.local.get("has_phone"):
        audit.findings.append(Finding(
            category="local",
            label="phone number not tappable",
            detail="The phone number on the page is text — not a tel: link. Mobile visitors can't tap-to-call.",
            confidence="high",
            impact="On mobile, an extra step to call = lost calls.",
        ))

    if location and not audit.local.get("city_appears_on_page"):
        audit.findings.append(Finding(
            category="local",
            label=f"{location.split(',')[0]} not mentioned on the page",
            detail=f"The homepage doesn't reference {location} — bad for local trust and search.",
            confidence="medium",
            impact="Locals want to feel like the business is for them.",
        ))

    # Surprises (always low-friction, high-curiosity references)
    for s in surprises:
        audit.findings.append(Finding(
            category="surprise",
            label="notable detail",
            detail=s,
            confidence="high",
            impact="",
        ))

    # Stash detector output too (useful for downstream personalization)
    audit.niche_check["booking"] = booking
    audit.trust["social"] = social
    audit.niche_check["surprises"] = surprises

    return audit


# ---------------------------------------------------------------------------
# CLI for quick testing on a single URL
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json
    from scraper import fetch_html

    if len(sys.argv) < 2:
        print("Usage: python conversion_audit.py <url> [niche] [location]")
        sys.exit(1)

    url = sys.argv[1]
    niche = sys.argv[2] if len(sys.argv) > 2 else ""
    location = sys.argv[3] if len(sys.argv) > 3 else ""

    if not url.startswith("http"):
        url = "https://" + url

    print(f"Fetching {url}...")
    fetch = fetch_html(url)
    if not fetch.get("html"):
        print(f"ERROR: {fetch.get('error')}")
        sys.exit(1)

    audit = audit_conversion(fetch["html"], url, niche=niche, location=location)
    print(json.dumps(audit.to_dict(), indent=2))
