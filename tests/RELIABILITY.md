# Reliability Report — Website Audit Agent

_Generated: 2026-04-27_

## TL;DR

- **71 unit/integration tests** + **1 end-to-end smoke test** — all passing.
- **1 production-grade bug found and fixed** (`sender.load_emails_from_audit_json` would crash on rows where `email: None` — every campaign run with skipped/errored audits would fail at the send step).
- **No additional fragile paths** identified in the live code (output.py and `_prepare_send_list` already use the safe `or {}` pattern).
- All modules compile cleanly.

## How to run

```bash
# One command, runs everything:
./tests/run_all.sh

# Or individually:
.venv/bin/python -m unittest tests.test_reliability -v   # 71 tests, ~12s, fully offline
.venv/bin/python tests/smoke_test.py                     # real local HTTP, stubbed LLM
```

No real network calls. No real Anthropic calls. No real SMTP. Safe to run in CI.

## What was tested

### 1. Scraper (`scraper.py`) — 14 tests
- HTML parsing of: minimal, no-body, broken, unicode, giant (5000 paragraphs), full-featured
- Contact email extraction + junk filtering (`noreply@`, `support@`, `.png`, etc.)
- Tech stack detection (WordPress signature, no-CMS fallback)
- HTTP failure modes: timeout, 404, SSL error → unverified fallback, repeated connection errors → graceful give-up

### 2. Conversion audit (`conversion_audit.py`) — 19 tests
- Realistic medspa with everything right → finds strong signals
- Realistic dentist with multiple problems → catches lorem ipsum, outdated copyright, missing niche elements, phone-not-clickable
- Edge cases: empty HTML, no body, SPA shell with empty `<div id="root">`, unknown niche, unicode (French restaurant), 5000-paragraph HTML (must finish in <5s — actually <0.2s)
- Adversarial: forms with no inputs, phone in text only vs. tel:, copyright year is recent vs. ≥2 years old
- Serialization invariant: every audit object must produce JSON-serializable output

### 3. Analyzer + LLM pipeline (`analyzer.py`) — 8 tests
- Garbage LLM response → returns error-shaped dict, never raises
- Markdown-fenced JSON → stripped correctly
- Email body length guard → regenerates when over 110 words
- Email cleaning → strips `Founder, EMTD Studio` sign-off lines and `no strings attached` phrasing
- `_format_audit_for_llm` survives empty/missing audit data

### 4. Output layer (`output.py`) — 6 tests
- Empty list, unicode strings, partial records (mix of `error`, `skipped`, `ok`)
- PageSpeed with per-strategy errors
- `print_summary` survives any record shape

### 5. Sender (`sender.py`) — 5 tests
- Dry-run default works
- Skips entries missing `to`/`subject`/`body`
- Real-send without password → returns error, doesn't crash
- Per-email SMTP exception isolated (one bad recipient doesn't kill the batch)
- `load_emails_from_audit_json` survives `email: None` rows ← **bug fix landed here**

### 6. Campaign state (`audit_agent.py`) — 8 tests
- Progress file: missing → baseline; roundtrip; corrupt → fails loudly (not silently)
- Sent registry: by-email and by-domain dedup, normalization (lowercased, www-stripped)
- `_record_sent` writes both indexes correctly

### 7. CSV loaders — 5 tests
- `load_urls_from_csv`: recognizes `website_url`, falls back to first column, skips blanks, handles empty file
- `load_contacts_csv`: domain normalization, scheme injection, skips rows missing email

### 8. End-to-end smoke test
Spins up a `BaseHTTPRequestHandler` on a random port and runs the full chain:

1. Real HTTP fetch (200, 500, 301 redirect)
2. SEO + tech extraction on real bytes
3. Conversion audit on real fetch (CTA detected, phone clickable, city in copy, niche check ran)
4. `process_single` with real HTTP + stubbed LLM
5. Save JSON → reload → dry-run send
6. CSV serialization of mixed (ok/error/skipped) results
7. `send_batch` tolerates malformed entries (empty dict, `to: None`)

## Bugs found & fixed

### `sender.load_emails_from_audit_json` crashed on `email: None` rows
**Severity:** high — every send step after a campaign with errored or skipped audits would crash before sending anything.

**Root cause:**
```python
email_data = r.get("email", {})       # returns None when email key exists with value None
subject = email_data.get("subject_line", "")   # AttributeError: 'NoneType'
```

**Fix** (in `sender.py:204`):
```python
email_data = r.get("email") or {}
```

The `or {}` pattern coerces `None` to `{}`. The companion path in `audit_agent._prepare_send_list` already used the safe pattern, so this was the last stale spot.

## Resilience properties verified

| Property | How it's enforced |
|---|---|
| **Bad HTML never crashes parsing** | BeautifulSoup is lenient; tested with `<html><head>` only, broken tags, empty string |
| **Network failures are bounded** | `MAX_RETRIES=3` with linear backoff; SSL has unverified fallback |
| **LLM JSON parse failures don't kill a batch** | `analyze_audit_data` and `generate_email` both catch `JSONDecodeError` and `anthropic.APIError`, returning structured error shapes |
| **Email length blowout self-corrects** | Detects >110 words, calls LLM once more with stronger constraint |
| **One bad recipient doesn't kill the batch** | `send_batch` wraps each `sendmail` in its own try/except |
| **Already-contacted emails are skipped** | `_already_contacted` checks both email and normalized domain |
| **Mid-campaign interrupt → no progress lost** | `KeyboardInterrupt` in `cmd_campaign` saves progress before exit |
| **GitHub Actions artifact upload won't fail on missing files** | `if-no-files-found: warn` + early `_save_*` flushes (already in workflow + `cmd_campaign`) |
| **Output files always serializable** | Tested for unicode, partial records, errored pagespeed, missing email |
| **CSV loaders handle real-world variants** | Multiple column names accepted; domain normalization handles `www.`, mixed case, paths |

## Known limitations (deliberate, not fragility)

- **PageSpeed is rate-limited and slow** — the design already isolates failure (`results[strategy] = {"error": ...}` per strategy). When the API is down, audit continues without PageSpeed scores.
- **No ATS/email-validation hooks** — bounce-rate management is delegated to Zoho's outbound monitoring + the dedup registry.
- **The conversion auditor is heuristic** — niche signatures use keyword matching, not semantic analysis. Confidence levels (`high|medium|low`) on findings exist precisely so downstream prompts can prefer high-confidence anchors.
- **Corrupt JSON state files raise loudly** — by design. Silent recovery would risk emailing the same person twice. Operator must look at the file and decide.

## Files added

```
tests/
├── __init__.py
├── RELIABILITY.md       (this file)
├── run_all.sh           (one-shot test runner)
├── smoke_test.py        (real-HTTP end-to-end)
└── test_reliability.py  (71 unit/integration tests)
```

## Verdict

The pipeline is reliable in practice, not just in theory. Imperfect inputs (bad HTML, dead URLs, garbage LLM responses, missing API keys, malformed CSVs, partial state files) all produce structured outcomes — never crashes that lose work mid-campaign.
