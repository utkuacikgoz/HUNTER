import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Profiles ---
# A profile lets one checkout run as more than one independent hunt (e.g. Utku's
# PM hunt vs. a friend's marketing hunt). Set HUNTER_PROFILE=<name> and drop a
# `.env.<name>` overlay next to `.env` — its values win via override. Give the
# profile its own DB_PATH so the two never share state. The default (empty)
# profile is the original single-hunt behavior, untouched.
HUNTER_PROFILE = os.getenv("HUNTER_PROFILE", "").strip()
if HUNTER_PROFILE:
    _overlay = BASE_DIR / f".env.{HUNTER_PROFILE}"
    if _overlay.exists():
        load_dotenv(_overlay, override=True)

# --- Logging ---
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Job preferences ---
LOCATIONS = [loc.strip() for loc in os.getenv("LOCATIONS", "EMEA,Remote,US").split(",") if loc.strip()]
MAX_JOBS_PER_DAY = int(os.getenv("MAX_JOBS_PER_DAY", "80"))
# How many of SEARCH_QUERIES to run per scrape (0 = all).
MAX_QUERIES_PER_RUN = int(os.getenv("MAX_QUERIES_PER_RUN", "0"))
# Max roles to pull from a SINGLE source per run. Set above MAX_JOBS_PER_DAY so a
# catalog source keeps scanning boards past the day's quota (more boards reached =
# more breadth); the target-driven loop then dedups/filters down to the day's fresh
# quota. Raise for more breadth, lower to rate-limit a noisy source.
SOURCE_FETCH_CAP = int(os.getenv("SOURCE_FETCH_CAP", "150"))
# How many ATS boards to fetch at once. The catalog sources walk ~60 Greenhouse and
# ~50 Ashby boards per hunt; fetched one at a time that phase is pure round-trip
# latency. Kept modest so we stay polite to each ATS. 1 restores serial fetching.
ATS_FETCH_CONCURRENCY = int(os.getenv("ATS_FETCH_CONCURRENCY", "6"))


def _csv_set(env_name: str, default: str) -> set[str]:
    raw = os.getenv(env_name, default)
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _csv_list(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _bool(env_name: str, default: bool) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


# --- Region / eligibility filtering ---
REGION_ALLOWLIST = _csv_set("REGION_ALLOWLIST", "us,eu,emea")
REMOTE_REQUIRED = _bool("REMOTE_REQUIRED", True)
# Exempt freelance/contract roles from the REMOTE_REQUIRED gate. With both on, the feed
# reads "remote OR freelance": an on-site contract gig in the candidate's own city
# survives, an on-site permanent role does not. Detection is text-only, title-first —
# see scraper/filters.detect_freelance. No effect when REMOTE_REQUIRED is off (the gate
# it modifies is already open). Only the remote gate is exempted: region and lock
# checks still apply.
ALLOW_ONSITE_FREELANCE = _bool("ALLOW_ONSITE_FREELANCE", False)
# The candidate works remotely from Turkey with no US/EU/UK work permit. A remote
# role explicitly LOCKED to a region they can't work from ("Remote - US only",
# "Remote (EU)", "Remote - Germany", "100% Remote (US/Canada)") is dropped — it
# requires residence / work authorization there, whatever the company's visa
# stance. Scopes that include Turkey (worldwide/global, EMEA, MENA, Turkey
# itself) stay in. See scraper/filters.detect_ineligible_remote_lock.
SPONSOR_FRIENDLY_COMPANIES = _csv_set(
    "SPONSOR_FRIENDLY_COMPANIES",
    "Stripe,GitLab,Automattic,Spotify,Klarna,Wise,Remote,Deel,Toptal,Doist,Buffer,Zapier,"
    "Hotjar,Elastic,HashiCorp,Sourcegraph,Vercel,Supabase,Linear,Notion,Figma,"
    "Atlassian,Mozilla,Shopify,Discord,Cloudflare,DigitalOcean,n8n,Mattermost,GitHub,"
    "MongoDB,Auth0,Postman,Snyk,Datadog,Miro,Loom,1Password,ClickUp,"
    "Databricks,Intercom",
)
# Companies to always drop. Matched against the exact lowercased company string, so list
# name variants (OpenAI posts as "OpenAI" / "Open AI" / "Open-AI").
SPONSOR_BLOCKLIST_COMPANIES = _csv_set(
    "SPONSOR_BLOCKLIST_COMPANIES",
    # Dataiku: does not hire Turkish citizens (owner-verified 2026-08-31).
    "canonical,openai,open-ai,open ai,dataiku",
)

# --- ATS catalog sources (Greenhouse / Lever / Ashby) ---
# Values are board *tokens* (not display names). Defaults are live-verified
# (2026-06-04); find a token by hitting e.g.
# boards-api.greenhouse.io/v1/boards/{token}/jobs (200 + non-empty jobs = valid).
# Boards are scanned in two tiers: the PRIORITY list below (EU/EMEA/global-remote,
# sponsor-friendly) is scanned first — rotated daily so its tail gets coverage — and
# the *_US_BOARDS list (US-only-remote giants) is only reached when the per-run cap
# isn't already filled. This keeps the feed weighted toward roles an overseas
# candidate can actually take. Tokens live-verified 2026-06-04..22.
GREENHOUSE_BOARDS = _csv_list(
    "GREENHOUSE_BOARDS",
    # tier-2/3 (fintech / crypto / marketplace / saas)
    "gocardless,form3,tide,truelayer,mercury,fireblocks,bitpanda,nansen,faire,"
    "wallapop,gigs,contentful,typeform,planetscale,"
    # EU/EMEA-focused (hire product in EU/EMEA)
    "monzo,sumup,getyourguide,doctolib,celonis,wolt,n26,hellofresh,trustpilot,"
    "skyscanner,adyen,algolia,raisin,cleo,graphcore,freenow,consensys,"
    "intercom,"
    # YC / well-known startups (region filter drops US-only)
    "brex,gusto,clickhouse,flexport,checkr,mixpanel,webflow,lithic,highnote,"
    # tier-1
    "stripe,datadog,mongodb,cloudflare,figma,gitlab,elastic,postman,"
    "vercel,discord,mozilla,mattermost,remote,"
    # EU/EMEA + global-remote batch, live-verified 2026-07-28
    "customerio,hightouch,qualio,goodnotes,invisible,"
    # web3 / DeFi / crypto batch (global-remote teams; the region filter drops
    # US-locked postings). Live-verified 2026-08-31; the rest of the original
    # batch 404'd (those companies are no longer on Greenhouse) and was pruned.
    "ripple",
)
# US-only-remote giants — scanned last (leftover cap only). Mostly post roles
# locked to US work authorization, which the filter drops for an overseas candidate.
GREENHOUSE_US_BOARDS = _csv_list(
    "GREENHOUSE_US_BOARDS",
    "databricks,airbnb,dropbox,lyft,pinterest,robinhood,coinbase,asana,twilio,"
    "affirm,marqeta,instacart,calendly,airtable,sofi,scaleai,samsara,twitch,"
    "chime,squarespace,reddit,upstart,betterment,peloton",
)
LEVER_BOARDS = _csv_list(
    "LEVER_BOARDS",
    # live-verified 2026-06-08/09: contentsquare/younited + YC (metabase/finch)
    "qonto,vestiairecollective,spotify,toptal,contentsquare,younited,"
    "metabase,finch,"
    # live-verified 2026-06-22
    "matchgroup,swordhealth",
)
ASHBY_BOARDS = _csv_list(
    "ASHBY_BOARDS",
    # tier-2/3
    "pleo,mollie,pennylane,sardine,taktile,swan,ramp,ledger,blockdaemon,safe,"
    "paxos,backmarket,gorgias,posthog,workos,fonoa,"
    # EU/EMEA-focused
    "synthesia,alan,tacto,wayve,vanta,photoroom,harvey,plaid,lovable,"
    # YC / AI-infra startups (often global/remote)
    "zip,cohere,temporal,replit,watershed,airbyte,mux,render,baseten,neon,"
    "weaviate,pinecone,langchain,llamaindex,column,"
    # tier-1
    "notion,1password,clickup,n8n,linear,zapier,supabase,buffer,"
    # EU/EMEA + global-remote batch, live-verified 2026-07-28
    "elevenlabs,attio,checkly,revenuecat,kong,resend,mazedesign,infisical,"
    "junction,natter,phantom,assured,"
    # web3/crypto batch, live-verified 2026-08-31 (uniswaplabs/kraken guesses
    # came back dead and were pruned).
    "dune,opensea,alchemy",
)
# US-only-remote AI labs / startups — scanned last (leftover cap only).
ASHBY_US_BOARDS = _csv_list(
    "ASHBY_US_BOARDS",
    "perplexity,cursor,character,sierra,decagon,drata,abridge,speak,suno,crusoe",
)
# Recruitee boards — subdomain token (https://{token}.recruitee.com/api/offers/).
# sendcloud pruned 2026-08-31 (404 — board gone).
RECRUITEE_BOARDS = _csv_list("RECRUITEE_BOARDS", "bunq")
# SmartRecruiters company identifiers (case-sensitive, as in their posting API
# https://api.smartrecruiters.com/v1/companies/{id}/postings).
# Visa pruned 2026-08-31 (board returns 0 postings).
SMARTRECRUITERS_COMPANIES = _csv_list("SMARTRECRUITERS_COMPANIES", "")
# Catalog sources list every role on a board; keep only titles containing one of
# these (case-insensitive substring). Target: Head of Product / Senior Product
# Manager, widened (2026-08-31, feed ran thin) to the adjacent senior-plus
# titles — lead/principal/director. Deliberately still excludes bare "product
# manager", group PM, and product owner. Override via env to retarget.
ROLE_MATCH_KEYWORDS = _csv_list(
    "ROLE_MATCH_KEYWORDS",
    "head of product,senior product manager,sr product manager,sr. product manager,"
    "lead product manager,principal product manager,director of product",
)
# Titles containing one of these (whole-word match) are dropped even if they also match a
# ROLE_MATCH_KEYWORD — the candidate wants senior+ roles. Matched word-boundary-safe so
# "intern" doesn't kill "International Product Manager".
ROLE_EXCLUDE_KEYWORDS = _csv_list(
    "ROLE_EXCLUDE_KEYWORDS",
    "junior,jr,associate,intern,apprentice,working student,graduate,"
    "entry level,entry-level,trainee,co-op,student,"
    # "Head/Director of Product X" adjacent functions that aren't the PM role itself.
    "head of product design,head of product marketing,"
    "director of product design,director of product marketing",
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
# How long an auto-skip lasts before the source gets another chance. A skipped run
# records nothing, so without this the zero-streak never ages out and a transient
# upstream outage would disable that source permanently (until scraper_health is
# cleared by hand). 0 restores that old permanent behavior.
SCRAPER_RETRY_AFTER_DAYS = int(os.getenv("SCRAPER_RETRY_AFTER_DAYS", "3"))
# Run the browser-based scrapers (Wellfound — it launches Chromium). Set false to
# run API-only (no Playwright/Chromium): lighter and needs no browser install.
ENABLE_BROWSER_SCRAPERS = _bool("ENABLE_BROWSER_SCRAPERS", True)

# --- Hiring velocity (DB-only) ---
# Window over which we count distinct roles per company.
VELOCITY_WINDOW_DAYS = int(os.getenv("VELOCITY_WINDOW_DAYS", "14"))
# Companies with at least this many distinct roles in the window get the 🔥 badge.
VELOCITY_HOT_THRESHOLD = int(os.getenv("VELOCITY_HOT_THRESHOLD", "3"))
# When true, rank pending jobs from hot companies above the rest.
VELOCITY_BOOST_RANK = _bool("VELOCITY_BOOST_RANK", True)

# Chromium launch args shared by every headless-browser call site.
#   --disable-dev-shm-usage: small /dev/shm environments crash the renderer
#     without it (spills to /tmp instead).
#   --disable-gpu / --disable-extensions / --disable-background-networking: trim
#     memory and startup work.
# NOTE: intentionally NOT --no-sandbox — disabling the sandbox would weaken
# isolation while browsing untrusted job pages.
CHROMIUM_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--disable-background-networking",
]

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

# --- Database ---
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "hunter.db")))
DB_BACKUP_DIR = BASE_DIR / "backups"

# --- Follow-up window (DB-only; used by tracker.get_jobs_needing_followup) ---
FOLLOWUP_DAYS = int(os.getenv("FOLLOWUP_DAYS", "7"))

# Search queries for query-driven sources (Wellfound). Default mirrors the
# narrow ROLE_MATCH_KEYWORDS target; override via env (comma-separated) for a
# different target, e.g. a marketing profile.
SEARCH_QUERIES = _csv_list(
    "SEARCH_QUERIES",
    "Senior Product Manager,Head of Product",
)

# RemoteOK is tag-driven: its API is /api?tag=<tag>. Multi-word SEARCH_QUERIES turn
# into dead tags (e.g. "senior-product-manager"), so RemoteOK uses these real tags
# instead. crypto/web3/defi widen the net into web3 companies; the title filter
# (ROLE_MATCH_KEYWORDS) still trims every tag down to the target roles.
REMOTEOK_TAGS = _csv_list("REMOTEOK_TAGS", "product-manager,product,crypto,web3,defi")

# We Work Remotely category RSS feeds (remote-only). The Product feed is broad —
# the title filter trims it to the target roles.
WEWORKREMOTELY_FEEDS = _csv_list(
    "WEWORKREMOTELY_FEEDS",
    "https://weworkremotely.com/categories/remote-product-jobs.rss",
)
