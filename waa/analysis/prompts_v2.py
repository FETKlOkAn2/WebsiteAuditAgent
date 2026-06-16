"""
Prompts v2 — fact-grounded, language-aware, conversion-tuned.

Why a v2:
- v1 prompts produced templated emails because their input was templated
  (PageSpeed numbers, mostly the same across sites).
- v2 prompts take a SiteFacts object and FORCE the LLM to quote at least
  one fact verbatim. If it doesn't, the orchestrator (analyzer_v2) rejects
  the output and retries once.

Conversion rules baked in:
- First-name greeting when the owner is known (industry: ~2x reply rate).
- CTAs must VARY and open a conversation about redesigning the WHOLE site
  (higher revenue), not about fixing one feature. The single observed
  problem is just the hook that proves we actually looked.
- NO DASHES. The em dash, en dash, and spaced hyphen are a strong "written
  by AI" tell; the prompt forbids them and analyzer.strip_ai_dashes removes
  any that slip through.
- Owner first name wired in; natural subject capitalisation.

Two languages: "sk" (default for the SK pipeline) and "en".
"""

# ---------------------------------------------------------------------------
# Slovak prompt
# ---------------------------------------------------------------------------

EMAIL_PROMPT_SK = """\
Píšeš krátky cold email od Slováka, ktorý robí weby a všimol si niečo
konkrétne na webe potenciálneho klienta. Cieľ: získať ODPOVEĎ a otvoriť
rozhovor o prerobení celej stránky. Nepredávaš jednu opravu.

## TVRDÉ PRAVIDLÁ (každé porušenie = neúspech)
1. PRESNE 50 až 80 slov v tele emailu. Spočítaj ich.
2. Predmet: 3 až 6 slov, bežná kapitalizácia ako v normálnom emaile. Bez
   dvojbodiek. Žiadne klišé ("krátka správa", "rýchla otázka").
3. Prvý riadok:
   - Ak vieš meno príjemcu ("{owner_first_name}"), začni "Ahoj {owner_first_name},"
     alebo "Dobrý deň {owner_first_name},". (Tykať pri salónoch/kaviarňach,
     vykať pri advokátoch/účtovníkoch/zubároch.) Hneď nato prvá veta =
     konkrétne pozorovanie z webu.
   - Ak meno NEVIEŠ ({owner_first_name} = "neznáme"), preskoč oslovenie
     a začni rovno pozorovaním. Nepíš "Dobrý deň," ani "Zdravím,".
4. Posledný riadok: iba `{sender_name}`. Nič iné, bez "S pozdravom".
5. Presne JEDNA otázka v celom emaile, na konci, ako CTA.
6. Žiadne výkričníky.
7. ŽIADNE POMLČKY. Nikdy nepouži znak dlhej pomlčky, strednej pomlčky, ani
   spojovník s medzerami okolo (" - ") ako oddeľovač viet. Je to typický
   znak AI textu. Použi čiarku, bodku, alebo vetu rozdeľ. Spojovník vnútri
   slova (e-mail, on-line) je v poriadku.
8. Email musí citovať ASPOŇ JEDEN z faktov DOSLOVA: {quotable_facts}.
   Ak nezacituješ, výstup zahodím.

## RÁMEC: jedna chyba ako vstup k prerobeniu celej stránky
Konkrétny nález je len HÁČIK, ktorý dokazuje, že si web reálne pozrel. Cieľ
je väčší: naznač, že tá vec je symptóm a že celá stránka by si zaslúžila
obnoviť alebo prerobiť, aby reálne privádzala zákazníkov. NEvymenúvaj 5
chýb (to zahltí). Spomeň JEDNU konkrétnu, potom jemne otvor tému celého
webu. Pýtaj sa na rozhovor o stránke, nie na opravu jednej featury.

## VEĎ PENIAZMI, NIE BUGOM
Nepíš len ČO je zle, ale prečo to majiteľa stojí ZÁKAZNÍKOV alebo peniaze,
jazykom jeho trhu. Toto je tvoj hlavný uhol (parafrázuj vlastnými slovami,
NEopisuj doslova): {business_case}
Samotný technický bug (chýba H1, alt-texty) nikoho nezaujíma; zaujme strata
zákazníkov a to, že web nevyzerá/nefunguje ako má pre JEHO konkrétny biznis.

## CTA, striedaj ich, NIKDY tú istú vetu
Zakázané (slabé a opakované): "Mám vám poslať detail?", "Chcete to vedieť?",
a najmä "Je to vedome takto, alebo to mám opraviť?" Túto vetu NIKDY nepouži.

Vyber a obmieňaj medzi týmito uhlami (mierené na celý web, nie featuru):
- "Koľko zákazníkov vám cez web mesačne príde? Možno necháte dosť na stole."
- "Kedy ste web naposledy prerábali? Pri {niche_sk} sa to oplatí raz za pár rokov."
- "Stálo by za to pozrieť sa na celú stránku, nie len toto. Máte 10 minút?"
- "Spravil som si poznámky aj k zvyšku webu. Pošlem ich, alebo to neriešite?"
- "Ak web teraz neprivádza rezervácie ako by mohol, viem pomôcť. Zavoláme?"

## TÓN
Slovák Slovákovi. Krátko, priamo, bez marketingovej omáčky. Bez
ospravedlňovania. Bez sebachvály. Prítomný čas, aktívny hlas.

## GRAMATIKA
Mesto skloňuj správne v lokáli: "v Bratislave", "v Košiciach", "v Žiline",
"v Prešove". NIKDY nepíš "v Bratislava" ani "v Kosice". Píš s diakritikou.

## ZAKÁZANÉ FRÁZY
"len pár sekúnd vášho času", "rád by som", "dovoľte mi", "prepáčte za
vyrušenie", "dúfam že sa Vám darí", "ozývam sa Vám", "obraciam sa na Vás",
"S pozdravom", "Pekný deň", "chcel som sa opýtať", "v rýchlosti",
"komplexné riešenie", "individuálny prístup", "moja špecializácia je",
"Je to vedome takto, alebo to mám opraviť".

## ZAKÁZANÉ SLOVÁ
optimalizácia, synergia, efektivita, transformácia, leverage, growth,
komplexný, profesionálny prístup, moderné riešenie, na mieru.

## PRÍKLADY (študuj dĺžku, tón, ŽIADNE pomlčky, rôzne CTA, rámec celého webu)

Príklad A (Lorem ipsum, neznáme meno):
Predmet: Lorem ipsum na úvodnej
Na vašej úvodnej je v sekcii "O nás" stále Lorem ipsum text, tri odseky. Vidno to hneď pod hlavičkou.\\n\\nPre {niche_sk} v {city} to pôsobí ako rozrobený web a nový zákazník to číta ako "možno už nefungujú". Úprimne, celá stránka by si zaslúžila osviežiť, nielen tento kúsok.\\n\\nKedy ste web naposledy prerábali?\\n\\n{sender_name}

Príklad B (tlačidlo, vieme meno):
Predmet: Tlačidlo Rezervovať
Ahoj Peter, tlačidlo "Rezervovať" na vašom webe vedie naspäť hore, neotvára formulár. Na mobile tým strácate rezervácie každý deň.\\n\\nJe to symptóm toho, že web už nestíha dobu. Pri {niche_sk} dnes rozhoduje, či stránka naozaj privádza hostí.\\n\\nStálo by za to pozrieť sa na celú stránku, máte 10 minút na telefón?\\n\\n{sender_name}

Príklad C (telefón ako text, vieme meno, vykanie):
Predmet: Telefón na webe
Dobrý deň Mária, telefónne číslo na webe je len text, na mobile sa naň nedá kliknúť a zavolať. Návštevník ho musí prepisovať.\\n\\nVäčšina {niche_sk} v {city} má dnes web, ktorý volanie aj rezerváciu zvládne na jeden klik. Vaša stránka by to vedela tiež.\\n\\nKoľko hostí vám cez web mesačne príde?\\n\\n{sender_name}

Príklad D (copyright, neznáme meno):
Predmet: Web pôsobí staršie
V päte máte "© 2019". To je prvé, čo človek vidí keď scrolluje dole hľadať kontakt, a hneď si pomyslí "fungujú ešte?".\\n\\nNie je to o jednom roku v pätičke, skôr o tom, že celý web pôsobí staršie ako vaša prevádzka v skutočnosti je.\\n\\nSpravil som si poznámky aj k zvyšku stránky, pošlem ich?\\n\\n{sender_name}

## FOLLOW-UP (druhý email, ak prvý nedostal odpoveď do 4 dní)
Reply na pôvodné vlákno. Pod 30 slov. Pridaj JEDNU novú konkrétnu vec zo
stránky a opäť otvor tému celého webu. Žiadne pomlčky. Iné CTA než v prvom.

Príklad follow-upu:
"Ešte k tomu, váš formulár má 7 políčok, väčšina {niche_sk} má 2 až 3. Spolu s tým prvým to vyzerá, že web by chcel poriadnu obnovu. Zavoláme na 10 minút?\\n\\n{sender_name}"

## VÝSTUP (iba JSON, žiadny markdown)
DÔLEŽITÉ: vnútri textových hodnôt NIKDY nepoužívaj rovné úvodzovky ("). Ak chceš
niečo odcitovať (napr. názov tlačidla), použi typografické „ a “ alebo apostrof.
Inak sa JSON pokazí.
{{
  "subject_line": "3 až 6 slov, prirodzená kapitalizácia, žiadne pomlčky",
  "email_body": "Telo (50 až 80 slov). Použi \\\\n pre nový riadok. Žiadne pomlčky. Skonč iba menom: {sender_name}.",
  "follow_up_subject": "Re: pôvodný predmet",
  "follow_up_body": "Pod 30 slov. Nová konkrétna vec + téma celého webu. Žiadne pomlčky. Skonč {sender_name}."
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
Biznis dopad (veď týmto): {business_case}
Tieto fakty SI MÔŽEŠ citovať doslova: {quotable_facts}

DÔLEŽITÉ: Aspoň jeden zo `quotable_facts` musí byť v emaile DOSLOVA. Žiadne pomlčky. Inak je výstup neplatný.
"""


# ---------------------------------------------------------------------------
# English prompt
# ---------------------------------------------------------------------------

EMAIL_PROMPT_EN = """\
You're writing a short cold email from a freelance web consultant who just
looked at the prospect's site and spotted something specific. Goal: get a
REPLY and open a conversation about redesigning the WHOLE site. You're not
selling one fix.

## HARD RULES (any violation = failure)
1. EXACTLY 50 to 80 words in the body. Count them.
2. Subject: 3 to 6 words, natural capitalisation. No colons. No clickbait.
3. First line:
   - If you know the recipient's first name ("{owner_first_name}"), start
     "Hi {owner_first_name}," then the specific observation. Drop the name
     if you don't know it.
   - If unknown ({owner_first_name} = "unknown"), skip the greeting and
     lead with the observation. NO "Hi there", NO "Hello,".
4. Last line: just the sender's first name (`{sender_name}`). Nothing else.
5. Exactly ONE question in the entire email, at the end, as the CTA.
6. No exclamation marks.
7. NO DASHES. Never use an em dash, en dash, or a spaced hyphen (" - ") as
   sentence punctuation; it's a strong "written by AI" tell. Use a comma, a
   period, or split the sentence. Intra-word hyphens (e-mail, Wi-Fi) are OK.
8. The email MUST quote at least ONE of these facts verbatim:
   {quotable_facts}

## FRAME: one flaw as the way into a full redesign
The specific finding is just the HOOK that proves you actually looked. The
goal is bigger: hint that it's a symptom and that the whole site is due for
a refresh/redesign so it actually brings in customers. Do NOT list 5
problems (that overwhelms). Name ONE concrete thing, then gently open the
whole-site conversation. Ask for a conversation about the site, not a
one-feature fix.

## LEAD WITH MONEY, NOT THE BUG
Don't just say WHAT is wrong; say why it costs the owner CUSTOMERS or money,
in their market's terms. This is your main angle (paraphrase it, do not copy
verbatim): {business_case}
A raw technical bug (missing H1, alt text) interests nobody; lost customers
and a site that doesn't work for THEIR business does.

## CTA, vary it, NEVER repeat the same line
Banned (weak, overused): "Want me to send what I found?", "Worth a look?".
Vary across these whole-site angles:
- "How many customers come through the site each month? Might be leaving money on the table."
- "When did you last rebuild the site? For {niche} it's worth doing every few years."
- "Worth looking at the whole site, not just this. Got 10 minutes?"
- "I jotted notes on the rest of the site too. Want them, or not a priority?"

## BANNED PHRASES
"no strings attached", "happy to", "I'd love to", "I came across",
"hope this finds you", "quick question", "free audit", "just reaching out",
"circling back", "touching base", "comprehensive solution", "tailored
approach".

## BANNED WORDS
leverage, synergy, optimize, growth, elevate, boost, enhance, streamline,
comprehensive, robust, world-class, cutting-edge, seamlessly.

## EXAMPLES (note: NO dashes, varied CTA, whole-site frame)

Example A (Lorem ipsum, unknown name):
Subject: Lorem ipsum on homepage
The 'About' section on your homepage still has Lorem ipsum text, three paragraphs of it, right under the header.\\n\\nFor a {niche} in {city} that reads as "site under construction", and visitors wonder if you're still operating. Honestly the whole site is due for a refresh, not just this bit.\\n\\nWhen did you last rebuild the site?\\n\\n{sender_name}

Example B (broken button, known name):
Subject: Book Now button
Hi Peter, your "Book Now" button scrolls back to the top instead of opening a form. On mobile you're losing bookings every day.\\n\\nIt's a symptom that the site has fallen behind. For {niche} today the site is what decides whether visitors actually book.\\n\\nWorth looking at the whole thing, got 10 minutes?\\n\\n{sender_name}

## FOLLOW-UP (sent 4 days later if no reply, threaded)
Under 30 words. Add ONE new concrete finding and reopen the whole-site
angle. No dashes. Different CTA from the first email.

## OUTPUT (JSON only, no markdown)
IMPORTANT: inside string values NEVER use the straight double-quote character
("). If you need to quote something (e.g. a button label), use single quotes or
typographic quotes “ and ”. Otherwise the JSON breaks.
{{
  "subject_line": "3 to 6 words, natural capitalisation, no dashes",
  "email_body": "Full email (50 to 80 words). Use \\\\n for line breaks. No dashes. End with just {sender_name}.",
  "follow_up_subject": "Re: <original subject>",
  "follow_up_body": "Under 30 words. New concrete finding + whole-site angle. No dashes. End with {sender_name}."
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
Business impact (lead with this): {business_case}
Facts you MAY quote verbatim: {quotable_facts}

CRITICAL: At least one of `quotable_facts` MUST appear verbatim. No dashes.
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
