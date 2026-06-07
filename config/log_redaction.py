"""Defense-in-depth log redaction.

A single `logging.Filter` attached to the root handlers scrubs known secret
shapes from EVERY log record, no matter which library emitted it. This is the
backstop for cases like python-telegram-bot logging a request URL that embeds the
bot token (`.../bot<TOKEN>/getUpdates`) on a non-connection error, which silencing
the httpx logger alone would not catch.

Patterns are matched on shape (not on the configured secret values) so it keeps
working even if a token is rotated, and it never needs the secrets themselves.
"""
import logging
import re

# (compiled pattern, replacement). Order doesn't matter; all are applied.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Telegram bot token: digits ":" 35-char base64-ish secret. Keep the numeric
    # bot id (not secret, useful for debugging), redact the secret half.
    (re.compile(r"(bot\d{5,})(:)[A-Za-z0-9_-]{20,}"), r"\1\2<REDACTED>"),
    # Anthropic / OpenAI-style API keys.
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk-<REDACTED>"),
    # LinkedIn li_at session cookie (appears as li_at=... or "li_at": "...").
    (re.compile(r"(li_at[\"'\s:=]+)[A-Za-z0-9_%-]{20,}"), r"\1<REDACTED>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrubs secret-shaped substrings from each record's message and args.

    Returns True always (never drops records) — it only rewrites them. Applied as
    a handler filter so it runs after formatting-time arg substitution is decided
    but we redact both the raw message and any string args to be safe.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(v) if isinstance(v, str) else v
                               for k, v in record.args.items()}
            else:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def install_redaction(*loggers: logging.Logger, quiet_chatty: bool = True) -> None:
    """Attach RedactingFilter to every handler of the given loggers, and pin
    chatty third-party loggers that log request URLs to WARNING."""
    filt = RedactingFilter()
    for lg in loggers:
        for handler in lg.handlers:
            handler.addFilter(filt)
    if quiet_chatty:
        for name in ("httpx", "telegram", "telegram.ext", "telegram.request", "aiohttp"):
            logging.getLogger(name).setLevel(logging.WARNING)
