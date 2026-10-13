from __future__ import annotations

from .dpi import set_dpi_awareness

set_dpi_awareness()

import tkinter as tk

from .app import DrawBotApp
from .logging_config import _log


def main() -> None:
    _log.info("Mouse corner failsafe enabled")

    root = tk.Tk()
    root.geometry("520x785")
    DrawBotApp(root)
    root.mainloop()
    _log.info("GUI closed; exiting")


if __name__ == "__main__":
    main()
