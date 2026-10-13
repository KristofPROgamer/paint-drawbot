"""Paint DrawBot package."""

from __future__ import annotations

__all__ = ["DrawBotApp"]


def __getattr__(name: str):
    if name == "DrawBotApp":
        from .app import DrawBotApp

        return DrawBotApp
    raise AttributeError(name)
