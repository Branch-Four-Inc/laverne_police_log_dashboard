"""Shared file logging for the LVPD data pipeline."""

import logging
import sys
from pathlib import Path


LOG_PATH = Path(__file__).resolve().parent / "output" / "scraper.log"
LOGGER_NAME = "lvpd_pipeline"


def get_logger() -> logging.Logger:
    """Create the shared timestamped file logger once per process."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    logger.addHandler(handler)
    return logger


def install_exception_logger(logger: logging.Logger) -> None:
    """Record unexpected crashes in the file log while retaining the terminal traceback."""
    original_hook = sys.excepthook

    def log_exception(exc_type, exc_value, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            original_hook(exc_type, exc_value, traceback)
            return
        logger.critical("Unhandled exception", exc_info=(exc_type, exc_value, traceback))
        original_hook(exc_type, exc_value, traceback)

    sys.excepthook = log_exception
