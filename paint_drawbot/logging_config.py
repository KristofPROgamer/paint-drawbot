from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .paths import LOG_FILE

_LOG_FILE = str(LOG_FILE)
_DEBUG_LOG = os.environ.get("DRAWBOT_DEBUG", "").strip().lower() in {
    "1", "true", "yes", "on",
}

_log = logging.getLogger("drawbot")
_log.setLevel(logging.DEBUG if _DEBUG_LOG else logging.INFO)

if not _log.handlers:
    _fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    _fh = RotatingFileHandler(
        _LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _fh.setLevel(logging.DEBUG if _DEBUG_LOG else logging.INFO)
    _fh.setFormatter(_fmt)
    _log.addHandler(_fh)

    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.INFO)
    _ch.setFormatter(_fmt)
    _log.addHandler(_ch)

_log.info("=" * 70)
_log.info("Paint DrawBot v5.0 starting")
_log.info("Log file: %s", _LOG_FILE)
