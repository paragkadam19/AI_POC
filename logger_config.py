"""
logger_config.py — Centralized logging setup for the entire app
==================================================================
One logger, shared everywhere. Logs to both:
  - console (same as your current print() output, but with timestamps/levels)
  - a rotating file on disk (storage/logs/app.log), so history survives
    past whatever scrolled off your terminal

Usage in any module:
    from logger_config import get_logger
    logger = get_logger(__name__)
    logger.info("message")
    logger.warning("message")
    logger.error("message")
"""
import logging
import os
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, "storage", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level=logging.INFO):
    """Call once, at app startup."""
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console — same place print() used to go
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # Rotating file — keeps last 5 files of 5MB each, so logs don't grow forever
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Quiet down noisy third-party libraries so they don't drown out our logs
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)