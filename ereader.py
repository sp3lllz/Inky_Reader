#!/usr/bin/env python3
"""
Inky Impression 4" E-Reader
============================

A full-featured e-reader for the Pimoroni Inky Impression 4" seven-colour
e-ink display (600×400) attached to a Raspberry Pi.

Usage
-----
    python3 ereader.py [books_directory] [--simulate]

    books_directory  Path to a folder of .txt files (default: ~/books/)
    --simulate       Render screens to numbered PNGs instead of driving
                     the display; useful for layout testing on a desktop.

Button controls (active-low, BCM GPIO)
--------------------------------------
    A (GPIO 5)   Scroll / page up      ◀
    D (GPIO 24)  Scroll / page down    ▶
    B (GPIO 6)   Confirm / open menu
    C (GPIO 16)  Context action (refresh library / back to reading)

Screens
-------
    Library Browser → Main Menu → Reading View
    Main Menu also offers Sleep (suspend) and Change Book.

Deep-sleep GPIO wake
--------------------
    To allow a GPIO button to wake the Pi from suspend, add a line like
    the following to /boot/firmware/config.txt (adjust the GPIO number
    to whichever button you want as the wake source):

        dtoverlay=gpio-shutdown,gpio_pin=5,active_low=1,gpio_pull=up

    Then reboot.  Pressing that button will wake the Pi from
    systemctl suspend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Portrait dimensions (we render at 400×600 then rotate −90° for the display)
WIDTH = 400
HEIGHT = 600

# Screen states
SCREEN_BROWSER = "SCREEN_BROWSER"
SCREEN_MENU = "SCREEN_MENU"
SCREEN_READING = "SCREEN_READING"
SCREEN_SLEEP = "SCREEN_SLEEP"

# GPIO button pins (BCM numbering, active-low)
BTN_A = 5
BTN_B = 6
BTN_C = 16
BTN_D = 24
ALL_BUTTONS = (BTN_A, BTN_B, BTN_C, BTN_D)

# Colour palette
COL_BG_DARK = "#161616"
COL_CARD = "#2c2c2c"
COL_CARD_SEL = "#ffffff"
COL_TEXT_LIGHT = "#ffffff"
COL_TEXT_DARK = "#111111"
COL_TEXT_MID = "#888888"
COL_ACCENT = "#bbbbbb"
COL_READING_BG = "#ffffff"
COL_READING_TEXT = "#000000"
COL_DIVIDER = "#444444"
COL_PROGRESS_BG = "#333333"
COL_PROGRESS_FILL = "#999999"
COL_SEL_BAR = "#6699ff"

# Layout
MARGIN_X = 24
MARGIN_TOP = 28
CARD_H = 68
CARD_GAP = 8
CARD_RADIUS = 10
CARD_X = 16
CARD_W = WIDTH - 32 - 12  # leave room for scrollbar
SCROLLBAR_W = 4
SCROLLBAR_X = WIDTH - 10
HINT_BAR_H = 28
LINE_SPACING = 4

# Reading view
READ_MARGIN_X = 24
READ_MARGIN_TOP = 28
STATUS_BAR_H = 48
PROGRESS_BAR_H = 8
PROGRESS_RADIUS = 4

# Save file
SAVE_PATH = Path.home() / ".ereader_saves.json"

# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

# Priority list for serif body and bold heading fonts
_SERIF_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _find_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return the first available TTF font at *size*, or the PIL bitmap fallback."""
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def load_fonts() -> dict:
    """Load all application fonts and return them in a dict."""
    return {
        "body": _find_font(_SERIF_CANDIDATES, 18),
        "body_small": _find_font(_SERIF_CANDIDATES, 14),
        "heading": _find_font(_BOLD_CANDIDATES, 22),
        "heading_small": _find_font(_BOLD_CANDIDATES, 16),
        "card_title": _find_font(_BOLD_CANDIDATES, 18),
        "card_sub": _find_font(_SERIF_CANDIDATES, 14),
        "hint": _find_font(_SERIF_CANDIDATES, 12),
        "sleep_big": _find_font(_BOLD_CANDIDATES, 28),
        "sleep_sub": _find_font(_SERIF_CANDIDATES, 16),
    }


# ---------------------------------------------------------------------------
# Save / restore helpers
# ---------------------------------------------------------------------------

def _book_key(filepath: str) -> str:
    """Return an MD5 hex digest of the book's resolved real path."""
    real = os.path.realpath(filepath)
    return hashlib.md5(real.encode()).hexdigest()


def load_saves() -> dict:
    """Load the JSON save file, returning an empty dict on any error."""
    try:
        return json.loads(SAVE_PATH.read_text())
    except Exception:
        return {}


def save_progress(filepath: str, page: int, saves: dict | None = None) -> dict:
    """Persist the current page for *filepath* and return the updated saves dict."""
    if saves is None:
        saves = load_saves()
    key = _book_key(filepath)
    saves[key] = {"path": filepath, "page": page}
    try:
        SAVE_PATH.write_text(json.dumps(saves, indent=2))
    except Exception as exc:
        print(f"[warn] Could not write save file: {exc}", file=sys.stderr)
    return saves


def get_saved_page(filepath: str, saves: dict) -> int | None:
    """Return the saved page number for *filepath*, or None."""
    entry = saves.get(_book_key(filepath))
    if entry:
        return entry.get("page")
    return None


# ---------------------------------------------------------------------------
# Book scanning and pagination
# ---------------------------------------------------------------------------

def scan_books(directory: str) -> list[dict]:
    """Return a sorted list of dicts with 'path', 'title', 'size' for .txt files."""
    books: list[dict] = []
    dirpath = Path(directory).expanduser()
    if not dirpath.is_dir():
        return books
    for p in sorted(dirpath.iterdir()):
        if p.suffix.lower() == ".txt" and p.is_file():
            title = p.stem.replace("_", " ").replace("-", " ")
            # Title-case if all lower or all upper
            if title == title.lower() or title == title.upper():
                title = title.title()
            books.append({
                "path": str(p),
                "title": title,
                "size": p.stat().st_size,
            })
    return books


def _measure_text_width(font, text: str) -> float:
    """Return the rendered width of *text* using the given font."""
    try:
        return font.getlength(text)
    except AttributeError:
        # Older Pillow
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0] if bbox else 0


def _font_line_height(font) -> int:
    """Return a reliable line height for *font*."""
    try:
        bbox = font.getbbox("Aygjpq|")
        return bbox[3] - bbox[1]
    except Exception:
        return 18


def wrap_text(text: str, font, max_width: int) -> list[str]:
    """Word-wrap *text* into lines that fit within *max_width* pixels.

    Preserves paragraph breaks (blank lines).  Uses binary search with
    Pillow's font metrics for accurate wrapping.
    """
    paragraphs = text.split("\n")
    lines: list[str] = []

    for para in paragraphs:
        para = para.rstrip()
        if not para:
            lines.append("")  # blank line = paragraph break
            continue

        words = para.split()
        if not words:
            lines.append("")
            continue

        current_line = ""
        for word in words:
            if not current_line:
                test = word
            else:
                test = current_line + " " + word

            w = _measure_text_width(font, test)
            if w <= max_width:
                current_line = test
            else:
                if current_line:
                    lines.append(current_line)
                # If a single word exceeds the width, force-break it
                if _measure_text_width(font, word) > max_width:
                    lines.extend(_force_break_word(word, font, max_width))
                    current_line = ""
                else:
                    current_line = word

        if current_line:
            lines.append(current_line)

    return lines


def _force_break_word(word: str, font, max_width: int) -> list[str]:
    """Break a single long word into chunks that each fit within *max_width*."""
    parts: list[str] = []
    start = 0
    while start < len(word):
        # Binary search for the longest prefix that fits
        lo, hi = start + 1, len(word)
        best = start + 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if _measure_text_width(font, word[start:mid]) <= max_width:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        parts.append(word[start:best])
        start = best
    return parts


def paginate(lines: list[str], lines_per_page: int) -> list[list[str]]:
    """Split *lines* into pages of at most *lines_per_page* lines each."""
    pages: list[list[str]] = []
    for i in range(0, len(lines), lines_per_page):
        pages.append(lines[i : i + lines_per_page])
    return pages


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def new_image(bg: str = COL_BG_DARK) -> tuple[Image.Image, ImageDraw.Draw]:
    """Create a fresh portrait-sized RGBA image with the given background."""
    img = Image.new("RGB", (WIDTH, HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    return img, draw


def draw_rounded_rect(draw: ImageDraw.Draw, xy, radius: int, fill):
    """Draw a rounded rectangle given (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def draw_hint_bar(draw: ImageDraw.Draw, fonts: dict, hints: str):
    """Draw the button-hint bar at the very bottom of a dark screen."""
    y = HEIGHT - HINT_BAR_H
    draw.line([(0, y), (WIDTH, y)], fill=COL_DIVIDER, width=1)
    draw.text((WIDTH // 2, y + HINT_BAR_H // 2), hints,
              fill=COL_TEXT_MID, font=fonts["hint"], anchor="mm")


def draw_scrollbar(draw: ImageDraw.Draw, total_items: int, visible_items: int,
                   top_index: int, list_y_start: int, list_y_end: int):
    """Draw a thin scrollbar track + thumb on the right edge."""
    if total_items <= visible_items:
        return
    track_h = list_y_end - list_y_start
    thumb_h = max(20, int(track_h * visible_items / total_items))
    max_scroll = total_items - visible_items
    if max_scroll <= 0:
        return
    thumb_y = list_y_start + int((track_h - thumb_h) * top_index / max_scroll)
    # Track
    draw.rectangle([SCROLLBAR_X, list_y_start, SCROLLBAR_X + SCROLLBAR_W,
                     list_y_end], fill=COL_CARD)
    # Thumb
    draw.rounded_rectangle([SCROLLBAR_X, thumb_y,
                             SCROLLBAR_X + SCROLLBAR_W, thumb_y + thumb_h],
                            radius=2, fill=COL_ACCENT)


# ---------------------------------------------------------------------------
# Screen renderers
# ---------------------------------------------------------------------------

def render_browser(books: list[dict], selected: int, scroll_offset: int,
                   saves: dict, fonts: dict) -> Image.Image:
    """Render the Library Browser screen."""
    img, draw = new_image()

    # Header
    draw.text((CARD_X, 14), "Library", fill=COL_TEXT_LIGHT, font=fonts["heading"])

    y_start = 50
    usable_h = HEIGHT - y_start - HINT_BAR_H - 4
    max_visible = usable_h // (CARD_H + CARD_GAP)

    if not books:
        draw.text((WIDTH // 2, HEIGHT // 2), "No .txt files found",
                  fill=COL_TEXT_MID, font=fonts["body"], anchor="mm")
        draw.text((WIDTH // 2, HEIGHT // 2 + 30),
                  "Place books in the books/ folder",
                  fill=COL_TEXT_MID, font=fonts["card_sub"], anchor="mm")
    else:
        for i in range(max_visible):
            idx = scroll_offset + i
            if idx >= len(books):
                break
            book = books[idx]
            is_sel = idx == selected
            card_y = y_start + i * (CARD_H + CARD_GAP)

            # Card background
            card_fill = COL_CARD_SEL if is_sel else COL_CARD
            draw_rounded_rect(draw, (CARD_X, card_y, CARD_X + CARD_W,
                                      card_y + CARD_H), CARD_RADIUS, card_fill)

            # Selection indicator bar
            if is_sel:
                draw.rounded_rectangle(
                    [CARD_X, card_y + 8, CARD_X + 4, card_y + CARD_H - 8],
                    radius=2, fill=COL_SEL_BAR)

            # Title
            title_col = COL_TEXT_DARK if is_sel else COL_TEXT_LIGHT
            sub_col = COL_TEXT_MID if not is_sel else "#555555"
            title_text = book["title"]
            # Truncate long titles
            max_title_w = CARD_W - 28
            while _measure_text_width(fonts["card_title"], title_text) > max_title_w and len(title_text) > 3:
                title_text = title_text[:-4] + "…"
            draw.text((CARD_X + 16, card_y + 12), title_text,
                      fill=title_col, font=fonts["card_title"])

            # Subtitle
            saved_page = get_saved_page(book["path"], saves)
            if saved_page is not None:
                sub = f"Last read: page {saved_page}"
            else:
                size_kb = book["size"] / 1024
                if size_kb >= 1024:
                    sub = f"{size_kb / 1024:.1f} MB"
                else:
                    sub = f"{size_kb:.0f} KB"
            draw.text((CARD_X + 16, card_y + 40), sub,
                      fill=sub_col, font=fonts["card_sub"])

        # Scrollbar
        draw_scrollbar(draw, len(books), max_visible, scroll_offset,
                       y_start, y_start + max_visible * (CARD_H + CARD_GAP))

    draw_hint_bar(draw, fonts, "[A]▲  [D]▼  [B]Open  [C]Refresh")
    return img


def render_menu(book_title: str, current_page: int, total_pages: int,
                selected: int, fonts: dict) -> Image.Image:
    """Render the Main Menu screen."""
    img, draw = new_image()

    # Header — book title
    header = book_title
    max_hw = WIDTH - 32
    while _measure_text_width(fonts["heading"], header) > max_hw and len(header) > 3:
        header = header[:-4] + "…"
    draw.text((WIDTH // 2, 24), header,
              fill=COL_TEXT_LIGHT, font=fonts["heading"], anchor="mt")

    pct = int(100 * current_page / max(total_pages, 1))
    items = [
        ("Continue Reading", f"Page {current_page} of {total_pages}  ({pct}%)"),
        ("Change Book", "Return to library"),
        ("Start from Beginning", "Reset to page 1"),
        ("Sleep", "Suspend the device"),
    ]

    y_start = 72
    for i, (title, sub) in enumerate(items):
        is_sel = i == selected
        card_y = y_start + i * (CARD_H + CARD_GAP)
        card_fill = COL_CARD_SEL if is_sel else COL_CARD
        draw_rounded_rect(draw, (CARD_X, card_y, CARD_X + CARD_W + 12,
                                  card_y + CARD_H), CARD_RADIUS, card_fill)
        if is_sel:
            draw.rounded_rectangle(
                [CARD_X, card_y + 8, CARD_X + 4, card_y + CARD_H - 8],
                radius=2, fill=COL_SEL_BAR)

        title_col = COL_TEXT_DARK if is_sel else COL_TEXT_LIGHT
        sub_col = "#555555" if is_sel else COL_TEXT_MID
        draw.text((CARD_X + 16, card_y + 12), title,
                  fill=title_col, font=fonts["card_title"])
        draw.text((CARD_X + 16, card_y + 40), sub,
                  fill=sub_col, font=fonts["card_sub"])

    draw_hint_bar(draw, fonts, "[A]▲  [D]▼  [B]Select  [C]Back to reading")
    return img


def render_reading(page_lines: list[str], page_num: int, total_pages: int,
                   fonts: dict) -> Image.Image:
    """Render a single reading-view page."""
    img, draw = new_image(COL_READING_BG)

    line_h = _font_line_height(fonts["body"]) + LINE_SPACING
    y = READ_MARGIN_TOP
    usable_w = WIDTH - 2 * READ_MARGIN_X

    for line in page_lines:
        draw.text((READ_MARGIN_X, y), line,
                  fill=COL_READING_TEXT, font=fonts["body"])
        y += line_h

    # --- Status bar ---
    bar_y = HEIGHT - STATUS_BAR_H
    draw.line([(READ_MARGIN_X, bar_y), (WIDTH - READ_MARGIN_X, bar_y)],
              fill="#cccccc", width=1)

    # Progress bar
    pb_y = bar_y + 10
    pb_x0 = READ_MARGIN_X
    pb_x1 = WIDTH - READ_MARGIN_X
    pb_w = pb_x1 - pb_x0
    # Background track
    draw.rounded_rectangle([pb_x0, pb_y, pb_x1, pb_y + PROGRESS_BAR_H],
                            radius=PROGRESS_RADIUS, fill="#dddddd")
    # Filled portion
    pct = page_num / max(total_pages, 1)
    fill_x1 = pb_x0 + max(int(pb_w * pct), PROGRESS_RADIUS * 2)
    if fill_x1 > pb_x1:
        fill_x1 = pb_x1
    draw.rounded_rectangle([pb_x0, pb_y, fill_x1, pb_y + PROGRESS_BAR_H],
                            radius=PROGRESS_RADIUS, fill="#555555")

    # Labels
    label_y = pb_y + PROGRESS_BAR_H + 4
    pct_int = int(100 * page_num / max(total_pages, 1))
    right_label = f"p.{page_num}/{total_pages}  {pct_int}%"
    left_label = "[A]◀  [D]▶  [B]Menu"
    draw.text((pb_x0, label_y), left_label,
              fill="#999999", font=fonts["hint"])
    draw.text((pb_x1, label_y), right_label,
              fill="#999999", font=fonts["hint"], anchor="ra")

    return img


def render_sleep(book_title: str | None, fonts: dict) -> Image.Image:
    """Render the Sleep screen with a crescent moon."""
    img, draw = new_image()

    # Crescent moon — draw a large circle then mask part with background
    cx, cy = WIDTH // 2, HEIGHT // 2 - 60
    r = 40
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COL_ACCENT)
    draw.ellipse([cx - r + 18, cy - r - 10, cx + r + 18, cy + r - 10],
                 fill=COL_BG_DARK)

    # Small decorative dots (stars)
    for sx, sy in [(cx - 70, cy - 50), (cx + 60, cy - 80),
                   (cx - 50, cy + 30), (cx + 80, cy + 10),
                   (cx + 30, cy - 60)]:
        draw.ellipse([sx - 2, sy - 2, sx + 2, sy + 2], fill=COL_ACCENT)

    draw.text((WIDTH // 2, cy + 70), "Sleeping…",
              fill=COL_TEXT_LIGHT, font=fonts["sleep_big"], anchor="mt")
    draw.text((WIDTH // 2, cy + 110), "Press any button to wake",
              fill=COL_TEXT_MID, font=fonts["sleep_sub"], anchor="mt")

    if book_title:
        draw.text((WIDTH // 2, cy + 145), book_title,
                  fill=COL_TEXT_MID, font=fonts["card_sub"], anchor="mt")

    return img


# ---------------------------------------------------------------------------
# Display output (hardware or simulation)
# ---------------------------------------------------------------------------

class DisplayDriver:
    """Abstraction over real Inky hardware or PNG-based simulation."""

    def __init__(self, simulate: bool = False):
        self.simulate = simulate
        self._frame = 0
        self._screen_label = ""
        self.inky = None

        if not simulate:
            try:
                from inky.auto import auto
                self.inky = auto()
                self.inky.set_border(self.inky.BLACK)
            except Exception as exc:
                print(f"[error] Could not init Inky display: {exc}", file=sys.stderr)
                print("[info]  Falling back to simulation mode.", file=sys.stderr)
                self.simulate = True

    def show(self, img: Image.Image, label: str = "screen"):
        """Display or save the portrait image."""
        self._screen_label = label
        if self.simulate:
            fname = f"sim_{self._frame:03d}_{label}.png"
            img.save(fname)
            print(f"[sim] Saved {fname}")
            self._frame += 1
        else:
            # Rotate −90° from portrait (400×600) to landscape (600×400)
            rotated = img.rotate(90, expand=True)
            self.inky.set_image(rotated)
            self.inky.show()


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

class InputHandler:
    """GPIO button reader (real) or scripted sequence (simulation)."""

    def __init__(self, simulate: bool = False):
        self.simulate = simulate
        self._gpio_setup = False

        if not simulate:
            try:
                import RPi.GPIO as GPIO
                self.GPIO = GPIO
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                for pin in ALL_BUTTONS:
                    GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                self._gpio_setup = True
            except Exception as exc:
                print(f"[warn] GPIO setup failed: {exc}", file=sys.stderr)

    def wait_for_button(self) -> int:
        """Block until a button is pressed and return its BCM pin number."""
        if self.simulate:
            return BTN_B  # auto-confirm in sim

        if not self._gpio_setup:
            time.sleep(0.5)
            return BTN_B

        GPIO = self.GPIO
        while True:
            for pin in ALL_BUTTONS:
                if GPIO.input(pin) == GPIO.LOW:
                    # Debounce
                    time.sleep(0.2)
                    # Wait for release
                    while GPIO.input(pin) == GPIO.LOW:
                        time.sleep(0.05)
                    return pin
            time.sleep(0.05)

    def wait_any_press(self):
        """Wait for any single button press (used after waking from sleep)."""
        self.wait_for_button()

    def cleanup(self):
        if self._gpio_setup:
            self.GPIO.cleanup()


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class EReaderApp:
    """Top-level e-reader application state machine."""

    def __init__(self, books_dir: str, simulate: bool = False):
        self.books_dir = str(Path(books_dir).expanduser())
        self.simulate = simulate
        self.fonts = load_fonts()
        self.display = DisplayDriver(simulate)
        self.input = InputHandler(simulate)
        self.saves = load_saves()

        # State
        self.screen = SCREEN_BROWSER
        self.books: list[dict] = []
        self.browser_sel = 0
        self.browser_scroll = 0
        self.menu_sel = 0

        # Current book
        self.book_path: str | None = None
        self.book_title: str = ""
        self.book_pages: list[list[str]] = []
        self.current_page = 1

        # Ensure books directory exists
        Path(self.books_dir).mkdir(parents=True, exist_ok=True)

        # Compute how many cards fit for browser scrolling
        usable_h = HEIGHT - 50 - HINT_BAR_H - 4
        self.browser_visible = usable_h // (CARD_H + CARD_GAP)

    # --- Book loading ---

    def _load_book(self, path: str):
        """Load and paginate a .txt book file."""
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        usable_w = WIDTH - 2 * READ_MARGIN_X
        line_h = _font_line_height(self.fonts["body"]) + LINE_SPACING
        usable_h = HEIGHT - READ_MARGIN_TOP - STATUS_BAR_H - 8
        lines_per_page = max(1, usable_h // line_h)

        lines = wrap_text(text, self.fonts["body"], usable_w)
        self.book_pages = paginate(lines, lines_per_page)
        if not self.book_pages:
            self.book_pages = [[""]]

        self.book_path = path
        # Derive display title
        self.book_title = Path(path).stem.replace("_", " ").replace("-", " ")
        if self.book_title == self.book_title.lower() or self.book_title == self.book_title.upper():
            self.book_title = self.book_title.title()

        # Restore saved position
        saved = get_saved_page(path, self.saves)
        if saved is not None and 1 <= saved <= len(self.book_pages):
            self.current_page = saved
        else:
            self.current_page = 1

    # --- Screen display ---

    def _show_browser(self):
        img = render_browser(self.books, self.browser_sel, self.browser_scroll,
                             self.saves, self.fonts)
        self.display.show(img, "browser")

    def _show_menu(self):
        total = len(self.book_pages)
        img = render_menu(self.book_title, self.current_page, total,
                          self.menu_sel, self.fonts)
        self.display.show(img, "menu")

    def _show_reading(self):
        page_idx = self.current_page - 1
        if 0 <= page_idx < len(self.book_pages):
            lines = self.book_pages[page_idx]
        else:
            lines = ["[Page out of range]"]
        total = len(self.book_pages)
        img = render_reading(lines, self.current_page, total, self.fonts)
        self.display.show(img, f"page_{self.current_page:04d}")

    def _show_sleep(self):
        img = render_sleep(self.book_title, self.fonts)
        self.display.show(img, "sleep")

    # --- Save helper ---

    def _save_current(self):
        if self.book_path:
            self.saves = save_progress(self.book_path, self.current_page, self.saves)

    # --- Sleep ---

    def _do_sleep(self):
        self._save_current()
        self._show_sleep()

        if not self.simulate:
            try:
                subprocess.run(["sudo", "systemctl", "suspend"],
                               timeout=10, check=True)
                # After resume, wait for a deliberate button press
                time.sleep(0.5)  # brief settling time
                self.input.wait_any_press()
            except Exception as exc:
                print(f"[warn] Suspend failed ({exc}), waiting for button press…",
                      file=sys.stderr)
                self.input.wait_any_press()

        # Return to menu
        self.screen = SCREEN_MENU
        self.menu_sel = 0

    # --- State machine ---

    def _handle_browser(self):
        self.books = scan_books(self.books_dir)
        if self.browser_sel >= len(self.books):
            self.browser_sel = max(0, len(self.books) - 1)
        self._show_browser()

        if self.simulate:
            # In simulation, auto-select the first book
            if self.books:
                self._load_book(self.books[0]["path"])
                self.screen = SCREEN_MENU
            return

        btn = self.input.wait_for_button()

        if btn == BTN_A:  # Scroll up
            if self.browser_sel > 0:
                self.browser_sel -= 1
                if self.browser_sel < self.browser_scroll:
                    self.browser_scroll = self.browser_sel
        elif btn == BTN_D:  # Scroll down
            if self.browser_sel < len(self.books) - 1:
                self.browser_sel += 1
                if self.browser_sel >= self.browser_scroll + self.browser_visible:
                    self.browser_scroll = self.browser_sel - self.browser_visible + 1
        elif btn == BTN_B:  # Open
            if self.books:
                self._load_book(self.books[self.browser_sel]["path"])
                self.screen = SCREEN_MENU
                self.menu_sel = 0
        elif btn == BTN_C:  # Refresh
            pass  # will rescan at top of loop

    def _handle_menu(self):
        total = len(self.book_pages)
        self._show_menu()

        if self.simulate:
            # In simulation, auto-select "Continue Reading"
            self.screen = SCREEN_READING
            return

        btn = self.input.wait_for_button()

        if btn == BTN_A:
            self.menu_sel = max(0, self.menu_sel - 1)
        elif btn == BTN_D:
            self.menu_sel = min(3, self.menu_sel + 1)
        elif btn == BTN_C:
            # Go straight back to reading
            self.screen = SCREEN_READING
        elif btn == BTN_B:
            if self.menu_sel == 0:  # Continue Reading
                self.screen = SCREEN_READING
            elif self.menu_sel == 1:  # Change Book
                self.screen = SCREEN_BROWSER
                self.browser_sel = 0
                self.browser_scroll = 0
            elif self.menu_sel == 2:  # Start from Beginning
                self.current_page = 1
                self._save_current()
                self.screen = SCREEN_READING
            elif self.menu_sel == 3:  # Sleep
                self._do_sleep()

    def _handle_reading(self):
        self._show_reading()

        if self.simulate:
            return  # will be driven externally

        btn = self.input.wait_for_button()

        if btn == BTN_A:  # Previous page
            if self.current_page > 1:
                self.current_page -= 1
                self._save_current()
        elif btn == BTN_D:  # Next page
            if self.current_page < len(self.book_pages):
                self.current_page += 1
                self._save_current()
        elif btn == BTN_B:  # Menu
            self.screen = SCREEN_MENU
            self.menu_sel = 0

    def run(self):
        """Main event loop."""
        if self.simulate:
            self._run_simulation()
            return

        while True:
            if self.screen == SCREEN_BROWSER:
                self._handle_browser()
            elif self.screen == SCREEN_MENU:
                self._handle_menu()
            elif self.screen == SCREEN_READING:
                self._handle_reading()

    def _run_simulation(self):
        """Render browser, menu, and first 3 pages, then exit."""
        # Browser
        self.books = scan_books(self.books_dir)
        self._show_browser()

        if not self.books:
            print("[sim] No books found — only browser rendered.")
            return

        # Load first book
        self._load_book(self.books[0]["path"])

        # Menu
        self._show_menu()

        # First 3 reading pages
        for p in range(1, min(4, len(self.book_pages) + 1)):
            self.current_page = p
            self._show_reading()

        # Sleep screen
        self._show_sleep()

        print(f"[sim] Done. Rendered {self.display._frame} screens.")

    def shutdown(self):
        """Save progress and clean up."""
        self._save_current()
        self.input.cleanup()


# ---------------------------------------------------------------------------
# Signal handlers & CLI entry point
# ---------------------------------------------------------------------------

_app_instance: EReaderApp | None = None


def _signal_handler(signum, frame):
    """Gracefully shut down on SIGINT / SIGTERM."""
    print(f"\n[info] Caught signal {signum}, shutting down…")
    if _app_instance:
        _app_instance.shutdown()
    sys.exit(0)


def main():
    global _app_instance

    parser = argparse.ArgumentParser(
        description="Inky Impression 4\" E-Reader")
    parser.add_argument("books_directory", nargs="?", default="~/books/",
                        help="Path to folder of .txt books (default: ~/books/)")
    parser.add_argument("--simulate", action="store_true",
                        help="Skip hardware, render numbered PNGs instead")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    app = EReaderApp(args.books_directory, simulate=args.simulate)
    _app_instance = app

    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
