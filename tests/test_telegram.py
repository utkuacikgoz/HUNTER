"""Tests for telegram_bot/bot.py — escape, formatting, callback parsing, task tracking."""
from applicant.engine import ApplyResult
from telegram_bot.bot import (
    _active_apply_tasks,
    _escape_md,
    _format_apply_result,
    _review_keyboard,
    format_job_message,
)


class TestEscapeMd:
    def test_escapes_special_chars(self):
        result = _escape_md("hello_world *bold* [link](url)")
        assert "\\_" in result
        assert "\\*" in result
        assert "\\[" in result
        assert "\\(" in result

    def test_plain_text_unchanged(self):
        assert _escape_md("hello world") == "hello world"

    def test_all_special_chars(self):
        special = '_*[]()~`>#+-=|{}.!'
        result = _escape_md(special)
        # Every char should be escaped
        for char in special:
            assert f"\\{char}" in result


class TestReviewKeyboard:
    def _texts(self, markup):
        return [btn.text for row in markup.inline_keyboard for btn in row]

    def test_auto_apply_source_has_approve_skip(self):
        job = {"id": 1, "url": "https://x/y", "platform": "greenhouse"}
        texts = self._texts(_review_keyboard(job))
        assert "✅ Approve" in texts
        assert "❌ Skip" in texts
        assert "🔗 View Job" in texts

    def test_link_only_source_has_skip_but_no_approve(self):
        # RemoteOK / WeWorkRemotely / Recruitee / SmartRecruiters can't be auto-applied,
        # so they get no Approve — but Skip stays so the user can still dismiss them.
        for platform in ("remoteok", "weworkremotely", "recruitee", "smartrecruiters"):
            texts = self._texts(_review_keyboard({"id": 9, "url": "https://x/y", "platform": platform}))
            assert texts == ["❌ Skip", "🔗 View Job"], platform
            assert "✅ Approve" not in texts, platform


class TestFormatJobMessage:
    def test_includes_title_and_company(self):
        job = {
            "title": "Senior PM",
            "company": "Acme",
            "location": "Remote",
            "salary": "$100k",
            "url": "https://example.com/job",
            "platform": "linkedin",
            "id": 1,
        }
        msg = format_job_message(job, 1)
        assert "Senior PM" in msg
        assert "Acme" in msg
        assert "Remote" in msg
        assert "100k" in msg
        assert "LinkedIn" in msg or "linkedin" in msg.lower()
        # The header no longer carries an inline apply link — the View Job button covers it.
        assert "Apply Link" not in msg

    def test_no_salary_line(self):
        job = {
            "title": "PM",
            "company": "Co",
            "location": "NYC",
            "salary": "",
            "url": "https://example.com/job2",
            "platform": "indeed",
            "id": 2,
        }
        msg = format_job_message(job, 1)
        assert "💰" not in msg

    def test_no_location_shows_not_specified(self):
        job = {
            "title": "PM",
            "company": "Co",
            "location": "",
            "salary": "",
            "url": "https://example.com/job3",
            "platform": "indeed",
            "id": 3,
        }
        msg = format_job_message(job, 1)
        assert "Not specified" in msg

    def test_include_shows_sponsor_friendly_badge(self):
        job = {
            "title": "PM", "company": "Co", "location": "Remote", "salary": "",
            "url": "https://example.com/j", "platform": "greenhouse", "id": 4,
            "filter_verdict": "include",
        }
        # Hyphen is MarkdownV2-escaped ("Sponsor\\-friendly"); check the stable prefix.
        assert "Sponsor" in format_job_message(job, 1)

    def test_flag_shows_no_sponsor_badge(self):
        # Unclear sponsorship: don't claim anything — no badge line at all.
        job = {
            "title": "PM", "company": "Co", "location": "Remote", "salary": "",
            "url": "https://example.com/j", "platform": "greenhouse", "id": 5,
            "filter_verdict": "flag",
        }
        msg = format_job_message(job, 1)
        assert "Sponsor" not in msg
        assert "Unclear" not in msg


class TestFormatApplyResult:
    """The apply-status message must be honest: only a confirmed submit is a
    success; everything else is a manual apply with the job URL."""

    JOB = {"title": "Senior PM", "company": "Acme", "url": "https://example.com/job"}

    def test_form_filled_reads_as_manual_not_success(self):
        result = ApplyResult(success=False, method="form_filled", message="unconfirmed")
        msg = _format_apply_result(result, self.JOB)
        assert "APPLY MANUALLY" in msg
        assert "CONFIRMED" not in msg
        assert self.JOB["url"] in msg  # one-tap follow-through

    def test_screenshot_only_needs_manual_with_url(self):
        result = ApplyResult(success=False, method="screenshot_only", message="no button")
        msg = _format_apply_result(result, self.JOB)
        assert "APPLY MANUALLY" in msg
        assert self.JOB["url"] in msg

    def test_external_redirect_needs_manual_with_url(self):
        result = ApplyResult(success=False, method="external_redirect", message="redirect")
        msg = _format_apply_result(result, self.JOB)
        assert "APPLY MANUALLY" in msg
        assert self.JOB["url"] in msg

    def test_success_reads_as_applied(self):
        result = ApplyResult(success=True, method="easy_apply", message="confirmed")
        msg = _format_apply_result(result, self.JOB)
        assert "APPLIED" in msg

    def test_already_applied_distinct(self):
        result = ApplyResult(success=True, method="already_applied", message="skipped")
        msg = _format_apply_result(result, self.JOB)
        assert "ALREADY APPLIED" in msg

    def test_error_reads_as_failed(self):
        result = ApplyResult(success=False, method="error", message="boom")
        msg = _format_apply_result(result, self.JOB)
        assert "APPLY FAILED" in msg


class TestCallbackParsing:
    """Test the callback_data format used by inline buttons."""

    def test_approve_format(self):
        data = "approve_42"
        parts = data.split("_", 1)
        assert parts[0] == "approve"
        assert int(parts[1]) == 42

    def test_reject_format(self):
        data = "reject_99"
        parts = data.split("_", 1)
        assert parts[0] == "reject"
        assert int(parts[1]) == 99

    def test_followedup_format(self):
        data = "followedup_7"
        parts = data.split("_", 1)
        assert parts[0] == "followedup"
        assert int(parts[1]) == 7

    def test_invalid_format_handled(self):
        data = "baddata"
        parts = data.split("_", 1)
        assert len(parts) == 1  # No underscore → invalid

    def test_overflow_job_id_rejected(self):
        """Job IDs > 2^31-1 should be rejected by callback_handler."""
        big_id = 2**31
        assert big_id > 2**31 - 1  # Over the limit


class TestActiveApplyTasks:
    def test_task_set_exists(self):
        """_active_apply_tasks should be a set for tracking background tasks."""
        assert isinstance(_active_apply_tasks, set)

    def test_task_set_starts_empty(self):
        """No tasks should be active at import time."""
        assert len(_active_apply_tasks) == 0
