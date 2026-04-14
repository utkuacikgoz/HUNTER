import os
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- LinkedIn ---
LINKEDIN_SESSION_COOKIE = os.getenv("LINKEDIN_SESSION_COOKIE", "")

# --- Anthropic Claude ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Job preferences ---
TARGET_ROLE = os.getenv("TARGET_ROLE", "Senior Product Manager")
MIN_SALARY = int(os.getenv("MIN_SALARY", "80000"))
LOCATIONS = [loc.strip() for loc in os.getenv("LOCATIONS", "EMEA,Remote,US").split(",") if loc.strip()]
MAX_JOBS_PER_DAY = int(os.getenv("MAX_JOBS_PER_DAY", "50"))

# --- Anti-detection ---
SCRAPE_DELAY_MIN = float(os.getenv("SCRAPE_DELAY_MIN", "2.0"))
SCRAPE_DELAY_MAX = float(os.getenv("SCRAPE_DELAY_MAX", "5.0"))
APPLY_DELAY_MIN = float(os.getenv("APPLY_DELAY_MIN", "3.0"))
APPLY_DELAY_MAX = float(os.getenv("APPLY_DELAY_MAX", "7.0"))
PROXY_URL = os.getenv("PROXY_URL", "")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# --- Schedule ---
HUNT_SCHEDULE_HOUR = int(os.getenv("HUNT_SCHEDULE_HOUR", "9"))
HUNT_SCHEDULE_MINUTE = int(os.getenv("HUNT_SCHEDULE_MINUTE", "0"))
FOLLOWUP_SCHEDULE_HOUR = int(os.getenv("FOLLOWUP_SCHEDULE_HOUR", "10"))

# --- Resume ---
RESUME_PATH = BASE_DIR / os.getenv("RESUME_PATH", "config/resume.pdf")

# --- Database ---
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "hunter.db")))
DB_BACKUP_DIR = BASE_DIR / "backups"

# --- Reminder ---
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "7"))

# Search queries - tailored for Utku's profile
SEARCH_QUERIES = [
    "Senior Product Manager",
    "Product Manager Fintech",
    "Product Manager Marketplace",
    "Product Manager Blockchain",
    "Product Manager SaaS",
    "Senior PM Remote",
    "Product Lead",
    "Head of Product",
]

# Platform URLs
PLATFORM_URLS = {
    "linkedin": "https://www.linkedin.com/jobs/search/",
    "indeed": "https://www.indeed.com/jobs",
    "glassdoor": "https://www.glassdoor.com/Job/jobs.htm",
    "wellfound": "https://wellfound.com/jobs",
    "remoteok": "https://remoteok.com/remote-product-manager-jobs",
}

# Resume text — loaded from file at runtime, NOT hardcoded in source
_resume_file = BASE_DIR / "config" / "resume.txt"
if _resume_file.exists():
    RESUME_TEXT = _resume_file.read_text(encoding="utf-8")
else:
    RESUME_TEXT = os.getenv("RESUME_TEXT", "")
    if not RESUME_TEXT:
        logging.getLogger(__name__).warning(
            "No resume text found. Create config/resume.txt or set RESUME_TEXT env var."
        )


def validate_config(command: str) -> list[str]:
    """Validate required config for a given command. Returns list of errors."""
    errors = []

    if command in ("hunt", "bot", "review"):
        if not TELEGRAM_BOT_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required. Talk to @BotFather on Telegram.")
        if not TELEGRAM_CHAT_ID:
            errors.append("TELEGRAM_CHAT_ID is required. Send a message to your bot, then check getUpdates API.")

    if command in ("apply", "bot"):
        if not ANTHROPIC_API_KEY:
            logging.getLogger(__name__).warning(
                "ANTHROPIC_API_KEY not set. Cover letters will use fallback template."
            )
        if not RESUME_TEXT:
            errors.append("Resume text not found. Create config/resume.txt or set RESUME_TEXT env var.")

    if command in ("apply", "hunt", "bot"):
        if not LINKEDIN_SESSION_COOKIE:
            logging.getLogger(__name__).warning(
                "LINKEDIN_SESSION_COOKIE not set. LinkedIn scraping/apply will be limited."
            )

    return errors
