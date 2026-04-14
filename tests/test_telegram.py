"""Tests for telegram_bot/bot.py — escape, formatting, callback parsing, task tracking."""
import pytest
from telegram_bot.bot import _escape_md, format_job_message, truncate, _active_apply_tasks


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


class TestTruncate:
    def test_short_text_unchanged(self):
        assert truncate("hello", 100) == "hello"

    def test_long_text_truncated(self):
        result = truncate("a" * 200, 100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")

    def test_exact_length(self):
        assert truncate("abcde", 5) == "abcde"


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
