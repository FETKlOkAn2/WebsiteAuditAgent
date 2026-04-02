import os
from pathlib import Path
from dotenv import load_dotenv

# Always load .env from the project root, overriding empty shell vars
load_dotenv(Path(__file__).parent / ".env", override=True)

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
