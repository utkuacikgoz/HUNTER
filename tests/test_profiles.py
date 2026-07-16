"""Tests for the profile system + cover-letter-only (no-auto-apply) behavior.

Covers: env-overridable SEARCH_QUERIES, the .env.<profile> overlay, the
ENABLE_AUTO_APPLY / ENABLE_BROWSER_SCRAPERS toggles, the Telegram cover-letter
review UX, and the de-Utku'd fallback cover letter.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _settings_value(expr: str, env: dict) -> str:
    """Import config.settings in a fresh subprocess (so import-time env is honored)
    and print `expr`. Returns stdout stripped."""
    code = f"import config.settings as s; print({expr})"
    out = subprocess.check_output([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env)
    return out.decode().strip()


class TestSearchQueriesEnvOverride:
    def test_default_is_pm(self):
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("SEARCH_QUERIES", "HUNTER_PROFILE")}
        val = _settings_value("s.SEARCH_QUERIES", env)
        assert "Senior Product Manager" in val

    def test_env_override_wins(self):
        import os
        env = {**os.environ, "SEARCH_QUERIES": "Performance Marketing Manager,Growth Lead"}
        env.pop("HUNTER_PROFILE", None)
        val = _settings_value("s.SEARCH_QUERIES", env)
        assert "Performance Marketing Manager" in val
        assert "Growth Lead" in val
        assert "Senior Product Manager" not in val


class TestProfileOverlay:
    def test_profile_env_file_overrides(self):
        import os

        # Drop a throwaway overlay next to .env; settings should load it when the
        # matching HUNTER_PROFILE is set. Cleaned up regardless of outcome.
        overlay = REPO_ROOT / ".env.pytestprofile"
        overlay.write_text("SEARCH_QUERIES=Overlay Marketing Role\nENABLE_AUTO_APPLY=false\n")
        try:
            env = {**os.environ, "HUNTER_PROFILE": "pytestprofile"}
            env.pop("SEARCH_QUERIES", None)
            env.pop("ENABLE_AUTO_APPLY", None)
            assert "Overlay Marketing Role" in _settings_value("s.SEARCH_QUERIES", env)
            assert _settings_value("s.ENABLE_AUTO_APPLY", env) == "False"
        finally:
            overlay.unlink(missing_ok=True)


class TestToggleDefaults:
    def test_auto_apply_default_true(self):
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("ENABLE_AUTO_APPLY", "HUNTER_PROFILE")}
        assert _settings_value("s.ENABLE_AUTO_APPLY", env) == "True"

    def test_auto_apply_parses_false(self):
        import os
        env = {**os.environ, "ENABLE_AUTO_APPLY": "false"}
        env.pop("HUNTER_PROFILE", None)
        assert _settings_value("s.ENABLE_AUTO_APPLY", env) == "False"

    def test_browser_scrapers_default_true(self):
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("ENABLE_BROWSER_SCRAPERS", "HUNTER_PROFILE")}
        assert _settings_value("s.ENABLE_BROWSER_SCRAPERS", env) == "True"

    def test_cover_letters_default_true(self):
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("ENABLE_COVER_LETTERS", "HUNTER_PROFILE")}
        assert _settings_value("s.ENABLE_COVER_LETTERS", env) == "True"

    def test_allow_onsite_freelance_default_false(self):
        # Off by default: the PM bot's remote-only feed must not widen silently.
        import os
        env = {k: v for k, v in os.environ.items() if k not in ("ALLOW_ONSITE_FREELANCE", "HUNTER_PROFILE")}
        assert _settings_value("s.ALLOW_ONSITE_FREELANCE", env) == "False"

    def test_allow_onsite_freelance_parses_true(self):
        import os
        env = {**os.environ, "ALLOW_ONSITE_FREELANCE": "true"}
        env.pop("HUNTER_PROFILE", None)
        assert _settings_value("s.ALLOW_ONSITE_FREELANCE", env) == "True"


class TestReviewKeyboardCoverLetterMode:
    def _texts(self, markup):
        return [btn.text for row in markup.inline_keyboard for btn in row]

    def _callbacks(self, markup):
        return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]

    def test_cover_letter_replaces_approve_when_auto_apply_off(self, monkeypatch):
        import telegram_bot.bot as bot
        monkeypatch.setattr(bot, "ENABLE_AUTO_APPLY", False)
        monkeypatch.setattr(bot, "ENABLE_COVER_LETTERS", True)
        # Even an auto-applyable platform gets Cover Letter (no Approve) in this mode.
        job = {"id": 7, "url": "https://x/y", "platform": "greenhouse"}
        markup = bot._review_keyboard(job)
        texts = self._texts(markup)
        assert "📄 Cover Letter" in texts
        assert "❌ Skip" in texts
        assert "🔗 View Job" in texts
        assert "✅ Approve" not in texts
        assert any(cb.startswith("coverletter_") for cb in self._callbacks(markup))

    def test_pure_feed_when_cover_letters_off(self, monkeypatch):
        import telegram_bot.bot as bot
        monkeypatch.setattr(bot, "ENABLE_AUTO_APPLY", False)
        monkeypatch.setattr(bot, "ENABLE_COVER_LETTERS", False)
        texts = self._texts(bot._review_keyboard({"id": 3, "url": "https://x/y", "platform": "greenhouse"}))
        assert texts == ["❌ Skip", "🔗 View Job"]
        assert "📄 Cover Letter" not in texts
        assert "✅ Approve" not in texts

    def test_approve_present_when_auto_apply_on(self, monkeypatch):
        import telegram_bot.bot as bot
        monkeypatch.setattr(bot, "ENABLE_AUTO_APPLY", True)
        texts = self._texts(bot._review_keyboard({"id": 1, "url": "https://x/y", "platform": "greenhouse"}))
        assert "✅ Approve" in texts
        assert "📄 Cover Letter" not in texts


class TestHandleCoverLetter:
    async def test_generates_stores_and_sends(self, monkeypatch):
        import telegram_bot.bot as bot

        captured = {}

        def fake_generate(title, company, desc):
            captured["gen"] = (title, company, desc)
            return "A tailored marketing cover letter."

        def fake_set(job_id, text):
            captured["stored"] = (job_id, text)

        sent = []

        class FakeBot:
            def __init__(self, token=None):
                pass

            async def send_message(self, chat_id, text, **kwargs):
                sent.append(text)

        class FakeQuery:
            def __init__(self):
                self.texts = []

            async def edit_message_text(self, text, **kwargs):
                self.texts.append(text)

        monkeypatch.setattr(bot, "ENABLE_COVER_LETTERS", True)
        monkeypatch.setattr(bot, "generate_cover_letter", fake_generate)
        monkeypatch.setattr(bot, "set_cover_letter", fake_set)
        monkeypatch.setattr(bot, "Bot", FakeBot)

        q = FakeQuery()
        job = {"id": 5, "title": "Growth Marketing Manager", "company": "Acme",
               "url": "https://x/y", "description": "Run paid social"}
        await bot._handle_coverletter(q, job)

        assert captured["gen"] == ("Growth Marketing Manager", "Acme", "Run paid social")
        assert captured["stored"][0] == 5
        assert captured["stored"][1] == "A tailored marketing cover letter."
        # The letter is delivered as a separate copy/paste message.
        assert any("A tailored marketing cover letter." in t for t in sent)
        # No application is submitted (no apply_to_single_job call path here).

    async def test_generation_failure_is_handled(self, monkeypatch):
        import telegram_bot.bot as bot

        def boom(*a, **k):
            raise RuntimeError("api down")

        class FakeQuery:
            def __init__(self):
                self.texts = []

            async def edit_message_text(self, text, **kwargs):
                self.texts.append(text)

        monkeypatch.setattr(bot, "generate_cover_letter", boom)
        q = FakeQuery()
        job = {"id": 6, "title": "PPC Manager", "company": "Beta", "url": "https://x/y", "description": ""}
        await bot._handle_coverletter(q, job)  # must not raise
        assert any("Couldn't generate" in t for t in q.texts)

    async def test_noop_when_cover_letters_disabled(self, monkeypatch):
        import telegram_bot.bot as bot

        def boom(*a, **k):
            raise AssertionError("generate_cover_letter must not be called when disabled")

        class FakeQuery:
            def __init__(self):
                self.texts = []

            async def edit_message_text(self, text, **kwargs):
                self.texts.append(text)

        monkeypatch.setattr(bot, "ENABLE_COVER_LETTERS", False)
        monkeypatch.setattr(bot, "generate_cover_letter", boom)
        q = FakeQuery()
        job = {"id": 8, "title": "x", "company": "y", "url": "https://x/y", "description": ""}
        await bot._handle_coverletter(q, job)  # must be a silent no-op
        assert q.texts == []

    def test_coverletter_registered_in_dispatch(self):
        import telegram_bot.bot as bot
        assert "coverletter" in bot._CALLBACK_HANDLERS


class TestBuildScrapers:
    def test_excludes_browser_scrapers_when_disabled(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "ENABLE_BROWSER_SCRAPERS", False)
        names = {type(s).__name__ for s in main._build_scrapers()}
        # Wellfound is the only browser scraper.
        assert "WellfoundScraper" not in names
        # API sources still present. RemoteOK is an ApiSource (aiohttp, no Chromium)
        # despite the name, so this toggle must not disable it — it did until the
        # ApiSource migration was finished, which silently killed REMOTEOK_TAGS for
        # every browser-free profile.
        assert "RemoteOKScraper" in names
        assert "GreenhouseSource" in names

    def test_includes_browser_scrapers_when_enabled(self, monkeypatch):
        import main
        monkeypatch.setattr(main, "ENABLE_BROWSER_SCRAPERS", True)
        names = {type(s).__name__ for s in main._build_scrapers()}
        assert "RemoteOKScraper" in names
        assert "WellfoundScraper" in names


class TestApplyRefusedWhenDisabled:
    async def test_apply_short_circuits(self, monkeypatch):
        import main

        monkeypatch.setattr(main, "ENABLE_AUTO_APPLY", False)
        # If it didn't short-circuit it would call get_approved_jobs; make that explode.
        monkeypatch.setattr(main, "get_approved_jobs", lambda: (_ for _ in ()).throw(AssertionError("called")))
        result = await main.apply()
        assert result == {"total": 0, "success": 0, "failed": 0, "needs_manual": 0}


class TestFallbackCoverLetterGeneric:
    def test_no_hardcoded_utku_bio(self):
        from prompts.generator import _fallback_cover_letter
        letter = _fallback_cover_letter("Growth Marketing Manager", "Acme")
        # Job details still present.
        assert "Growth Marketing Manager" in letter
        assert "Acme" in letter
        # None of Utku's old hardcoded specifics leak in for other profiles.
        for leak in ("8+ years", "BiLira", "Upshift", "Senior Product Manager", "11+ digital products"):
            assert leak not in letter
