# HUNTER hardening audit

Date: 2026-06-22 · Scope: full repo, post `more-job-sources` (#15) + `fix-source-ordering` (#16).
Method: code inspection + live checks against production (`hunter-whjqdw`). Severity is
Critical / High / Medium / Low. Report-first: nothing below is fixed yet — triage and we fix by priority.

## Summary

No Critical or High findings. The one high-impact issue — source ordering starving the
remote-rich sources and drops eating the daily budget — was caught during the production
smoke and already fixed (#16), taking the hunt from 5 → 28 reviewable jobs sent. Remaining
findings are tuning, dead code, and housekeeping.

## Verified healthy (no action)

- **Secret redaction** — `config/log_redaction.py` installs a shape-based `RedactingFilter`
  on the root handler after `basicConfig` (scrubs Telegram tokens, `sk-` keys, `li_at`
  cookies regardless of emitter). No PII is logged; `print()` appears only in the `stats`
  CLI output.
- **Git hygiene** — `.gitignore` covers `.env`, `*.db`, `config/resume.*`, `screenshots/`,
  `backups/`; `git ls-files` shows nothing sensitive tracked.
- **Resource cleanup** — `_safe_close(page, context)` in `finally` across every apply path
  ([applicant/engine.py](applicant/engine.py)); `ApiSource`/`BrowserSource` close session/
  browser in `__aexit__`; the apply worker drains then cancels on shutdown
  ([telegram_bot/bot.py](telegram_bot/bot.py#L402)). No obvious session/browser leak.
- **Fallbacks** — cover letter → `_fallback_cover_letter`; form answers → `""`; sponsor
  scoring → `unclear`; per-source/per-board scrape exceptions are swallowed, recorded, and
  auto-skipped after a zero streak.
- **DB** — WAL + `foreign_keys=ON`; dedup via `INSERT OR IGNORE` on the unique `url`;
  additive auto-migration guarded by `PRAGMA table_info`. Idempotent.
- **Prompt-injection defense** — `_sanitize_external_text` + `<<< >>>` delimiters + a
  system prompt that pins external text as literal data.
- **CI** — ruff + mypy + pip-audit + pytest on every PR; `pip-audit` clean after the
  `aiohttp 3.14.0→3.14.1` CVE fix (#15).

## Findings

### Medium

- **M1 — Board list skews US-centric for a sponsorship-needing candidate.** The 40 boards
  added in #15 include US-only-remote giants (databricks, lyft, robinhood, coinbase, airbnb,
  openai, …). Their US-locked roles are _correctly_ dropped for a Turkey-based candidate, but
  they dominate scrape volume (252 drops in the live run) and compete with EU/EMEA/global
  boards for the per-run cap. _Fix:_ split board lists by region and weight EU/EMEA/global
  ahead of US in the survival ordering, or prune the lowest-yield US-only boards. Needs a
  product call (volume vs. relevance). [config/settings.py](config/settings.py)
- **M2 — SmartRecruiters is a dead source.** Only one board, `Visa` (7 postings, 0 PM today),
  so it always returns 0 and gets auto-skipped. _Fix:_ add SmartRecruiters companies that
  hire PMs, or remove the source. [config/settings.py](config/settings.py), [scraper/ats.py](scraper/ats.py)
- **M3 — Cover-letter/answer generators don't short-circuit on a missing API key.** They
  attempt an API call that 401s, then fall back — wasteful and noisy. `score_sponsor_signal`
  already guards with `if not ANTHROPIC_API_KEY`. _Fix:_ add the same early guard to
  `generate_cover_letter` / answer generation. [prompts/generator.py](prompts/generator.py#L57)

### Low

- **L1 — Dead config/code.** `PLATFORM_URLS` is defined but unused; `INDEED_API_KEY` is
  configured with no Indeed scraper. _Fix:_ remove, or wire up. [config/settings.py](config/settings.py)
- **L2 — Redaction doesn't cover PII.** `log_redaction` scrubs secrets but not `APPLICANT_*`
  values. PII isn't logged today, so this is defense-in-depth only. _Optional:_ add PII
  patterns. [config/log_redaction.py](config/log_redaction.py)
- **L3 — Wellfound is effectively dormant.** Browser-based, fragile, last in order → usually
  ceiling-skipped or zero-skipped. _Decide:_ keep as best-effort or retire (retiring shrinks
  the Playwright surface for scraping; the apply engine still needs it). [scraper/wellfound.py](scraper/wellfound.py)
- **L4 — Dependency housekeeping.** No outstanding CVEs. Safe patch available:
  `python-telegram-bot 22.7→22.8`. Held per policy (majors): mypy 2, pytest 9,
  pytest-asyncio 1, pytest-cov 7. Optional pre-1.0/dev bumps to verify: anthropic
  0.107→0.111, ruff 0.8→0.15.
- **L5 — Board coverage depends on the daily rotation + collection ceiling.** The ceiling
  (`MAX_JOBS_PER_DAY*4 = 320`) can stop before Greenhouse/Wellfound run. Acceptable now that
  ordering favors high-survival sources, but consider logging which boards were scanned, or
  nudging the ceiling. [main.py](main.py#L108)

## Ops note (already actioned during smoke)

- Cleared RemoteOK's stale zero-streak in `scraper_health` on production (it was skipped from
  the pre-rebuild dead-tag era); verified the rebuilt scraper now yields ~36 roles.
- The reviewable-budget change (#16) means each unknown-sponsor flag triggers one Claude
  sponsor-scoring call; bounded by the number of flags per run (~67 in the live run). Cheap,
  but worth watching the monthly Anthropic spend.

## Suggested fix order

1. **M3** + **L1** — tiny, safe, no behavior risk (early API-key guard; delete dead config).
2. **M2** — add SmartRecruiters PM boards or drop the source.
3. **M1** — region-weight the board lists (product call on volume vs. relevance).
4. **L4** — bump `python-telegram-bot` to 22.8; leave majors.
5. **L2 / L3 / L5** — optional, low urgency.

## Resolution (hardening PR)

- **M1 ✅** — boards split into a priority tier (EU/EMEA/global, daily-rotated) and a
  `*_US_BOARDS` tail scanned only on leftover cap. Implemented via `AtsSource.priority_count`
  + `_ordered_boards()` ([scraper/ats.py](scraper/ats.py)) and `GREENHOUSE_US_BOARDS` /
  `ASHBY_US_BOARDS` ([config/settings.py](config/settings.py)).
- **M2 ✅** — dead SmartRecruiters source removed from the active pipeline ([main.py](main.py));
  class/tests/settings retained for future revival.
- **M3 ✅** — `generate_cover_letter` / `generate_form_answer` now short-circuit to the
  fallback when `ANTHROPIC_API_KEY` is unset ([prompts/generator.py](prompts/generator.py)).
- **L1 ✅** — removed unused `PLATFORM_URLS` and the doc-only `INDEED_API_KEY` placeholder.
- **L4 ✅** — bumped `python-telegram-bot 22.7→22.8`; majors held; pip-audit clean.
- **L2 / L3 / L5** — deferred (low urgency): PII redaction patterns, retiring Wellfound, and
  board-scan observability.

