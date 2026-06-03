# HUNTER — Improvement Plan

> Status: confirmed roadmap (2026-06-03). Turns the production-readiness audit into an
> ordered, executable plan. Phases are independent and shippable on their own.
>
> **Decisions locked:** execute **Phase 1 first**, **hybrid autonomy** (auto-submit only on
> confirmable paths), and pursue **all three source types** — ATS APIs, generic career pages,
> and Twitter/X (sequenced in that order of reliability-per-effort).

## Context

HUNTER today is a **safe, human-in-the-loop job-hunting pipeline**: `scrape → filter →
Telegram review → human-approved apply → 7-day follow-ups`. It is production-ready as a
careful personal assistant, but three goals are still unmet:

1. **"Magic agent"** — one command, end-to-end, with artifact (portfolio/CV PDF) generation.
2. **Broader sourcing** — niche boards, ATS APIs, company career pages, Twitter/X — not just
   the big boards.
3. **Cleanups** — the audit surfaced fixable rough edges (disabled scrapers, thin auto-apply,
   fragile region detection, dated SDK, unit-only tests).

The central tension: "just applies like magic" conflicts with the **deliberate** approve↔apply
safety split added in `ac937d1`. This plan resolves that with a **hybrid autonomy model**
(auto-submit only on confirmable paths) rather than removing the human gate wholesale.

---

## Recommended sequencing

Phase 1 (hardening) is a prerequisite for trusting any autonomy. Phase 2 (sourcing) delivers
the biggest user-visible gain. Phase 3/4 (autonomy + artifacts) are the "magic" layer and
depend on 1+2 being solid. Do them in order.

---

## Phase 1 — Production hardening (low risk, ship first)

Goal: make what exists genuinely trustworthy before adding autonomy.

- **Bump `anthropic`** from `0.45.0` → current, re-pin lockfile, run the suite.
  Files: [requirements.txt](requirements.txt), `requirements.lock`. Verify cover-letter and
  sponsor-scoring calls in [prompts/generator.py](prompts/generator.py) still work (the model
  ID `claude-sonnet-4-20250514` and message shape may need updating).
- **Decide the fate of the 3 disabled scrapers** ([main.py:75-77](main.py#L75-L77)).
  Either re-enable with a fix (Phase 2) or **delete** `scraper/linkedin.py`, `scraper/indeed.py`,
  `scraper/glassdoor.py` so untested code isn't shipped. Recommendation: keep `linkedin.py`
  (best apply path), delete `indeed.py` + `glassdoor.py` until needed.
- **Fix the `form_filled` UX**: today Indeed/Wellfound/generic always return "needs manual,"
  which makes auto-apply feel broken. Make the Telegram message explicit ("form pre-filled,
  finish in browser → <url>") so the value is legible. File: [applicant/engine.py](applicant/engine.py),
  [telegram_bot/bot.py](telegram_bot/bot.py).
- **Replace fragile region regex** in [scraper/filters.py](scraper/filters.py) with a
  normalized lookup (country/city token set + alias map) and add table-driven tests for the
  known-bad cases ("London, UK", "Remote — EMEA", etc.).
- **Add CI depth**: `mypy` (or pyright) check + `pytest --cov` gate in
  [.github/workflows/ci.yml](.github/workflows/ci.yml). Reuse existing ruff config in
  [pyproject.toml](pyproject.toml).
- **Add one pipeline e2e test**: `hunt → classify → store → send (mocked telegram) → apply
  (mocked browser)` so the orchestrator wiring in [main.py](main.py) is covered, not just units.

Verify: `ruff check . && mypy . && pytest -q --cov` all green; `python main.py stats` runs;
manual `python main.py hunt` against RemoteOK returns jobs.

---

## Phase 2 — Sourcing engine (biggest leverage)

Goal: go wide and reliable. The current base class is browser-centric; split the seam first.

- **Refactor the source interface**: introduce a lightweight `JobSource` protocol with two
  concrete bases — `ApiSource` (aiohttp, no browser) and `BrowserSource` (current
  [scraper/base.py](scraper/base.py) Playwright logic). RemoteOK becomes an `ApiSource`; it
  shouldn't launch Chromium. Keep `scrape() -> list[dict]` contract and `_normalize_job`.
- **ATS connectors (do these first — structured JSON, no anti-bot):**
  - Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{company}/jobs`
  - Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
  - Ashby: public posting API.
  Driven by a configurable company list in [config/settings.py](config/settings.py). This is
  the highest reliability-per-effort win and covers most startup/tech roles.
- **Generic career-page crawler**: fetch a company `/careers` URL, extract postings via
  configurable CSS selectors with an **LLM-extraction fallback** (reuse the sanitized-prompt
  pattern in [prompts/generator.py](prompts/generator.py)) when selectors miss. Covers the
  niche/long-tail goal.
- **Twitter/X + social (in scope, sequenced last)**: high-signal but hardest (API cost, anti-bot,
  noise). Build after ATS + career-page connectors are stable. Approach: X search for hiring
  posts ("hiring", role keywords, "remote") + targeted company social pages, normalized through
  the same `JobSource` contract and run through the existing sanitizer before any LLM extraction.
  Treat as its own spike with a clear kill-switch via `should_skip_scraper` if signal/noise is bad.
- **Per-source health already exists** (`record_scraper_run` / `should_skip_scraper`) — wire new
  sources into it so a broken connector auto-disables instead of failing the run.

Verify: add a source, run `python main.py hunt`, confirm jobs from the new connector land in
SQLite with correct `platform`; unit test each connector against a recorded JSON/HTML fixture.

---

## Phase 3 — Hybrid autonomy ("magic," safely)

Goal: one command does as much as is safe, human only confirms the risky tail.

- **Trust tiers**: auto-submit only on **confirmable** paths (LinkedIn Easy Apply, known ATS
  forms where a success signal exists). Everything else stays human-reviewed and pre-filled.
  PII never goes to an unverified URL — preserves the `ac937d1` guarantee.
- **`auto` command**: `python main.py auto` chains `hunt → classify → (auto-apply trusted tier)
  → queue the rest for review`, with a Telegram digest of what it did vs. what needs a tap.
- **Real submission confirmation**: replace screenshot-only "proof" with an actual success
  check (post-submit DOM/redirect assertion) so the trusted tier is genuinely trustworthy.
  File: [applicant/engine.py](applicant/engine.py).

Decision needed: how aggressive the trusted tier is. Default = Easy Apply + ATS only.

---

## Phase 4 — Artifact generation (portfolio / CV PDF)

Goal: per-application tailored documents, not just a cover-letter string.

- **Tailored CV/portfolio PDF**: from a structured profile (YAML/JSON) + the job description,
  render a one-page tailored PDF. Library: `weasyprint` (HTML/CSS → PDF, easiest to template)
  or `reportlab`. New module `documents/` reusing the sanitized Claude-prompt pattern.
- **Attach generated PDF** to the apply flow (resume upload input already exists in
  [applicant/engine.py](applicant/engine.py)).
- **Store artifacts** alongside the job row (path in `jobs` table) and prune like screenshots.

Verify: `python main.py apply` (or the per-job flow) produces a PDF on disk, it opens, and the
content reflects the target job.

---

## Decisions (confirmed 2026-06-03)

| # | Decision | Chosen | Notes |
|---|----------|--------|-------|
| 1 | Execution order | **Phase 1 first**, then 2 → 3 → 4 | Harden before adding autonomy |
| 2 | Autonomy model | **Hybrid** (auto only on confirmable paths) | Preserves the `ac937d1` PII guarantee |
| 3 | Sourcing scope | **All three**: ATS APIs → career pages → Twitter/X | ATS first (highest reliability/effort) |
| 4 | Disabled scrapers | Keep `linkedin.py`, delete `indeed.py` + `glassdoor.py` | Open — confirm during Phase 1 |
| 5 | PDF library | **weasyprint** | Open — confirm during Phase 4 |

---

## Out of scope / explicitly not doing

- Multi-user / enterprise scale, distributed rate-limiting (Redis), Prometheus metrics — this is
  a single-user tool; that would be over-engineering.
- Removing the human gate entirely (re-introduces the exact PII risk the last commit fixed).
