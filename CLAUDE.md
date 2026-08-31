# CLAUDE.md

Guidance for working in this repository.

<!-- caveman-mode: keep this block in sync across repos -->
## Caveman mode

Default to caveman. Short. No filler.

- No preamble, no recap, no "I'll now…". Lead with the answer.
- Status is fragments, not sentences: "Bug found." "Fix. Test." "Done."
- No closing summary unless asked. Never restate the diff in bullets.
- Tradeoffs, pushback, and explanations: plain English, still short. Not caveman —
  clarity wins there.
- Terse ≠ skipping work. Required gates (tests, lint, type-check, CI, deploy checks)
  still run and still get reported — just briefly.
- Code, comments, commit messages, and PR bodies are unaffected. Caveman is chat only.

## What HUNTER is

A personal job-hunting automation tool (Python 3.13, asyncio). The brief: list the
jobs and companies hiring **Head of Product / Senior Product Manager, remote only**,
that a Turkish citizen with no US/EU/UK work permit can actually take. A remote role
locked to US/Canada, the EU, or the UK ("Remote - US", "Remote (EU)", "Remote -
Germany") is dropped; worldwide/EMEA/Turkey scopes are kept; bare "Remote" is
flagged for review. It scrapes several sources, filters, stores to a local SQLite
DB, and either prints the list (`list`, or credential-free `hunt`) or pushes to a
Telegram bot for review; the Playwright auto-apply path is still there behind
Telegram approval.

Pipeline: **scrape → filter/classify → store → list / Telegram review → apply → track/follow-up**

## Layout

- [main.py](main.py) — CLI entry point and orchestrator. Subcommands: `hunt`, `list`,
  `apply`, `followup`, `stats`, `bot`, `backup`. `bot` runs the Telegram bot plus an APScheduler
  cron (daily hunt, follow-up, DB backup, screenshot prune) with graceful shutdown.
- [config/settings.py](config/settings.py) — **all configuration**, read from env via
  `python-dotenv`. Also `validate_config(command)` which gates each subcommand on its
  required vars. See "Environment / config" below.
- [scraper/](scraper/) — job sources.
  - [base.py](scraper/base.py): `JobSource` contract; `BrowserSource` (Playwright/Chromium)
    and `ApiSource` (aiohttp JSON, no browser). `BaseScraper` is an alias of `BrowserSource`.
  - Browser scrapers: [wellfound.py](scraper/wellfound.py), [remoteok.py](scraper/remoteok.py),
    [linkedin.py](scraper/linkedin.py) (disabled — session-cookie issues, kept for Phase 3).
  - [ats.py](scraper/ats.py): Greenhouse / Lever / Ashby API catalog sources.
  - [filters.py](scraper/filters.py): `evaluate_job_async` → verdict `include` / `flag` / `drop`.
- [tracker/database.py](tracker/database.py) — SQLite access. Auto-migrates by adding
  missing columns; WAL mode. DB at `hunter.db` (override with `DB_PATH`).
- [prompts/generator.py](prompts/generator.py) — Claude cover-letter / answer generation and
  sponsor scoring. Three models, picked per task and set in [config/settings.py](config/settings.py)
  (each overridable via env): `COVER_LETTER_MODEL` (strongest — low-volume, represents you to
  employers), `CLAUDE_MODEL` (form answers), `SPONSOR_MODEL` (cheap yes/no classifier run on most
  flagged jobs every hunt). Falls back to a template if `ANTHROPIC_API_KEY` is unset.
- [applicant/engine.py](applicant/engine.py) — Playwright auto-apply engine. Fills text
  fields from `COMMON_ANSWERS`, free-text questions from the LLM, and dropdowns in three
  passes: native `<select>`, react-select comboboxes (what current Greenhouse forms use —
  there is no `<select>` on them at all), and radio/checkbox groups. Each dropdown is
  answered by rule first (`_select_target`) and by `choose_dropdown_option` second, which
  picks from the option list actually on the page. Demographic questions resolve to
  "decline" by rule and are never sent to the model. Before submitting it checks
  `_missing_required_fields`: an incomplete form is never submitted, it's handed back as a
  manual apply naming the unanswered questions. A submit that isn't confirmed is recorded
  as `apply_submitted_unconfirmed` and never retried automatically (a duplicate application
  is worse than none).
- [telegram_bot/bot.py](telegram_bot/bot.py) — Telegram review UI and apply worker queue.
- [tests/](tests/) — pytest suite (asyncio auto mode).

## Environment / config

All settings are environment variables. There are three layers, in override order:

1. **Defaults** — [config/settings.py](config/settings.py). Single source of truth for
   every var and the long lists (sponsor companies, ATS boards, search queries). Don't
   read `os.getenv` elsewhere; add a setting here and import it.
2. **Non-secret config** — committed. Local: `.env`. Production: `[env]` in
   [fly.toml](fly.toml). Only set a key to override a default.
3. **Secrets + PII** — never committed. Local: `.env` (gitignored). Production:
   `fly secrets set ...` (see the documented command in [fly.toml](fly.toml)).

[.env.example](.env.example) is the template, split into SECRETS vs CONFIG sections that
mirror this split. Local dev uses one `.env` for both; production splits them across
`fly secrets` and `fly.toml [env]`.

```
cp .env.example .env    # local dev
```

`validate_config(command)` in settings.py defines what each subcommand *requires* vs. warns on.

**Secrets** (→ `fly secrets`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`,
`RESUME_TEXT`, all `APPLICANT_*` PII, and optional `LINKEDIN_SESSION_COOKIE`.
Everything else is non-secret config.

Required vars by command (from `validate_config`):

| Command | Hard requirement | Warns if missing |
|---|---|---|
| `bot` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | `LINKEDIN_SESSION_COOKIE` |
| `hunt` | none — without Telegram creds it prints the list instead of sending | Telegram creds, `LINKEDIN_SESSION_COOKIE` |
| `apply`, `bot` | `config/resume.txt` or `RESUME_TEXT` | `ANTHROPIC_API_KEY`, `LINKEDIN_SESSION_COOKIE` |
| `list`, `stats`, `backup`, `followup` | none (DB-only / local) | — |

`list`, `stats`, `backup`, and `followup` run with no credentials, so use them to
smoke-test; `hunt` needs only network.
Resume text is read from `config/resume.txt` (preferred) or the `RESUME_TEXT` env var.

### Profiles (running more than one bot from one checkout)

The same code can run as several independent bots (e.g. Utku's PM hunt + a friend's
marketing hunt) without a second deployment. Set `HUNTER_PROFILE=<name>` in the real
environment; settings.py then loads `.env.<name>` (or `/data/.env.<name>` in prod) as
an **overlay on top of `.env`** (overlay wins), and prefers `config/resume.<name>.txt`.
Give the profile its own `DB_PATH` and Telegram bot so the two never share state.

```
HUNTER_PROFILE=friend .venv/bin/python main.py bot        # one profile
HUNTER_PROFILES=,friend .venv/bin/python main.py bot-all  # supervisor: default + friend
```

`.env.<name>` and `config/resume.<name>.txt` are gitignored (PII); a committed
`.env.<name>.example` is the template. Relevant toggles: `ENABLE_AUTO_APPLY` (false →
review UI drops Approve, `/apply` refuses), `ENABLE_COVER_LETTERS` (when auto-apply is
off, false → pure job feed, true → adds a 📄 Cover Letter button), and
`ENABLE_BROWSER_SCRAPERS` (false → API-only sources, no Chromium — only Wellfound is a
browser scraper), and `ALLOW_ONSITE_FREELANCE` (with `REMOTE_REQUIRED=true` → feed reads
"remote OR freelance"; exempts only the remote gate, region/sponsor checks still apply).
`SEARCH_QUERIES`, `ROLE_MATCH_KEYWORDS`, `REMOTEOK_TAGS`, and `WEWORKREMOTELY_FEEDS`
retarget the search (e.g. PM → marketing) — note `ROLE_MATCH_KEYWORDS` gates catalog
titles *before* `filters.py` runs, and matches plain substrings. A marketing,
sourcing-only, remote-or-freelance profile is a typical use of these toggles.

## Setup & common commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/playwright install chromium        # needed for scrape/apply paths

.venv/bin/python main.py stats               # DB-only, safe smoke test
.venv/bin/python main.py hunt                # scrape + filter; prints list if no Telegram creds
.venv/bin/python main.py list                # print stored open roles by company (no creds)
```

Quality gates (mirror CI in [.github/workflows/ci.yml](.github/workflows/ci.yml)):

```bash
.venv/bin/python -m pytest -q     # 396 tests
.venv/bin/ruff check .            # lint
.venv/bin/mypy                    # type check (files configured in pyproject.toml)
```

CI installs from [requirements.lock](requirements.lock) and runs lint → mypy → pytest on
Python 3.13 — the same minor version the Docker image (and therefore prod) runs. `coverage fail_under = 40` is a regression ratchet, not a target (I/O-heavy
paths can't be unit-tested without live services).

`requirements.txt` holds direct pins; `requirements.lock` is the fully-pinned freeze CI
and prod install. Dependabot bumps only `requirements.txt`, so CI runs
[scripts/check_lock_sync.py](scripts/check_lock_sync.py) to fail when the two drift.
After bumping `requirements.txt`, regenerate the lock:
`pip install -r requirements.txt && pip freeze > requirements.lock`.

## Conventions

- Python 3.13 target; ruff line-length 110 (E501 ignored). Type-checked with mypy
  (`telegram_bot.bot` relaxes two telegram-Optional noise codes — see pyproject.toml).
- Config is centralized in `config/settings.py`; don't read `os.getenv` elsewhere — add a
  setting there and import it.
- New job sources subclass `ApiSource` (preferred, no browser) or `BrowserSource`.
- External/untrusted text (job descriptions) is sanitized before going into LLM prompts —
  see `_sanitize_external_text` in [prompts/generator.py](prompts/generator.py).
- **No AI attribution.** Commits are authored by the repository owner, and nothing
  in git may say otherwise. No `Co-Authored-By: Claude`/`Codex` trailer, no
  `Claude-Session:` trailer, no "Generated with Claude Code" line in a commit or PR
  body, and no `claude/…` or `codex/…` branch name.

## Planning docs

[IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md) and [PHASE_2_PLAN.md](PHASE_2_PLAN.md) track the
roadmap. LinkedIn scraping/apply is deferred to "Phase 3".
