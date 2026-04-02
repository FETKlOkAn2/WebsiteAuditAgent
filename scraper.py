"""
Website scraping and analysis layer.
Fetches HTML, extracts SEO signals, detects tech stack, checks PageSpeed.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

import config

logger = logging.getLogger(__name__)


def fetch_html(url: str) -> dict:
    """Fetch raw HTML and return parsed data."""
    result = {
        "url": url,
        "status_code": None,
        "html": None,
        "error": None,
        "load_time_ms": None,
    }

    for attempt in range(1, config.MAX_RETRIES + 1):
        resp = None
        start = time.time()
        try:
            resp = requests.get(
                url, headers=config.HEADERS, timeout=15, allow_redirects=True,
            )
        except requests.exceptions.SSLError:
            # Fall back to unverified if SSL cert chain is broken locally
            try:
                resp = requests.get(
                    url, headers=config.HEADERS, timeout=15,
                    allow_redirects=True, verify=False,
                )
            except requests.RequestException as e:
                result["error"] = str(e)
        except requests.RequestException as e:
            result["error"] = str(e)

        if resp is not None:
            elapsed = round((time.time() - start) * 1000)
            result["status_code"] = resp.status_code
            result["load_time_ms"] = elapsed
            result["final_url"] = resp.url
            if resp.status_code == 200:
                result["html"] = resp.text
                return result
            else:
                result["error"] = f"HTTP {resp.status_code}"

        if attempt < config.MAX_RETRIES:
            time.sleep(config.RETRY_BACKOFF * attempt)

    return result


def extract_seo_signals(html: str, url: str) -> dict:
    """Extract SEO and UX signals from HTML."""
    soup = BeautifulSoup(html, "lxml")

    # Title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None

    # Meta description
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_desc_tag["content"].strip() if meta_desc_tag and meta_desc_tag.get("content") else None

    # H1 tags
    h1_tags = [h1.get_text(strip=True) for h1 in soup.find_all("h1")]

    # H2 tags (first 5)
    h2_tags = [h2.get_text(strip=True) for h2 in soup.find_all("h2")[:5]]

    # Images without alt text
    images = soup.find_all("img")
    images_total = len(images)
    images_missing_alt = len([
        img for img in images
        if not img.get("alt") or img["alt"].strip() == ""
    ])

    # Viewport meta (mobile responsiveness indicator)
    viewport = soup.find("meta", attrs={"name": "viewport"})
    has_viewport = viewport is not None

    # Open Graph tags
    og_tags = {}
    for og in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        og_tags[og.get("property", "")] = og.get("content", "")

    # Canonical URL
    canonical_tag = soup.find("link", attrs={"rel": "canonical"})
    canonical = canonical_tag["href"] if canonical_tag and canonical_tag.get("href") else None

    # CTA detection (buttons and links with action words)
    cta_keywords = [
        "get started", "sign up", "buy", "order", "contact", "free trial",
        "book", "schedule", "request", "download", "subscribe", "learn more",
        "start", "try", "demo", "quote",
    ]
    ctas_found = []
    for el in soup.find_all(["a", "button"]):
        text = el.get_text(strip=True).lower()
        if any(kw in text for kw in cta_keywords):
            ctas_found.append(el.get_text(strip=True))
    ctas_found = ctas_found[:5]  # limit

    # SSL check (simple: does the URL use https?)
    uses_https = urlparse(url).scheme == "https"

    # Schema.org / structured data
    has_schema = bool(soup.find("script", attrs={"type": "application/ld+json"}))

    # Links analysis
    internal_links = 0
    external_links = 0
    broken_anchors = 0
    base_domain = urlparse(url).netloc

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#"):
            continue
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full_url = urljoin(url, href)
        parsed = urlparse(full_url)
        if parsed.netloc == base_domain:
            internal_links += 1
        else:
            external_links += 1
        if href == "#" or href == "":
            broken_anchors += 1

    # Page word count (rough content assessment)
    body = soup.find("body")
    word_count = len(body.get_text(separator=" ", strip=True).split()) if body else 0

    return {
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": meta_description,
        "meta_description_length": len(meta_description) if meta_description else 0,
        "h1_tags": h1_tags,
        "h1_count": len(h1_tags),
        "h2_tags": h2_tags,
        "images_total": images_total,
        "images_missing_alt": images_missing_alt,
        "has_viewport": has_viewport,
        "has_og_tags": len(og_tags) > 0,
        "og_tags": og_tags,
        "canonical": canonical,
        "ctas_found": ctas_found,
        "uses_https": uses_https,
        "has_schema": has_schema,
        "internal_links": internal_links,
        "external_links": external_links,
        "broken_anchors": broken_anchors,
        "word_count": word_count,
    }


def detect_tech_stack(html: str, headers: dict = None) -> dict:
    """Detect CMS, frameworks, and tools from HTML signatures."""
    soup = BeautifulSoup(html, "lxml")
    html_lower = html.lower()
    techs = []

    # CMS detection
    cms_signatures = {
        "WordPress": [
            "wp-content", "wp-includes", "wp-json",
            'name="generator" content="WordPress',
        ],
        "Shopify": [
            "cdn.shopify.com", "shopify.com/s/", "Shopify.theme",
        ],
        "Wix": [
            "wix.com", "X-Wix", "wixsite.com",
        ],
        "Squarespace": [
            "squarespace.com", "static.squarespace.com", "squarespace-cdn",
        ],
        "Webflow": [
            "webflow.com", "assets.website-files.com", "w-nav",
        ],
        "Drupal": [
            "drupal.js", "drupal.settings", "/sites/default/files",
        ],
        "Joomla": [
            "/media/jui/", "joomla",
        ],
        "Ghost": [
            "ghost.org", "ghost-", 'content="Ghost"',
        ],
        "HubSpot": [
            "hubspot.com", "hs-scripts.com", "hbspt",
        ],
    }

    detected_cms = None
    for cms, patterns in cms_signatures.items():
        if any(p.lower() in html_lower for p in patterns):
            detected_cms = cms
            techs.append(cms)
            break

    # JavaScript frameworks
    js_signatures = {
        "React": ["react", "__NEXT_DATA__", "_next/", "reactroot"],
        "Next.js": ["__NEXT_DATA__", "_next/static", "next/dist"],
        "Vue.js": ["vue.js", "vue.min.js", "__vue__", "vue-"],
        "Nuxt.js": ["__NUXT__", "_nuxt/"],
        "Angular": ["ng-version", "angular.js", "angular.min.js"],
        "Svelte": ["svelte", "__svelte"],
        "jQuery": ["jquery.min.js", "jquery.js", "jquery/"],
        "Gatsby": ["gatsby-", "gatsby.js"],
    }

    for fw, patterns in js_signatures.items():
        if any(p.lower() in html_lower for p in patterns):
            techs.append(fw)

    # Analytics & tools
    tool_signatures = {
        "Google Analytics": ["google-analytics.com", "gtag(", "ga("],
        "Google Tag Manager": ["googletagmanager.com", "gtm.js"],
        "Facebook Pixel": ["connect.facebook.net", "fbq("],
        "Hotjar": ["hotjar.com", "hj("],
        "Intercom": ["intercom.io", "Intercom("],
        "Drift": ["drift.com", "driftt.com"],
        "Crisp": ["crisp.chat"],
        "Cloudflare": ["cloudflare", "cf-ray"],
        "Tailwind CSS": ["tailwindcss", "tailwind"],
        "Bootstrap": ["bootstrap.min.css", "bootstrap.min.js"],
    }

    for tool, patterns in tool_signatures.items():
        if any(p.lower() in html_lower for p in patterns):
            techs.append(tool)

    # Server info from headers
    server = None
    if headers:
        server = headers.get("Server") or headers.get("server")
        powered_by = headers.get("X-Powered-By") or headers.get("x-powered-by")
        if powered_by:
            techs.append(f"X-Powered-By: {powered_by}")

    return {
        "cms": detected_cms,
        "technologies": techs,
        "server": server,
    }


def fetch_pagespeed(url: str) -> dict:
    """Fetch Google PageSpeed Insights data."""
    if not config.PAGESPEED_API_KEY:
        logger.info("No PageSpeed API key configured, skipping PageSpeed analysis")
        return {"available": False, "error": "No API key configured"}

    api_url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

    results = {}
    for strategy in ["mobile", "desktop"]:
        params = {
            "url": url,
            "key": config.PAGESPEED_API_KEY,
            "strategy": strategy,
            "category": ["performance", "seo", "best-practices", "accessibility"],
        }

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = requests.get(api_url, params=params, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    lighthouse = data.get("lighthouseResult", {})
                    categories = lighthouse.get("categories", {})
                    audits = lighthouse.get("audits", {})

                    results[strategy] = {
                        "performance_score": _score(categories.get("performance")),
                        "seo_score": _score(categories.get("seo")),
                        "best_practices_score": _score(categories.get("best-practices")),
                        "accessibility_score": _score(categories.get("accessibility")),
                        "lcp_ms": _metric_ms(audits.get("largest-contentful-paint")),
                        "fid_ms": _metric_ms(audits.get("max-potential-fid")),
                        "cls": _metric_value(audits.get("cumulative-layout-shift")),
                        "fcp_ms": _metric_ms(audits.get("first-contentful-paint")),
                        "speed_index_ms": _metric_ms(audits.get("speed-index")),
                        "tti_ms": _metric_ms(audits.get("interactive")),
                        "total_blocking_time_ms": _metric_ms(audits.get("total-blocking-time")),
                    }
                    break
                elif resp.status_code == 429:
                    logger.warning(f"PageSpeed rate limited, waiting...")
                    time.sleep(config.PAGESPEED_DELAY * attempt * 2)
                else:
                    results[strategy] = {"error": f"HTTP {resp.status_code}"}
                    break
            except requests.RequestException as e:
                if attempt < config.MAX_RETRIES:
                    time.sleep(config.RETRY_BACKOFF * attempt)
                else:
                    results[strategy] = {"error": str(e)}

        time.sleep(config.PAGESPEED_DELAY)

    results["available"] = bool(results.get("mobile") or results.get("desktop"))
    return results


def _score(category: dict) -> int | None:
    if not category:
        return None
    return round(category.get("score", 0) * 100)


def _metric_ms(audit: dict) -> float | None:
    if not audit:
        return None
    return audit.get("numericValue")


def _metric_value(audit: dict) -> float | None:
    if not audit:
        return None
    return audit.get("numericValue")


def extract_contact_emails(html: str, url: str) -> list[str]:
    """
    Extract contact email addresses from HTML.
    Looks at mailto: links, visible text patterns, and common contact pages.
    Filters out generic/noreply addresses.
    """
    emails = set()
    soup = BeautifulSoup(html, "lxml")

    # 1. mailto: links (most reliable)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("mailto:"):
            email = href.replace("mailto:", "").split("?")[0].strip().lower()
            if email and "@" in email:
                emails.add(email)

    # 2. Email pattern in visible text
    email_pattern = re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    )
    body = soup.find("body")
    if body:
        text = body.get_text(separator=" ")
        for match in email_pattern.findall(text):
            emails.add(match.lower())

    # 3. Email pattern in href attributes (some sites obfuscate)
    for a in soup.find_all("a", href=True):
        for match in email_pattern.findall(a["href"]):
            emails.add(match.lower())

    # Filter out junk emails
    skip_patterns = [
        "noreply", "no-reply", "donotreply", "mailer-daemon",
        "example.com", "sentry.io", "wixpress.com",
        "wordpress.org", "support@", "privacy@",
        ".png", ".jpg", ".gif", ".svg",  # false matches from URLs
    ]
    filtered = []
    for e in emails:
        if any(p in e for p in skip_patterns):
            continue
        # Skip if it looks like a file path, not an email
        if "/" in e:
            continue
        filtered.append(e)

    # Prioritize: info@, contact@, hello@, owner-looking emails first
    def sort_key(email):
        prefixes = ["info@", "contact@", "hello@", "office@", "admin@"]
        for i, p in enumerate(prefixes):
            if email.startswith(p):
                return i
        return 10  # personal emails last but still good

    filtered.sort(key=sort_key)
    return filtered


def scrape_contact_page(url: str) -> list[str]:
    """
    Try to find and scrape a /contact page for additional emails.
    """
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    contact_paths = ["/contact", "/contact-us", "/about", "/about-us"]

    emails = []
    for path in contact_paths:
        contact_url = base + path
        try:
            resp = requests.get(
                contact_url, headers=config.HEADERS, timeout=10,
                allow_redirects=True, verify=False,
            )
            if resp.status_code == 200:
                found = extract_contact_emails(resp.text, contact_url)
                emails.extend(found)
                if found:
                    logger.info(f"Found emails on {contact_url}: {found}")
                    break  # got emails, no need to check more pages
        except requests.RequestException:
            continue

    return emails


def analyze_website(url: str, skip_pagespeed: bool = False) -> dict:
    """Full website analysis pipeline for a single URL."""
    # Normalize URL
    if not url.startswith("http"):
        url = "https://" + url

    logger.info(f"Analyzing: {url}")

    # Step 1: Fetch HTML
    fetch_result = fetch_html(url)
    if not fetch_result["html"]:
        return {
            "url": url,
            "error": f"Could not fetch site: {fetch_result['error']}",
            "seo": None,
            "tech": None,
            "pagespeed": None,
        }

    # Step 2: Extract SEO signals
    seo = extract_seo_signals(fetch_result["html"], url)

    # Step 3: Detect tech stack
    tech = detect_tech_stack(fetch_result["html"])

    # Step 4: Extract contact emails
    contact_emails = extract_contact_emails(fetch_result["html"], url)
    if not contact_emails:
        # Try /contact page
        contact_emails = scrape_contact_page(url)

    # Step 5: PageSpeed
    pagespeed = None
    if not skip_pagespeed:
        pagespeed = fetch_pagespeed(url)
        time.sleep(config.PAGESPEED_DELAY)

    return {
        "url": url,
        "final_url": fetch_result.get("final_url", url),
        "status_code": fetch_result["status_code"],
        "load_time_ms": fetch_result["load_time_ms"],
        "error": None,
        "seo": seo,
        "tech": tech,
        "pagespeed": pagespeed,
        "contact_emails": contact_emails,
    }
