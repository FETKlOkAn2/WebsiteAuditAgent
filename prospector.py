"""
Lead Prospector — finds business websites that likely need agency services.

Strategy:
  1. Search Google for businesses in a niche + location
  2. Quick-qualify each result (fast HTML check for red flags)
  3. Score and filter — only pass good prospects to the audit pipeline

Red flags we look for (signs they need help):
  - Outdated CMS or no CMS (static HTML, old WordPress themes)
  - No HTTPS
  - Missing viewport meta (not mobile-friendly)
  - Very slow server response
  - No analytics/tracking installed
  - Thin content / no clear CTAs
  - Old copyright year in footer
  - No schema markup, no OG tags
  - Stock/template design indicators (default favicon, generic titles)
"""

import csv
import json
import logging
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

import config
from scraper import fetch_html, extract_seo_signals, detect_tech_stack

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------

def search_google_serp(query: str, num_results: int = 20, api_key: str = "", cx: str = "") -> list[dict]:
    """
    Search via Serper.dev API (2500 free queries/month).
    Set SERPER_API_KEY in .env
    """
    api_key = api_key or os.getenv("SERPER_API_KEY", "")

    if not api_key:
        logger.warning("No Serper API key configured (SERPER_API_KEY)")
        return []

    results = []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": min(num_results, 10)},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("organic", []):
                results.append({
                    "url": item.get("link", ""),
                    "title": item.get("title", ""),
                    "snippet": item.get("snippet", ""),
                })
        elif resp.status_code == 429:
            logger.warning("Serper API rate limited")
        else:
            logger.error(f"Serper API error: {resp.status_code} — {resp.text[:200]}")
    except requests.RequestException as e:
        logger.error(f"Serper search error: {e}")

    return results[:num_results]


def search_duckduckgo(query: str, num_results: int = 20) -> list[dict]:
    """
    Search via DuckDuckGo HTML endpoint (no API key, no JS required).
    Reliable fallback when Google API is unavailable.
    """
    results = []
    headers = {
        "User-Agent": config.HEADERS["User-Agent"],
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        for attempt in range(3):
            resp = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                break
            elif resp.status_code == 202:
                # Rate limited — wait and retry
                wait = 5 * (attempt + 1)
                logger.info(f"DuckDuckGo rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                logger.warning(f"DuckDuckGo returned {resp.status_code}")
                return []

        if resp.status_code != 200:
            logger.warning(f"DuckDuckGo still returning {resp.status_code} after retries")
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        for a in soup.find_all("a", class_="result__a", href=True):
            href = a["href"]
            title = a.get_text(strip=True)

            # DuckDuckGo wraps URLs in a redirect: //duckduckgo.com/l/?uddg=<encoded_url>
            if "uddg=" in href:
                from urllib.parse import unquote
                actual_url = unquote(href.split("uddg=")[1].split("&")[0])
            elif href.startswith("http"):
                actual_url = href
            else:
                continue

            if "duckduckgo.com" in actual_url:
                continue

            if title and actual_url.startswith("http"):
                # Find snippet
                snippet = ""
                parent = a.find_parent("div")
                if parent:
                    snippet_tag = parent.find("a", class_="result__snippet")
                    if snippet_tag:
                        snippet = snippet_tag.get_text(strip=True)

                results.append({
                    "url": actual_url,
                    "title": title,
                    "snippet": snippet,
                })

    except requests.RequestException as e:
        logger.error(f"DuckDuckGo search error: {e}")

    # Deduplicate
    seen = set()
    unique = []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    return unique[:num_results]


def search_with_llm(niche: str, location: str, count: int = 20) -> list[str]:
    """
    Use Claude to generate smart search queries for a niche.
    Returns a list of Google search queries optimized to find
    businesses with weak websites.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""Generate {min(count, 10)} simple search queries to find actual business websites
for "{niche}" businesses{f' in {location}' if location else ''}.

IMPORTANT RULES:
- Keep queries SIMPLE — just the business type + location
- Do NOT use advanced operators like site:, inurl:, OR, "quotes", etc.
- These will be used on DuckDuckGo, not Google
- Focus on finding the actual business homepages, not directories
- Add terms like "website", "book online", "schedule" to find real business sites
- Exclude directories by adding -yelp -yellowpages -facebook -instagram

Return ONLY a JSON array of search query strings, nothing else.
Example: ["{niche} {location}", "{niche} {location} book appointment", "{niche} near {location} website"]"""

    message = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    return json.loads(text)


# ---------------------------------------------------------------------------
# Quick qualification (fast checks, no LLM needed)
# ---------------------------------------------------------------------------

def quick_qualify(url: str) -> dict:
    """
    Fast qualification of a URL. Returns a score and red flags.
    This is intentionally lightweight — the full audit comes later.
    """
    result = {
        "url": url,
        "qualified": False,
        "score": 0,
        "red_flags": [],
        "green_flags": [],
        "name": "",
        "skip_reason": None,
    }

    # Skip known directories, social media, and marketplaces
    skip_domains = [
        "yelp.com", "facebook.com", "instagram.com", "twitter.com",
        "linkedin.com", "youtube.com", "tiktok.com", "pinterest.com",
        "yellowpages.com", "bbb.org", "indeed.com", "glassdoor.com",
        "amazon.com", "ebay.com", "etsy.com", "reddit.com",
        "wikipedia.org", "craigslist.org", "nextdoor.com",
        "thumbtack.com", "angi.com", "homeadvisor.com",
    ]

    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    for skip in skip_domains:
        if skip in domain:
            result["skip_reason"] = f"Directory/social site: {skip}"
            return result

    # Fetch HTML (quick, single attempt)
    fetch = fetch_html(url)
    if not fetch.get("html"):
        result["skip_reason"] = f"Could not fetch: {fetch.get('error', 'unknown')}"
        return result

    html = fetch["html"]
    seo = extract_seo_signals(html, url)
    tech = detect_tech_stack(html)

    # Extract business name
    if seo.get("title"):
        result["name"] = seo["title"].split("|")[0].split("-")[0].split("–")[0].strip()

    score = 0  # Higher = more likely to need services (better prospect)

    # --- Red flags (signs they need help = GOOD for us) ---

    # No HTTPS
    if not seo.get("uses_https"):
        score += 15
        result["red_flags"].append("No HTTPS")

    # No viewport (not mobile-friendly)
    if not seo.get("has_viewport"):
        score += 15
        result["red_flags"].append("Not mobile-friendly")

    # Missing or bad meta description
    if not seo.get("meta_description"):
        score += 10
        result["red_flags"].append("No meta description")
    elif seo.get("meta_description_length", 0) < 50:
        score += 5
        result["red_flags"].append("Meta description too short")

    # No H1 or multiple H1s
    if seo.get("h1_count", 0) == 0:
        score += 8
        result["red_flags"].append("No H1 tag")
    elif seo.get("h1_count", 0) > 3:
        score += 5
        result["red_flags"].append("Too many H1 tags")

    # Missing alt text on images
    if seo.get("images_total", 0) > 0 and seo.get("images_missing_alt", 0) > 0:
        ratio = seo["images_missing_alt"] / seo["images_total"]
        if ratio > 0.5:
            score += 8
            result["red_flags"].append(f"{seo['images_missing_alt']}/{seo['images_total']} images missing alt text")

    # No CTAs found
    if not seo.get("ctas_found"):
        score += 10
        result["red_flags"].append("No clear call-to-action")

    # Thin content
    if seo.get("word_count", 0) < 200:
        score += 8
        result["red_flags"].append(f"Thin content ({seo.get('word_count', 0)} words)")

    # No schema markup
    if not seo.get("has_schema"):
        score += 5
        result["red_flags"].append("No structured data")

    # No Open Graph tags
    if not seo.get("has_og_tags"):
        score += 5
        result["red_flags"].append("No Open Graph tags")

    # No analytics
    has_analytics = any(
        t in (tech.get("technologies") or [])
        for t in ["Google Analytics", "Google Tag Manager", "Facebook Pixel"]
    )
    if not has_analytics:
        score += 10
        result["red_flags"].append("No analytics/tracking detected")

    # Slow server response
    if fetch.get("load_time_ms") and fetch["load_time_ms"] > 3000:
        score += 10
        result["red_flags"].append(f"Slow response ({fetch['load_time_ms']}ms)")

    # Old copyright year in footer
    soup = BeautifulSoup(html, "lxml")
    footer = soup.find("footer")
    if footer:
        footer_text = footer.get_text()
        year_match = re.search(r"©\s*(\d{4})", footer_text)
        if year_match:
            year = int(year_match.group(1))
            current_year = datetime.now().year
            if year < current_year - 1:
                score += 10
                result["red_flags"].append(f"Outdated copyright year ({year})")

    # Outdated CMS / old tech
    cms = tech.get("cms")
    if cms == "WordPress":
        # WordPress itself isn't bad, but it's a sign they might need modernization
        score += 3
        # Check for old jQuery (often a sign of old WP themes)
        if "jQuery" in (tech.get("technologies") or []):
            score += 3

    # --- Green flags (signs they DON'T need us = worse prospect) ---
    modern_tech = ["React", "Next.js", "Vue.js", "Nuxt.js", "Svelte", "Gatsby", "Tailwind CSS"]
    for t in modern_tech:
        if t in (tech.get("technologies") or []):
            score -= 10
            result["green_flags"].append(f"Uses {t}")

    if cms in ["Webflow", "Shopify"]:
        score -= 5
        result["green_flags"].append(f"Uses {cms} (usually decent)")

    # Clamp score
    score = max(0, min(100, score))
    result["score"] = score
    result["qualified"] = score >= 25  # Threshold: at least a few red flags

    return result


# ---------------------------------------------------------------------------
# Main prospecting pipeline
# ---------------------------------------------------------------------------

def prospect(
    niche: str,
    location: str = "",
    num_results: int = 30,
    min_score: int = 25,
    queries: list[str] = None,
) -> list[dict]:
    """
    Full prospecting pipeline:
    1. Generate/use search queries
    2. Search Google for results
    3. Quick-qualify each result
    4. Return qualified leads sorted by score
    """
    # Step 1: Get search queries
    loc = f" {location}" if location else ""
    # Always start with reliable basic queries
    base_queries = [
        f"{niche}{loc}",
        f"{niche}{loc} book appointment",
        f"{niche}{loc} near me",
        f"best {niche}{loc}",
    ]

    if not queries:
        logger.info(f"Generating search queries for: {niche} in {location or 'any location'}")
        try:
            llm_queries = search_with_llm(niche, location, count=5)
            logger.info(f"Generated {len(llm_queries)} search queries")
            # Combine: basic first, then LLM-generated
            queries = base_queries + [q for q in llm_queries if q not in base_queries]
        except Exception as e:
            logger.error(f"Failed to generate queries with LLM: {e}")
            queries = base_queries

    for q in queries:
        logger.info(f"  Query: {q}")

    # Step 2: Search
    all_results = []
    seen_domains = set()

    for query in queries:
        # Try Google API first, fall back to DuckDuckGo
        results = []
        if os.getenv("SERPER_API_KEY"):
            results = search_google_serp(query, num_results=10)
        if not results:
            logger.info(f"  Using DuckDuckGo for: {query}")
            results = search_duckduckgo(query, num_results=10)

        for r in results:
            domain = urlparse(r["url"]).netloc.lower().replace("www.", "")
            if domain not in seen_domains:
                seen_domains.add(domain)
                all_results.append(r)

        # Longer delay between search queries to avoid rate limiting
        time.sleep(max(config.SCRAPE_DELAY, 4))

    logger.info(f"Found {len(all_results)} unique URLs from search")

    # Step 3: Quick-qualify
    qualified = []
    for i, r in enumerate(all_results[:num_results], 1):
        logger.info(f"[{i}/{min(len(all_results), num_results)}] Qualifying: {r['url']}")

        result = quick_qualify(r["url"])
        result["search_title"] = r.get("title", "")
        result["search_snippet"] = r.get("snippet", "")

        if result.get("skip_reason"):
            logger.info(f"  Skipped: {result['skip_reason']}")
            continue

        if result["score"] >= min_score:
            qualified.append(result)
            flags = ", ".join(result["red_flags"][:3])
            logger.info(f"  QUALIFIED (score: {result['score']}) — {flags}")
        else:
            logger.info(f"  Not qualified (score: {result['score']})")

        time.sleep(config.SCRAPE_DELAY)

    # Sort by score (highest = most promising lead)
    qualified.sort(key=lambda x: x["score"], reverse=True)

    logger.info(f"Qualified {len(qualified)} leads out of {len(all_results)} found")
    return qualified


def save_prospects_csv(prospects: list[dict], filename: str = None) -> str:
    """Save qualified prospects as CSV in the format the audit agent expects."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"prospects_{ts}.csv"

    path = os.path.join(config.OUTPUT_DIR, filename)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "website_url", "name", "prospect_score", "red_flags",
        ])
        writer.writeheader()
        for p in prospects:
            writer.writerow({
                "website_url": p["url"],
                "name": p.get("name", ""),
                "prospect_score": p["score"],
                "red_flags": " | ".join(p.get("red_flags", [])),
            })

    logger.info(f"Prospects CSV saved: {path}")
    return path


def print_prospect_summary(prospects: list[dict]):
    """Print a summary of found prospects."""
    print(f"\n{'='*60}")
    print(f" PROSPECTING COMPLETE — {len(prospects)} qualified leads")
    print(f"{'='*60}\n")

    for p in prospects:
        flags = ", ".join(p.get("red_flags", [])[:3])
        print(f"  [{p['score']:3d}] {p['url']}")
        if p.get("name"):
            print(f"        Name: {p['name']}")
        print(f"        Issues: {flags}")
        print()