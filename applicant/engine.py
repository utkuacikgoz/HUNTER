"""Playwright-based auto-apply engine for job applications."""
import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Page, async_playwright

from config.settings import (
    APPLY_DRY_RUN,
    CHROMIUM_LAUNCH_ARGS,
    COMMON_ANSWERS,
    LINKEDIN_SESSION_COOKIE,
    MAX_APPLIES_PER_RUN,
    RESUME_PATH,
)
from prompts.generator import (
    choose_dropdown_option,
    generate_cover_letter,
    generate_form_answer,
)
from tracker.database import (
    get_job_by_id,
    has_unconfirmed_submit,
    log_action,
    mark_applied,
    set_cover_letter,
)

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
MENU_SETTLE_S = 0.4              # pause for a dropdown menu to render its options
OPTION_WAIT_S = 3.0              # how long to wait for a filtered option list to arrive
CONFIRM_TIMEOUT_S = 15           # how long to wait for a submit to be confirmed
CLICK_TIMEOUT_MS = 5000          # per-click actionability wait (Playwright's default is 30s)

# Collect radio / checkbox groups as {key: {question, kind, required, checked, options}}.
# Grouped by `name` so a single-select rendered as several checkboxes (Lever's custom
# cards) is answered with exactly one option. The question is the nearest label that
# isn't one of the group's own option labels — otherwise "Woman" reads as the question.
_CHOICE_GROUPS_JS = r"""() => {
    const out = {};
    document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach((el, i) => {
        const name = el.getAttribute('name') || ('__unnamed_' + i);
        const optLabel = (
            (el.id && document.querySelector('label[for="' + (window.CSS?.escape ? CSS.escape(el.id) : el.id) + '"]')?.textContent)
            || el.closest('label')?.textContent
            || el.getAttribute('value') || ''
        ).replace(/\s+/g, ' ').trim();
        if (!out[name]) {
            out[name] = {question: '', kind: el.getAttribute('type'), required: false,
                         checked: false, options: []};
        }
        out[name].options.push({id: el.id, name: el.getAttribute('name'),
                                value: el.getAttribute('value'), label: optLabel});
        if (el.required || el.getAttribute('aria-required') === 'true') out[name].required = true;
        if (el.checked) out[name].checked = true;
    });
    document.querySelectorAll('input[type=radio], input[type=checkbox]').forEach((el, i) => {
        const name = el.getAttribute('name') || ('__unnamed_' + i);
        const grp = out[name];
        if (!grp || grp.question) return;
        const own = new Set(grp.options.map(o => o.label.toLowerCase()));
        let node = el;
        for (let d = 0; d < 8 && node; d++) {
            node = node.parentElement;
            if (!node) break;
            const cand = node.querySelector('.application-label, legend, .field-label, label');
            if (cand) {
                const t = cand.textContent.replace(/\s+/g, ' ').trim();
                if (t && !own.has(t.toLowerCase()) && !/^(yes|no)$/i.test(t)) {
                    grp.question = t.slice(0, 200);
                    break;
                }
            }
        }
    });
    return out;
}"""

# Resolve the question text for a form control: <label for=id>, then an enclosing
# label, then aria-label / name. Shared by every field-filling pass so they all read
# the same question for the same element.
_LABEL_JS = r"""el => {
    let t = '';
    const id = el.getAttribute('id');
    if (id) {
        const l = document.querySelector('label[for="' + (window.CSS?.escape ? CSS.escape(id) : id) + '"]');
        if (l) t = l.textContent;
    }
    if (!t) {
        const by = el.getAttribute('aria-labelledby');
        if (by) { const l = document.getElementById(by); if (l) t = l.textContent; }
    }
    if (!t) { const l = el.closest('div')?.querySelector('label, legend'); if (l) t = l.textContent; }
    return (t || el.getAttribute('aria-label') || el.getAttribute('name') || '').replace(/\s+/g, ' ').trim();
}"""


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
        # A previous run clicked submit but couldn't confirm it. The application may
        # well have landed, so re-running would double-apply to the same employer —
        # which is worse than doing nothing. Hand it back for a human to check.
        if has_unconfirmed_submit(job_id):
            logger.info(f"Job {job_id} has an unconfirmed submit; not re-applying")
            return ApplyResult(
                success=False, method="manual_handoff",
                message=(
                    "A previous run already clicked submit but couldn't confirm it. "
                    "Check your email before applying again."
                ),
            )

        logger.info(f"Applying to: {job['title']} at {job['company']} ({platform})")

        try:
            # Generate cover letter. generate_cover_letter is a blocking Anthropic
            # call and this coroutine runs on the bot's event loop (via the apply
            # worker), so it goes to a thread — otherwise Telegram polling and the
            # scheduler stall for the whole request.
            cover_letter = await asyncio.to_thread(
                generate_cover_letter,
                job["title"], job["company"], job.get("description", ""),
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
            elif result.method == "submitted_unconfirmed":
                # Recorded under its own action so the guard above can find it.
                log_action(job_id, "apply_submitted_unconfirmed", result.message)
                logger.warning(f"⚠️ Unconfirmed submit: {job['title']} at {job['company']}")
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
        # Lever asks for these as their own required fields ("org", "Current company").
        if "current company" in hint or "current employer" in hint or hint.strip() == "org":
            return COMMON_ANSWERS["current_company"]
        if "current title" in hint or "current role" in hint or "job title" in hint:
            return COMMON_ANSWERS["current_title"]
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

    async def _fill_typeahead(self, page: Page, selectors: list[str], value: str) -> bool:
        """Fill a geocoder-style autocomplete and select a suggestion.

        Lever's "Current location" only counts as answered once a suggestion is
        picked (it writes a hidden `selectedLocation`); typing alone leaves the field
        failing validation. The suggestion list renders inside a container the widget
        keeps `display:none`, so the click is dispatched in-page — Playwright rightly
        refuses to click an invisible element.
        """
        if not value:
            return False
        for sel in selectors:
            el = await page.query_selector(sel)
            if not el:
                continue
            # Click when the field allows it (some widgets only start their lookup on
            # a real pointer event), else fall back to focus for fields that never
            # become "actionable".
            try:
                await el.click(timeout=CLICK_TIMEOUT_MS)
            except Exception as e:
                logger.debug(f"typeahead click failed for {sel}, using focus: {e}")
                try:
                    await el.focus()
                except Exception as focus_err:
                    logger.debug(f"typeahead focus failed for {sel}: {focus_err}")
                    continue
            try:
                await page.keyboard.type(value, delay=40)
            except Exception as e:
                logger.debug(f"typeahead type failed for {sel}: {e}")
                continue
            deadline = asyncio.get_running_loop().time() + OPTION_WAIT_S
            while asyncio.get_running_loop().time() < deadline:
                suggestion = await page.query_selector(
                    ".dropdown-results > *, [role='option'], .location-suggestion"
                )
                if suggestion:
                    try:
                        await suggestion.evaluate("el => el.click()")
                        await asyncio.sleep(0.3)
                        logger.debug(f"typeahead {sel} -> {(await suggestion.inner_text())[:40]!r}")
                        return True
                    except Exception as e:
                        logger.debug(f"typeahead select failed: {e}")
                        return False
                await asyncio.sleep(0.2)
            logger.info(f"No suggestions for {sel} ({value!r}) — left as typed")
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
                label = await el.evaluate(_LABEL_JS)
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
        """Map a dropdown's question to the option text to pick, from the candidate's
        known answers. Returns None when no rule applies — the caller then asks the
        model to choose from the actual option list.

        "decline" and the yes/no answers are matched against option text by
        `_match_option`; anything else is a literal value to type or substring-match
        (a city, a country, a referral source).
        """
        h = (label or "").lower()
        if not h:
            return None
        # Demographic questions are answered by rule and never sent to the model:
        # we always decline, so there is nothing to reason about.
        if any(w in h for w in ("gender", "race", "ethnic", "veteran", "disability",
                                "hispanic", "latino", "identify", "orientation",
                                "neurodiver", "pronoun")):
            return "decline"
        # Check authorization BEFORE sponsorship: "authorized to work WITHOUT
        # sponsorship?" mentions sponsorship but is really an authorization question
        # (answer = work_authorized = No), distinct from "do you REQUIRE sponsorship?"
        if "authori" in h or "eligible" in h or "legally" in h or "right to work" in h:
            return "yes" if COMMON_ANSWERS["work_authorized"].lower().startswith("y") else "no"
        if "sponsor" in h:  # "Will you require visa sponsorship?"
            return "yes" if COMMON_ANSWERS["requires_sponsorship"].lower().startswith("y") else "no"
        # Residency/tax questions follow from where the candidate actually lives.
        if "tax resident" in h or "resident of" in h:
            return "yes" if COMMON_ANSWERS["us_tax_resident"].lower().startswith("y") else "no"
        # Mandatory consent/acknowledgement questions ("do you consent to us storing
        # your application?"). The user approved this specific job for auto-apply, and
        # the ATS won't accept the application without it, so the affirmative option
        # is chosen — see _match_option, which refuses any negated variant.
        if "consent" in h or "privacy" in h or "agree to" in h or "acknowledge" in h:
            return "consent"
        if "hear about" in h or "how did you" in h or "referr" in h or "source" in h:
            return COMMON_ANSWERS["referral_source"].lower()
        # Location dropdowns are split into country and city on most ATS forms, and a
        # combined "Istanbul, Turkey" matches neither list.
        if "country" in h:
            return COMMON_ANSWERS["country"].lower()
        if "city" in h or "location" in h:
            return COMMON_ANSWERS["city"].lower()
        if any(w in h for w in ("remote", "work from home", "wfh")) or "relocat" in h:
            return "yes"
        return None

    @staticmethod
    def _match_option(target: str, options: list[str]) -> int | None:
        """Index of the option that satisfies `target`, or None."""
        lowered = [(o or "").strip().lower() for o in options]
        if target == "decline":
            for i, txt in enumerate(lowered):
                if any(p in txt for p in ("decline", "prefer not", "wish not", "do not wish",
                                          "not to answer", "not to say", "rather not",
                                          "do not want to answer", "don't want to answer",
                                          "not disclose", "choose not")):
                    return i
            return None
        if target == "consent":
            negated = ("not ", "n't", "withdraw", "decline", "refuse", "object")
            for i, txt in enumerate(lowered):
                if any(n in txt for n in negated):
                    continue
                if txt.startswith(("i consent", "consent", "i agree", "agree", "yes")) or \
                        "i consent" in txt or "i agree" in txt:
                    return i
            return None
        if target in ("yes", "no"):
            for i, txt in enumerate(lowered):
                if txt == target or txt.startswith(target):
                    return i
            return None
        for i, txt in enumerate(lowered):
            if target in txt:
                return i
        return None

    async def _fill_selects(self, page: Page) -> None:
        """Answer native <select> dropdowns (work auth, sponsorship, EEO, source)
        from the candidate's known answers. Custom div-comboboxes are skipped."""
        for sel in await page.query_selector_all("select"):
            try:
                label = await sel.evaluate(_LABEL_JS)
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

    async def _safe_click(self, el) -> bool:
        """Scroll into view, then click with a short timeout.

        Playwright's default 30s actionability wait turns one covered element into a
        30-second stall; an open dropdown overlapping the next field did exactly that
        three times per Greenhouse form, so a single application took minutes and
        left the covered questions unanswered.
        """
        try:
            await el.scroll_into_view_if_needed(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            logger.debug(f"scroll_into_view failed: {e}")
        try:
            await el.click(timeout=CLICK_TIMEOUT_MS)
            return True
        except Exception as e:
            logger.debug(f"click failed: {e}")
            return False

    async def _fill_comboboxes(self, page: Page, job: dict | None = None) -> None:
        """Answer react-select style comboboxes.

        Greenhouse's current application form renders every dropdown — country, city,
        work authorization, EEO — as `input[role=combobox]` backed by a hidden mirror
        input, with no native <select> anywhere on the page. `_fill_selects` never
        sees them, so before this existed they stayed blank and the required ones
        failed the form's own validation: the submit bounced and every Greenhouse
        application degraded to a manual apply.

        Rules first (`_select_target`), then the model picks from the actual option
        list. Demographic questions never reach the model — they resolve to "decline"
        by rule, and are skipped entirely if no decline-style option exists.
        """
        # Answering one react-select re-renders the form, which detaches every element
        # handle captured before it: the stale handles then just time out, silently
        # leaving the rest of the questions blank. So collect ids once and re-query
        # each box immediately before touching it.
        # Only comboboxes inside the form: job boards put their own search box on the
        # page, and answering that one costs a model call and types into the wrong field.
        ids = await page.evaluate(
            "() => Array.from(document.querySelectorAll('form input[role=combobox]'))"
            ".map(el => el.getAttribute('id') || '')"
        )
        for position, box_id in enumerate(ids):
            try:
                box = await self._combobox_handle(page, box_id, position)
                if box is None:
                    continue
                if await box.evaluate("el => { const r = el.getBoundingClientRect();"
                                      " return r.width === 0 && r.height === 0; }"):
                    continue  # e.g. the phone widget's hidden country search input
                if await self._combobox_has_value(page, box):
                    continue
                label = await box.evaluate(_LABEL_JS)
                if not label:
                    continue
                # Close anything still open so it can't cover the next field.
                await page.keyboard.press("Escape")
                await self._pick_combobox_option(page, box, label)
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.debug(f"combobox skipped: {e}")

    async def _combobox_handle(self, page: Page, box_id: str, position: int):
        """A fresh handle for one combobox: by id, else by position in the form."""
        if box_id:
            # Attribute selector, not '#id': Greenhouse uses numeric ids ("627"),
            # which are not valid CSS id selectors.
            return await page.query_selector(f'form input[role="combobox"][id="{box_id}"]')
        boxes = await page.query_selector_all("form input[role='combobox']")
        return boxes[position] if position < len(boxes) else None

    async def _pick_combobox_option(self, page: Page, box, label: str) -> bool:
        """Open one combobox, choose an option, and click it. True if answered."""
        target = self._select_target(label)
        # Open with the keyboard, not the mouse. These inputs are ~4px wide and shift
        # as the form re-renders around them, so Playwright's actionability check
        # ("visible and stable") times out on a plain click — which is what silently
        # left the required work-authorization and location questions blank. focus()
        # has no such wait, and react-select opens its menu on ArrowDown.
        try:
            await box.focus()
        except Exception as e:
            logger.debug(f"combobox focus failed: {e}")
            return False
        await page.keyboard.press("ArrowDown")
        await asyncio.sleep(MENU_SETTLE_S)

        # Long filterable lists (country, city) only render matching options once
        # you type, so type the literal answer first.
        typed = target if target and target not in ("yes", "no", "decline", "consent") else None
        if typed:
            await page.keyboard.type(typed, delay=15)

        # Some lists are served by a lookup that answers well after the keystrokes
        # (Greenhouse's city field is a geocoder), so poll rather than sleep once.
        options: list = []
        texts: list[str] = []
        deadline = asyncio.get_running_loop().time() + (
            OPTION_WAIT_S if typed is not None else MENU_SETTLE_S
        )
        while True:
            options = await page.query_selector_all("[role='option']")
            texts = [((await o.inner_text()) or "").strip() for o in options]
            if texts and (typed is None or self._match_option(typed, texts) is not None):
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.2)

        if not texts:
            await page.keyboard.press("Escape")
            return False

        index = self._match_option(target, texts) if target else None
        if index is None:
            if target == "decline":
                # No decline-style option: leave the demographic question blank
                # rather than answering it, and never send it to the model.
                await page.keyboard.press("Escape")
                return False
            index = await asyncio.to_thread(choose_dropdown_option, label, texts)

        if index is None or index >= len(options):
            await page.keyboard.press("Escape")
            logger.info(f"No answer for dropdown {label[:60]!r} — left blank")
            return False
        picked = await self._safe_click(options[index])
        if not picked:
            # Menu re-rendered under the pointer: walk to the option with the keyboard
            # instead (react-select highlights the first option when the menu opens).
            for _ in range(index):
                await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            picked = await self._combobox_has_value(page, box)
        await page.keyboard.press("Escape")
        if picked:
            logger.debug(f"dropdown {label[:40]!r} -> {texts[index][:40]!r}")
        return picked

    async def _combobox_has_value(self, page: Page, box) -> bool:
        """True once the combobox shows a selected value."""
        try:
            return bool(await box.evaluate(
                "el => !!el.closest('.select, [class*=select]')"
                "?.querySelector('.select__single-value, [class*=singleValue]')"
            ))
        except Exception as e:
            logger.debug(f"combobox value check failed: {e}")
            return False

    async def _missing_required_fields(self, page: Page) -> list[str]:
        """Labels of required fields that are still empty.

        Used as a pre-submit gate: submitting an incomplete form either bounces off
        the ATS's validation (and reads as "submitted but unconfirmed" downstream) or,
        worse, lands a half-filled application under the candidate's name. Either way
        it's better to hand the job back for a manual apply and say which questions
        were unanswered.

        `input[role=combobox]` is excluded on purpose: react-select clears that input
        after a choice, so it always looks empty — its hidden mirror input carries the
        real value and is checked instead.
        """
        return await page.evaluate(
            r"""() => {
            const out = [];
            const els = document.querySelectorAll(
                'input[required], select[required], textarea[required], [aria-required="true"]'
            );
            els.forEach(el => {
                if (el.getAttribute('role') === 'combobox') return;
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (type === 'hidden') return;
                if (type === 'checkbox' || type === 'radio') {
                    const name = el.getAttribute('name');
                    if (!name) return;
                    const esc = window.CSS?.escape ? CSS.escape(name) : name;
                    if (document.querySelector('input[name="' + esc + '"]:checked')) return;
                } else if ((el.value || '').trim()) {
                    return;
                }
                let label = '';
                const id = el.getAttribute('id');
                if (id) {
                    const esc = window.CSS?.escape ? CSS.escape(id) : id;
                    const l = document.querySelector('label[for="' + esc + '"]');
                    if (l) label = l.textContent;
                }
                if (!label) {
                    // Walk up until a label turns up: react-select's hidden mirror
                    // input (the element that actually carries `required`) sits
                    // several divs below the question's label.
                    let node = el.parentElement;
                    for (let i = 0; i < 6 && node && !label; i++) {
                        const l = node.querySelector('label, legend');
                        if (l) label = l.textContent;
                        node = node.parentElement;
                    }
                }
                label = (label || el.getAttribute('aria-label') || el.getAttribute('name') || '')
                    .replace(/\s+/g, ' ').replace(/\*$/, '').trim();
                if (label) out.push(label);
            });
            return [...new Set(out)];
        }"""
        )

    async def _await_confirmation(
        self, page: Page, before_url: str, confirm_selectors: list[str],
        confirm_url_substrings: list[str],
    ) -> bool:
        """Poll for real evidence the application landed.

        Deliberately not a bare text match on the whole page: `*:has-text('Thank you')`
        matches <html> whenever those words appear anywhere — including in the footer
        of the form we just failed to submit — which would report a confirmed apply
        for an application that was never sent. Evidence is a navigation to a
        confirmation URL, or a confirmation element on a page that no longer shows
        the form.
        """
        deadline = asyncio.get_running_loop().time() + CONFIRM_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            url = (page.url or "").lower()
            if url != before_url.lower() and any(sub in url for sub in confirm_url_substrings):
                return True
            for sel in confirm_selectors:
                try:
                    if await page.query_selector(sel):
                        return True
                except Exception as e:
                    logger.debug(f"confirm selector {sel} failed: {e}")
            await asyncio.sleep(0.5)
        return False

    async def _fill_choice_groups(self, page: Page) -> list[str]:
        """Answer radio groups, checkbox groups, and standalone consent checkboxes.

        These are one mechanism, not three: an ATS renders a single-select question as
        radios (Ashby) or as a group of checkboxes sharing a name (Lever's custom
        cards). They must be answered with exactly ONE option — ticking every required
        checkbox in a group would answer "No" *and* "Yes" to the same question.

        A required checkbox that is alone in its group is a mandatory acknowledgement
        (data-processing consent, "I confirm the above"); the application cannot be
        submitted without it, so it is ticked and returned for the application log.
        Optional checkboxes — marketing opt-ins, talent-pool sign-ups — are never
        touched.

        Rules first, then the model picks from the real option labels; demographic
        questions resolve to "decline" by rule and never reach the model.
        """
        groups = await page.evaluate(_CHOICE_GROUPS_JS)
        consented: list[str] = []
        for grp in groups.values():
            try:
                if grp.get("checked"):
                    continue
                options = grp.get("options", [])
                labels = [(o.get("label") or "").strip() for o in options]
                question = grp.get("question", "")

                # Lone required checkbox => mandatory acknowledgement.
                if grp.get("kind") == "checkbox" and len(options) == 1:
                    if not grp.get("required"):
                        continue
                    if await self._click_choice(page, options[0]):
                        consented.append(question or labels[0] or "(unlabeled consent box)")
                    continue

                target = self._select_target(question)
                index = self._match_option(target, labels) if target else None
                if index is None:
                    if target == "decline":
                        continue  # leave a demographic question blank, never guess
                    index = await asyncio.to_thread(choose_dropdown_option, question, labels)
                if index is None or index >= len(options):
                    continue
                if await self._click_choice(page, options[index]):
                    logger.debug(f"choice {question[:40]!r} -> {labels[index][:40]!r}")
            except Exception as e:
                logger.debug(f"choice group skipped: {e}")
        if consented:
            logger.info(f"Accepted {len(consented)} required consent box(es): {consented}")
        return consented

    async def _click_choice(self, page: Page, option: dict) -> bool:
        """Tick one radio/checkbox.

        The input itself is usually visually hidden behind a styled label, so a real
        click on it never becomes actionable — Lever's survey inputs and Ashby's
        radios both fail that way. Try the label, then the input, then dispatch the
        click in-page, which is what actually works for a hidden input.
        """
        selectors = []
        if option.get("id"):
            selectors.append(f'label[for="{option["id"]}"]')
        if option.get("name") and option.get("value") is not None:
            selectors.append(f'input[name="{option["name"]}"][value="{option["value"]}"]')

        handle = None
        for sel in selectors:
            try:
                await page.click(sel, timeout=CLICK_TIMEOUT_MS)
                return True
            except Exception as e:
                logger.debug(f"choice click {sel} failed: {e}")
                if handle is None:
                    handle = await page.query_selector(sel)
        # Last resort: click it from inside the page. A hidden <input type=radio>
        # still toggles and still fires change/input handlers this way.
        for sel in selectors:
            el = await page.query_selector(sel)
            if el is None:
                continue
            try:
                await el.evaluate("el => el.click()")
                return True
            except Exception as e:
                logger.debug(f"in-page choice click {sel} failed: {e}")
        return False

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
        missing = await self._missing_required_fields(page)

        if APPLY_DRY_RUN:
            await page.screenshot(path=ss_path)
            gaps = f" Unanswered required: {', '.join(missing[:6])}." if missing else " All required fields answered."
            return ApplyResult(
                success=False, method="form_filled", screenshot_path=ss_path,
                message=f"DRY RUN: form filled, submit skipped (APPLY_DRY_RUN).{gaps}",
            )

        # Don't submit a form the ATS will reject anyway — and never send a
        # half-filled application under the candidate's name. Say which questions
        # were unanswered so the manual apply takes seconds.
        if missing:
            await page.screenshot(path=ss_path)
            logger.warning(f"{platform}: not submitting, unanswered required fields: {missing}")
            return ApplyResult(
                success=False, method="form_filled", screenshot_path=ss_path,
                message=(
                    "Didn't submit — these required questions were left unanswered: "
                    f"{', '.join(missing[:6])}. Apply manually via the link."
                ),
            )

        before_url = page.url or ""
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

        confirmed = await self._await_confirmation(
            page, before_url, confirm_selectors, confirm_url_substrings
        )
        await page.screenshot(path=ss_path)
        if confirmed:
            return ApplyResult(
                success=True, method="submitted", screenshot_path=ss_path,
                message="Application submitted and confirmed.",
            )
        # Submit was clicked but nothing confirmed it. The application may or may not
        # have landed, so this is reported as its own method: the caller records it and
        # refuses to auto-resubmit later, which would double-apply to the same employer.
        return ApplyResult(
            success=False, method="submitted_unconfirmed", screenshot_path=ss_path,
            message=(
                f"Clicked submit but saw no confirmation within {CONFIRM_TIMEOUT_S}s. "
                "Check your email before re-applying."
            ),
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
            # The current Greenhouse form (job-boards.greenhouse.io) has no native
            # <select> at all — country, city, work authorization and EEO are all
            # react-select comboboxes.
            await self._fill_comboboxes(page, job)
            await self._fill_choice_groups(page)
            return await self._submit_and_confirm(
                page, job, "greenhouse",
                submit_selectors=[
                    "button[type='submit']", "button:has-text('Submit application')",
                    "#submit_app", "button:has-text('Submit')",
                ],
                confirm_selectors=[
                    "#application_confirmation",
                    "[class*='confirmation']",
                    "h1:has-text('Thank you')",
                    "h2:has-text('Thank you')",
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
            # Lever requires location (geocoder-backed) and current employer.
            await self._fill_typeahead(
                page, ["#location-input", "input[name='location']"], COMMON_ANSWERS["city"],
            )
            await self._set_first(
                page, ["input[name='org']"], COMMON_ANSWERS["current_company"],
            )
            await self._set_first(
                page, ["input[name='urls[LinkedIn]']", "input[name='urls[LinkedIn URL]']"],
                COMMON_ANSWERS["linkedin"],
            )
            await self._upload_resume(page, ["input[name='resume']", "input[type='file']"])
            await self._set_first(page, ["textarea[name='comments']"], cover_letter)
            await self._fill_labeled_questions(page, cover_letter, job)
            await self._fill_selects(page)
            await self._fill_comboboxes(page, job)
            await self._fill_choice_groups(page)
            return await self._submit_and_confirm(
                page, job, "lever",
                submit_selectors=["button[type='submit']", "button:has-text('Submit application')", "#btn-submit"],
                confirm_selectors=[
                    "[class*='confirmation']",
                    "h1:has-text('Thank you')",
                    "h2:has-text('Thank you')",
                    "h1:has-text('Application received')",
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
            await self._fill_comboboxes(page, job)
            await self._fill_choice_groups(page)
            return await self._submit_and_confirm(
                page, job, "ashby",
                submit_selectors=[
                    "button:has-text('Submit Application')", "button[type='submit']",
                    "button:has-text('Submit')",
                ],
                confirm_selectors=[
                    "[class*='confirmation']",
                    "h1:has-text('has been submitted')",
                    "h2:has-text('has been submitted')",
                    "h1:has-text('Thank you')",
                    "h2:has-text('Thank you')",
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
            elif result.method in ("screenshot_only", "external_redirect", "manual_handoff",
                                   "form_filled", "submitted_unconfirmed"):
                results["needs_manual"] += 1
            else:
                results["failed"] += 1
            # Rate limiting between applications
            await asyncio.sleep(3)

    return results
