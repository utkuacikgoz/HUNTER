"""Tests for config/log_redaction.py — secret scrubbing in logs."""
import logging

from config.log_redaction import RedactingFilter, install_redaction, redact

# A realistic-shaped (fake) Telegram token.
FAKE_TOKEN = "bot8213816196:AAH82f9HZf9HKFvJJLbYPKySLXM4M_I7YQk"


class TestRedact:
    def test_telegram_token_in_url(self):
        out = redact(f"POST https://api.telegram.org/{FAKE_TOKEN}/getUpdates")
        assert "AAH82f9" not in out
        assert "<REDACTED>" in out
        # numeric bot id is kept for debuggability
        assert "bot8213816196:" in out

    def test_anthropic_key(self):
        out = redact("key=sk-ant-api03-abc123DEF456ghi789JKL012mno345")
        assert "abc123DEF" not in out
        assert "sk-<REDACTED>" in out

    def test_li_at_cookie(self):
        out = redact('cookie li_at="AQEDAReallyLongSessionValue1234567890"')
        assert "ReallyLongSessionValue" not in out
        assert "<REDACTED>" in out

    def test_clean_text_untouched(self):
        msg = "Bot is running. Scheduler active. 50 jobs found."
        assert redact(msg) == msg


class TestRedactingFilter:
    def test_filter_scrubs_message(self):
        f = RedactingFilter()
        rec = logging.LogRecord(
            "x", logging.INFO, __file__, 1,
            f"calling {FAKE_TOKEN}/getUpdates", None, None,
        )
        assert f.filter(rec) is True
        assert "AAH82f9" not in rec.getMessage()

    def test_filter_scrubs_args(self):
        f = RedactingFilter()
        rec = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "url=%s", (f"{FAKE_TOKEN}/x",), None,
        )
        f.filter(rec)
        assert "AAH82f9" not in rec.getMessage()

    def test_install_attaches_filter_and_quiets_httpx(self):
        lg = logging.getLogger("test_install_redaction")
        lg.addHandler(logging.StreamHandler())
        install_redaction(lg)
        assert any(isinstance(flt, RedactingFilter)
                   for h in lg.handlers for flt in h.filters)
        assert logging.getLogger("httpx").level == logging.WARNING
