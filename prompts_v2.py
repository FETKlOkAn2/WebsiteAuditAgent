"""
Prompts v2 — fact-grounded, language-aware, conversion-tuned.

Why a v2:
- v1 prompts produced templated emails because their input was templated
  (PageSpeed numbers, mostly the same across sites).
- v2 prompts take a SiteFacts object and FORCE the LLM to quote at least
  one fact verbatim. If it doesn't, the orchestrator (analyzer_v2) rejects
  the output and retries once.

What changed for conversion (this revision):
- First-name greeting when owner is known (industry: ~2× reply rate).
- CTAs rewritten away from "want me to send what I found?" pattern, which
  is the single most overused cold-email line in 2026. Replaced with
  curiosity loops, binary asks, or specific time anchors.
- Expanded banned phrases list — the previous list missed "comprehensive",
  "tailored", "just wanted to", "just reaching out", "circling back",
  "touching base", etc. — every dead corporate filler that signals
  "this is a template".
- Examples are now SURPRISE-FIRST (Lorem ipsum, copyright year, broken
  button) rather than performance-first (LCP, mobile load). Performance
  findings are pattern-matched by every other agency; surprises feel
  human-spotted.
- Subject lines vary in case style (lowercase, sentence case) rather than
  always lowercase — the always-lowercase trick is itself a tell now.

Two languages:
- "sk" — Slovak, default for the SK pipeline.
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
2. Predmet: 3–6 slov. Nemusí byť celý malými písmenami — môžeš použiť
   štandardné veľké písmená na začiatku ako pri bežnom emaile. Bez
   dvojbodiek. Nepoužívaj klišé ako "krátka správa", "rýchla otázka".
3. Prvý riadok:
   - Ak vieš meno príjemcu ("{owner_first_name}"), začni "Ahoj {owner_first_name},"
     alebo "Dobrý deň {owner_first_name},". (Tykať pri salónoch/kaviarňach,
     vykať pri advokátoch/účtovníkoch/zubároch.) Hneď za oslovením prvá
     veta = konkrétne pozorovanie z webu.
   - Ak meno NEVIEŠ ({owner_first_name} = "neznáme"), preskoč oslovenie
     úplne a začni rovno pozorovaním. Nepíš "Dobrý deň," ani "Zdravím,".
4. Posledný riadok: iba `{sender_name}`. Nič iné — bez podpisu, bez
   pozdravu, bez "S pozdravom".
5. Presne JEDNA otázka v celom emaile, na konci, ako CTA.
6. Žiadne výkričníky.
7. Email musí citovať ASPOŇ JEDEN z nasledujúcich faktov DOSLOVA:
   {quotable_facts}. Ak nezacituješ, výstup zahodím.

## CTA — toto je najdôležitejšie
Najslabšie CTA je: "Mám vám poslať detail?" / "Chcete to vedieť?" / "Mám
vám to poslať?" Toto NIKDY nepoužívaj. Recipient nemá dôvod odpovedať
— path of least resistance je ignorovať.

POUŽI niektorú z týchto silnejších verzií:
- KONKRÉTNY ČAS:    "Stihnem to opraviť do piatka. Mám sa pustiť?"
- BINÁRNE: ÁNO/NIE: "Je to vedome takto, alebo to mám opraviť?"
- ZVEDAVOSTNÁ:      "Je tam dôvod prečo to nechávate ako-je?"
- LOOP-CLOSE:       "Stojí to vás N zákazníkov mesačne (odhadom). Záujem?"
- LOW-COMMIT CALL:  "Štvrtok o 11:00 — 10 minút na telefón sa hodí?"

CTA musí byť konkrétne, nech recipient vie čo presne odpoveďou potvrdí.

## TÓN
Píšeš ako Slovák Slovákovi — krátko, priamo, bez marketingovej omáčky.
Bez ospravedlňovania ("prepáčte za vyrušenie"). Bez sebachvály ("špecializujem
sa na…"). Bez budúcich časov ("rád by som"). Píš v prítomnosti
a aktívnym hlasom.

## ZAKÁZANÉ FRÁZY
"len pár sekúnd vášho času", "rád by som", "dovoľte mi", "prepáčte za
vyrušenie", "len chcem", "dúfam že sa Vám darí", "ozývam sa Vám",
"obraciam sa na Vás", "S pozdravom", "Pekný deň", "Krásny deň",
"chcel som sa opýtať", "neviem či ste si všimli", "v rýchlosti",
"komplexné riešenie", "individuálny prístup", "moja špecializácia je".

## ZAKÁZANÉ SLOVÁ
optimalizácia, synergia, efektivita, transformácia, posunúť na ďalšiu
úroveň, leverage, growth, komplexný, profesionálny prístup,
moderné riešenie, prispôsobiť na mieru.

## STRATÉGIA — vyber JEDEN uhol
- POZOROVANIE: Zacituj konkrétny prvok zo stránky (H1, tlačidlo, mesto,
  niečo nezvyčajné), poveď čo na ňom nesedí, a čo to znamená pre biznis.
- OTÁZKA: Spýtaj sa na konkrétny prvok ("Je to schválne, že...?").
- POROVNANIE: "Väčšina {niche_sk} v {city} má X. Vy nie."

## PRÍKLADY (študuj dĺžku, tón, CTA)

Príklad A — SURPRISE FIRST (Lorem ipsum, neznáme meno príjemcu):
Predmet: Lorem ipsum na úvodnej
Na vašej úvodnej je v sekcii "O nás" stále zaparkovaný Lorem ipsum text — tri odseky.\\n\\nVidno to hneď ako človek scrolluje pod hero. Pre {niche_sk} v {city} to vyzerá ako "web je v rozrobe", čo nový zákazník číta ako "možno už nefungujú".\\n\\nJe to vedome, alebo to mám dnes opraviť?\\n\\n{sender_name}

Príklad B — POZOROVANIE + LOOP-CLOSE (vieme meno):
Predmet: Tlačidlo Rezervovať
Ahoj Peter, všimol som si že tlačidlo "Rezervovať" na vašom webe vedie naspäť hore, neotvára formulár.\\n\\nNa mobile to znamená že každý kto chce rezervovať, odíde skôr ako sa dostane k akémukoľvek formuláru. Pre {niche_sk} v {city} to môže byť pár stratených rezervácií denne.\\n\\nMám sa pozrieť čo to spôsobuje?\\n\\n{sender_name}

Príklad C — POROVNANIE + BINÁRNE (vieme meno, vykanie):
Predmet: Telefón ako text
Dobrý deň Mária, telefónne číslo na vašom webe je len text — nedá sa na neho na mobile kliknúť a zavolať.\\n\\nVäčšina {niche_sk} v {city} to má ako "tel:" link, takže návštevník stlačí číslo a hneď volá. U vás musí číslo opísať.\\n\\nMám vám to opraviť, alebo to nechávate vedome?\\n\\n{sender_name}

Príklad D — OTÁZKA + KONKRÉTNY ČAS (neznáme meno):
Predmet: Copyright 2019
V päte vášho webu je "© 2019". Pri scrollovaní dole hľadať kontakt to človek vidí ako prvé.\\n\\nNeviem či je to zámer alebo nedopatrenie, ale pri {niche_sk} si nový zákazník hneď pomyslí "fungujú ešte?" — a zatvorí to.\\n\\nViem to opraviť dnes večer, ak chcete poslať detail?\\n\\n{sender_name}

## FOLLOW-UP (druhý email, ak prvý nedostal odpoveď do 4 dní)
Reply na pôvodný thread. Pod 30 slov. Pridaj JEDNU novú konkrétnu vec
zo stránky. Nepýtaj sa znova to isté — daj iný uhol.

Príklad follow-upu pre prípad A:
"Ešte k tomu emailu — všimol som si že váš formulár má 7 políčok. Pri väčšine {niche_sk} sú 2-3. Mám sa pozrieť na oboje?\\n\\n{sender_name}"

## VÝSTUP (iba JSON, žiadny markdown)
{{
  "subject_line": "3-6 slov, prirodzená kapitalizácia",
  "email_body": "Telo emailu (50-80 slov). Použi \\\\n pre nový riadok. Skonč iba menom: {sender_name}.",
  "follow_up_subject": "Re: pôvodný predmet",
  "follow_up_body": "Pod 30 slov. Nová konkrétna vec. Skonč {sender_name}."
}}

## VSTUP
URL: {url}
Biznis: {site_name}
Niche (SK): {niche_sk}
Mesto: {city}
Meno príjemcu (ak vieš): {owner_first_name}
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
# English prompt
# ---------------------------------------------------------------------------

EMAIL_PROMPT_EN = """\
You're writing a short cold email from a freelance web consultant who just
looked at the prospect's site and noticed something specific. Goal: get a
REPLY. Not a sale.

## HARD RULES (any violation = failure)
1. EXACTLY 50–80 words in the body. Count them.
2. Subject: 3–6 words, natural capitalisation (NOT forced all-lowercase —
   that's a known cold-email tell now). No colons. No clickbait.
3. First line:
   - If you know the recipient's first name ("{owner_first_name}"), start
     with "Hi {owner_first_name}," and immediately the first specific
     observation on the next line. Drop the name if you don't know it.
   - If unknown ({owner_first_name} = "unknown"), skip the greeting
     entirely and lead with the observation. NO "Hi there", NO "Hello,".
4. Last line: just the sender's first name (`{sender_name}`). Nothing
   else — no sign-off, no title, no "Best", no "Cheers".
5. Exactly ONE question in the entire email, at the end, as the CTA.
6. No exclamation marks.
7. The email MUST quote at least ONE of these facts verbatim:
   {quotable_facts}

## CTA — most important part
The weakest CTA is "Want me to send what I found?" or "Worth a look?"
These get ignored — the recipient has no reason to engage, and saying
"yes" obligates them to receive even more content.

USE one of these stronger versions instead:
- TIME-ANCHORED:   "I can fix it by Friday. Want me to?"
- BINARY YES/NO:   "Is that intentional, or should I fix it?"
- CURIOSITY LOOP:  "Any reason you're leaving it that way?"
- LOSS QUANTIFY:   "Costs you maybe N customers a month. Worth fixing?"
- CALL ASK:        "Free for a 10-min call Thursday at 11?"

CTAs should make replying easy — make a clear ask the recipient can
respond to in 5 words.

## TONE
Like you're texting someone who runs a small business about something
you spotted on their site. Direct. No apologising ("sorry to bother
you"). No selling ("I specialise in…"). No future tense ("I'd love
to"). Present tense, active voice.

## BANNED PHRASES (any of these = failure)
"no strings attached", "happy to", "I'd love to", "I came across",
"I noticed", "hope this finds you", "quick question", "free audit",
"mini-audit", "complimentary", "just reaching out", "just wanted to",
"circling back", "touching base", "wanted to share", "thought you'd
find this interesting", "comprehensive solution", "tailored approach",
"professional results", "expert team", "synergies".

## BANNED WORDS
leverage, synergy, optimize, growth, elevate, boost, enhance,
streamline, comprehensive, robust, scalable, world-class, cutting-edge,
seamlessly, holistic.

## ANGLE — pick ONE
- OBSERVATION: quote a specific weird element (Lorem ipsum, old
  copyright year, broken button), say what it costs.
- QUESTION: ask if a specific element is intentional.
- COMPARISON: "Most {niche} in {city} have X. Yours doesn't."

## EXAMPLES (study length, tone, CTA — note the surprise-first angle)

Example A — SURPRISE FIRST (unknown name):
Subject: Lorem ipsum on homepage
The 'About' section on your homepage still has Lorem ipsum text — three paragraphs of it, right under the hero.\\n\\nIt's the first thing visitors see after scrolling. For a {niche} in {city}, that reads as "site under construction" and "maybe they're not operating anymore".\\n\\nIs that on purpose, or should I fix it today?\\n\\n{sender_name}

Example B — OBSERVATION + LOSS-QUANTIFY (known name):
Subject: Book Now button broken
Hi Peter, your "Book Now" button on the homepage scrolls back to the top instead of opening a form.\\n\\nOn mobile, that means anyone tapping it bounces before they even see a booking field. For {niche} in {city}, you're probably losing a handful of bookings a day.\\n\\nShould I take a look at what's causing it?\\n\\n{sender_name}

Example C — COMPARISON + BINARY (known name):
Subject: Phone shown as text
Hi Maria, the phone number on your site is text only — visitors can't tap to call from mobile.\\n\\nMost {niche} in {city} have it as a "tel:" link so customers tap and dial directly. Yours forces them to copy the number.\\n\\nIntentional, or should I fix it?\\n\\n{sender_name}

Example D — QUESTION + TIME-ANCHORED (unknown name):
Subject: Copyright still says 2019
The footer on your site still says "© 2019". It's the first thing visitors see when they scroll down looking for contact info.\\n\\nNo idea if that's intentional, but for a {niche} that gives the impression you might not be active.\\n\\nI can fix it tonight if you want the details — yes or no?\\n\\n{sender_name}

## FOLLOW-UP (sent 4 days later if no reply, threaded as a reply)
Under 30 words. Add ONE new specific finding from the site. Different
angle from the first email. Don't repeat the original ask.

Example follow-up for case A:
"One more thing — your contact form has 7 fields. Most {niche} use 2-3. Want me to look at both?\\n\\n{sender_name}"

## OUTPUT (JSON only, no markdown)
{{
  "subject_line": "3-6 words, natural capitalisation",
  "email_body": "Full email (50-80 words). Use \\\\n for line breaks. End with just {sender_name}.",
  "follow_up_subject": "Re: <original subject>",
  "follow_up_body": "Under 30 words. New concrete finding. End with {sender_name}."
}}

## INPUT
URL: {url}
Business: {site_name}
Niche: {niche}
City: {city}
Recipient first name (if known): {owner_first_name}
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
