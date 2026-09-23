"""One logging setup for the web app, scripts, evals and spikes.

Every log line goes through RedactFilter, so an email address, phone number or key-shaped string that ends
up in a message (an exception text, an API error) is masked before it reaches the console or Render's logs.
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


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for pattern, replacement in _PATTERNS:
            message = pattern.sub(replacement, message)
        record.msg, record.args = message, None
        return True


def configure_logging(*, cli: bool = False, level: str | int | None = None) -> None:
    """Call once at start-up. `cli=True` for command-line tools (plain messages on stdout)."""
    level = level or os.environ.get("LOG_LEVEL", "INFO")
    handler = logging.StreamHandler(sys.stdout if cli else sys.stderr)
    handler.setFormatter(logging.Formatter(CLI_FORMAT if cli else APP_FORMAT))
    handler.addFilter(RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "apify_client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
