"""
Prompts v2 — fact-grounded, language-aware.

Why a v2:
- v1 prompts produced templated emails because their input was templated
  (PageSpeed numbers, mostly the same across sites).
- v2 prompts take a SiteFacts object and FORCE the LLM to quote at least
  one fact verbatim. If it doesn't, the orchestrator (analyzer_v2) rejects
  the output and retries once.

Two languages:
- "sk" — Slovak, default for the SK pipeline. Tone: tykanie, krátko, bez
  marketingovej omáčky. Subject line in lowercase Slovak.
- "en" — kept for the legacy US targets, but rewritten with the same
  fact-grounding constraint.
"""

# ---------------------------------------------------------------------------
# Slovak prompt
# ---------------------------------------------------------------------------

EMAIL_PROMPT_SK = """\
Píšeš krátky cold email od Slováka, ktorý spravuje weby a všimol si niečo
konkrétne na webe potenciálneho klienta. Cieľ: získať ODPOVEĎ. Nie predaj.

## TVRDÉ PRAVIDLÁ (každé porušenie = neúspech)
1. PRESNE 50–80 slov v tele emailu. Spočítaj ich.
2. Predmet: 3–5 slov, malými písmenami okrem vlastných mien. Bez dvojbodiek.
3. Prvý riadok: konkrétne pozorovanie z webu. Bez "Dobrý deň", bez "Zdravím".
4. Posledný riadok: iba meno odosielateľa (`{sender_name}`). Nič iné.
5. Presne JEDNA otázka v celom emaile, na konci, ako CTA.
6. Žiadne výkričníky.
7. Email musí citovať ASPOŇ JEDEN z nasledujúcich faktov DOSLOVA: {quotable_facts}.
   Ak nezacituješ, výstup zahodím.
8. Zakázané frázy: "len pár sekúnd vášho času", "rád by som", "dovoľte mi",
   "prepáčte za vyrušenie", "len chcem", "dúfam že sa Vám darí".
9. Zakázané slová: optimalizácia, synergia, efektivita, transformácia,
   posunúť na ďalšiu úroveň, leverage, growth.

## TÓN
Píšeš ako keď napíšeš kamarátovi že si si všimol niečo na jeho webe. Krátko,
priamo, bez marketingovej omáčky. Ako keby si práve teraz pozrel ich stránku.
Tykanie OK ak pôsobí prirodzene; vykanie tiež OK — vyber podľa kontextu.

## STRATÉGIA
Vyber JEDEN uhol:
- POZOROVANIE: Zacituj konkrétny prvok zo stránky (H1, tlačidlo, mesto,
  niečo nezvyčajné), poveď čo na ňom nesedí, a čo to môže stáť.
- OTÁZKA: Spýtaj sa na konkrétny prvok ("Je to schválne, že...?").
- POROVNANIE: "Väčšina {niche_sk} v {city} má X. Vy nie."

## PRÍKLADY (študuj dĺžku a tón)

Príklad A (pozorovanie):
Predmet: tlačidlo nikam nevedie
Všimol som si, že tlačidlo "Rezervovať" na vašej úvodnej stránke vedie naspäť hore — neotvára formulár ani rezervačný systém.\\n\\nPre {niche_sk} v {city} to znamená, že každý kto chce rezervovať na mobile, odíde skôr ako sa dostane k formuláru.\\n\\nMôžem vám poslať čo presne treba opraviť?\\n\\n{sender_name}

Príklad B (otázka):
Predmet: copyright 2019
Na vašom webe je v päte uvedené © 2019. To je prvá vec, ktorú človek vidí keď scrolluje dole hľadať kontakt.\\n\\nViem že to znie ako maličkosť, ale pri {niche_sk} si nový zákazník hneď pomyslí "fungujú ešte?".\\n\\nChcete to vedieť opraviť spolu s pár ďalšími vecami?\\n\\n{sender_name}

Príklad C (porovnanie):
Predmet: telefón sa nedá kliknúť
Telefónne číslo na vašom webe je len text — nedá sa naňho kliknúť na mobile.\\n\\nVäčšina {niche_sk} v {city} to má ako "tel:" link, takže človek priamo zavolá. U vás musí číslo opísať.\\n\\nPodľa mojich odhadov to znamená pár stratených hovorov denne. Mám vám poslať detail?\\n\\n{sender_name}

## VÝSTUP (iba JSON, žiadny markdown)
{{
  "subject_line": "3–5 slov, malé písmená",
  "email_body": "Telo emailu (50–80 slov). Použi \\\\n pre nový riadok. Skonč iba menom: {sender_name}.",
  "follow_up_subject": "Re: pôvodný predmet",
  "follow_up_body": "Pod 30 slov. Odkáž na prvý email, pridaj jednu novú konkrétnu vec, skonč {sender_name}."
}}

## VSTUP
URL: {url}
Biznis: {site_name}
Niche (SK): {niche_sk}
Mesto: {city}
H1 z webu: "{h1}"
Hlavné tlačidlo: "{primary_cta}"
Telefón klikateľný: {phone_clickable}
Niche-špecifické chýba: {niche_missing}
Niche-špecifické má: {niche_present}
Nezvyčajný detail: {surprise}
Hlavné zistenie (high-confidence): {hi_finding}
Tieto fakty SI MÔŽEŠ citovať doslova: {quotable_facts}

DÔLEŽITÉ: Aspoň jeden zo `quotable_facts` musí byť v emaile DOSLOVA. Inak je výstup neplatný.
"""


# ---------------------------------------------------------------------------
# English prompt (kept for compatibility)
# ---------------------------------------------------------------------------

EMAIL_PROMPT_EN = """\
You're writing a short cold email from a freelance web consultant who just
looked at the prospect's site and noticed something specific. Goal: get a
REPLY. Not a sale.

## HARD RULES (any violation = failure)
1. EXACTLY 50–80 words in the body. Count them.
2. Subject: 3–5 words, lowercase except proper nouns. No colons.
3. First line: a specific observation from the site. No "Hi", no "Hey".
4. Last line: just the sender's first name (`{sender_name}`). Nothing else.
5. Exactly ONE question in the entire email, at the end, as the CTA.
6. No exclamation marks.
7. The email MUST quote at least ONE of these facts verbatim:
   {quotable_facts}
8. Banned phrases: "no strings attached", "happy to", "I'd love to",
   "I came across", "I noticed", "hope this finds you", "quick question",
   "free audit", "mini-audit", "complimentary".
9. Banned words: leverage, synergy, optimize, growth, elevate, boost,
   enhance, streamline.

## TONE
Like you're texting a friend about something you spotted on their site.
Short sentences. No sales energy. Casual but respectful.

## ANGLE — pick ONE
- OBSERVATION: quote a specific element (H1, button text, copyright year,
  something unusual), say what's off, name the cost.
- QUESTION: ask about a specific element ("Is it intentional that...?").
- COMPARISON: "Most {niche} sites in {city} have X. Yours doesn't."

## OUTPUT (JSON only, no markdown)
{{
  "subject_line": "3–5 lowercase words",
  "email_body": "Full email (50–80 words). Use \\\\n for line breaks. End with just {sender_name}.",
  "follow_up_subject": "Re: <original subject>",
  "follow_up_body": "Under 30 words. Reference the first email, add one new concrete thing, end with {sender_name}."
}}

## INPUT
URL: {url}
Business: {site_name}
Niche: {niche}
City: {city}
Site H1: "{h1}"
Main CTA: "{primary_cta}"
Phone clickable: {phone_clickable}
Niche elements missing: {niche_missing}
Niche elements present: {niche_present}
Unusual detail: {surprise}
Top finding (high-confidence): {hi_finding}
Facts you MAY quote verbatim: {quotable_facts}

CRITICAL: At least one of `quotable_facts` MUST appear verbatim in the body.
Otherwise the output is rejected.
"""


# ---------------------------------------------------------------------------
# Niche translation table — feeds the {niche_sk} placeholder
# ---------------------------------------------------------------------------

NICHE_TRANSLATIONS_SK = {
    "restauracia": "reštaurácie",
    "kaviaren": "kaviarne",
    "fitness centrum": "fitness centrá",
    "joga studio": "joga štúdiá",
    "crossfit": "crossfit boxy",
    "kadernictvo": "kaderníctva",
    "barber shop": "barber shopy",
    "nechtove studio": "nechtové štúdiá",
    "kozmeticky salon": "kozmetické salóny",
    "masaze": "masážne salóny",
    "zubar": "zubné ambulancie",
    "zubna ambulancia": "zubné ambulancie",
    "ortodoncia": "ortodoncie",
    "fyzioterapia": "fyzioterapeutické centrá",
    "chiropraktik": "chiropraktici",
    "optika": "optiky",
    "veterina": "veterinárne ambulancie",
    "hotel": "hotely",
    "penzion": "penzióny",
    "wellness": "wellness centrá",
    "autoservis": "autoservisy",
    "pneuservis": "pneuservisy",
    "karoseria": "karosárne",
    "autoumyvaren": "autoumyvárne",
    "instalater": "inštalatéri",
    "elektrikar": "elektrikári",
    "murar": "murári",
    "malovanie a stierkovanie": "maliarske firmy",
    "zahradnik": "záhradnícke firmy",
    "upratovacia firma": "upratovacie firmy",
    "stahovacia firma": "sťahovacie firmy",
    "realitna kancelaria": "realitné kancelárie",
    "advokatska kancelaria": "advokátske kancelárie",
    "notar": "notárske úrady",
    "uctovnik": "účtovníci",
    "financny poradca": "finanční poradcovia",
    "poistovaci agent": "poisťovací agenti",
    "fotograf": "fotografi",
    "svadobny fotograf": "svadobní fotografi",
    "kvetinarstvo": "kvetinárstva",
    "cukraren": "cukrárne",
    "pekaren": "pekárne",
    "psia skola": "psie školy",
    "strihanie psov": "salóny pre psov",
    "detska skolka": "súkromné škôlky",
    "jazykova skola": "jazykové školy",
    "doucovanie": "doučovacie agentúry",
    "autoskola": "autoškoly",
    "tetovacie studio": "tetovacie štúdiá",
    "piano studio": "piano štúdiá",
    "hudobna skola": "hudobné školy",
    "tanecna skola": "tanečné školy",
}


def translate_niche(niche: str, lang: str) -> str:
    """Map a niche slug to a human-readable plural in the chosen language."""
    if not niche:
        return "businesses" if lang == "en" else "firmy"
    if lang == "sk":
        return NICHE_TRANSLATIONS_SK.get(niche.lower().strip(), niche)
    return niche
