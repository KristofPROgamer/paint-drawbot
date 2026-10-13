#!/usr/bin/env python3
"""Compatibility launcher for the modular Paint DrawBot app."""

from __future__ import annotations

from paint_drawbot.app import DrawBotApp
from paint_drawbot.main import main

__all__ = ["DrawBotApp", "main"]


if __name__ == "__main__":
    main()
