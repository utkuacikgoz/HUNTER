# HUNTER

Personal job-hunting automation for Product Manager roles (Python 3.12, asyncio).

HUNTER scrapes PM postings from several sources, filters them by region /
remote / visa-sponsor signals, pushes candidates to a Telegram bot for human
review, then (optionally) auto-applies to approved jobs with Playwright and
Claude-generated cover letters. All state lives in a local SQLite database.

```
scrape → filter/classify → store → Telegram review → apply → track/follow-up
```

## Features

- **Multiple sources** — Greenhouse / Lever / Ashby ATS catalogs and Wellfound
  (Playwright), We Work Remotely / RemoteOK (JSON/RSS, no browser).
- **Smart filtering** — role-title matching, region/remote gating, and an
  LLM sponsor-likelihood classifier; external text is sanitized before it ever
  reaches a prompt.
- **Human-in-the-loop** — review candidates in Telegram; approve to auto-apply.
- **Auto-apply** — Playwright fills applications with Claude-written cover
  letters (falls back to a template when no API key is set).
- **Multi-profile** — run several independent bots from one checkout
  (`HUNTER_PROFILE=<name>` overlays `.env.<name>`).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/playwright install chromium        # needed for scrape/apply paths

cp .env.example .env                          # then fill in your values
.venv/bin/python main.py stats               # DB-only, safe smoke test
.venv/bin/python main.py hunt                # scrape + push to Telegram (needs creds)
```

## Commands

`main.py` is the entry point. Subcommands:

| Command    | What it does                                                            |
| ---------- | ----------------------------------------------------------------------- |
| `hunt`     | Scrape all sources, filter, and push candidates to Telegram             |
| `apply`    | Auto-apply to approved jobs                                             |
| `followup` | Send follow-ups on applied jobs                                         |
| `stats`    | Print DB stats (no credentials required)                                |
| `bot`      | Run the Telegram bot + APScheduler cron (daily hunt, follow-up, backup) |
| `backup`   | Back up the SQLite DB                                                   |

`stats`, `backup`, and `followup` need no credentials — use them to smoke-test.

## Configuration

All settings are environment variables, centralized in
[config/settings.py](config/settings.py) (the single source of truth — don't
call `os.getenv` elsewhere). [.env.example](.env.example) is the template, split
into **SECRETS/PII** (never committed) and **non-secret CONFIG** sections.

**Secrets** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`,
`RESUME_TEXT`, all `APPLICANT_*` PII) go in `.env` locally and `fly secrets set`
in production. Everything else is non-secret config. `validate_config(command)`
gates each subcommand on the vars it actually needs.

See [CLAUDE.md](CLAUDE.md) for the full config reference, profiles, and
architecture notes.

## Deployment

Runs on [Fly.io](https://fly.io) — non-secret config lives in `[env]` in
[fly.toml](fly.toml); secrets are set with `fly secrets set …` (the exact
command is documented at the top of `fly.toml`).

## Quality gates

CI (Python 3.12) installs from [requirements.lock](requirements.lock) and runs
lint → type check → security lint → dependency audit → tests, plus a full-history
secret scan:

```bash
.venv/bin/python -m pytest -q --cov=.   # tests + coverage (fail_under = 40 ratchet)
.venv/bin/ruff check .                   # lint
.venv/bin/mypy                           # type check
.venv/bin/bandit -q -r . -ll -x ./tests  # security lint (medium+)
.venv/bin/pip-audit -r requirements.lock # dependency CVE audit
```

The `coverage fail_under = 40` floor is a regression ratchet, not a target —
the Playwright/network/LLM I/O paths can't be unit-tested without live services.
