import os
from dotenv import load_dotenv

load_dotenv()

# ── Blinkit locations ─────────────────────────────────────────────────────────
# Blinkit requires lat/lng to serve products. Set your delivery location here.
DEFAULT_LAT = float(os.getenv("BLINKIT_LAT", "28.6139"))   # New Delhi
DEFAULT_LNG = float(os.getenv("BLINKIT_LNG", "77.2090"))
DEFAULT_PINCODE = os.getenv("BLINKIT_PINCODE", "110001")

# ── Request pacing ────────────────────────────────────────────────────────────
MIN_DELAY_S = float(os.getenv("MIN_DELAY_S", "1.5"))
MAX_DELAY_S = float(os.getenv("MAX_DELAY_S", "4.0"))

# Human-like scroll / interaction delays (ms)
SCROLL_DELAY_MS = int(os.getenv("SCROLL_DELAY_MS", "800"))
ACTION_DELAY_MS = int(os.getenv("ACTION_DELAY_MS", "500"))

# ── Proxy ─────────────────────────────────────────────────────────────────────
# Set PROXY_URL=http://user:pass@host:port to route through a proxy
PROXY_URL = os.getenv("PROXY_URL", "")

# ── Browser ───────────────────────────────────────────────────────────────────
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "60000"))

# ── User-agent pool ───────────────────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

# ── Debug ─────────────────────────────────────────────────────────────────────
# Set DEBUG_API_DUMP=true to save every JSON API response to output/api_dump/
# Useful for discovering Blinkit's actual API schema.
DEBUG_API_DUMP = os.getenv("DEBUG_API_DUMP", "false").lower() == "true"
