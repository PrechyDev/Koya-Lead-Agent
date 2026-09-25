"""One logging setup for the web app and the scripts.

Every log line is masked by RedactingFormatter AFTER it is fully formatted, so an email address or key-shaped
string in a message, an exception traceback or uvicorn's own access/error lines is masked before it reaches the
console or Render's logs. (A filter would only see the message, not the traceback added later.)
Stdlib only, so scripts/secret_scan.py (run by the pre-commit hook) can use it too.
"""

import logging
import os
import re
import sys

# Kept here (not imported from app.lib.sanitize) so this module has no dependencies.
_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[email redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{16,}|apify_api_[A-Za-z0-9]{16,}|fc-[A-Za-z0-9]{16,}"
                r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-.]+)"), "[token redacted]"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[password redacted]@"),
]

APP_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
CLI_FORMAT = "%(message)s"  # scripts print tables and reports; timestamps would only add noise


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Formats the whole line (message + traceback), then masks it."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(*, cli: bool = False, level: str | int | None = None) -> None:
    """Call once at start-up. `cli=True` for command-line tools (plain messages on stdout)."""
    wanted = str(level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    level = wanted if wanted in logging.getLevelNamesMapping() else "INFO"  # a typo can't stop the app starting
    handler = logging.StreamHandler(sys.stdout if cli else sys.stderr)
    handler.setFormatter(RedactingFormatter(CLI_FORMAT if cli else APP_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn installs its own handlers (they bypass the root): route them through ours so they're masked too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    for noisy in ("httpx", "httpcore", "apify_client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
