"""Tests for the serial apply queue, screenshot pruning, and form_filled handling."""
import asyncio
import time


class FakeQuery:
    """Minimal stand-in for a Telegram CallbackQuery."""
    def __init__(self):
        self.texts = []

    async def edit_message_text(self, text, **kwargs):
        self.texts.append(text)


class TestApplyQueueSerialization:
    """The apply worker must process jobs one at a time (never concurrently)."""

    async def test_jobs_processed_serially_in_order(self, monkeypatch):
        import telegram_bot.bot as bot

        order = []
        concurrent = 0
        max_concurrent = 0

        async def fake_auto_apply(query, job_id, job):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.01)  # simulate work
            order.append(job_id)
            concurrent -= 1

        monkeypatch.setattr(bot, "_auto_apply", fake_auto_apply)
        # Reset worker so it picks up the patched _auto_apply.
        bot._apply_worker_task = None

        q = FakeQuery()
        for jid in (1, 2, 3):
            bot._ensure_apply_worker()
            await bot._apply_queue.put((q, jid, {"id": jid, "title": "t", "company": "c"}))

        await asyncio.wait_for(bot._apply_queue.join(), timeout=5)

        assert order == [1, 2, 3]
        assert max_concurrent == 1  # never more than one browser at a time

        await bot.shutdown_apply_worker(timeout=2)


class TestFormFilledHandling:
    """form_filled is unconfirmed → must never report 'applied'; it should
    surface as an honest manual-apply prompt with the job URL."""

    async def test_form_filled_prompts_review_not_applied(self, monkeypatch):
        import telegram_bot.bot as bot
        from applicant.engine import ApplyResult

        async def fake_apply(job, headless=True):
            return ApplyResult(success=False, method="form_filled", message="unconfirmed")

        monkeypatch.setattr(bot, "apply_to_single_job", fake_apply)
        monkeypatch.setattr(bot.os.path, "exists", lambda p: False)

        q = FakeQuery()
        job = {"id": 1, "title": "PM", "company": "Acme", "url": "https://example.com/job"}
        await bot._auto_apply(q, 1, job)

        # Critical invariant: an unconfirmed fill is never a success.
        assert not any("CONFIRMED" in t for t in q.texts)
        # Honest UX: a manual-apply prompt with the URL (the link is a blank form).
        assert any("APPLY MANUALLY" in t and job["url"] in t for t in q.texts)


class TestPruneScreenshots:
    def test_prunes_old_keeps_recent(self, tmp_path, monkeypatch):
        import applicant.engine as engine
        import main

        monkeypatch.setattr(engine, "SCREENSHOTS_DIR", tmp_path)

        old = tmp_path / "linkedin_1.png"
        new = tmp_path / "linkedin_2.png"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        # Age the old file to 40 days.
        forty_days_ago = time.time() - (40 * 86400)
        import os
        os.utime(old, (forty_days_ago, forty_days_ago))

        main.prune_screenshots(max_age_days=30)

        assert not old.exists()
        assert new.exists()

    def test_no_dir_is_noop(self, tmp_path, monkeypatch):
        import applicant.engine as engine
        import main
        monkeypatch.setattr(engine, "SCREENSHOTS_DIR", tmp_path / "does_not_exist")
        main.prune_screenshots()  # should not raise
