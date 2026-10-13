# Paint DrawBot

Convert an image into a small Paint palette, plan drawing strokes, and reproduce the result through Windows mouse input in Microsoft Paint.

**Status:** Version 5.0.0 source with a modular package, tests and two earlier script snapshots.

## What is here

- `paint_drawbot/`: palette conversion, stroke planning, UI and execution.
- `paint_paint.py`: launcher.
- `tests/`: existing source checks.
- `legacy/`: pre-modularization and pre-audit scripts.

## Getting started

Requires Windows, Microsoft Paint and Python 3.11+.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python paint_paint.py
```

Follow [the original guide](README.original.md) and calibrate for your own screen before drawing. The original machine’s calibration and drawing logs are not included.

## Current boundaries

Live mouse/keyboard control was not used for publication checks. Older scripts are historical snapshots, not alternative recommended launchers. Keep control of the desktop while running automation.

## Preservation and checks

This repository preserves a development snapshot, including incomplete work. See [PUBLICATION.md](PUBLICATION.md) for what was retained or excluded and [VALIDATION.md](VALIDATION.md) for the checks actually run. Original copyright and license notices take precedence over the repository’s display name.
