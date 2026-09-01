"""Tests for config/settings.py and module imports."""


class TestSettings:
    def test_no_credential_settings_remain(self):
        # HUNTER runs fully locally: no bot tokens, chat ids, or API keys.
        import config.settings as settings
        for gone in (
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY",
            "RESUME_TEXT", "COMMON_ANSWERS", "LINKEDIN_SESSION_COOKIE",
        ):
            assert not hasattr(settings, gone), f"{gone} should be gone"

    def test_role_target_is_senior_product_roles(self):
        from config.settings import ROLE_MATCH_KEYWORDS
        assert "head of product" in ROLE_MATCH_KEYWORDS
        assert "senior product manager" in ROLE_MATCH_KEYWORDS


class TestModuleImports:
    """Verify key functions/classes are importable from their modules."""

    def test_scrapers_importable(self):
        from scraper import RemoteOKScraper, WellfoundScraper
        assert callable(RemoteOKScraper)
        assert callable(WellfoundScraper)

    def test_filter_entrypoint_importable(self):
        from scraper.filters import evaluate_job
        assert callable(evaluate_job)

    def test_cli_commands_importable(self):
        import main
        for cmd in ("hunt", "list_jobs", "stats", "backup_database"):
            assert callable(getattr(main, cmd))


class TestBackupFunction:
    def test_backup_database_importable(self):
        from main import backup_database
        assert callable(backup_database)
