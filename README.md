# HUNTER

Personal job-hunting automation (Python 3.13, asyncio). Runs entirely on your
machine — no accounts, no API keys, no deployment.

HUNTER lists the jobs and companies hiring **Head of Product / Senior Product
Manager, remote only**, that a Turkey-based candidate with no US/EU/UK work
permit can actually take: remote roles locked to US/Canada, the EU, or the UK
are dropped; worldwide/EMEA/Turkey scopes are kept. Results print to the
terminal and are stored in a local SQLite database.

```
scrape → filter/classify → store → print
```

## Features

- **Multiple sources** — Greenhouse / Lever / Ashby / Recruitee / SmartRecruiters
  ATS catalogs, We Work Remotely and RemoteOK (JSON/RSS, no browser), and
  Wellfound (Playwright).
- **Eligibility filtering** — role-title matching, remote gating, and a
  remote-scope check that drops roles locked to a jurisdiction you can't work from.
- **Company blocklist** — companies you never want to see again stay out of the
  feed, retroactively.
- **Hiring velocity** — companies posting several roles in the window rank first.
- **Multi-profile** — run several independent hunts from one checkout
  (`HUNTER_PROFILE=<name>` overlays `.env.<name>`).

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/playwright install chromium        # only for the Wellfound scraper

.venv/bin/python main.py hunt                # scrape, filter, store, print
.venv/bin/python main.py list                # re-print stored roles anytime
```

Set `ENABLE_BROWSER_SCRAPERS=false` to run API-only and skip the Chromium install.

## Commands

`main.py` is the entry point. Subcommands:

| Command  | What it does                                                |
| -------- | ----------------------------------------------------------- |
| `hunt`   | Scrape all sources, filter, store, and print the roles       |
| `list`   | Print stored open roles, grouped by company                  |
| `stats`  | Print DB stats                                               |
| `backup` | Back up the SQLite DB                                        |

No command needs credentials — there are none.

## Configuration

All settings are environment variables with defaults in
[config/settings.py](config/settings.py); [.env.example](.env.example) is the
template.

```bash
cp .env.example .env
```

The knobs you'll actually reach for: `ROLE_MATCH_KEYWORDS` (which titles count),
`SPONSOR_BLOCKLIST_COMPANIES` (companies to hide), `MAX_JOBS_PER_DAY`, and the
`*_BOARDS` lists of ATS board tokens.

## Maintenance

ATS board tokens drift as companies churn or switch ATS. Check them with:

```bash
.venv/bin/python -m scripts.verify_boards --quiet   # prints DEAD / no-PM boards
```

`DEAD` means the board is gone — prune the token. `no-PM` just means that board
has no matching opening today, which is normal.

## Development

```bash
.venv/bin/python -m pytest -q     # tests
.venv/bin/ruff check .            # lint
.venv/bin/mypy                    # type check
```
