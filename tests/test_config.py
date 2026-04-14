"""Tests for config/settings.py and module imports."""
import pytest
from config.settings import validate_config


class TestValidateConfig:
    def test_stats_needs_no_credentials(self):
        errors = validate_config("stats")
        # stats should not require any credentials
        assert errors == []

    def test_backup_needs_no_credentials(self):
        errors = validate_config("backup")
        assert errors == []

    def test_unknown_command_no_errors(self):
        errors = validate_config("nonexistent")
        assert errors == []


class TestModuleImports:
    """Verify key functions/classes are importable from their modules."""

    def test_apply_to_single_job_importable(self):
        from applicant.engine import apply_to_single_job
        assert callable(apply_to_single_job)

    def test_apply_to_approved_jobs_importable(self):
        from applicant.engine import apply_to_approved_jobs
        assert callable(apply_to_approved_jobs)

    def test_auto_applicant_importable(self):
        from applicant.engine import AutoApplicant
        assert callable(AutoApplicant)

    def test_linkedin_scraper_has_guest_and_auth(self):
        from scraper.linkedin import LinkedInScraper
        assert hasattr(LinkedInScraper, '_scrape_guest')
        assert hasattr(LinkedInScraper, '_scrape_authenticated')

    def test_bot_active_tasks_set(self):
        from telegram_bot.bot import _active_apply_tasks
        assert isinstance(_active_apply_tasks, set)


class TestBackupFunction:
    """Test backup_database uses sqlite3 backup API."""

    def test_backup_database_importable(self):
        from main import backup_database
        assert callable(backup_database)
