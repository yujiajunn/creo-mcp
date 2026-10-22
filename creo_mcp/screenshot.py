"""Capture the visible Creo Parametric window for visual verification."""

from __future__ import annotations

import os
import time
from pathlib import Path


def screenshots_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "CreoMCP" / "screenshots"


def _creo_windows() -> list[tuple[int, str]]:
    """Return visible Creo Parametric top-level windows, largest first."""
    import win32gui

    found: list[tuple[int, int, str]] = []

    def callback(hwnd: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if "Creo Parametric" not in title:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = max(0, right - left)
        height = max(0, bottom - top)
        if width and height:
            found.append((width * height, hwnd, title))

    win32gui.EnumWindows(callback, None)
    found.sort(reverse=True)
    return [(hwnd, title) for _area, hwnd, title in found]


def capture_creo_window(output_path: str | None = None) -> tuple[Path, str, int, int]:
    """Save a PNG screenshot of the main Creo window and return its metadata.

    Uses PrintWindow so the Creo window is captured directly without bringing it
    to the foreground or switching the desktop.
    """
    import ctypes

    import win32con
    import win32gui
    import win32ui
    from PIL import Image

    windows = _creo_windows()
    if not windows:
        raise RuntimeError(
            "No visible Creo Parametric window found. Open Creo before capturing."
        )
    hwnd, title = windows[0]
    if win32gui.IsIconic(hwnd):
        # Restore without activating: PrintWindow cannot capture a minimized window.
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        time.sleep(0.3)
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(0, right - left)
    height = max(0, bottom - top)
    if not width or not height:
        raise RuntimeError("The Creo window has no drawable area.")

    window_dc = win32gui.GetWindowDC(hwnd)
    source_dc = win32ui.CreateDCFromHandle(window_dc)
    memory_dc = source_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)
        rendered = ctypes.windll.user32.PrintWindow(
            hwnd, memory_dc.GetSafeHdc(), 2
        )
        if not rendered:
            raise RuntimeError("Creo did not render its window for capture.")
        bits = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB", (width, height), bits, "raw", "BGRX", 0, 1
        )
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory_dc.DeleteDC()
        source_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, window_dc)

    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            raise ValueError("output_path must be an absolute path")
    else:
        path = screenshots_dir() / f"creo_{time.strftime('%Y%m%d-%H%M%S')}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")
    return path, title, image.width, image.height
