# HUNTER — Business & Operations Audit

A standing reference for the cost, ROI, legal, and sustainability side of running
HUNTER as a daily apply machine. Pair with the technical roadmap in
`IMPROVEMENT_PLAN.md` / `PHASE_2_PLAN.md`.

## 1. Cost model (single-user, ~50 roles/day)

| Item | Driver | Rough monthly cost |
|---|---|---|
| fly.io compute | 1× shared-cpu-1x, 1 GB, always-on | ~$5–7 |
| fly.io volume | 1 GB encrypted (`hunter_data`) | ~$0.15 |
| Anthropic — cover letters | ~50/day × ~700 output tokens, Sonnet | dominant LLM cost |
| Anthropic — sponsor scoring | only "unknown sponsor" jobs (`ENABLE_LLM_SPONSOR_SCORING`) | small |
| Anthropic — form answers | only unmatched free-text questions, **cached per question** | small |

**Levers if cost matters:** keep `CLAUDE_MODEL` on Sonnet (already default); the
form-answer cache (`applicant/engine._ANSWER_CACHE`) avoids paying twice for the
same question; cover letters are the main spend — generate only on apply (already
the case), not on every scraped job.

**Recommend:** set an Anthropic monthly budget + usage alert in the console.

## 2. ROI tracking

The funnel is already instrumented — use `/report` (Telegram) or
`tracker.database.get_funnel()`:

```
sourced (include/flag/drop) → pending → approved → applied → interviewing → offered
```

Track weekly: **applied → response rate → interview rate → cost per interview.**
`get_filter_precision()` shows approve-rate by verdict — if `flag` roles are mostly
approved, loosen the filter; if mostly skipped, tighten it. The goal metric is
**interviews per week** and **cost per interview**, not raw applications.

## 3. Legal / ToS risk

- **LinkedIn** scraping & auto-apply violate LinkedIn's ToS and risk account bans.
  LinkedIn scraping is **disabled** (`main.py`); keep auto-apply off LinkedIn or
  do it manually. This is the highest-risk surface — leave it off.
- **ATS public application forms** (Greenhouse/Lever/Ashby/Recruitee/SmartRecruiters)
  are public endpoints; automated submission is lower risk but must: (a) respect
  rate limits (existing `SCRAPE_DELAY_*` + 3 s between applies), (b) answer
  **honestly** — never misstate work authorization or sponsorship needs.
- **Honesty guardrail:** sensitive dropdowns (work authorization / visa sponsorship)
  are deliberately **not** auto-answered; they route to manual review so the bot
  never misrepresents you to an employer.
- **Data / GDPR:** PII lives in the encrypted fly volume + `fly secrets`; the
  resume is a secret (`RESUME_TEXT`) not baked into the image. Document retention
  and delete on request.

## 4. Sustainability / maintenance

- **Board-token drift** — companies churn/rename/migrate ATS. Run
  `python -m scripts.verify_boards` periodically (non-zero exit on any DEAD board);
  it currently reports 81 boards, 0 dead. Wire it into a monthly check.
- **Selector drift** — Wellfound/Greenhouse/Lever DOMs change. The apply engine
  degrades safely (only marks `applied` on confirmation) and `scraper_health`
  auto-skips a source after `SCRAPER_SKIP_AFTER_ZEROS` zero-yield runs and alerts.
- **Apply confirmation** — `_submit_and_confirm` never reports success without a
  detected confirmation, so silent breakage surfaces as "needs manual", not a
  false "applied".

## 5. Open risks & next bets

- Real submission is verified by unit tests + dry-run screenshots, **not** by live
  submitting to real employers (which would be a real-world side effect). Roll out
  via `APPLY_DRY_RUN=true` → inspect screenshots → set `false`.
- Ashby/Recruitee/SmartRecruiters still use the generic (screenshot) apply path;
  promote to structured submission once Greenhouse/Lever are proven in production.
- Highest-leverage next investment is wherever the funnel leaks most (check
  `/report`): usually sourcing breadth (more verified EU/EMEA boards) or apply
  completion rate on forms with required custom questions.
