# Paint DrawBot

Paint DrawBot is a Windows desktop automation app that converts an image into a
limited MS Paint palette, plans efficient strokes, and drives Paint with Win32
mouse input.

## Requirements

- Windows with Microsoft Paint
- Python 3.11 or newer
- A normal Tkinter-enabled Python install

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python paint_paint.py
```

Or, after an editable install:

```powershell
python -m pip install -e .
paint-drawbot
```

## Workflow

1. Open MS Paint with a blank canvas.
2. Run Paint DrawBot.
3. Calibrate the pencil, canvas, and palette positions.
4. Select an image, preview the palette conversion, tune detail/brush settings,
   then start drawing.

Once an image is selected, the estimate line runs an image-aware plan in the
background and counts the actual planned actions, colour changes, fills, and
auto-brush strokes for the current settings. While drawing with the main window
hidden, a small click-through HUD shows exact progress, ETA, action rate, and
current phase.

The app writes local runtime files such as `paint_drawbot.log` and
`paint_drawbot_cal.json`. They are intentionally ignored by git because they are
machine-specific.

## Safety

Use `CTRL+ESC` to stop and `ESC` to pause/resume. Moving the mouse to any screen
corner aborts the direct Win32 mouse input path.

## Checks

```powershell
python -m unittest discover -s tests
python -m compileall -q paint_drawbot paint_paint.py tests
```
