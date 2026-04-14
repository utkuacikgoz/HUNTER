---
description: "HUNTER project context — job hunting automation tool. Use for any work on this codebase."
---

# HUNTER — Job Hunting Automation Tool

## Project Overview

Automated job hunting: scrapes 50 jobs/day from 5 platforms, sends to Telegram for approve/reject, auto-applies via Playwright, tracks applications in SQLite, sends 7-day follow-up reminders.

**Owner:** Utku Acikgoz — Senior Product Manager, 8+ years, fintech/blockchain/marketplace/SaaS.

## Stack

- **Python 3.12**, venv at `.venv/`
- **Playwright 1.58.0** — Chromium headless for scraping + auto-apply
- **python-telegram-bot 22.7** — Bot with inline keyboard (approve/reject)
- **Anthropic Claude** (`claude-sonnet-4-20250514`) — Cover letter + form answer generation
- **SQLite** — `hunter.db` with WAL mode, `jobs` + `application_log` tables
- **APScheduler 3.11.2** — Daily hunt + followup scheduled jobs
- **Tenacity 9.1.4** — Retry logic (RemoteOK scraper)
- **Docker** — Dockerfile + docker-compose.yml, non-root user

## Architecture

```
config/settings.py      — Central config from env vars, validate_config()
tracker/database.py     — SQLite CRUD, status transitions, stats, followup logic
scraper/base.py         — Abstract Playwright scraper, anti-detection (UA rotation, delays, SSRF-safe proxy)
scraper/linkedin.py     — LinkedIn scraper (session cookie auth)
scraper/indeed.py       — Indeed scraper
scraper/glassdoor.py    — Glassdoor scraper
scraper/wellfound.py    — Wellfound scraper
scraper/remoteok.py     — RemoteOK JSON API scraper (with tenacity retry)
telegram_bot/bot.py     — Telegram notifications, inline buttons, auth checks on all handlers
applicant/engine.py     — Auto-apply engine (LinkedIn Easy Apply, Indeed, Wellfound, generic)
prompts/generator.py    — Claude cover letter gen, prompt injection defense, COMMON_ANSWERS dict
main.py                 — CLI orchestrator + APScheduler bot mode + backup + graceful shutdown
tests/                  — 66 pytest tests (database, generator, scraper, telegram, config)
```

## Key Design Decisions

- **All credentials in `.env`** (gitignored), never hardcoded. Resume text in `config/resume.txt`.
- **Auth on ALL Telegram handlers** — `_is_authorized()` checks `TELEGRAM_CHAT_ID` before processing.
- **Prompt injection defense** — `_sanitize_external_text()` strips markers, uses `<<<>>>` delimiters, system prompt tells Claude to treat job descriptions as literal data.
- **INSERT OR IGNORE + changes()** — insert_job returns None for dupes, no race condition.
- **Scraper resource cleanup** — All scrapers use try/finally for page+context. Applicant engine has safe cleanup (page.close() failures don't block context.close()).
- **Parameterized SQL everywhere** — No string interpolation in queries.
- **Status whitelist** — `update_job_status()` validates against enum set.
- **Job ID bounds** — Telegram callback validates 0 < id <= 2^31-1.

## Commands

```bash
python main.py hunt       # Scrape + send to Telegram
python main.py apply      # Apply to approved jobs
python main.py followup   # Send follow-up reminders
python main.py stats      # Show statistics
python main.py bot        # Run Telegram bot with scheduler
python main.py backup     # Backup database
python -m pytest tests/ -v  # Run tests
```

## Coding Conventions

- No type hints on existing code unless changing it
- No docstrings on existing functions unless changing them
- All DB functions use try/finally for connection cleanup
- `log_action()` with external conn does NOT commit — caller controls transaction
- `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)
- Use `datetime()` SQLite function for date comparisons in queries
- Anti-detection: random delays via `SCRAPE_DELAY_MIN/MAX`, UA rotation
- Tests use a temp DB file, init once, `DELETE` rows between tests

## Security Audit History

- 3 full audits completed (23+ issues fixed)
- SSRF proxy validation, prompt injection defense, auth on all handlers, bounds checking, resource leak prevention, PII removed from source
