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
- Terse ≠ skipping work. Required gates (tests, lint, type-check, CI) still run and
  still get reported — just briefly.
- Code, comments, commit messages, and PR bodies are unaffected. Caveman is chat only.

## What HUNTER is

A personal job-hunting tool (Python 3.13, asyncio) that runs **entirely locally** —
no credentials, no deployment, no LLM. The brief: list the jobs and companies hiring
**Head of Product / Senior Product Manager, remote only**, that a Turkish citizen with
no US/EU/UK work permit can actually take. A remote role locked to US/Canada, the EU,
or the UK ("Remote - US", "Remote (EU)", "Remote - Germany") is dropped;
worldwide/EMEA/Turkey scopes are kept; bare "Remote" is flagged for review. It scrapes
several sources, filters, stores to a local SQLite DB, and prints the list.

Pipeline: **scrape → filter/classify → store → print**

## Layout

- [main.py](main.py) — CLI entry point and orchestrator. Subcommands: `hunt`, `list`,
  `stats`, `backup`.
- [config/settings.py](config/settings.py) — **all configuration**, read from env via
  `python-dotenv`. Holds no secrets: there are none.
- [scraper/](scraper/) — job sources.
  - [base.py](scraper/base.py): `JobSource` contract; `BrowserSource` (Playwright/Chromium)
    and `ApiSource` (aiohttp JSON, no browser). `BaseScraper` is an alias of `BrowserSource`.
    `matches_role_title` gates catalog titles before `filters.py` runs.
  - [ats.py](scraper/ats.py): Greenhouse / Lever / Ashby / Recruitee / SmartRecruiters
    API catalog sources.
  - [remoteok.py](scraper/remoteok.py), [weworkremotely.py](scraper/weworkremotely.py):
    remote-only API/RSS sources. [wellfound.py](scraper/wellfound.py): the only browser scraper.
  - [filters.py](scraper/filters.py): `evaluate_job` → verdict `include` / `flag` / `drop`.
    Pure Python, no network.
- [tracker/database.py](tracker/database.py) — SQLite access. Auto-migrates by adding
  missing columns; WAL mode. DB at `hunter.db` (override with `DB_PATH`).
  `get_pending_jobs` also filters out blocklisted companies, so blocklisting is retroactive.
- [scripts/verify_boards.py](scripts/verify_boards.py) — checks every configured ATS
  board token is still live and carries matching roles.
- [tests/](tests/) — pytest suite (asyncio auto mode).

## Environment / config

All settings are environment variables with defaults in
[config/settings.py](config/settings.py) — the single source of truth for every var
and the long lists (blocklist, ATS boards, role keywords). Don't read `os.getenv`
elsewhere; add a setting there and import it. [.env.example](.env.example) is the
template; `.env` is gitignored.

```
cp .env.example .env
```

No command requires credentials. There are no secrets in this project: no tokens,
no API keys, no PII. Keep it that way.

### Profiles (running more than one hunt from one checkout)

Set `HUNTER_PROFILE=<name>`; settings.py then loads `.env.<name>` as an **overlay on
top of `.env`** (overlay wins). Give the profile its own `DB_PATH` so the two never
share state.

```
HUNTER_PROFILE=friend .venv/bin/python main.py hunt
```

`.env.<name>` is gitignored. `SEARCH_QUERIES`, `ROLE_MATCH_KEYWORDS`, `REMOTEOK_TAGS`,
and `WEWORKREMOTELY_FEEDS` retarget the search (e.g. PM → marketing) — note
`ROLE_MATCH_KEYWORDS` gates catalog titles *before* `filters.py` runs, and matches
plain substrings. `ALLOW_ONSITE_FREELANCE` (with `REMOTE_REQUIRED=true`) makes the
feed read "remote OR freelance"; it exempts only the remote gate, region and lock
checks still apply.

## Setup & common commands

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/playwright install chromium        # only for the Wellfound scraper

.venv/bin/python main.py hunt                # scrape + filter + print
.venv/bin/python main.py list                # print stored open roles by company
.venv/bin/python main.py stats               # DB-only, safe smoke test
```

Quality gates (mirror CI in [.github/workflows/ci.yml](.github/workflows/ci.yml)):

```bash
.venv/bin/python -m pytest -q     # 236 tests
.venv/bin/ruff check .            # lint
.venv/bin/mypy                    # type check (files configured in pyproject.toml)
```

CI installs from [requirements.lock](requirements.lock) and runs lint → mypy → bandit →
pip-audit → pytest on Python 3.13. `coverage fail_under = 40` is a regression ratchet,
not a target (network-bound scraper paths can't be unit-tested without live services).

`requirements.txt` holds direct pins; `requirements.lock` is the fully-pinned freeze CI
installs. Dependabot bumps only `requirements.txt`, so CI runs
[scripts/check_lock_sync.py](scripts/check_lock_sync.py) to fail when the two drift.
After bumping `requirements.txt`, regenerate the lock:
`pip install -r requirements.txt && pip freeze > requirements.lock`.

## Maintenance

ATS board tokens drift. `python -m scripts.verify_boards --quiet` lists `DEAD` boards
(gone — prune the token) and `no-PM` boards (alive, just no matching opening today —
normal, leave them).

## Conventions

- Python 3.13 target; ruff line-length 110 (E501 ignored). Type-checked with mypy.
- Config is centralized in `config/settings.py`; don't read `os.getenv` elsewhere.
- New job sources subclass `ApiSource` (preferred, no browser) or `BrowserSource`.
- **No AI attribution.** Commits are authored by the repository owner, and nothing
  in git may say otherwise. No `Co-Authored-By: Claude`/`Codex` trailer, no
  `Claude-Session:` trailer, no "Generated with Claude Code" line in a commit or PR
  body, and no `claude/…` or `codex/…` branch name.
