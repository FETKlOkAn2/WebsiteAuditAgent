import os
from pathlib import Path
from dotenv import load_dotenv

# .env lives at the repo root. This module is waa/config.py, so the root is
# two levels up (waa/ -> repo root).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
PAGESPEED_DELAY = float(os.getenv("PAGESPEED_DELAY_SECONDS", "2"))
SCRAPE_DELAY = float(os.getenv("SCRAPE_DELAY_SECONDS", "1"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds, multiplied by attempt number
