"""Stable per-user paths and diagnostics for source and frozen builds."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

APP_NAME = "SnipBoard"
_previous_hook = _installed_hook = None


def app_data_root(environ: dict[str, str] | None = None, home: Path | None = None) -> Path:
    values = os.environ if environ is None else environ
    local = values.get("LOCALAPPDATA")
    if local:
        return Path(local).expanduser().resolve() / APP_NAME
    base = Path.home() if home is None else home
    return (base / "AppData" / "Local" / APP_NAME).resolve()


def default_library_dir() -> Path:
    return app_data_root() / "library"


def thumbnail_dir(library_root: Path) -> Path:
    return library_root.resolve() / "thumbnails"


def configure_logging(root: Path | None = None) -> Path:
    global _previous_hook, _installed_hook
    directory = (root or app_data_root()) / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "snipboard.log"
    logger = logging.getLogger()
    if not any(getattr(handler, "_snipboard", False) for handler in logger.handlers):
        handler = RotatingFileHandler(
            destination, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler._snipboard = True
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        previous_hook = sys.excepthook
        _previous_hook = previous_hook

        def log_uncaught(kind, value, traceback):
            logging.getLogger(APP_NAME).critical(
                "uncaught exception", exc_info=(kind, value, traceback)
            )
            previous_hook(kind, value, traceback)

        sys.excepthook = log_uncaught
        _installed_hook = log_uncaught
    return Path(next(h.baseFilename for h in logger.handlers if getattr(h, '_snipboard', False)))


def close_logging() -> None:
    global _previous_hook, _installed_hook
    logger = logging.getLogger()
    for handler in list(logger.handlers):
        if getattr(handler, "_snipboard", False):
            logger.removeHandler(handler)
            handler.close()
    if sys.excepthook is _installed_hook and _previous_hook:
        sys.excepthook = _previous_hook
    _previous_hook = _installed_hook = None
