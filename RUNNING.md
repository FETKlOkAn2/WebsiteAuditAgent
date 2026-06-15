# Ako spustiť Website Audit Agent

Praktický návod — manuálne spustenie z počítača + automatické cez GitHub Actions.

---

## 0. Jednorazové nastavenie (len prvýkrát)

```bash
# v priečinku projektu
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # pre screenshoty (proof krok)
```

`.env` súbor musí mať (väčšinu už máš):

```
ANTHROPIC_API_KEY=sk-ant-...        # povinné (generovanie emailov)
SMTP_HOST=smtp.zoho.eu
SMTP_PORT=465
SMTP_EMAIL=tomasmaxim@emtdstudio.com
SMTP_PASSWORD=...                    # Zoho heslo / app password
IMAP_HOST=imap.zoho.eu               # pre monitor-replies
DISCORD_WEBHOOK_URL=...              # pre monitor-replies (voliteľné)
# voliteľní ďalší senderi:
SMTP_EMAIL_2=...  SMTP_PASSWORD_2=...   # Erik
SMTP_EMAIL_3=...  SMTP_PASSWORD_3=...   # Michal
```

> **Pozn.:** Serper API kľúč už **nepotrebuješ** — prospekty berieme z
> OpenStreetMap (zadarmo). Ak ho v `.env` necháš a dôjdu kredity, agent ho
> ticho preskočí.

Vo všetkých príkazoch nižšie je `python` = `.venv/bin/python`.

---

## 1. NAJPRV: pozri si čo by agent poslal (nič sa neodošle)

Toto je krok ktorý **vždy sprav ako prvý** pri novom niche/meste. Vygeneruje
emaily + proof screenshoty do jedného HTML, ktoré otvoríš v prehliadači.

```bash
.venv/bin/python audit_agent.py preview --niche restauracia --location Bratislava --count 5 --open
```

- `--niche` — typ biznisu po slovensky (`restauracia`, `kaviaren`, `zubar`,
  `kadernictvo`, `kvetinarstvo`, `fitness centrum`, …)
- `--location` — mesto (`Bratislava`, `Kosice`, `Zilina`, …)
- `--count` — koľko prospektov (default 5)
- `--open` — automaticky otvorí HTML v prehliadači

Otvor HTML, prejdi 5–10 výstupov očami. Pýtaj sa: *znie ten email ako od
človeka? je krúžok na screenshote na správnom mieste? je to problém ktorý
by majiteľa zaujímal?* Tu sa naučíš rozoznať dobrý lead od slabého.

**Nič sa neodosiela.** Toto je čistá kontrola.

---

## 2. Ostré odoslanie — manuálne, malá dávka

Keď si spokojný s kvalitou z kroku 1:

```bash
# DRY RUN — vygeneruje a ukáže, ale NEODOŠLE (bezpečné)
.venv/bin/python audit_agent.py campaign

# OSTRÉ ODOSLANIE — naozaj pošle
.venv/bin/python audit_agent.py campaign --send --confirm-send --email-limit 15
```

Čo `campaign` robí: prejde `agent_input.csv` (niche/mesto páry) →
OpenStreetMap nájde biznisy s webom → audit → vygeneruje SK email →
**overí adresu** (zahodí neexistujúce) → pošle → uloží postup.

Užitočné flagy:

| Flag | Čo robí |
|---|---|
| `--send --confirm-send` | naozaj odošle (bez nich = dry run) |
| `--email-limit 15` | max 15 mailov za beh (gentle ramp; default 40) |
| `--lang sk` | jazyk emailu (default `sk` pre campaign) |
| `--audit-mode v2` | fact-grounded prompty (default `v2`) |
| `--sender "Erik" --sender-full "Erik Pitorak"` | poslať za iného sendera |
| `--reset` | začať `agent_input.csv` odznova |
| `--allow-weekends` | povoliť víkendové odosielanie (default zakázané) |

> Víkendy sú automaticky preskočené (nízka reply rate). Cez víkend `campaign`
> spraví audit ale neodošle — drafty budú pripravené na pondelok.

---

## 3. Follow-upy (druhý dotyk — ~polovica odpovedí)

Pošli follow-up tým, čo do 4 dní neodpovedali. Vlákno sa správne pripojí
k pôvodnému emailu.

```bash
# Najprv ukáž koho by oslovil (nič neodošle)
.venv/bin/python audit_agent.py send-followups --dry-run

# Ostré
.venv/bin/python audit_agent.py send-followups --confirm-send
```

`--after-days 4` (default), `--max-per-run 20` (default). Kto už odpovedal,
sa preskočí automaticky (vďaka monitor-replies nižšie).

---

## 4. Sledovanie odpovedí (zdieľané cez Discord)

Skontroluje všetky 3 schránky (Tomas/Erik/Michal) a nové odpovede pošle do
Discord kanála, nech ich vidíte všetci.

```bash
# Test bez Discordu (vypíše do konzoly)
.venv/bin/python audit_agent.py monitor-replies --dry-run

# Ostré (treba DISCORD_WEBHOOK_URL v .env)
.venv/bin/python audit_agent.py monitor-replies
```

Keď ti niekto odpovie *„áno pošli ten screenshot"*, screenshot už máš
predpripravený v `output/screenshots/` (z kroku 1 alebo z `--screenshots`).
Priložíš ho do odpovede z vlastnej schránky.

---

## 5. Automaticky na GitHub Actions (beží samo)

Cron je **znovu zapnutý** (`.github/workflows/daily_campaign.yml`):

- **Po–Pia**, 3× denne, jeden sender na slot (Tomas 11:00, Erik 14:00,
  Michal 17:00 bratislavského času)
- Limit **15 mailov/sender/deň** (gentle ramp)
- Víkendy preskočené
- Stav (postup, odoslané, videné odpovede) sa drží medzi behmi cez artifacts

Druhý workflow `replies_monitor.yml` beží **každých 15 min** a posiela
odpovede do Discordu.

### Čo musíš mať v GitHub repo Secrets
`Settings → Secrets and variables → Actions`:

```
ANTHROPIC_API_KEY, SMTP_HOST, SMTP_PORT,
SMTP_EMAIL, SMTP_PASSWORD,                 # Tomas
SMTP_EMAIL_2, SMTP_PASSWORD_2,             # Erik (voliteľné)
SMTP_EMAIL_3, SMTP_PASSWORD_3,             # Michal (voliteľné)
DISCORD_WEBHOOK_URL                        # pre replies monitor
```

### Manuálne spustiť workflow z GitHubu
`Actions → Daily Campaign → Run workflow` → vyber sendera, prípadne zaškrtni
`dry_run`.

### Ako spomaliť / zrýchliť ramp
V `daily_campaign.yml` zmeň `RAMP="--email-limit 15"` (vyššie číslo = viac
mailov/deň). Prvý týždeň nechaj nízko.

### Ako zase vypnúť
Zakomentuj `schedule:` blok v `daily_campaign.yml` (workflow ostane
spustiteľný len manuálne cez `Run workflow`).

---

## Odporúčaný postup pre prvý týždeň

1. **Deň 0:** `preview` na 3–4 nichoch, pozri kvalitu. Uprav `agent_input.csv`
   ak treba (nechaj len málo saturované nichy: reštaurácia, kaviareň, fitko,
   kvetinárstvo).
2. **Deň 1:** lokálne `campaign --send --confirm-send --email-limit 10`,
   sleduj `monitor-replies`.
3. **Deň 1–5:** nechaj bežať GitHub cron (15/deň). Každé ráno `monitor-replies`
   alebo sleduj Discord.
4. **Deň 4+:** `send-followups --confirm-send`.
5. **Deň 7:** pozri reply rate. ≥5% → škáluj (zvýš `--email-limit`). <5% →
   ladíme copy/cielenie, nie objem.

---

## Rýchla referencia príkazov

```bash
# QA náhľad (nič neodošle)
python audit_agent.py preview --niche restauracia --location Bratislava --open

# Audit jednej stránky + screenshot
python audit_agent.py audit --url https://example.sk --audit-mode v2 --lang sk --niche restauracia --screenshots

# Kampaň (dry run / ostrá)
python audit_agent.py campaign
python audit_agent.py campaign --send --confirm-send --email-limit 15

# Follow-upy
python audit_agent.py send-followups --confirm-send

# Odpovede → Discord
python audit_agent.py monitor-replies
```
