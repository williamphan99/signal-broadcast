"""Shared Mac colours for readable light and dark interfaces."""
import subprocess


def _detect_dark() -> bool:
    try:
        r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                           capture_output=True, text=True)
        return "Dark" in r.stdout
    except Exception:
        return False


DARK = _detect_dark()
# tk.Text widgets don't follow the macOS theme, so set both colours explicitly.
PALETTE = {
    "text_bg": "#1e1f22" if DARK else "#ffffff",
    "text_fg": "#e8e8e8" if DARK else "#1a1a1a",
    "muted": "#9aa0a6",
    "error": "#ff6b6b" if DARK else "#c0392b",
    "ok": "#4ec973" if DARK else "#1a7f37",
    "accent": "#2c6bed",                       # the one primary button (Send)
    "accent_hi": "#1f57c9",                     # hover
    "accent_fg": "#ffffff",
    "disabled": "#3a3b3e" if DARK else "#d7d9dd",
    "tile_bg": "#2b2d31" if DARK else "#eceef2",     # photo tiles, on the strip's well
    "tile_line": "#3c3e43" if DARK else "#d7d9dd",
    "badge_bg": "#2b2d31",                            # the ✕ corner — dark in both themes
}
