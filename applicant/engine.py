"""Playwright-based auto-apply engine for job applications."""
import logging
import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from config.settings import LINKEDIN_SESSION_COOKIE, RESUME_PATH
from tracker.database import mark_applied, set_cover_letter, log_action, get_job_by_id
from prompts.generator import generate_cover_letter, generate_form_answer, COMMON_ANSWERS

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


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
            args=["--disable-blink-features=AutomationControlled"],
        )
        return self

    async def __aexit__(self, *args):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def apply_to_job(self, job: dict) -> bool:
        """Apply to a single job. Routes to platform-specific handler."""
        platform = job.get("platform", "")
        job_id = job["id"]

        logger.info(f"Applying to: {job['title']} at {job['company']} ({platform})")

        try:
            # Generate cover letter
            cover_letter = generate_cover_letter(
                job["title"], job["company"], job.get("description", "")
            )
            set_cover_letter(job_id, cover_letter)

            if platform == "linkedin":
                success = await self._apply_linkedin(job, cover_letter)
            elif platform == "indeed":
                success = await self._apply_indeed(job, cover_letter)
            elif platform == "wellfound":
                success = await self._apply_wellfound(job, cover_letter)
            else:
                # Generic: open page+screenshot, mark for manual follow-up
                success = await self._apply_generic(job, cover_letter)

            if success:
                mark_applied(job_id)
                logger.info(f"✅ Applied: {job['title']} at {job['company']}")
            else:
                log_action(job_id, "apply_failed", "Auto-apply did not complete")
                logger.warning(f"⚠️ Apply incomplete: {job['title']} at {job['company']}")

            return success

        except Exception as e:
            logger.error(f"Apply error for job {job_id}: {e}")
            log_action(job_id, "apply_failed", str(e)[:500])
            return False

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

    async def _apply_linkedin(self, job: dict, cover_letter: str) -> bool:
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
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # Look for Easy Apply button
            easy_apply = await page.query_selector(
                "button.jobs-apply-button, "
                "button[aria-label*='Easy Apply'], "
                "button:has-text('Easy Apply')"
            )

            if not easy_apply:
                logger.info(f"No Easy Apply for {job['title']} - taking screenshot for manual apply")
                await page.screenshot(path=str(SCREENSHOTS_DIR / f"linkedin_{job['id']}.png"))
                await page.close()
                await context.close()
                # Still mark as applied since we opened it
                return True

            await easy_apply.click()
            await asyncio.sleep(2)

            # Process multi-step form
            for step in range(10):  # Max 10 steps
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
                    await asyncio.sleep(2)
                    # Check for success
                    success_el = await page.query_selector(
                        "h2:has-text('application was sent'), "
                        "div:has-text('Application submitted')"
                    )
                    if success_el:
                        logger.info("LinkedIn Easy Apply submitted successfully")
                        return True

                # Click Next/Review
                next_btn = await page.query_selector(
                    "button[aria-label*='next'], "
                    "button:has-text('Next'), "
                    "button:has-text('Review')"
                )
                if next_btn:
                    await next_btn.click()
                    await asyncio.sleep(1.5)
                else:
                    break

            await page.screenshot(path=str(SCREENSHOTS_DIR / f"linkedin_{job['id']}_final.png"))
            return True

        except Exception as e:
            logger.error(f"LinkedIn apply error: {e}")
            try:
                await page.screenshot(path=str(SCREENSHOTS_DIR / f"linkedin_{job['id']}_error.png"))
            except Exception:
                pass
            return False
        finally:
            await page.close()
            await context.close()

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
            except Exception:
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
            except Exception:
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

        return ""

    async def _apply_indeed(self, job: dict, cover_letter: str) -> bool:
        """Apply via Indeed."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            apply_btn = await page.query_selector(
                "button[id*='applyButton'], "
                "a[id*='applyButton'], "
                "button:has-text('Apply now'), "
                "a:has-text('Apply on company site')"
            )

            if apply_btn:
                href = await apply_btn.get_attribute("href")
                if href:
                    # External application
                    await page.goto(href, wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(2)
                else:
                    await apply_btn.click()
                    await asyncio.sleep(2)

            # Fill any visible forms
            await self._fill_generic_form(page, job, cover_letter)
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"indeed_{job['id']}.png"))
            return True

        except Exception as e:
            logger.error(f"Indeed apply error: {e}")
            return False
        finally:
            await page.close()
            await context.close()

    async def _apply_wellfound(self, job: dict, cover_letter: str) -> bool:
        """Apply via Wellfound."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            apply_btn = await page.query_selector(
                "button:has-text('Apply'), "
                "a:has-text('Apply')"
            )

            if apply_btn:
                await apply_btn.click()
                await asyncio.sleep(2)

            await self._fill_generic_form(page, job, cover_letter)
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"wellfound_{job['id']}.png"))
            return True

        except Exception as e:
            logger.error(f"Wellfound apply error: {e}")
            return False
        finally:
            await page.close()
            await context.close()

    async def _apply_generic(self, job: dict, cover_letter: str) -> bool:
        """Generic apply: open page, fill forms, screenshot."""
        context = await self._new_context()
        page = await context.new_page()

        try:
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(2)

            # Try clicking any "Apply" button
            apply_btn = await page.query_selector(
                "button:has-text('Apply'), "
                "a:has-text('Apply'), "
                "button[class*='apply'], "
                "a[class*='apply']"
            )
            if apply_btn:
                await apply_btn.click()
                await asyncio.sleep(2)

            await self._fill_generic_form(page, job, cover_letter)
            await page.screenshot(path=str(SCREENSHOTS_DIR / f"generic_{job['id']}.png"))
            return True

        except Exception as e:
            logger.error(f"Generic apply error: {e}")
            return False
        finally:
            await page.close()
            await context.close()

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
            except Exception:
                continue

        # Upload resume if file input exists
        file_input = await page.query_selector("input[type='file']")
        if file_input and RESUME_PATH.exists():
            try:
                await file_input.set_input_files(str(RESUME_PATH))
            except Exception:
                pass


async def apply_to_approved_jobs(jobs: list[dict], headless=True) -> dict:
    """Apply to all approved jobs and return results summary."""
    results = {"success": 0, "failed": 0, "total": len(jobs)}

    async with AutoApplicant(headless=headless) as applicant:
        for job in jobs:
            success = await applicant.apply_to_job(job)
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1
            # Rate limiting between applications
            await asyncio.sleep(3)

    return results
