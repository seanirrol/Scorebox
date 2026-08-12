#!/usr/bin/env python3
"""
Renders /winlossgraph's monthly chart as a transparent PNG - one horizontal
stacked bar per day (win% in green, loss% in red), attached to the command's
embed via `attachment://` the same way scoreimage.py's score cards are.

Push/void/pending picks aren't part of this at all - dailylog.daily_win_loss
already excludes them before this module ever sees the data (see that
function's own docstring for why).
"""

import datetime
import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")

WIDTH = 700
LABEL_WIDTH = 76
RIGHT_PADDING = 20
BAR_WIDTH = WIDTH - LABEL_WIDTH - RIGHT_PADDING
BAR_HEIGHT = 26
ROW_GAP = 10
ROW_HEIGHT = BAR_HEIGHT + ROW_GAP
TOP_PADDING = 44
LEGEND_HEIGHT = 34
BOTTOM_PADDING = 16

TITLE_SIZE = 20
LABEL_SIZE = 13
PCT_SIZE = 12
LEGEND_SIZE = 13

_WIN_COLOR = (46, 204, 113, 255)  # matches scoreimage._PALETTE["won"]
_LOSS_COLOR = (231, 76, 60, 255)  # matches scoreimage._PALETTE["lost"]
_LABEL_COLOR = (185, 187, 190, 255)
_TITLE_COLOR = (220, 221, 222, 255)
_PCT_TEXT_COLOR = (60, 60, 60, 255)  # matches scoreimage._PILL_TEXT_COLOR

_BOLD_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
_REGULAR_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


def _load_font(paths: list[str], size: int) -> ImageFont.ImageFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


_TITLE_FONT = _load_font(_BOLD_FONT_PATHS, TITLE_SIZE)
_LABEL_FONT = _load_font(_BOLD_FONT_PATHS, LABEL_SIZE)
_PCT_FONT = _load_font(_BOLD_FONT_PATHS, PCT_SIZE)
_LEGEND_FONT = _load_font(_REGULAR_FONT_PATHS, LEGEND_SIZE)

# Minimum segment width a percentage label is drawn in - anything narrower
# and the text wouldn't fit without overflowing into the other segment.
_MIN_LABEL_SEGMENT_WIDTH = 26


def _day_label(date_str: str) -> str:
    d = datetime.date.fromisoformat(date_str)
    return d.strftime("%b ") + str(d.day)


def _month_title(year_month: str) -> str:
    d = datetime.datetime.strptime(year_month, "%Y-%m")
    return d.strftime("%B %Y")


def render_month_chart(year_month: str, rows: list[tuple[str, int, int]]) -> bytes:
    """rows is dailylog.daily_win_loss's output: (date_str, won, lost)
    tuples, oldest first. Every row is guaranteed won+lost > 0 by that
    function's own filtering, so win_pct + loss_pct always spans the full
    bar width with nothing left over for push/void."""
    height = TOP_PADDING + len(rows) * ROW_HEIGHT + LEGEND_HEIGHT + BOTTOM_PADDING
    img = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.text((WIDTH / 2, TOP_PADDING / 2), f"Win/Loss Rate — {_month_title(year_month)}", font=_TITLE_FONT, fill=_TITLE_COLOR, anchor="mm")

    y = TOP_PADDING
    for date_str, won, lost in rows:
        decided = won + lost
        win_w = round(BAR_WIDTH * won / decided)
        loss_w = BAR_WIDTH - win_w

        draw.text((LABEL_WIDTH - 10, y + BAR_HEIGHT / 2), _day_label(date_str), font=_LABEL_FONT, fill=_LABEL_COLOR, anchor="rm")

        x = LABEL_WIDTH
        if win_w > 0:
            draw.rectangle([x, y, x + win_w, y + BAR_HEIGHT], fill=_WIN_COLOR)
            if win_w >= _MIN_LABEL_SEGMENT_WIDTH:
                pct_text = f"{round(100 * won / decided)}%"
                draw.text((x + win_w / 2, y + BAR_HEIGHT / 2), pct_text, font=_PCT_FONT, fill=_PCT_TEXT_COLOR, anchor="mm")
        x += win_w
        if loss_w > 0:
            draw.rectangle([x, y, x + loss_w, y + BAR_HEIGHT], fill=_LOSS_COLOR)
            if loss_w >= _MIN_LABEL_SEGMENT_WIDTH:
                pct_text = f"{round(100 * lost / decided)}%"
                draw.text((x + loss_w / 2, y + BAR_HEIGHT / 2), pct_text, font=_PCT_FONT, fill=_PCT_TEXT_COLOR, anchor="mm")

        y += ROW_HEIGHT

    legend_y = y + LEGEND_HEIGHT / 2
    swatch = 12
    lx = WIDTH / 2 - 90
    draw.rectangle([lx, legend_y - swatch / 2, lx + swatch, legend_y + swatch / 2], fill=_WIN_COLOR)
    draw.text((lx + swatch + 6, legend_y), "Win", font=_LEGEND_FONT, fill=_LABEL_COLOR, anchor="lm")
    lx += 90
    draw.rectangle([lx, legend_y - swatch / 2, lx + swatch, legend_y + swatch / 2], fill=_LOSS_COLOR)
    draw.text((lx + swatch + 6, legend_y), "Loss", font=_LEGEND_FONT, fill=_LABEL_COLOR, anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
