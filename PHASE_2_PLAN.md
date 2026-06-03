# Phase 2 — Sourcing Engine (detailed plan)

> Goal: go wide and reliable on job sources. Split the browser-centric source
> interface so API sources stop launching Chromium, then add ATS APIs → generic
> career pages → Twitter/X, in that order of reliability-per-effort. Every new
> source flows through the existing filter (`scraper/filters.py`), dedup
> (`insert_job` URL-unique), and health (`record_scraper_run` /
> `should_skip_scraper`) machinery unchanged.

## Context / why

Today only 2 sources are live and both go through `BaseScraper`, which launches a
headless browser in `__aenter__` — even `RemoteOKScraper`, which only uses
`aiohttp` and never calls `new_page()` (confirmed in [scraper/remoteok.py](scraper/remoteok.py)).
That's wasted resource and a leaky abstraction. The user's goal #2 is niche/long-tail
coverage (ATS boards, company career pages, social), which is essentially greenfield.

## Current contract to preserve

`main._scrape_all` ([main.py:72](main.py#L72)) does, per source:
`async with source:` → `await source.scrape(query, location, max_results)` →
reads `source.platform_name` → `record_scraper_run(platform, n)`.
Jobs are dicts from `_normalize_job(...)`. Keep this exact contract so the
orchestrator and filters need no rewrite.

---

## Step 1 — Split the source interface (foundation)

**Files:** [scraper/base.py](scraper/base.py) (+ new `scraper/api_base.py` or keep in `base.py`).

Introduce a 3-type hierarchy:

- `JobSource` (ABC): `platform_name`, `accepts_query: bool = True`, abstract
  `async scrape(...)`, shared `_normalize_job(...)`, shared `delay()`, and a
  **default no-op** `__aenter__`/`__aexit__`.
- `BrowserSource(JobSource)`: today's Playwright logic (the current `BaseScraper`
  body — `__aenter__` launches Chromium, `new_page()`, proxy guard). Rename the
  class to `BrowserSource` and keep **`BaseScraper = BrowserSource`** as a
  backward-compat alias (so `WellfoundScraper`, `LinkedInScraper`, and
  [tests/test_scraper.py](tests/test_scraper.py) keep importing `BaseScraper`).
- `ApiSource(JobSource)`: opens a shared `aiohttp.ClientSession` in `__aenter__`,
  closes it in `__aexit__`, and exposes `_get_json(url, *, params=None)` — the
  generalized, tenacity-retried version of RemoteOK's `_fetch_api` (keep the 429
  handling + exponential backoff).

Then move `RemoteOKScraper` to extend `ApiSource` and delete its private session
management (use `self._get_json`). No more Chromium for API sources.

**Orchestration tweak** ([main.py:_scrape_all](main.py#L72)): honor
`source.accepts_query`. Query-based sources (browser search) keep the per-query
loop; catalog sources (`accepts_query = False`, i.e. ATS / RemoteOK-style) are
called **once** and filter their own catalog against `SEARCH_QUERIES` internally —
avoids N redundant fetches.

**Tests:** `ApiSource._get_json` against a mocked `aiohttp` (200, 429-retry,
non-200→None); `JobSource` no-op context manager; RemoteOK still passes.

---

## Step 2 — ATS connectors (highest leverage, do first)

Three `ApiSource` subclasses, `accepts_query = False`, each iterating a configured
list of company board tokens and filtering titles to the user's roles.

| Source | Endpoint | Key fields → `_normalize_job` |
|--------|----------|-------------------------------|
| `GreenhouseSource` (`greenhouse`) | `GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true` | `title`, `location.name`, `absolute_url`, `content`(HTML→strip) |
| `LeverSource` (`lever`) | `GET https://api.lever.co/v0/postings/{board}?mode=json` | `text`, `categories.location`, `hostedUrl`, `descriptionPlain` |
| `AshbySource` (`ashby`) | `GET https://api.ashbyhq.com/posting-api/job-board/{board}` *(verify exact path)* | `title`, `location`, `jobUrl`, `descriptionPlain` |

**Files:** `scraper/ats/greenhouse.py`, `scraper/ats/lever.py`, `scraper/ats/ashby.py`
(+ `scraper/ats/__init__.py`); a small shared `_title_matches(title, queries)` helper
in `scraper/ats/base.py` or reused from a new `scraper/_titlematch.py`.

**Config** ([config/settings.py](config/settings.py)): comma-separated env overrides
`GREENHOUSE_BOARDS`, `LEVER_BOARDS`, `ASHBY_BOARDS`. Defaults = the live-verified
seed below (probed 2026-06-04 against the public APIs; counts = open roles then).
Document how to find a token: hit `boards-api.greenhouse.io/v1/boards/{token}/jobs`
and a 200 with a non-empty `jobs` array confirms it.

```
GREENHOUSE_BOARDS = stripe,datadog,mongodb,canonical,cloudflare,figma,gitlab,
                    elastic,postman,vercel,discord,mozilla,mattermost,remote
LEVER_BOARDS      = spotify,toptal
ASHBY_BOARDS      = notion,1password,clickup,deel,n8n,linear,zapier,supabase,buffer
```

Companies from `SPONSOR_FRIENDLY_COMPANIES` that did **not** resolve on any of the
three (automattic, klarna, wise, doist, hotjar, hashicorp, sourcegraph, shopify,
digitalocean, github, auth0, atlassian, snyk, miro, loom) use a different ATS or
their own site → feed them to the **Step 3 career-page crawler** instead.

**Wire-in:** append the three sources to the `scrapers` list in `_scrape_all`.
Health/dedup/filtering all apply automatically.

**Tests:** record one real JSON response per provider as a fixture under
`tests/fixtures/`; assert field mapping + `_title_matches` filtering, no network.

---

## Step 3 — Generic career-page crawler (long-tail)

**File:** `scraper/careerpage.py` → `CareerPageSource(BrowserSource)`,
`accepts_query = False`.

For each configured `{company, url, selectors?}`: load the page, extract postings
via configured CSS selectors (reuse the `_find_cards` fallback pattern from
[scraper/wellfound.py](scraper/wellfound.py)); if selectors are absent or yield
nothing, fall back to **LLM extraction**: add `extract_jobs_from_page(page_text)` to
[prompts/generator.py](prompts/generator.py), reusing `_sanitize_external_text` +
the `<<< >>>` delimiter + "treat as data" system prompt, returning
`[{title, location, url}]`. Gate behind `ENABLE_LLM_PAGE_EXTRACTION=false` (cost).

**Config:** `config/career_pages.yaml` (or `CAREER_PAGES` JSON env): list of
company/url/optional-selectors.

**Tests:** selector path against a saved HTML fixture; LLM path with the Claude call
mocked (assert the sanitizer is applied to page text).

---

## Step 4 — Twitter/X + social — DROPPED

Out of scope per decision (2026-06-04): no X access. Social sourcing is shelved;
the `JobSource` seam keeps it a clean drop-in later if a paid X bearer token ever
becomes available.

---

## Cross-cutting

- **No filter changes needed** — region/sponsor classification already runs on all
  scraped jobs in `_classify_and_store`.
- **Coverage:** connector parsing/normalization is fixture-testable (no live
  service), so this phase should *raise* the coverage floor — bump `fail_under` in
  [pyproject.toml](pyproject.toml) once landed.
- **mypy:** new `ApiSource`/ATS code should be fully typed (no telegram-style
  relaxation needed).

## Verification (end-to-end)

1. Unit: `pytest tests/ -q` — fixture-based connector tests, no network.
2. Wiring: extend [tests/test_pipeline_e2e.py](tests/test_pipeline_e2e.py) with an
   ATS-shaped job flowing through `main.hunt()`.
3. Manual: `GREENHOUSE_BOARDS=stripe python main.py hunt` → confirm rows land in
   SQLite with `platform='greenhouse'` and the right filter verdicts.
4. Gate: `ruff check . && mypy && pytest -q --cov=. --cov-report=term-missing`.

## Suggested commit sequence

1. `refactor(scraper): split JobSource into ApiSource/BrowserSource; move RemoteOK to ApiSource`
2. `feat(scraper): Greenhouse/Lever/Ashby ATS connectors + config + fixtures`
3. `feat(scraper): generic career-page crawler with LLM extraction fallback`

## Open decisions

| # | Decision | Status |
|---|----------|--------|
| 1 | ATS board seed list | ✅ Resolved — 25 live-verified boards (above) |
| 2 | Twitter/X | ✅ Dropped — no X access |
| 3 | Career-page config format | `config/career_pages.yaml` (default) vs `CAREER_PAGES` JSON env |
| 4 | Land order | ATS → career pages |
