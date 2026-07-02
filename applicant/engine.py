"""Playwright-based auto-apply engine for job applications."""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config.settings import (
    APPLY_DRY_RUN,
    CHROMIUM_LAUNCH_ARGS,
    LINKEDIN_SESSION_COOKIE,
    MAX_APPLIES_PER_RUN,
    RESUME_PATH,
)
from prompts.generator import COMMON_ANSWERS, generate_cover_letter, generate_form_answer
from tracker.database import get_job_by_id, log_action, mark_applied, set_cover_letter

# Cache LLM answers per question text within a process so we don't pay for the
# same free-text question twice across a batch of applications.
_ANSWER_CACHE: dict[str, str] = {}


@dataclass
class ApplyResult:
    """Result of an apply attempt."""
    success: bool
    method: str  # 'easy_apply', 'form_filled', 'screenshot_only', 'external_redirect'
    screenshot_path: str | None = None
    message: str = ""

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# Tunables (previously magic numbers scattered through the apply methods).
PAGE_TIMEOUT_MS = 20000          # page.goto navigation timeout
PAGE_SETTLE_S = 2                # pause after navigation / clicks for JS to render
STEP_PAUSE_S = 1.5               # pause between LinkedIn Easy Apply steps
MAX_FORM_STEPS = 10              # max LinkedIn multi-step form pages to traverse


class AutoApplicant:
    """Automated job application engine using Playwright."""

    def __init__(self, headless=True):
        self.headless = headless
        self._playwright = None
        self._browser = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=CHROMIUM_LAUNCH_ARGS,
        )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:
                logger.warning(f"AutoApplicant: browser.close failed: {e}")
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"AutoApplicant: playwright.stop failed: {e}")

    async def apply_to_job(self, job: dict) -> ApplyResult:
        """Apply to a single job. Routes to platform-specific handler."""
        platform = job.get("platform", "")
        job_id = job["id"]

        # Guard against duplicate apply attempts
        fresh = get_job_by_id(job_id)
        if fresh and fresh.get("status") == "applied":
            logger.info(f"Already applied to job {job_id}, skipping")
            return ApplyResult(
                success=True, method="already_applied",
                message="Already applied; skipped.",
            )

        logger.info(f"Applying to: {job['title']} at {job['company']} ({platform})")

        try:
            # Generate cover letter
            cover_letter = generate_cover_letter(
                job["title"], job["company"], job.get("description", "")
            )
            set_cover_letter(job_id, cover_letter)

            if platform == "linkedin":
                result = await self._apply_linkedin(job, cover_letter)
            elif platform == "wellfound":
                result = await self._apply_wellfound(job, cover_letter)
            elif platform == "greenhouse":
                result = await self._apply_greenhouse(job, cover_letter)
            elif platform == "lever":
                result = await self._apply_lever(job, cover_letter)
            elif platform == "ashby":
                result = await self._apply_ashby(job, cover_letter)
            else:
                # ATSes we can't drive reliably (Recruitee/SmartRecruiters):
                # skip the browser entirely and hand off a fast manual apply — the
                # cover letter is already generated and sent for copy/paste.
                result = ApplyResult(
                    success=False, method="manual_handoff",
                    message="This ATS isn't auto-submittable — apply via the link (cover letter sent).",
                )

            if result.success:
                mark_applied(job_id)
                log_action(job_id, "applied", f"method={result.method}: {result.message}")
                logger.info(f"✅ Applied ({result.method}): {job['title']} at {job['company']}")
            else:
                log_action(job_id, "apply_failed", f"method={result.method}: {result.message}")
                logger.warning(f"⚠️ Apply incomplete ({result.method}): {job['title']} at {job['company']}")

            return result

        except Exception as e:
            logger.error(f"Apply error for job {job_id}: {e}")
            log_action(job_id, "apply_failed", str(e)[:500])
            return ApplyResult(success=False, method="error", message=str(e)[:200])

    async def _new_context(self, cookies=None):
        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        if cookies:
            await context.add_cookies(cookies)
        return context

    async def _apply_linkedin(self, job: dict, cover_letter: str) -> ApplyResult:
        """Apply via LinkedIn Easy Apply."""
        cookies = []
        if LINKEDIN_SESSION_COOKIE:
            cookies = [{
                "name": "li_at",
                "value": LINKEDIN_SESSION_COOKIE,
                "domain": ".linkedin.com",
                "path": "/",
            }]

        context = await self._new_context(cookies=cookies)
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S)

            # Look for Easy Apply button
            easy_apply = await page.query_selector(
                "button.jobs-apply-button, "
                "button[aria-label*='Easy Apply'], "
                "button:has-text('Easy Apply')"
            )

            if not easy_apply:
                logger.info(f"No Easy Apply for {job['title']} - taking screenshot for manual apply")
                ss_path = str(SCREENSHOTS_DIR / f"linkedin_{job['id']}.png")
                await page.screenshot(path=ss_path)
                await page.close()
                await context.close()
                return ApplyResult(
                    success=False, method="screenshot_only", screenshot_path=ss_path,
                    message="No Easy Apply button found. Needs manual application.",
                )

            await easy_apply.click()
            await asyncio.sleep(PAGE_SETTLE_S)

            # Process multi-step form
            for _step in range(MAX_FORM_STEPS):
                # Fill in any visible form fields
                await self._fill_linkedin_fields(page, cover_letter)

                # Check for submit button
                submit = await page.query_selector(
                    "button[aria-label*='Submit'], "
                    "button:has-text('Submit application'), "
                    "button:has-text('Submit')"
                )
                if submit:
                    await submit.click()
                    await asyncio.sleep(PAGE_SETTLE_S)
                    # Check for success
                    success_el = await page.query_selector(
                        "h2:has-text('application was sent'), "
                        "div:has-text('Application submitted')"
                    )
                    if success_el:
                        ss_path = str(SCREENSHOTS_DIR / f"linkedin_{job['id']}_success.png")
                        await page.screenshot(path=ss_path)
                        logger.info("LinkedIn Easy Apply submitted successfully")
                        return ApplyResult(
                            success=True, method="easy_apply", screenshot_path=ss_path,
                            message="Easy Apply form submitted and confirmed.",
                        )

                # Click Next/Review
                next_btn = await page.query_selector(
                    "button[aria-label*='next'], "
                    "button:has-text('Next'), "
                    "button:has-text('Review')"
                )
                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(STEP_PAUSE_S)
                else:
                    break

            ss_path = str(SCREENSHOTS_DIR / f"linkedin_{job['id']}_final.png")
            await page.screenshot(path=ss_path)
            return ApplyResult(
                success=False, method="form_filled", screenshot_path=ss_path,
                message="Easy Apply form opened but could not confirm submission.",
            )

        except Exception as e:
            logger.error(f"LinkedIn apply error: {e}")
            ss_path = str(SCREENSHOTS_DIR / f"linkedin_{job['id']}_error.png")
            try:
                await page.screenshot(path=ss_path)
            except Exception as ss_err:
                logger.debug(f"error screenshot failed: {ss_err}")
                ss_path = None
            return ApplyResult(
                success=False, method="error", screenshot_path=ss_path,
                message=str(e)[:200],
            )
        finally:
            try:
                await page.close()
            except Exception as e:
                logger.debug(f"page.close failed: {e}")
            try:
                await context.close()
            except Exception as e:
                logger.debug(f"context.close failed: {e}")

    async def _fill_linkedin_fields(self, page: Page, cover_letter: str):
        """Fill LinkedIn Easy Apply form fields."""
        # Upload resume if file input exists
        file_input = await page.query_selector("input[type='file']")
        if file_input and RESUME_PATH.exists():
            try:
                await file_input.set_input_files(str(RESUME_PATH))
                await asyncio.sleep(1)
            except Exception as e:
                logger.info(f"Resume upload failed, may need manual upload: {e}")
        elif file_input:
            logger.warning(f"Resume file not found at {RESUME_PATH}")

        # Fill text inputs
        inputs = await page.query_selector_all(
            "input[type='text']:not([readonly]), "
            "input:not([type]):not([readonly]), "
            "textarea"
        )
        for inp in inputs:
            try:
                label_el = await inp.evaluate(
                    """el => {
                        const label = el.closest('div')?.querySelector('label');
                        return label ? label.textContent.trim().toLowerCase() : '';
                    }"""
                )
                placeholder = (await inp.get_attribute("placeholder") or "").lower()
                aria_label = (await inp.get_attribute("aria-label") or "").lower()
                current_val = await inp.input_value()

                if current_val:
                    continue  # Already filled

                field_hint = f"{label_el} {placeholder} {aria_label}"

                value = self._match_field_value(field_hint, cover_letter)
                if value:
                    await inp.fill(value)
                    await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug(f"linkedin field fill skipped: {e}")
                continue

        # Handle dropdowns/selects
        selects = await page.query_selector_all("select")
        for select in selects:
            try:
                options = await select.query_selector_all("option")
                if len(options) > 1:
                    # Select the first non-empty option (usually "Yes" or similar)
                    for opt in options[1:]:
                        text = (await opt.inner_text()).lower()
                        if text in ["yes", "true", "8", "8+"]:
                            val = await opt.get_attribute("value")
                            if val:
                                await select.select_option(val)
                                break
            except Exception as e:
                logger.debug(f"linkedin select skipped: {e}")
                continue

    def _match_field_value(self, field_hint: str, cover_letter: str) -> str:
        """Match a form field to the appropriate value."""
        hint = field_hint.lower()

        if any(w in hint for w in ["first name", "first_name", "given name"]):
            return COMMON_ANSWERS["first_name"]
        if any(w in hint for w in ["last name", "last_name", "family name", "surname"]):
            return COMMON_ANSWERS["last_name"]
        if any(w in hint for w in ["full name", "your name"]):
            return COMMON_ANSWERS["name"]
        if "email" in hint:
            return COMMON_ANSWERS["email"]
        if "phone" in hint or "mobile" in hint or "tel" in hint:
            return COMMON_ANSWERS["phone"]
        if "linkedin" in hint:
            return COMMON_ANSWERS["linkedin"]
        if "website" in hint or "portfolio" in hint or "url" in hint:
            return COMMON_ANSWERS["website"]
        if "salary" in hint or "compensation" in hint or "pay" in hint:
            return COMMON_ANSWERS["salary"]
        if "cover letter" in hint or "letter" in hint:
            return cover_letter
        if "experience" in hint and "year" in hint:
            return COMMON_ANSWERS["years_experience"]
        if any(w in hint for w in ["remote", "work from home", "wfh"]):
            return COMMON_ANSWERS["remote"]
        if any(w in hint for w in ["authorized", "authorization", "visa", "sponsorship"]):
            return COMMON_ANSWERS["work_authorization"]
        if "available" in hint or "start date" in hint:
            return COMMON_ANSWERS["availability"]
        if hint.strip() in ("location", "your location", "current location", "city"):
            return COMMON_ANSWERS["location"]

        return ""

    async def _resolve_field_value(
        self, hint: str, cover_letter: str, *, is_freetext: bool, job: dict | None = None
    ) -> str:
        """Static COMMON_ANSWERS match first; for an unmatched free-text question
        fall back to an LLM-generated, cached answer (wires generate_form_answer)."""
        value = self._match_field_value(hint, cover_letter)
        if value:
            return value
        question = hint.strip()
        if not is_freetext or len(question) < 8:
            return ""
        if question in _ANSWER_CACHE:
            return _ANSWER_CACHE[question]
        job = job or {}
        try:
            answer = await asyncio.to_thread(
                generate_form_answer, question,
                job.get("title", ""), job.get("company", ""),
            )
        except Exception as e:
            logger.debug(f"LLM form answer failed for {question!r}: {e}")
            answer = ""
        _ANSWER_CACHE[question] = answer
        return answer

    async def _set_first(self, page: Page, selectors: list[str], value: str) -> bool:
        """Fill the first matching selector with `value`. Returns True if filled."""
        if not value:
            return False
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.fill(value)
                    return True
                except Exception as e:
                    logger.debug(f"fill {sel} failed: {e}")
        return False

    async def _upload_resume(self, page: Page, selectors: list[str]) -> None:
        if not RESUME_PATH.exists():
            logger.warning(f"Resume not found at {RESUME_PATH}; skipping upload")
            return
        for sel in selectors:
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.set_input_files(str(RESUME_PATH))
                    await asyncio.sleep(1)
                    return
                except Exception as e:
                    logger.debug(f"resume upload {sel} failed: {e}")

    async def _fill_labeled_questions(self, page: Page, cover_letter: str, job: dict | None = None) -> None:
        """Fill remaining labeled text fields / custom questions, using the LLM for
        free-text (textarea) questions that don't match a canned answer."""
        fields = await page.query_selector_all(
            "textarea, input[type='text']:not([readonly]), input:not([type]):not([readonly])"
        )
        for el in fields:
            try:
                if await el.input_value():
                    continue
                label = await el.evaluate(
                    """el => {
                        let t = '';
                        const id = el.getAttribute('id');
                        if (id) { const l = document.querySelector('label[for="'+id+'"]'); if (l) t = l.textContent; }
                        if (!t) { const l = el.closest('div')?.querySelector('label'); if (l) t = l.textContent; }
                        return (t || el.getAttribute('aria-label') || el.getAttribute('name') || '').trim();
                    }"""
                )
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                value = await self._resolve_field_value(
                    label, cover_letter, is_freetext=(tag == "textarea"), job=job
                )
                if value:
                    await el.fill(value)
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"labeled question skipped: {e}")

    def _select_target(self, label: str) -> str | None:
        """Map a dropdown's question to the option keyword to pick, from the
        candidate's known answers. Returns None to leave the select untouched."""
        h = (label or "").lower()
        if not h:
            return None
        # Check authorization BEFORE sponsorship: "authorized to work WITHOUT
        # sponsorship?" mentions sponsorship but is really an authorization question
        # (answer = work_authorized = No), distinct from "do you REQUIRE sponsorship?"
        if "authori" in h or "eligible" in h or "legally" in h or "right to work" in h:
            return "yes" if COMMON_ANSWERS["work_authorized"].lower().startswith("y") else "no"
        if "sponsor" in h:  # "Will you require visa sponsorship?"
            return "yes" if COMMON_ANSWERS["requires_sponsorship"].lower().startswith("y") else "no"
        if any(w in h for w in ("gender", "race", "ethnic", "veteran", "disability",
                                "hispanic", "latino", "identify")):
            return "decline"
        if "hear about" in h or "how did you" in h or "referr" in h or "source" in h:
            return COMMON_ANSWERS["referral_source"].lower()
        if any(w in h for w in ("remote", "work from home", "wfh")) or "relocat" in h:
            return "yes"
        return None

    async def _fill_selects(self, page: Page) -> None:
        """Answer native <select> dropdowns (work auth, sponsorship, EEO, source)
        from the candidate's known answers. Custom div-comboboxes are skipped."""
        for sel in await page.query_selector_all("select"):
            try:
                label = await sel.evaluate(
                    """el => {
                        let t = ''; const id = el.getAttribute('id');
                        if (id) { const l = document.querySelector('label[for="'+id+'"]'); if (l) t = l.textContent; }
                        if (!t) { const l = el.closest('div')?.querySelector('label'); if (l) t = l.textContent; }
                        return (t || el.getAttribute('aria-label') || el.getAttribute('name') || '').trim();
                    }"""
                )
                target = self._select_target(label)
                if not target:
                    continue
                chosen = None
                for opt in await sel.query_selector_all("option"):
                    txt = ((await opt.inner_text()) or "").strip().lower()
                    val = await opt.get_attribute("value")
                    if not val or not txt:
                        continue
                    if target == "decline":
                        if any(p in txt for p in ("decline", "prefer not", "wish not",
                                                  "do not wish", "not to answer", "not to say")):
                            chosen = val
                            break
                    elif target in ("yes", "no"):
                        if txt == target or txt.startswith(target):
                            chosen = val
                            break
                    elif target in txt:
                        chosen = val
                        break
                if chosen is not None:
                    await sel.select_option(chosen)
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"select fill skipped: {e}")

    async def _fill_radios(self, page: Page) -> None:
        """Answer Yes/No radio-group questions (Ashby uses radios, not selects, for
        the work-authorization / sponsorship / EEO questions)."""
        groups = await page.evaluate(
            """() => {
                const out = {};
                document.querySelectorAll('input[type=radio]').forEach(r => {
                    const optLabel = (r.id && document.querySelector('label[for="'+r.id+'"]')?.textContent || '').trim();
                    let q = '', el = r;
                    for (let i = 0; i < 6 && el; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        const lab = el.querySelector('label, legend');
                        if (lab) {
                            const t = lab.textContent.trim();
                            if (t && !/^(yes|no)$/i.test(t)) { q = t; break; }
                        }
                    }
                    if (!out[r.name]) out[r.name] = {question: q, options: []};
                    out[r.name].options.push({id: r.id, label: optLabel});
                });
                return out;
            }"""
        )
        for grp in groups.values():
            target = self._select_target(grp.get("question", ""))
            if not target:
                continue
            for opt in grp["options"]:
                lab = (opt["label"] or "").strip().lower()
                if target in ("yes", "no"):
                    match = lab == target or lab.startswith(target)
                elif target == "decline":
                    match = any(p in lab for p in ("decline", "prefer not", "wish not"))
                else:
                    match = target in lab
                if match and opt["id"]:
                    try:
                        await page.click(f'label[for="{opt["id"]}"]')
                    except Exception as e:
                        logger.debug(f"radio click failed: {e}")
                    break

    async def _submit_and_confirm(
        self, page: Page, job: dict, platform: str, *,
        submit_selectors: list[str], confirm_selectors: list[str],
        confirm_url_substrings: list[str],
    ) -> ApplyResult:
        """Submit the form and only report success when a confirmation is detected.

        Honors APPLY_DRY_RUN (fills but never submits). Never returns success unless
        the page confirms the application landed, so a failed/blocked submit is
        surfaced for manual review instead of being falsely marked applied.
        """
        ss_path = str(SCREENSHOTS_DIR / f"{platform}_{job['id']}.png")
        if APPLY_DRY_RUN:
            await page.screenshot(path=ss_path)
            return ApplyResult(
                success=False, method="form_filled", screenshot_path=ss_path,
                message="DRY RUN: form filled, submit skipped (APPLY_DRY_RUN).",
            )
        submitted = False
        for sel in submit_selectors:
            btn = await page.query_selector(sel)
            if btn:
                try:
                    await btn.click()
                    submitted = True
                    break
                except Exception as e:
                    logger.debug(f"submit click {sel} failed: {e}")
        if not submitted:
            await page.screenshot(path=ss_path)
            return ApplyResult(
                success=False, method="form_filled", screenshot_path=ss_path,
                message="Filled form but found no submit button — needs manual apply.",
            )
        await asyncio.sleep(PAGE_SETTLE_S)
        url = (page.url or "").lower()
        confirmed = any(s in url for s in confirm_url_substrings)
        if not confirmed:
            for sel in confirm_selectors:
                if await page.query_selector(sel):
                    confirmed = True
                    break
        await page.screenshot(path=ss_path)
        if confirmed:
            return ApplyResult(
                success=True, method="submitted", screenshot_path=ss_path,
                message="Application submitted and confirmed.",
            )
        return ApplyResult(
            success=False, method="form_filled", screenshot_path=ss_path,
            message="Submitted but no confirmation detected — verify manually.",
        )

    async def _apply_greenhouse(self, job: dict, cover_letter: str) -> ApplyResult:
        """Structured apply for Greenhouse boards (stable field ids/names)."""
        context = await self._new_context()
        page = await context.new_page()
        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S)
            apply_btn = await page.query_selector(
                "a#apply_button, button:has-text('Apply'), a:has-text('Apply for this job')"
            )
            if apply_btn:
                try:
                    await apply_btn.click()
                    await asyncio.sleep(PAGE_SETTLE_S)
                except Exception as e:
                    logger.debug(f"greenhouse apply button click: {e}")

            await self._set_first(
                page, ["#first_name", "input[name='first_name']", "input[autocomplete='given-name']"],
                COMMON_ANSWERS["first_name"],
            )
            await self._set_first(
                page, ["#last_name", "input[name='last_name']", "input[autocomplete='family-name']"],
                COMMON_ANSWERS["last_name"],
            )
            await self._set_first(
                page, ["#email", "input[name='email']", "input[type='email']"], COMMON_ANSWERS["email"],
            )
            await self._set_first(
                page, ["#phone", "input[name='phone']", "input[type='tel']"], COMMON_ANSWERS["phone"],
            )
            await self._upload_resume(page, ["#resume", "input[name='resume']", "input[type='file']"])
            await self._set_first(
                page, ["#cover_letter_text", "textarea[name='cover_letter_text']",
                       "textarea[aria-label*='cover' i]"], cover_letter,
            )
            await self._fill_labeled_questions(page, cover_letter, job)
            await self._fill_selects(page)
            return await self._submit_and_confirm(
                page, job, "greenhouse",
                submit_selectors=["#submit_app", "button[type='submit']", "button:has-text('Submit')"],
                confirm_selectors=[
                    "#application_confirmation",
                    "*:has-text('Thank you for applying')",
                    "*:has-text('application has been submitted')",
                ],
                confirm_url_substrings=["confirmation", "thank"],
            )
        except Exception as e:
            logger.error(f"Greenhouse apply error: {e}")
            return ApplyResult(success=False, method="error", message=str(e)[:200])
        finally:
            await self._safe_close(page, context)

    async def _apply_lever(self, job: dict, cover_letter: str) -> ApplyResult:
        """Structured apply for Lever postings (the /apply form)."""
        context = await self._new_context()
        page = await context.new_page()
        try:
            url = job["url"].rstrip("/")
            if not url.endswith("/apply"):
                url = url + "/apply"
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S)

            await self._set_first(page, ["input[name='name']"], COMMON_ANSWERS["name"])
            await self._set_first(page, ["input[name='email']"], COMMON_ANSWERS["email"])
            await self._set_first(page, ["input[name='phone']"], COMMON_ANSWERS["phone"])
            await self._set_first(
                page, ["input[name='urls[LinkedIn]']", "input[name='urls[LinkedIn URL]']"],
                COMMON_ANSWERS["linkedin"],
            )
            await self._upload_resume(page, ["input[name='resume']", "input[type='file']"])
            await self._set_first(page, ["textarea[name='comments']"], cover_letter)
            await self._fill_labeled_questions(page, cover_letter, job)
            await self._fill_selects(page)
            return await self._submit_and_confirm(
                page, job, "lever",
                submit_selectors=["button[type='submit']", "button:has-text('Submit application')", "#btn-submit"],
                confirm_selectors=[
                    "*:has-text('Thank you')",
                    "*:has-text('application has been submitted')",
                    "*:has-text('received your application')",
                ],
                confirm_url_substrings=["thanks", "thank", "confirmation"],
            )
        except Exception as e:
            logger.error(f"Lever apply error: {e}")
            return ApplyResult(success=False, method="error", message=str(e)[:200])
        finally:
            await self._safe_close(page, context)

    async def _apply_ashby(self, job: dict, cover_letter: str) -> ApplyResult:
        """Structured apply for Ashby hosted forms (jobs.ashbyhq.com/{board}/{id}).

        Ashby uses stable _systemfield_* ids for name/email/résumé and radio groups
        for the work-auth/sponsorship questions. Some boards add a reCAPTCHA; if it
        blocks the submit, confirmation isn't detected and it degrades to manual.
        """
        context = await self._new_context()
        page = await context.new_page()
        try:
            url = job["url"].rstrip("/")
            if not url.endswith("/application"):
                url = url + "/application"
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S + 1)  # React form needs a beat to render

            await self._set_first(
                page, ["#_systemfield_name", "input[name='_systemfield_name']"],
                COMMON_ANSWERS["name"],
            )
            await self._set_first(
                page, ["#_systemfield_email", "input[name='_systemfield_email']", "input[type='email']"],
                COMMON_ANSWERS["email"],
            )
            await self._set_first(page, ["input[type='tel']"], COMMON_ANSWERS["phone"])
            await self._upload_resume(page, ["#_systemfield_resume", "input[type='file']"])
            await self._fill_labeled_questions(page, cover_letter, job)
            await self._fill_selects(page)
            await self._fill_radios(page)
            return await self._submit_and_confirm(
                page, job, "ashby",
                submit_selectors=[
                    "button:has-text('Submit Application')", "button[type='submit']",
                    "button:has-text('Submit')",
                ],
                confirm_selectors=[
                    "*:has-text('has been submitted')",
                    "*:has-text('Application received')",
                    "*:has-text('Thank you')",
                ],
                confirm_url_substrings=["thank", "submitted", "confirmation", "success"],
            )
        except Exception as e:
            logger.error(f"Ashby apply error: {e}")
            return ApplyResult(success=False, method="error", message=str(e)[:200])
        finally:
            await self._safe_close(page, context)

    async def _safe_close(self, page, context) -> None:
        try:
            await page.close()
        except Exception as e:
            logger.debug(f"page.close failed: {e}")
        try:
            await context.close()
        except Exception as e:
            logger.debug(f"context.close failed: {e}")

    async def _apply_wellfound(self, job: dict, cover_letter: str) -> ApplyResult:
        """Apply via Wellfound."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S)

            apply_btn = await page.query_selector(
                "button:has-text('Apply'), "
                "a:has-text('Apply')"
            )

            method = "screenshot_only"
            message = "Job page opened but no apply button found."

            if apply_btn:
                await apply_btn.click()
                await asyncio.sleep(PAGE_SETTLE_S)
                await self._fill_generic_form(page, job, cover_letter)
                method = "form_filled"
                message = "Apply button clicked and form filled (unconfirmed)."

            ss_path = str(SCREENSHOTS_DIR / f"wellfound_{job['id']}.png")
            await page.screenshot(path=ss_path)

            # form_filled means we filled inputs but never confirmed a submission,
            # so we do NOT report success — it surfaces as "needs manual" instead.
            actually_applied = False
            return ApplyResult(
                success=actually_applied, method=method,
                screenshot_path=ss_path, message=message,
            )

        except Exception as e:
            logger.error(f"Wellfound apply error: {e}")
            return ApplyResult(success=False, method="error", message=str(e)[:200])
        finally:
            try:
                await page.close()
            except Exception as e:
                logger.debug(f"page.close failed: {e}")
            try:
                await context.close()
            except Exception as e:
                logger.debug(f"context.close failed: {e}")

    async def _apply_generic(self, job: dict, cover_letter: str) -> ApplyResult:
        """Generic apply: open page, fill forms, screenshot."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            await asyncio.sleep(PAGE_SETTLE_S)

            # Try clicking any "Apply" button
            apply_btn = await page.query_selector(
                "button:has-text('Apply'), "
                "a:has-text('Apply'), "
                "button[class*='apply'], "
                "a[class*='apply']"
            )

            method = "screenshot_only"
            message = "Job page opened. Needs manual application."

            if apply_btn:
                await apply_btn.click()
                await asyncio.sleep(PAGE_SETTLE_S)
                await self._fill_generic_form(page, job, cover_letter)
                method = "form_filled"
                message = "Apply button clicked and form filled (unconfirmed)."

            ss_path = str(SCREENSHOTS_DIR / f"generic_{job['id']}.png")
            await page.screenshot(path=ss_path)

            # Generic applies are never confirmed — mark as needing manual review
            return ApplyResult(
                success=False, method=method, screenshot_path=ss_path,
                message=message,
            )

        except Exception as e:
            logger.error(f"Generic apply error: {e}")
            return ApplyResult(success=False, method="error", message=str(e)[:200])
        finally:
            try:
                await page.close()
            except Exception as e:
                logger.debug(f"page.close failed: {e}")
            try:
                await context.close()
            except Exception as e:
                logger.debug(f"context.close failed: {e}")

    async def _fill_generic_form(self, page: Page, job: dict, cover_letter: str):
        """Attempt to fill any form fields on a generic page."""
        all_inputs = await page.query_selector_all(
            "input[type='text'], input[type='email'], "
            "input[type='tel'], input[type='url'], textarea"
        )

        for inp in all_inputs:
            try:
                current = await inp.input_value()
                if current:
                    continue

                name = (await inp.get_attribute("name") or "").lower()
                placeholder = (await inp.get_attribute("placeholder") or "").lower()
                inp_type = (await inp.get_attribute("type") or "").lower()
                aria = (await inp.get_attribute("aria-label") or "").lower()

                hint = f"{name} {placeholder} {inp_type} {aria}"
                value = self._match_field_value(hint, cover_letter)

                if value:
                    await inp.fill(value)
                    await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"generic field fill skipped: {e}")
                continue

        # Upload resume if file input exists
        file_input = await page.query_selector("input[type='file']")
        if file_input and RESUME_PATH.exists():
            try:
                await file_input.set_input_files(str(RESUME_PATH))
            except Exception as e:
                logger.debug(f"generic resume upload failed: {e}")


async def apply_to_single_job(job: dict, headless=True) -> ApplyResult:
    """Apply to a single approved job. Returns ApplyResult."""
    async with AutoApplicant(headless=headless) as applicant:
        return await applicant.apply_to_job(job)


async def apply_to_approved_jobs(jobs: list[dict], headless=True) -> dict:
    """Apply to approved jobs (capped per run) and return a results summary."""
    cap = MAX_APPLIES_PER_RUN if MAX_APPLIES_PER_RUN > 0 else len(jobs)
    batch = jobs[:cap]
    skipped = len(jobs) - len(batch)
    results = {
        "success": 0, "failed": 0, "needs_manual": 0,
        "total": len(batch), "skipped_over_cap": skipped,
    }

    async with AutoApplicant(headless=headless) as applicant:
        for job in batch:
            result = await applicant.apply_to_job(job)
            if result.success:
                results["success"] += 1
            elif result.method in ("screenshot_only", "external_redirect", "manual_handoff", "form_filled"):
                results["needs_manual"] += 1
            else:
                results["failed"] += 1
            # Rate limiting between applications
            await asyncio.sleep(3)

    return results
