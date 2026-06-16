# Website Audit Agent

Automated cold outreach pipeline for web agencies. Finds local businesses with bad websites, audits them for real issues, generates personalized cold emails, and sends them — all on autopilot.

Built by [EMTD Studio](https://emtdstudio.com) (Bratislava, Slovakia).

## How It Works

```
agent_input.csv (niches + cities)
        |
        v
   [ PROSPECT ]  Search Google/Serper for businesses
        |         Filter out directories, booking platforms
        |         Quick-qualify by red flags (score 0-100)
        v
   [ AUDIT ]     Fetch HTML + PageSpeed scores
        |         Check for contact email first (skip if none = save tokens)
        |         Send audit data to Claude for analysis
        v
   [ EMAIL ]     Claude generates personalized cold email
        |         References real issues found on their site
        v
   [ SEND ]      SMTP via Zoho Mail
        |         30s delay between sends
        |         Sent registry prevents duplicate outreach
        v
   output/*.json + *.csv (full results, CRM-ready)
```

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/FETKlOkAn2/WebsiteAuditAgent.git
cd WebsiteAuditAgent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — add your API keys (see Configuration below)

# 3. Run a single pipeline
python -m waa pipeline --niche "medspa" --location "Scottsdale" --send

# 4. Or run the full campaign (loops through all niches, respects daily limits)
python -m waa campaign --send --confirm-send
```

## Commands

### `campaign` — Daily autopilot (recommended)

Reads `agent_input.csv`, runs the full pipeline for each niche/location pair, stops when daily API limits are reached. Resume the next day — progress is tracked.

```bash
# Dry run (no emails sent)
python -m waa campaign

# Send emails for real
python -m waa campaign --send --confirm-send

# Reset progress and start from scratch
python -m waa campaign --reset

# Custom limits
python -m waa campaign --serper-limit 100 --email-limit 30
```

### `pipeline` — Single niche run

Find leads, audit them, optionally send emails — one command.

```bash
python -m waa pipeline --niche "dentist" --location "Miami FL" --send --confirm-send
python -m waa pipeline --niche "plumber" --location "Austin TX" --audit-top 5
```

### `prospect` — Find leads only

Search for businesses and score them by website quality.

```bash
python -m waa prospect --niche "medspa" --location "Dallas" --count 30
```

### `audit` — Analyze specific sites

Audit a CSV of URLs or a single site.

```bash
python -m waa audit sites.csv
python -m waa audit --url https://some-business.com
```

### `send` — Send emails from audit results

```bash
# Preview (dry run)
python -m waa send output/audit_results.json

# Actually send
python -m waa send output/audit_results.json --confirm-send
```

## Configuration

Copy `.env.example` to `.env` and fill in your keys:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...          # Claude API for analysis + email generation

# Search (need at least one)
SERPER_API_KEY=...                     # serper.dev — 2,500 free queries/month

# Performance analysis (optional but recommended)
PAGESPEED_API_KEY=...                  # Google PageSpeed Insights — 25,000/day free

# Email sending
SMTP_HOST=smtp.zoho.eu                # smtp.zoho.eu for EU, smtp.zoho.com for US
SMTP_PORT=465
SMTP_EMAIL=you@yourdomain.com
SMTP_PASSWORD=your-app-password        # Zoho app-specific password, not your login

# Defaults
LLM_MODEL=claude-sonnet-4-6
SEND_DELAY_SECONDS=30
SCRAPE_DELAY_SECONDS=1
PAGESPEED_DELAY_SECONDS=2
OUTPUT_DIR=output
```

### Getting API Keys

| Service | Free Tier | How to Get |
|---------|-----------|------------|
| Anthropic Claude | Pay per use (~$0.02/audit) | [console.anthropic.com](https://console.anthropic.com) |
| Serper.dev | 2,500 queries/month | [serper.dev](https://serper.dev) — sign up, get key |
| Google PageSpeed | 25,000/day | [Google Cloud Console](https://console.cloud.google.com) → Enable PageSpeed Insights API → Create credentials |
| Zoho Mail | 50 emails/day | [zoho.eu](https://www.zoho.eu/mail/) → Settings → App Passwords |

## GitHub Actions (Daily Automation)

The included workflow runs the campaign automatically every day. Supports up to 3 senders rotating throughout the day.

### Setup

1. Push the repo to GitHub
2. Go to **Settings > Secrets and variables > Actions**
3. Add secrets:

**Required (Sender 1 / main):**
- `ANTHROPIC_API_KEY`
- `PAGESPEED_API_KEY`
- `SERPER_API_KEY`
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_EMAIL`, `SMTP_PASSWORD`

**Optional (Sender 2):**
- `SERPER_API_KEY_2`, `SMTP_EMAIL_2`, `SMTP_PASSWORD_2`

**Optional (Sender 3):**
- `SERPER_API_KEY_3`, `SMTP_EMAIL_3`, `SMTP_PASSWORD_3`

### Schedule

| Time (UTC) | Time (CET) | Sender |
|------------|------------|--------|
| 09:00 | 11:00 | Tomas (main) |
| 12:00 | 14:00 | Sender 2 |
| 15:00 | 17:00 | Sender 3 |

Unconfigured senders are skipped automatically.

### Manual Trigger

Go to **Actions > Daily Campaign > Run workflow**. Options:
- Choose which sender to use
- Reset progress (start from scratch)
- Dry run (preview only, no emails sent)

## How Prospect Scoring Works

Each site gets a 0-100 score based on red flags:

| Signal | Points | Why it matters |
|--------|--------|----------------|
| No HTTPS | +15 | Looks unprofessional, security risk |
| No viewport meta | +15 | Not mobile-friendly |
| No analytics/tracking | +10 | They can't measure anything |
| No CTAs | +10 | No way to convert visitors |
| Slow response (>3s) | +10 | Visitors leave |
| Outdated copyright year | +10 | Site is neglected |
| No meta description | +10 | Bad for Google rankings |
| Thin content (<200 words) | +8 | Nothing for Google to index |
| Missing alt text (>50%) | +8 | Accessibility + SEO issue |
| No H1 tag | +8 | Poor page structure |
| No schema markup | +5 | Missing rich search results |
| No Open Graph tags | +5 | Looks bad when shared on social |
| Uses React/Next.js/etc. | -10 | Already modern — not a prospect |
| Uses Webflow/Shopify | -5 | Usually decent enough |

**Threshold: score >= 25 qualifies as a prospect.**

## Token-Saving: Email Check Before Audit

In `pipeline` and `campaign` modes, the tool checks for a contact email **before** running the expensive Claude analysis. If no email is found on the site, the LLM calls are skipped entirely — saving ~$0.02 per site.

Typical savings: 40-60% of sites have no extractable email, so this cuts Anthropic costs roughly in half.

## Sent Registry (Duplicate Prevention)

Every sent email is recorded in `output/sent_registry.json`. Before sending, the tool checks:
- Has this **email address** been contacted before?
- Has this **domain** been contacted before?

If either matches, the email is skipped. This works across all commands and persists between GitHub Actions runs.

## Filtered Domains

The prospector automatically skips 60+ non-business domains:
- Social media (Facebook, Instagram, TikTok, Reddit, etc.)
- Directories (Yelp, Yellow Pages, BBB, etc.)
- Booking platforms (Booksy, Vagaro, Fresha, Square, etc.)
- Medical/legal directories (Zocdoc, Avvo, Healthgrades, etc.)
- Real estate platforms (Zillow, Realtor.com, Redfin, etc.)
- Ranking/list sites (Expertise.com, Three Best Rated, etc.)

## Project Structure

The application lives in the `waa/` package, grouped by responsibility.
Run it with `python -m waa <command>` (or `waa <command>` after
`pip install -e .`).

```
waa/
  __main__.py          — `python -m waa` entry point
  cli.py               — all commands + argparse (prospect, preview, audit,
                         send, pipeline, campaign, monitor-replies, send-followups)
  config.py            — env / .env loading (reads .env from the repo root)
  core/
    storage.py         — domain_of(), JsonStore (load-or-default + save)
    output.py          — JSON/CSV output + summaries
  discovery/
    prospector.py      — OpenStreetMap (primary) + Serper/DuckDuckGo search,
                         quick qualification, lead scoring
    scraper.py         — HTML fetch, SEO signals, tech-stack + contact emails
  analysis/
    conversion_audit.py — conversion-grade findings (CTA, trust, niche, etc.)
    personalization.py  — SiteFacts extraction + personalizable gate
    owner_finder.py     — owner first-name extraction (for greetings)
    analyzer.py         — Claude calls, JSON parsing, dash stripping (v1)
    analyzer_v2.py      — fact-grounded orchestration + citation validator
    prompts.py / prompts_v2.py — LLM prompts (v1 and v2/SK+EN)
  outreach/
    sender.py           — Zoho SMTP, Message-ID threading, rate limiting
    email_validator.py  — 5-level address validation (syntax→MX→SMTP probe)
    replies_monitor.py  — IMAP poll all mailboxes → Discord webhook
  proof/
    screenshot.py       — annotated "proof" screenshots (Playwright)
    preview_report.py   — email + screenshot QA report (HTML)

data/
  agent_input.csv      — niche/location pairs for campaign mode
  sample_*.csv         — example inputs
tests/                 — 226 offline tests + a real-HTTP smoke test
docs/                  — RUNNING.md (how-to-run), usage notes
.github/workflows/     — daily_campaign.yml, replies_monitor.yml
pyproject.toml         — package metadata + `waa` console entry point
Dockerfile             — container build (CMD: python -m waa campaign)
.env                   — API keys and config (gitignored)
```

## Daily Limits (Free Tier)

| Service | Limit | Our Usage (per sender) | Safe? |
|---------|-------|----------------------|-------|
| Serper | ~80/day (2,500/month) | ~80 queries | Yes |
| Zoho email | 50/day per mailbox | 40 (10 buffer) | Yes |
| PageSpeed | 25,000/day | ~50 | Yes |
| Anthropic | Pay per use | ~$1-2/day | N/A |

With 3 senders: ~120 emails/day, ~240 Serper queries/day (need 3 separate Serper accounts for this).

## Best Niches (Ranked by Conversion Potential)

The `agent_input.csv` is ordered by quality. Top niches:

1. **Med spas** — High client value, care about image, have budget
2. **Dentists** — Professional, need online presence, own their sites
3. **Chiropractors** — Often have outdated sites, responsive to outreach
4. **Lawyers** — High case value, bad sites are common
5. **Home services (plumber, HVAC, roofing)** — Many have DIY sites

Avoid (at the bottom of the list): barbershops, nail salons, restaurants — low margins, heavy use of booking platforms, rarely have contact emails on their sites.
