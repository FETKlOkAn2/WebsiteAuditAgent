# Website Audit Agent

Automated pipeline for cold outreach. Finds business websites that need work, audits them, and generates personalized cold emails.

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │            pipeline (all-in-one)        │
                    └─────────────┬───────────────────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                                                  ▼
┌─────────────────┐                              ┌──────────────────┐
│    prospect     │                              │      audit       │
│                 │                              │                  │
│ Google Search   │   prospects.csv              │ HTML Scraping    │
│ Quick Qualify   │ ─────────────────────────▶   │ PageSpeed API    │
│ Score & Filter  │                              │ LLM Analysis     │
│                 │                              │ Email Generation │
└─────────────────┘                              └──────────────────┘
                                                          │
                                                          ▼
                                                 JSON + CSV output
                                                 (CRM-ready emails)
```

## Quick Start

```bash
# 1. Install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Add ANTHROPIC_API_KEY (required)
# Optionally add PAGESPEED_API_KEY, GOOGLE_SEARCH_API_KEY

# 3. Full pipeline — find leads + audit + generate emails
python audit_agent.py pipeline --niche "plumber" --location "Austin TX" --agency "WebPro"

# 4. Or run steps separately:
python audit_agent.py prospect --niche "dentist" --location "Miami"
python audit_agent.py audit output/prospects_*.csv --agency "WebPro" --sender "Alex"

# 5. Single URL audit
python audit_agent.py audit --url https://some-business.com
```

## Three Modes

### `prospect` — Find leads that need your services

Searches Google for businesses in a niche/location, then **quick-qualifies** each site by checking for red flags:

- No HTTPS, no viewport meta (not mobile-friendly)
- Missing meta descriptions, no H1 tags
- No analytics/tracking installed
- Thin content, no CTAs
- Old copyright year in footer
- No schema markup, no Open Graph tags
- Outdated tech stack

Sites with modern tech (React, Next.js, Tailwind) get scored down — they probably don't need you.

```bash
python audit_agent.py prospect --niche "plumber" --location "Austin TX" --count 50
```

### `audit` — Deep analysis + email generation

Takes a CSV of URLs (or single `--url`), runs full analysis, then uses Claude to:
1. Identify 2-4 high-impact problems with business impact
2. Generate a personalized cold email + follow-up

```bash
python audit_agent.py audit sites.csv --agency "WebPro" --sender "Sarah" --title "CEO"
```

### `pipeline` — All-in-one

Prospect → qualify → audit top leads → generate emails. One command.

```bash
python audit_agent.py pipeline --niche "dentist" --location "Miami" --audit-top 10
```

## Input CSV Format

```csv
website_url,name
https://example.com,Example Corp
https://another-site.com,Another Business
```

The `name` column is optional.

## Output

Results saved to `output/`:

- **prospects_*.csv** — Qualified leads with scores and red flags
- **audit_results_*.json** — Full audit data + analysis + emails
- **audit_results_*.csv** — Flat CRM-ready format (email subject, body, follow-up)

## CLI Reference

```
python audit_agent.py prospect --niche NICHE [--location LOC] [--count N] [--min-score N]
python audit_agent.py audit [FILE.csv] [--url URL] [--skip-pagespeed] [--agency X] [--sender X] [--title X]
python audit_agent.py pipeline --niche NICHE [--location LOC] [--audit-top N] [--agency X] [--sender X]
```

## API Keys

| Key | Required | Free Tier | Purpose |
|-----|----------|-----------|---------|
| `ANTHROPIC_API_KEY` | Yes | No | LLM analysis + email generation |
| `PAGESPEED_API_KEY` | No | Yes (daily limit) | Google PageSpeed scores |
| `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_CX` | No | Yes (100/day) | Prospect search via API |

Without Google Search API keys, prospecting falls back to scraping Google results directly.

## How Prospect Scoring Works

Each site gets a 0-100 score based on red flags found during quick qualification:

| Signal | Points |
|--------|--------|
| No HTTPS | +15 |
| No viewport (not mobile-friendly) | +15 |
| No analytics/tracking | +10 |
| No CTAs | +10 |
| Slow response (>3s) | +10 |
| Outdated copyright year | +10 |
| No meta description | +10 |
| Thin content (<200 words) | +8 |
| Missing alt text (>50%) | +8 |
| No H1 tag | +8 |
| No schema markup | +5 |
| No Open Graph tags | +5 |
| Uses modern framework (React, etc.) | -10 |
| Uses Webflow/Shopify | -5 |

Default threshold: score >= 25 qualifies as a prospect.

## Project Structure

```
audit_agent.py   — CLI with 3 modes: prospect, audit, pipeline
prospector.py    — Google search, quick qualification, scoring
scraper.py       — HTML fetch, SEO extraction, tech detection, PageSpeed
analyzer.py      — LLM analysis & email generation
prompts.py       — LLM prompts (separated for easy iteration)
output.py        — JSON/CSV output
config.py        — Configuration from .env
```

## Integrations

**Email tools**: CSV output maps directly to Instantly/Smartlead import format.

**Webhook / n8n**: Add `requests.post("https://your-webhook.com", json=result)` after each audit.

**Notion / Airtable**: Use their Python SDKs to push results.
