from __future__ import annotations

import time

import numpy as np
from PIL import Image

from .models import DitherPreset, IndexedImageResult, PlannerMode

def _colour_dist_sq(r1: int, g1: int, b1: int,
                    r2: int, g2: int, b2: int) -> float:
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return 0.299 * dr * dr + 0.587 * dg * dg + 0.114 * db * db


def _closest_colour(palette: list[dict], pal_rgb: list[tuple],
                    r: int, g: int, b: int) -> dict:
    return palette[_closest_colour_idx(pal_rgb, r, g, b)]


def _closest_colour_idx(pal_rgb: list[tuple], r: int, g: int, b: int) -> int:
    best_idx, best_d = 0, 1e18
    for i, (pr, pg, pb) in enumerate(pal_rgb):
        d = _colour_dist_sq(r, g, b, pr, pg, pb)
        if d < best_d:
            best_d, best_idx = d, i
            if d == 0:
                break
    return best_idx


# ─────────────────────────────────────────────────────────────────────────────
def _palette_index_data(img: Image.Image, pal_rgb: list[tuple],
                        dither: bool = False) -> bytes:
    pal_img = Image.new("P", (1, 1))
    flat_palette: list[int] = []
    for r, g, b in pal_rgb:
        flat_palette.extend([int(r), int(g), int(b)])
    flat_palette.extend([0, 0, 0] * (256 - len(pal_rgb)))
    pal_img.putpalette(flat_palette)
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    indexed = img.quantize(palette=pal_img, dither=dither_mode)
    return indexed.tobytes()


_NP_RGB_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_BAYER4 = (np.array(
    [[0, 8, 2, 10],
     [12, 4, 14, 6],
     [3, 11, 1, 9],
     [15, 7, 13, 5]],
    dtype=np.float32,
) + 0.5) / 16.0 - 0.5
_DITHER_COMPACT_ABS_ERR = 128.0
_DITHER_BRIDGE_ABS_ERR = 192.0


def _nearest_palette_indices_np(
        rgb_arr: np.ndarray,
        pal_arr: np.ndarray) -> np.ndarray:
    flat = rgb_arr.reshape(-1, 3)
    out = np.empty(flat.shape[0], dtype=np.uint8)
    weights = _NP_RGB_WEIGHTS
    chunk_size = 65536
    # PERF: Chunked NumPy distance keeps the fast dither path vectorized without
    # building one huge pixels x palette temporary for large canvases.
    for start in range(0, flat.shape[0], chunk_size):
        end = min(flat.shape[0], start + chunk_size)
        diff = flat[start:end, None, :] - pal_arr[None, :, :]
        dist = (diff * diff * weights).sum(axis=2)
        out[start:end] = np.argmin(dist, axis=1).astype(np.uint8)
    return out.reshape(rgb_arr.shape[:2])


def _weighted_rgb_mse_np(
        rgb_arr: np.ndarray,
        pixel_indices: bytes | np.ndarray,
        pal_arr: np.ndarray) -> float:
    if isinstance(pixel_indices, bytes):
        idx = np.frombuffer(pixel_indices, dtype=np.uint8).reshape(rgb_arr.shape[:2])
    else:
        idx = pixel_indices
    mapped = pal_arr[idx.astype(np.intp, copy=False)]
    diff = rgb_arr - mapped
    return float((diff * diff * _NP_RGB_WEIGHTS).sum(axis=2).mean())


def _weighted_rgb_error_np(
        rgb_arr: np.ndarray,
        indices: np.ndarray,
        pal_arr: np.ndarray) -> np.ndarray:
    mapped = pal_arr[indices.astype(np.intp, copy=False)]
    diff = rgb_arr - mapped
    return (diff * diff * _NP_RGB_WEIGHTS).sum(axis=2)


def _transition_count_np(indices: np.ndarray) -> int:
    if indices.size == 0:
        return 0
    h_trans = int(np.count_nonzero(indices[:, 1:] != indices[:, :-1]))
    v_trans = int(np.count_nonzero(indices[1:, :] != indices[:-1, :]))
    return h_trans + v_trans


def _row_run_count_np(indices: np.ndarray) -> int:
    if indices.size == 0:
        return 0
    return int(indices.shape[0] + np.count_nonzero(indices[:, 1:] != indices[:, :-1]))


def _ordered_dither_indices_np(
        rgb_arr: np.ndarray,
        pal_arr: np.ndarray) -> np.ndarray:
    h, w = rgb_arr.shape[:2]
    tile = np.tile(_BAYER4, ((h + 3) // 4, (w + 3) // 4))[:h, :w]
    yy, xx = np.indices((h, w), dtype=np.uint32)
    # PERF: Deterministic hash noise breaks Bayer blocks while staying vectorized.
    blueish = (((xx * 1103515245) ^ (yy * 12345)) & 255).astype(np.float32)
    noise = ((tile * 0.75) + ((blueish / 255.0) - 0.5) * 0.25) * 48.0
    dithered = np.clip(rgb_arr + noise[:, :, None], 0.0, 255.0)
    return _nearest_palette_indices_np(dithered, pal_arr)


def _compact_dither_indices_np(
        rgb_arr: np.ndarray,
        indices: np.ndarray,
        pal_arr: np.ndarray) -> tuple[np.ndarray, int]:
    h, w = indices.shape
    if h < 3 or w < 3:
        return indices, 0

    compacted = indices
    total_changed = 0
    for pass_no in range(2):
        next_indices, changed = _compact_dither_pass_np(
            rgb_arr, compacted, pal_arr, pass_no
        )
        total_changed += changed
        compacted = next_indices
        if changed == 0:
            break

    compacted, bridged = _bridge_short_dither_runs_np(
        rgb_arr, compacted, pal_arr
    )
    return compacted, total_changed + bridged


def _compact_dither_pass_np(
        rgb_arr: np.ndarray,
        indices: np.ndarray,
        pal_arr: np.ndarray,
        pass_no: int) -> tuple[np.ndarray, int]:
    h, w = indices.shape
    pal_count = pal_arr.shape[0]
    counts = np.zeros((pal_count, h, w), dtype=np.uint8)
    padded = np.pad(indices, 1, mode="edge")
    weighted_offsets = (
        (-1, -1, 1), (-1, 0, 1), (-1, 1, 1),
        (0, -1, 2),              (0, 1, 2),
        (1, -1, 1),  (1, 0, 1),  (1, 1, 1),
    )
    for dy, dx, weight in weighted_offsets:
        neighbour = padded[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        for colour_idx in range(pal_count):
            counts[colour_idx] += (
                (neighbour == colour_idx).astype(np.uint8, copy=False) * weight
            )

    candidate = np.argmax(counts, axis=0).astype(np.uint8)
    best_count = np.max(counts, axis=0)
    current_count = np.take_along_axis(
        counts,
        indices.astype(np.intp, copy=False)[None, :, :],
        axis=0,
    )[0]

    cur_err = _weighted_rgb_error_np(rgb_arr, indices, pal_arr)
    cand_err = _weighted_rgb_error_np(rgb_arr, candidate, pal_arr)

    left = np.pad(indices[:, :-1], ((0, 0), (1, 0)), mode="edge")
    right = np.pad(indices[:, 1:], ((0, 0), (0, 1)), mode="edge")
    up = np.pad(indices[:-1, :], ((1, 0), (0, 0)), mode="edge")
    down = np.pad(indices[1:, :], ((0, 1), (0, 0)), mode="edge")
    cur_edges = (
        (indices != left).astype(np.int8)
        + (indices != right).astype(np.int8)
        + (indices != up).astype(np.int8)
        + (indices != down).astype(np.int8)
    )
    cand_edges = (
        (candidate != left).astype(np.int8)
        + (candidate != right).astype(np.int8)
        + (candidate != up).astype(np.int8)
        + (candidate != down).astype(np.int8)
    )
    saved_edges = cur_edges - cand_edges

    isolated = (
        (best_count >= 5)
        & (current_count <= 2)
        & (cand_err <= cur_err * 1.20 + _DITHER_COMPACT_ABS_ERR)
    )
    draw_saving = (
        (saved_edges >= 2 + pass_no)
        & (best_count >= 4)
        & (cand_err <= cur_err * 1.10 + _DITHER_COMPACT_ABS_ERR)
    )
    change = (candidate != indices) & (isolated | draw_saving)
    changed = int(np.count_nonzero(change))
    if not changed:
        return indices, 0

    compacted = indices.copy()
    compacted[change] = candidate[change]
    return compacted, changed


def _bridge_short_dither_runs_np(
        rgb_arr: np.ndarray,
        indices: np.ndarray,
        pal_arr: np.ndarray) -> tuple[np.ndarray, int]:
    out = indices.copy()
    changed = 0
    h, w = out.shape
    for gy in range(h):
        row = out[gy]
        gx = 0
        while gx < w:
            colour_idx = int(row[gx])
            end = gx + 1
            while end < w and int(row[end]) == colour_idx:
                end += 1

            run_len = end - gx
            if 0 < gx and end < w and run_len <= 2:
                left_idx = int(row[gx - 1])
                right_idx = int(row[end])
                if left_idx == right_idx and left_idx != colour_idx:
                    cur = np.full((run_len,), colour_idx, dtype=np.uint8)
                    cand = np.full((run_len,), left_idx, dtype=np.uint8)
                    rgb = rgb_arr[gy, gx:end]
                    cur_err = _weighted_rgb_error_np(
                        rgb.reshape(1, run_len, 3),
                        cur.reshape(1, run_len),
                        pal_arr,
                    )[0]
                    cand_err = _weighted_rgb_error_np(
                        rgb.reshape(1, run_len, 3),
                        cand.reshape(1, run_len),
                        pal_arr,
                    )[0]
                    if (
                            float(cand_err.mean())
                            <= float(cur_err.mean()) * 1.22 + _DITHER_BRIDGE_ABS_ERR
                            and np.all(
                                cand_err <= cur_err * 1.45 + _DITHER_BRIDGE_ABS_ERR
                            )):
                        row[gx:end] = left_idx
                        changed += run_len

            gx = end

    if changed == 0:
        return indices, 0
    return out, changed


def _fast_palette_index_data(
        img: Image.Image,
        pal_rgb: list[tuple],
        exact_pixel_indices: bytes) -> tuple[bytes, bool, dict[str, float | int | bool]]:
    if len(pal_rgb) > 255:
        return exact_pixel_indices, False, {"fallback": True}

    rgb_arr = np.asarray(img, dtype=np.float32)
    pal_arr = np.asarray(pal_rgb, dtype=np.float32)
    ordered = _ordered_dither_indices_np(rgb_arr, pal_arr)
    compacted, compacted_pixels = _compact_dither_indices_np(
        rgb_arr, ordered, pal_arr
    )
    exact_mse = _weighted_rgb_mse_np(rgb_arr, exact_pixel_indices, pal_arr)
    fast_mse = _weighted_rgb_mse_np(rgb_arr, compacted, pal_arr)
    allowed_mse = exact_mse * 1.10 + (24.0 if exact_mse > 1.0 else 1e-6)
    exact_arr = np.frombuffer(exact_pixel_indices, dtype=np.uint8).reshape(
        rgb_arr.shape[:2]
    )
    ordered_runs = _row_run_count_np(ordered)
    compacted_runs = _row_run_count_np(compacted)
    exact_runs = _row_run_count_np(exact_arr)
    ordered_transitions = _transition_count_np(ordered)
    compacted_transitions = _transition_count_np(compacted)
    exact_transitions = _transition_count_np(exact_arr)
    fallback = fast_mse > allowed_mse
    stats: dict[str, float | int | bool] = {
        "exact_mse": exact_mse,
        "fast_mse": fast_mse,
        "allowed_mse": allowed_mse,
        "compacted_pixels": compacted_pixels,
        "exact_runs": exact_runs,
        "ordered_runs": ordered_runs,
        "compacted_runs": compacted_runs,
        "exact_transitions": exact_transitions,
        "ordered_transitions": ordered_transitions,
        "compacted_transitions": compacted_transitions,
        "fallback": fallback,
    }
    if fallback:
        return exact_pixel_indices, False, stats
    return compacted.astype(np.uint8, copy=False).tobytes(), True, stats


def _atkinson_dither_indices_np(
        img: Image.Image,
        pal_rgb: list[tuple]) -> bytes:
    rgb_arr = np.asarray(img, dtype=np.float32).copy()
    pal_arr = np.asarray(pal_rgb, dtype=np.float32)
    h, w = rgb_arr.shape[:2]
    out = np.empty((h, w), dtype=np.uint8)
    weights = _NP_RGB_WEIGHTS
    offsets = (
        (1, 0), (2, 0),
        (-1, 1), (0, 1), (1, 1),
        (0, 2),
    )

    for y in range(h):
        row = rgb_arr[y]
        for x in range(w):
            old = row[x].copy()
            diff = pal_arr - old[None, :]
            dist = (diff * diff * weights).sum(axis=1)
            idx = int(np.argmin(dist))
            out[y, x] = idx
            new = pal_arr[idx]
            err = (old - new) * 0.125
            for dx, dy in offsets:
                nx = x + dx
                ny = y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    rgb_arr[ny, nx] = np.clip(
                        rgb_arr[ny, nx] + err, 0.0, 255.0
                    )

    return out.tobytes()


def _index_image_data(
        img: Image.Image,
        pal_rgb: list[tuple],
        dither: DitherPreset,
        planner: PlannerMode = PlannerMode.AUTO) -> IndexedImageResult:
    t0 = time.monotonic()
    if dither == DitherPreset.FLAT:
        data = _palette_index_data(img, pal_rgb, False)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    if dither == DitherPreset.PHOTO:
        data = _palette_index_data(img, pal_rgb, True)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    if dither == DitherPreset.CLEAN:
        data = _atkinson_dither_indices_np(img, pal_rgb)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    exact = _palette_index_data(img, pal_rgb, True)
    if planner == PlannerMode.EXACT:
        return IndexedImageResult(
            exact,
            dither,
            False,
            {"mode": "Photo exact guard", "seconds": time.monotonic() - t0},
        )

    data, fast_used, stats = _fast_palette_index_data(img, pal_rgb, exact)
    stats["mode"] = dither.value
    stats["seconds"] = time.monotonic() - t0
    return IndexedImageResult(data, dither, fast_used, stats)
