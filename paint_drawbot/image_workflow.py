from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog

import numpy as np
from PIL import Image, ImageTk

from .image_indexing import _index_image_data
from .logging_config import _log


class ImageWorkflowMixin:
    def _select_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images",
                        "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff")]
        )
        if not path:
            return
        self._image_path = path
        _log.info("Image selected: %s", path)
        name = path.replace("\\", "/").split("/")[-1]
        self._img_lbl.config(text=f"  {name}", fg="#c8c8c8")
        self._set_status("Image loaded. Set BRUSH SIZE, then press START.")
        self._refresh_estimate()
        self._check_start_ready()


    def _preview_conversion(self) -> None:
        if not self._is_calibrated or not self._image_path:
            self._set_status("Select an image after calibration to preview.", self.warn)
            return
        step = self._step()
        cols, rows = self._grid_dims(step)
        active_indices = self._active_palette_indices()
        active_pal_rgb = [tuple(self.palette[idx]["rgb"]) for idx in active_indices]
        palette_rgb = [c["rgb"] for c in self.palette]
        dither_preset = self._dither_preset()
        planner_mode = self._planner()
        image_path = self._image_path
        self._preview_btn.config(state="disabled")
        threading.Thread(
            target=self._preview_conversion_thread,
            args=(
                image_path,
                cols,
                rows,
                active_indices,
                active_pal_rgb,
                palette_rgb,
                dither_preset,
                planner_mode,
            ),
            daemon=True,
        ).start()


    def _preview_conversion_thread(
            self,
            image_path: str,
            cols: int,
            rows: int,
            active_indices: list[int],
            active_pal_rgb: list[tuple],
            palette_rgb: list[tuple],
            dither_preset,
            planner_mode) -> None:
        try:
            img = Image.open(image_path).convert("RGB")
            img = img.resize((cols, rows), Image.Resampling.LANCZOS)
            result = _index_image_data(
                img, active_pal_rgb, dither_preset, planner_mode
            )
            pixel_indices = result.pixel_indices
            if active_indices != list(range(len(palette_rgb))):
                index_map = bytes(
                    active_indices[i] if i < len(active_indices) else 0
                    for i in range(256)
                )
                pixel_indices = pixel_indices.translate(index_map)
            arr = np.frombuffer(pixel_indices, dtype=np.uint8).reshape((rows, cols))
            pal_arr = np.asarray(palette_rgb, dtype=np.uint8)
            preview = Image.fromarray(pal_arr[arr], "RGB")
            preview.thumbnail((460, 360), Image.Resampling.NEAREST)
            used = len(set(pixel_indices))
            approx_runs = sum(
                1 for gy in range(rows)
                for gx in range(cols)
                if gx == 0 or arr[gy, gx] != arr[gy, gx - 1]
            )

            def _show() -> None:
                top = tk.Toplevel(self.root)
                top.title("DrawBot Preview")
                top.configure(bg=self.bg)
                photo = ImageTk.PhotoImage(preview)
                self._preview_img_ref = photo
                tk.Label(top, image=photo, bg=self.bg).pack(padx=12, pady=(12, 6))
                stats = (
                    f"{cols}x{rows} grid | {used} colours | "
                    f"~{approx_runs:,} row runs | {dither_preset.value} | "
                    f"{result.stats.get('seconds', 0.0):.2f}s"
                )
                tk.Label(
                    top, text=stats, font=("Consolas", 9),
                    bg=self.bg, fg=self.acc,
                ).pack(padx=12, pady=(0, 12))
                self._preview_btn.config(state="normal")

            self.root.after(0, _show)
        except Exception as exc:
            _log.exception("Preview failed: %s", exc)
            self._set_status(f"Preview failed: {exc}", self.err)
            self.root.after(0, lambda: self._preview_btn.config(state="normal"))
