"""
Slovak email polish — deterministic grammar fixes applied to a generated email.

Two tells made the cold emails read as machine-written, regardless of how good
the LLM prompt was:

1. Wrong city case after a preposition: "advokát v Zilina" / "v Banska" instead
   of the locative "v Žiline" / "v Banskej Bystrici". A Slovak never writes
   "v Zilina"; it is an instant non-native / AI signal.
2. No greeting at all — the email dove straight into the observation, which
   reads as abrupt and robotic for Slovak business correspondence.

The LLM prompt now also asks for both, but models slip on Slovak declension, so
this module is the deterministic safety net: a final pass that guarantees the
city is in the correct locative form and that the body opens with a greeting.

Pure functions, no LLM / network — fully unit-testable.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional


# ---------------------------------------------------------------------------
# City data
# ---------------------------------------------------------------------------

# normalized (ascii, lower) nominative -> full locative phrase WITH preposition.
# The preposition is "vo" before Zvolen/Svit-style clusters, "v" otherwise.
_CITY_LOCATIVE: dict[str, str] = {
    "bratislava": "v Bratislave",
    "kosice": "v Košiciach",
    "zilina": "v Žiline",
    "presov": "v Prešove",
    "banska bystrica": "v Banskej Bystrici",
    "trnava": "v Trnave",
    "nitra": "v Nitre",
    "trencin": "v Trenčíne",
    "poprad": "v Poprade",
    "martin": "v Martine",
    "zvolen": "vo Zvolene",
    "piestany": "v Piešťanoch",
    "prievidza": "v Prievidzi",
    "nove zamky": "v Nových Zámkoch",
    "dunajska streda": "v Dunajskej Strede",
    "spisska nova ves": "v Spišskej Novej Vsi",
    "liptovsky mikulas": "v Liptovskom Mikuláši",
    "ruzomberok": "v Ružomberku",
    "michalovce": "v Michalovciach",
    "levice": "v Leviciach",
    "humenne": "v Humennom",
    "bardejov": "v Bardejove",
}

# normalized -> proper-diacritics nominative (for matching what the model wrote).
_CITY_NOMINATIVE: dict[str, str] = {
    "bratislava": "Bratislava",
    "kosice": "Košice",
    "zilina": "Žilina",
    "presov": "Prešov",
    "banska bystrica": "Banská Bystrica",
    "trnava": "Trnava",
    "nitra": "Nitra",
    "trencin": "Trenčín",
    "poprad": "Poprad",
    "martin": "Martin",
    "zvolen": "Zvolen",
    "piestany": "Piešťany",
    "prievidza": "Prievidza",
    "nove zamky": "Nové Zámky",
    "dunajska streda": "Dunajská Streda",
    "spisska nova ves": "Spišská Nová Ves",
    "liptovsky mikulas": "Liptovský Mikuláš",
    "ruzomberok": "Ružomberok",
    "michalovce": "Michalovce",
    "levice": "Levice",
    "humenne": "Humenné",
    "bardejov": "Bardejov",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _norm(text: str) -> str:
    """Diacritic-insensitive, lower, single-spaced key for city lookup."""
    return re.sub(r"\s+", " ", _strip_diacritics(text or "")).strip().lower()


def locative_phrase(city: str) -> Optional[str]:
    """Return e.g. 'v Žiline' for a known city, else None."""
    return _CITY_LOCATIVE.get(_norm(city))


def proper_nominative(city: str) -> str:
    """Return the properly-accented nominative for a known city, else the input
    trimmed (so a greeting/label still reads cleanly)."""
    return _CITY_NOMINATIVE.get(_norm(city), (city or "").strip())


# A body is considered already greeted if it opens with one of these.
_GREETING_RE = re.compile(
    r"^\s*(dobr[ýy]\s+de[ňn]|ahoj|zdrav[íi]m|[čc]au|vitajte|hi[ ,]|hello|hey[ ,])",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public polish functions
# ---------------------------------------------------------------------------

def fix_city_phrases(text: str, city: str) -> str:
    """
    Correct the "preposition + nominative city" error to the proper locative.

    Only touches the specific mistake (a nominative city right after v/vo);
    genitive ('zo Žiliny') or standalone uses are left alone. Also fixes the
    preposition itself (v Zvolen -> vo Zvolene). Unknown cities are untouched.
    """
    phrase = locative_phrase(city)
    if not text or not phrase:
        return text

    norm = _norm(city)
    # Match the city as the model may have written it: ascii, proper-diacritic,
    # or exactly as passed in.
    forms = {
        re.escape(norm),
        re.escape(proper_nominative(city)),
        re.escape((city or "").strip()),
    }
    alt = "|".join(sorted((f for f in forms if f), key=len, reverse=True))
    # [Vv][oO]? matches v / vo / V / Vo; then the nominative city form.
    pattern = re.compile(rf"\b[Vv][oO]?\s+(?:{alt})\b", re.IGNORECASE)
    return pattern.sub(phrase, text)


def ensure_greeting(body: str, owner_first_name: Optional[str] = None,
                    lang: str = "sk") -> str:
    """
    Guarantee the body opens with a greeting. Idempotent: if it already starts
    with one, the body is returned unchanged. Uses the first name when known.
    """
    if not body or not body.strip():
        return body
    if _GREETING_RE.match(body):
        return body

    name = (owner_first_name or "").strip()
    if lang == "en":
        greet = f"Hi {name}," if name else "Hi,"
    else:
        greet = f"Dobrý deň, {name}," if name else "Dobrý deň,"
    return f"{greet}\n\n{body.lstrip()}"


def polish_email(*, body: str, subject: str = "", follow_up_body: str = "",
                 city: str = "", owner_first_name: Optional[str] = None,
                 lang: str = "sk") -> dict:
    """
    Apply all deterministic fixes to a generated email and return the cleaned
    parts. The follow-up is a reply inside the same thread, so it gets the city
    fix but NOT a greeting (a second 'Dobrý deň' mid-thread reads worse).
    """
    subject = fix_city_phrases(subject, city)
    body = fix_city_phrases(body, city)
    body = ensure_greeting(body, owner_first_name, lang)
    follow_up_body = fix_city_phrases(follow_up_body, city)
    return {"subject": subject, "body": body, "follow_up_body": follow_up_body}
