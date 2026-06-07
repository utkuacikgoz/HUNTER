import logging
import os
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
# Model used for cover letters, form answers, and sponsor scoring. Override via env.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Job preferences ---
TARGET_ROLE = os.getenv("TARGET_ROLE", "Senior Product Manager")
MIN_SALARY = int(os.getenv("MIN_SALARY", "80000"))
LOCATIONS = [loc.strip() for loc in os.getenv("LOCATIONS", "EMEA,Remote,US").split(",") if loc.strip()]
MAX_JOBS_PER_DAY = int(os.getenv("MAX_JOBS_PER_DAY", "50"))
# How many of SEARCH_QUERIES to run per scrape (0 = all). Previously hardcoded to 3.
MAX_QUERIES_PER_RUN = int(os.getenv("MAX_QUERIES_PER_RUN", "0"))


def _csv_set(env_name: str, default: str) -> set[str]:
    raw = os.getenv(env_name, default)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _csv_list(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Region / sponsor filtering ---
REGION_ALLOWLIST = _csv_set("REGION_ALLOWLIST", "us,eu,emea")
REMOTE_REQUIRED = os.getenv("REMOTE_REQUIRED", "true").strip().lower() in {"1", "true", "yes", "y"}
# Regions where the candidate can actually work (no sponsorship/relocation needed).
# A remote role explicitly LOCKED to countries outside these regions (e.g.
# "Remote - US only", "100% Remote (US/Canada)") is dropped — it won't accept an
# overseas candidate even at a sponsor-friendly company. Empty disables the check.
CANDIDATE_WORK_REGIONS = _csv_set("CANDIDATE_WORK_REGIONS", "emea,eu")
SPONSOR_FRIENDLY_COMPANIES = _csv_set(
    "SPONSOR_FRIENDLY_COMPANIES",
    "Stripe,GitLab,Automattic,Spotify,Klarna,Wise,Remote,Deel,Toptal,Doist,Buffer,Zapier,"
    "Hotjar,Canonical,Elastic,HashiCorp,Sourcegraph,Vercel,Supabase,Linear,Notion,Figma,"
    "Atlassian,Mozilla,Shopify,Discord,Cloudflare,DigitalOcean,n8n,Mattermost,GitHub,"
    "MongoDB,Auth0,Postman,Snyk,Datadog,Miro,Loom,1Password,ClickUp",
)
SPONSOR_BLOCKLIST_COMPANIES = _csv_set("SPONSOR_BLOCKLIST_COMPANIES", "")
ENABLE_LLM_SPONSOR_SCORING = os.getenv("ENABLE_LLM_SPONSOR_SCORING", "false").strip().lower() in {"1", "true", "yes", "y"}

# --- ATS catalog sources (Greenhouse / Lever / Ashby) ---
# Values are board *tokens* (not display names). Defaults are live-verified
# (2026-06-04); find a token by hitting e.g.
# boards-api.greenhouse.io/v1/boards/{token}/jobs (200 + non-empty jobs = valid).
# Tier-2/3 scale-ups are listed FIRST so they fill the per-run job cap before the
# tier-1 giants (each source stops once it has enough roles). All board tokens are
# live-verified (2026-06-04).
GREENHOUSE_BOARDS = _csv_list(
    "GREENHOUSE_BOARDS",
    # tier-2/3 (fintech / crypto / marketplace / saas)
    "gocardless,form3,tide,truelayer,mercury,fireblocks,bitpanda,nansen,faire,"
    "wallapop,gigs,contentful,typeform,planetscale,"
    # tier-1
    "stripe,datadog,mongodb,canonical,cloudflare,figma,gitlab,elastic,postman,"
    "vercel,discord,mozilla,mattermost,remote",
)
LEVER_BOARDS = _csv_list("LEVER_BOARDS", "qonto,vestiairecollective,spotify,toptal")
ASHBY_BOARDS = _csv_list(
    "ASHBY_BOARDS",
    # tier-2/3
    "pleo,mollie,pennylane,sardine,taktile,swan,ramp,ledger,blockdaemon,safe,"
    "paxos,backmarket,gorgias,posthog,workos,"
    # tier-1
    "notion,1password,clickup,deel,n8n,linear,zapier,supabase,buffer",
)
# Catalog sources list every role on a board; keep only titles containing one of
# these (case-insensitive substring). Anchored on the role function (…manager /
# lead / owner / head / director / vp of product) so "Staff Product Designer" and
# similar non-PM roles drop out. Override via env for a different target.
ROLE_MATCH_KEYWORDS = _csv_list(
    "ROLE_MATCH_KEYWORDS",
    "product manager,product management,product lead,lead product manager,"
    "head of product,product owner,director of product,vp of product,vp product,"
    "chief product,group product manager,principal product manager,"
    "senior product manager,staff product manager,technical product manager,"
    "platform product manager",
)

# --- Scraper health / selector overrides ---
# Comma-separated Playwright selectors tried in order. Add new variants as Wellfound DOM drifts.
WELLFOUND_SELECTORS = [
    s.strip() for s in os.getenv(
        "WELLFOUND_SELECTORS",
        "div.styles_jobListing__aFBtk,div[class*='jobListing'],div.mb-6",
    ).split(",") if s.strip()
]
# Skip a scraper if its last N runs all returned 0 jobs (0 disables the check).
SCRAPER_SKIP_AFTER_ZEROS = int(os.getenv("SCRAPER_SKIP_AFTER_ZEROS", "3"))

# --- Hiring velocity (DB-only) ---
# Window over which we count distinct roles per company.
VELOCITY_WINDOW_DAYS = int(os.getenv("VELOCITY_WINDOW_DAYS", "14"))
# Companies with at least this many distinct roles in the window get the 🔥 badge.
VELOCITY_HOT_THRESHOLD = int(os.getenv("VELOCITY_HOT_THRESHOLD", "3"))
# When true, rank pending jobs from hot companies above the rest.
VELOCITY_BOOST_RANK = os.getenv("VELOCITY_BOOST_RANK", "true").strip().lower() in {"1", "true", "yes", "y"}

# --- Anti-detection ---
SCRAPE_DELAY_MIN = float(os.getenv("SCRAPE_DELAY_MIN", "2.0"))
SCRAPE_DELAY_MAX = float(os.getenv("SCRAPE_DELAY_MAX", "5.0"))
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
    "wellfound": "https://wellfound.com/jobs",
    "remoteok": "https://remoteok.com/remote-product-manager-jobs",
}

# Resume text — loaded from file at runtime, NOT hardcoded in source.
# The "missing resume" warning is emitted from validate_config(), not at import,
# so it doesn't spam during tests or unrelated CLI commands.
_resume_file = BASE_DIR / "config" / "resume.txt"
if _resume_file.exists():
    RESUME_TEXT = _resume_file.read_text(encoding="utf-8")
else:
    RESUME_TEXT = os.getenv("RESUME_TEXT", "")


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
