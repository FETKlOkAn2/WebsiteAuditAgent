"""
Owner / first-name extractor.

The single biggest cold-email reply-rate driver in industry research is
"Hi <FirstName>" instead of "Hi there" or no greeting at all.

We try, in order:

  1. Parse the email's local-part: `peter.kovac@biz.com` → "Peter"
  2. Look at scraped HTML for owner-style signals:
       - About / O nás page heading "John Smith, Founder" / "Peter Kováč, majiteľ"
       - Schema.org Person markup
       - Common "Meet the team" / "Our staff" blocks
  3. Snippet from search results that mention an owner

The returned name is a single first-name string suitable for direct
greeting. We deliberately do NOT guess — if confidence is low, return None
and let the email be addressed neutrally rather than wrong-name (which is
worse than no name).
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config


# ---------------------------------------------------------------------------
# Email local-part parsing
# ---------------------------------------------------------------------------

_GENERIC_LOCALS = {
    "info", "contact", "hello", "office", "admin", "support",
    "sales", "marketing", "press", "owner", "ceo", "founder",
    "team", "staff", "mail", "email", "kontakt", "podpora",
    "objednavky", "rezervacia", "rezervacie", "obchod",
}

# Common SK + CZ + EN first names, lowercase — used for confidence scoring
# only. NOT exhaustive; only enough to avoid mis-extracting "tomas" vs "x".
_KNOWN_FIRST_NAMES = {
    # SK male
    "tomas", "tomáš", "peter", "marek", "michal", "ján", "jan", "milan",
    "lukas", "lukáš", "patrik", "martin", "andrej", "matúš", "matus",
    "filip", "jakub", "miroslav", "miro", "jozef", "jaro", "stano",
    "stanislav", "vladimír", "vlado", "rado", "radoslav", "samo", "samuel",
    "dominik", "richard", "rišo", "rado", "branislav", "brano", "erik",
    "adam", "boris", "daniel", "denis", "david", "dávid", "fero", "ferdinand",
    "gabo", "gabriel", "ivan", "igor", "ján", "ján", "július", "kamil",
    "karol", "kristián", "ladislav", "laco", "marián", "marian", "mário",
    "matej", "metod", "miloš", "milos", "miloš", "miroslav", "nikolas",
    "norbert", "ondrej", "oliver", "pavol", "pavel", "rastislav", "rasťo",
    "robert", "robo", "roman", "róbert", "róbert", "róbert", "samuel",
    "sebastián", "šimon", "simon", "štefan", "stefan", "tibor", "tomáš",
    "tomas", "vasil", "vavrinec", "vincent", "vladimír", "viktor", "vlado",
    "vojtech", "voloďa", "zoltán", "zoltan",
    # SK female
    "anna", "andrea", "alena", "barbora", "beáta", "barbara", "bibiana",
    "blanka", "bohuslava", "dana", "daniela", "denisa", "diana", "dominika",
    "elena", "eva", "evka", "gabika", "gabriela", "hana", "helena", "henrieta",
    "ivana", "iveta", "ivana", "jana", "janka", "jarmila", "jolana", "katarína",
    "katarina", "katka", "klaudia", "kristína", "kristina", "lenka", "lubica",
    "ľubica", "lucia", "lucka", "magdaléna", "magda", "margita", "mária",
    "maria", "marianna", "marta", "martina", "michaela", "miška", "miska",
    "milada", "miriam", "mirka", "monika", "natália", "natalia", "nikola",
    "oľga", "olga", "petra", "renáta", "renata", "romana", "silvia",
    "simona", "stanislava", "soňa", "sona", "tatiana", "terézia", "terezia",
    "veronika", "viera", "vlasta", "zdenka", "zdena", "zlata", "zora", "zuzana",
    # EN/global common (just enough)
    "john", "michael", "david", "james", "robert", "mark", "paul", "richard",
    "thomas", "chris", "matt", "matthew", "andrew", "andy", "alex",
    "alexander", "sarah", "jennifer", "lisa", "mary", "patricia", "linda",
    "elizabeth", "barbara", "susan", "jessica", "anna", "rachel", "rebecca",
}


def first_name_from_email(email: str) -> Optional[tuple[str, str]]:
    """
    Try to derive a first-name from an email's local-part.
    Returns (name, confidence) tuple or None.

      peter.kovac@biz.com           → ("Peter", "high")
      pkovac@biz.com                → None  (initial + surname, unreliable)
      info@biz.com                  → None  (role account)
      tomas@biz.com                 → ("Tomas", "high") if in known names
      johnsmith@biz.com             → ("John", "medium") if recognised prefix
    """
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0].lower()
    if local in _GENERIC_LOCALS:
        return None

    # Try splitting on common separators: peter.kovac, peter_kovac, peter-kovac
    for sep in (".", "_", "-"):
        if sep in local:
            first = local.split(sep, 1)[0]
            if _looks_like_first_name(first):
                return (_titlecase(first), "high")
            break

    # Bare name? "tomas@biz.com"
    if local.isalpha() and 3 <= len(local) <= 15:
        if local in _KNOWN_FIRST_NAMES:
            return (_titlecase(local), "high")
        # Looks like a single name but unknown — skip
        return None

    # Names glued together: "johnsmith", "peterkovac"
    # Try a prefix scan against known names
    for n in sorted(_KNOWN_FIRST_NAMES, key=len, reverse=True):
        if local.startswith(n) and len(local) > len(n):
            return (_titlecase(n), "medium")

    return None


# ---------------------------------------------------------------------------
# HTML / page parsing
# ---------------------------------------------------------------------------

_ABOUT_PATHS = [
    "/about", "/about-us", "/about_us", "/team", "/o-nas", "/o_nas",
    "/onas", "/kontakt", "/contact", "/contact-us", "/profile",
    "/about-me", "/o-mne",
]

# Patterns that surface ownership in copy
_OWNER_LINE_RE = re.compile(
    r"\b(?:i'm|i am|my name is|som|volám sa|volam sa|jmenuji se|som)\s+"
    r"([A-ZČĎĽŇŠŤŽÁÉÍÓÚÝ][a-zčďľňšťžáéíóúýA-ZČĎĽŇŠŤŽÁÉÍÓÚÝ]+)",
    re.I,
)

_TEAM_NAME_RE = re.compile(
    r"\b([A-ZČĎĽŇŠŤŽÁÉÍÓÚÝ][a-zčďľňšťžáéíóúý]{2,}\s+[A-ZČĎĽŇŠŤŽÁÉÍÓÚÝ][a-zčďľňšťžáéíóúý]{2,})"
    r"\s*[,\-–—]\s*"
    r"(?:founder|owner|ceo|majite[ľl]ka?|zakladate[ľl]ka?|riadite[ľl]ka?|director)",
    re.I,
)


def first_name_from_html(html: str, base_url: str) -> Optional[tuple[str, str]]:
    """
    Look for owner / founder name patterns on the page.
    Returns (name, confidence) or None.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")

    # 1. Schema.org Person / Organization with founder
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            import json
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("founder", "owner", "author"):
                val = item.get(key)
                if isinstance(val, dict):
                    n = val.get("name")
                    if n and isinstance(n, str):
                        first = n.strip().split()[0]
                        if _looks_like_first_name(first):
                            return (_titlecase(first), "high")
                elif isinstance(val, str):
                    first = val.strip().split()[0]
                    if _looks_like_first_name(first):
                        return (_titlecase(first), "high")

    text = soup.get_text(" ", strip=True)

    # 2. "Som Peter Kováč" / "I'm John Smith"
    m = _OWNER_LINE_RE.search(text)
    if m:
        first = m.group(1).strip().split()[0]
        if _looks_like_first_name(first):
            return (_titlecase(first), "high")

    # 3. "Peter Kováč, majiteľ" / "John Smith, Founder"
    m = _TEAM_NAME_RE.search(text)
    if m:
        first = m.group(1).strip().split()[0]
        if _looks_like_first_name(first):
            return (_titlecase(first), "medium")

    return None


def find_owner_name(
    html: str,
    base_url: str,
    contact_emails: list[str] | None = None,
    follow_about: bool = True,
) -> Optional[dict]:
    """
    Top-level owner-name finder.

    Args:
        html: homepage HTML (already fetched)
        base_url: site root
        contact_emails: emails extracted from the page (best-prioritised first)
        follow_about: if True, additionally fetch /about|/o-nas etc

    Returns:
        {"name": "Peter", "confidence": "high|medium", "source": "email|html|about_page"}
        or None.
    """
    # 1. Best signal: a personal email on the page
    for email in (contact_emails or []):
        hit = first_name_from_email(email)
        if hit:
            name, conf = hit
            return {"name": name, "confidence": conf, "source": "email"}

    # 2. Homepage HTML
    hit = first_name_from_html(html, base_url)
    if hit:
        name, conf = hit
        return {"name": name, "confidence": conf, "source": "homepage"}

    if not follow_about:
        return None

    # 3. Fetch About / Contact pages — that's where owner bios usually live
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    for path in _ABOUT_PATHS:
        try:
            resp = requests.get(
                urljoin(root, path),
                headers=config.HEADERS,
                timeout=8,
                allow_redirects=True,
                verify=False,
            )
            if resp.status_code != 200 or not resp.text:
                continue
            hit = first_name_from_html(resp.text, base_url)
            if hit:
                name, conf = hit
                return {"name": name, "confidence": conf, "source": f"about_page:{path}"}
        except requests.RequestException:
            continue

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_first_name(token: str) -> bool:
    if not token:
        return False
    t = token.lower().strip()
    if len(t) < 3 or len(t) > 20:
        return False
    if not t.replace("'", "").replace("-", "").isalpha():
        return False
    if t in _GENERIC_LOCALS:
        return False
    return t in _KNOWN_FIRST_NAMES


def _titlecase(name: str) -> str:
    return name[:1].upper() + name[1:].lower()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json
    from scraper import fetch_html, extract_contact_emails

    if len(sys.argv) < 2:
        print("Usage: python owner_finder.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url

    fetch = fetch_html(url)
    html = fetch.get("html") or ""
    emails = extract_contact_emails(html, url) if html else []
    print(f"Emails found: {emails}")
    result = find_owner_name(html, url, contact_emails=emails)
    print(json.dumps(result, indent=2))
