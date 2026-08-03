"""One place that configures logging for berthcast.

Before this, errors were swallowed all over the codebase with `except Exception:
pass` — most dangerously around email/alert sending, so a critical-stock alert
could fail to reach a client and nobody would ever know. Now those spots log
through this logger.

Logs go to two places:
  - stdout — Render captures this in its dashboard log stream.
  - a rotating file on the persistent disk (so logs survive a deploy and can be
    read back). Rotated DAILY, keeping 90 days, so client IP addresses in the
    logs are deleted within ~90 days — matching the retention promise in the
    privacy policy (PDPA s25 Retention Limitation).

Import and use:  `from logging_setup import logger`  then `logger.warning(...)`,
`logger.exception(...)`, etc. Configuration is idempotent — importing from many
modules won't stack duplicate handlers.
"""
import logging
import os
from logging.handlers import TimedRotatingFileHandler

_LOGGER_NAME = "berthcast"


def _log_file_path():
    """A logs/ folder beside the DB, so it's on the persistent disk in production."""
    db_path = os.environ.get("DB_PATH", "berthcast.db")
    parent = os.path.dirname(db_path) or "."
    return os.path.join(parent, "logs", "berthcast.log")


def setup_logging():
    """Configure and return the berthcast logger. Safe to call repeatedly —
    if it's already configured, it just returns the existing logger."""
    log = logging.getLogger(_LOGGER_NAME)
    if log.handlers:                      # already configured
        return log

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level_name, logging.INFO))
    log.propagate = False                 # don't double-log via the root logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    stream = logging.StreamHandler()      # -> stdout (captured by Render)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    # Rotating file on the persistent disk. Best-effort: if the directory can't be
    # made (e.g. disk problem) we still have stdout, so logging never crashes boot.
    try:
        path = _log_file_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Time-based (daily, keep 90) so IP addresses age out within ~90 days to
        # honour the privacy policy's retention promise. ponytail: no per-file
        # size cap now — low traffic keeps the total tiny; if a noisy day ever
        # bloats the disk, add a size guard or lower backupCount.
        fileh = TimedRotatingFileHandler(path, when="D", interval=1, backupCount=90,
                                         encoding="utf-8")
        fileh.setFormatter(fmt)
        log.addHandler(fileh)
    except Exception:
        log.warning("Could not open log file; logging to stdout only", exc_info=True)

    return log


logger = setup_logging()
