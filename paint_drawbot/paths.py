from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
LOG_FILE = APP_DIR / "paint_drawbot.log"
CAL_FILE = APP_DIR / "paint_drawbot_cal.json"
CAL_SCHEMA_VERSION = 3
