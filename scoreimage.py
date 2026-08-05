#!/usr/bin/env python3
"""
Renders the score card as a small transparent PNG - a rounded card panel,
a centered period/set pill on top, then one row per team (logo + name on
the left, score(s) on the right).

Discord embeds have no way to center-align or color text (or show a custom
card background), so anything we want fully controlled is drawn here as an
image instead and attached to the embed via `attachment://`. The embed
title/author/footer stay native Discord text.

Team names are never shrunk - they wrap to a second line instead, and only
get truncated with an ellipsis if two lines still aren't enough. Because a
wrapped name needs extra vertical room, the canvas height is computed after
laying out both rows rather than being a fixed constant.
"""

import io
import os
import subprocess
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

_LOGO_FETCH_TIMEOUT_SECONDS = 4
_LOGO_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")

WIDTH = 380
MARGIN = 16
TOP_PADDING = 12
BOTTOM_PADDING = 12
COLUMN_GAP = 14
NAME_TO_SCORES_GAP = 14
LOGO_SIZE = 32
LOGO_TO_NAME_GAP = 8
NAME_LINE_HEIGHT = 20
SCORE_ROW_MIN_HEIGHT = 36
ROW_GAP = 8
PILL_GAP = 10
MAX_NAME_LINES = 2

MAIN_SCORE_SIZE = 18
SUB_SCORE_SIZE = 14
PERIOD_SIZE = 10
NAME_FONT_SIZE = 16  # fixed - names wrap instead of shrinking

# Prefer DejaVu (installed via `fonts-dejavu-core` in the Dockerfile on Linux),
# fall back to whatever's available on the dev machine, then Pillow's built-in
# bitmap font so rendering never hard-fails for lack of a TTF.
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
# Compact/condensed look for team names specifically - PT Sans Narrow Bold
# (SIL Open Font License, bundled here as an actual file rather than an OS
# path so it renders identically on Windows and in the Linux/Docker deploy)
# is the closest freely-licensed match to Abadi MT Condensed's rounded,
# humanist-narrow feel - Abadi itself is a commercial Microsoft Office font
# that isn't installed here and can't be legally redistributed.
_COMPACT_FONT_PATHS = [
    os.path.join(_FONTS_DIR, "PTSansNarrow-Bold.ttf"),
    "C:/Windows/Fonts/tahomabd.ttf",
    "/System/Library/Fonts/Supplemental/Tahoma Bold.ttf",
] + _BOLD_FONT_PATHS
# Distinguished, high-impact look for the main score specifically - Anton is
# a bold condensed display face (SIL Open Font License, bundled the same way
# as PT Sans Narrow above) built for exactly this kind of big scoreboard number.
_SCORE_DISPLAY_FONT_PATHS = [os.path.join(_FONTS_DIR, "Anton-Regular.ttf")] + _BOLD_FONT_PATHS

_CARD_BG_COLOR = (45, 47, 51, 255)  # dark gray
_CARD_BORDER_COLOR = (255, 255, 255, 25)
_NAME_COLOR = (220, 221, 222, 255)  # discord's default light text
_PILL_TEXT_COLOR = (60, 60, 60, 255)  # dark gray - same regardless of the pill's own status color

# A card's pill/score-digit color reflects its current lifecycle state -
# hibernating before kickoff, live once it starts, then whichever of
# won/lost/push/void it's finally graded as. "finished" is a fallback for a
# finished card with nothing to grade at all (e.g. a manual /track with no
# pick attached) - green, matching this bot's original behavior from before
# result-specific coloring existed. Single source of truth for both the
# image (RGBA) and each tracker's Discord embed side-color (hex, below).
_PALETTE = {
    "notstarted": (52, 152, 219),  # blue
    "inprogress": (241, 196, 15),  # yellow
    "won": (46, 204, 113),  # green
    "lost": (231, 76, 60),  # red
    "push": (149, 165, 166),  # grey
    "void": (155, 89, 182),  # purple
    "finished": (46, 204, 113),  # green (fallback)
}
_SCORE_COLOR = {key: (*rgb, 255) for key, rgb in _PALETTE.items()}
# Score digits now match the pill/card color for the same state rather than
# a separate always-white-while-live palette.
_SCORE_TEXT_COLOR = _SCORE_COLOR
EMBED_COLOR = {key: (rgb[0] << 16) + (rgb[1] << 8) + rgb[2] for key, rgb in _PALETTE.items()}

_logo_cache: dict[str, Image.Image] = {}


def _load_font(paths: list[str], size: int) -> ImageFont.ImageFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


_MAIN_SCORE_FONT = _load_font(_SCORE_DISPLAY_FONT_PATHS, MAIN_SCORE_SIZE)
_SUB_SCORE_FONT = _load_font(_REGULAR_FONT_PATHS, SUB_SCORE_SIZE)
_PERIOD_FONT = _load_font(_BOLD_FONT_PATHS, PERIOD_SIZE)
_NAME_FONT = _load_font(_COMPACT_FONT_PATHS, NAME_FONT_SIZE)


def _ellipsize(text: str, font: ImageFont.ImageFont, max_width: float, draw: ImageDraw.ImageDraw) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    truncated = text
    while truncated and draw.textlength(truncated + "…", font=font) > max_width:
        truncated = truncated[:-1]
    return (truncated + "…") if truncated else "…"


def _wrap_name(text: str, max_width: float, draw: ImageDraw.ImageDraw) -> list[str]:
    """
    Greedy word-wrap up to MAX_NAME_LINES, at a fixed font size (never
    shrunk). Anything left over after that many lines gets folded into the
    last line and ellipsized rather than dropped or overflowing.
    """
    words = text.split()
    if not words:
        return [text]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=_NAME_FONT) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)

    if len(lines) <= MAX_NAME_LINES:
        return [_ellipsize(line, _NAME_FONT, max_width, draw) for line in lines]

    kept = lines[:MAX_NAME_LINES - 1]
    remainder = " ".join(lines[MAX_NAME_LINES - 1:])
    return [_ellipsize(line, _NAME_FONT, max_width, draw) for line in kept] + [
        _ellipsize(remainder, _NAME_FONT, max_width, draw)
    ]


def _fetch_circular_logo(logo_url: Optional[str], size: int = LOGO_SIZE) -> Optional[Image.Image]:
    """
    Team/player crest or headshot, resized and circle-masked, cached
    in-process by (URL, size). Provider-agnostic - callers resolve the
    actual URL (see scores365.competitor_logo_url / sofascore.team_logo_url
    / sofascore.player_photo_url).

    Fetched via curl rather than a Python HTTP client - confirmed live that
    Sofascore's image endpoint 403s `requests` (same TLS-fingerprinting
    block as its JSON API) but returns 200 to curl; 365scores' image CDN
    works fine either way, so curl is used for both for one consistent path.
    """
    if not logo_url:
        return None
    cache_key = f"{size}:{logo_url}"
    cached = _logo_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            ["curl", "-s", "-A", _LOGO_USER_AGENT, logo_url],
            capture_output=True, timeout=_LOGO_FETCH_TIMEOUT_SECONDS, check=False,
        )
        raw = Image.open(io.BytesIO(result.stdout)).convert("RGBA").resize((size, size), Image.LANCZOS)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return None

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(raw, (0, 0), mask)
    _logo_cache[cache_key] = circular
    return circular


def _draw_lines_centered(draw: ImageDraw.ImageDraw, x: float, center_y: float, lines: list[str]):
    block_h = len(lines) * NAME_LINE_HEIGHT
    top = center_y - block_h / 2
    for i, line in enumerate(lines):
        line_center_y = top + NAME_LINE_HEIGHT * (i + 0.5)
        draw.text((x, line_center_y), line, font=_NAME_FONT, fill=_NAME_COLOR, anchor="lm")


PLAYER_PHOTO_SIZE = 56
TEAM_LABEL_SIZE = 12
SMALL_TEAM_NAME_SIZE = 11
STAT_LABEL_SIZE = 13
VALUE_SIZE = 34
HEADER_LINE_GAP = 3
HEADER_GAP = 10
PHOTO_GAP = 10
PLAYER_NAME_GAP = 4
SMALL_TEAM_NAME_GAP = 6
STAT_LABEL_GAP = 2
VALUE_GAP = 4

_TEAM_LABEL_FONT = _load_font(_BOLD_FONT_PATHS, TEAM_LABEL_SIZE)
_SMALL_TEAM_FONT = _load_font(_REGULAR_FONT_PATHS, SMALL_TEAM_NAME_SIZE)
_STAT_LABEL_FONT = _load_font(_REGULAR_FONT_PATHS, STAT_LABEL_SIZE)
_VALUE_FONT = _load_font(_SCORE_DISPLAY_FONT_PATHS, VALUE_SIZE)


def render_player_card(
    team_name: str,
    photo_url: Optional[str],
    player_name: str,
    stat_label: str,
    value_text: str,
    status: str,
    period_text: str = "",
) -> bytes:
    """
    A single-player card: circular photo on the left, name/team/stat
    label/value stacked to its right - saves vertical space versus stacking
    everything under a centered photo. Sport/tournament and matchup live in
    the embed text above the image instead (see proptracker.build_embed),
    same as /track's author/description - but the live/final status is drawn
    as a pill here, same placement and style as render_score_card's, so both
    card types show it the same way (e.g. "Final").
    """
    photo = _fetch_circular_logo(photo_url, size=PLAYER_PHOTO_SIZE)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pill_color = _SCORE_COLOR.get(status, (255, 255, 255, 255))
    value_color = _SCORE_TEXT_COLOR.get(status, (255, 255, 255, 255))

    max_text_width = WIDTH - MARGIN * 2 - PLAYER_PHOTO_SIZE - PHOTO_GAP

    name_lines = _wrap_name(player_name, max_text_width, measure)
    small_team_text = _ellipsize(team_name, _SMALL_TEAM_FONT, max_text_width, measure)
    stat_label_text = _ellipsize(stat_label, _STAT_LABEL_FONT, max_text_width, measure)
    value_line = _ellipsize(value_text, _VALUE_FONT, max_text_width, measure)

    # Center the photo+text group as a whole, so it's not pinned to the left
    # margin - width based on the widest actual line, not the full available space.
    text_content_width = max(
        [measure.textlength(line, font=_NAME_FONT) for line in name_lines]
        + [
            measure.textlength(small_team_text, font=_SMALL_TEAM_FONT),
            measure.textlength(stat_label_text, font=_STAT_LABEL_FONT),
            measure.textlength(value_line, font=_VALUE_FONT),
        ]
    )
    group_width = PLAYER_PHOTO_SIZE + PHOTO_GAP + text_content_width
    photo_x = (WIDTH - group_width) / 2
    text_x = photo_x + PLAYER_PHOTO_SIZE + PHOTO_GAP

    name_block_h = len(name_lines) * NAME_LINE_HEIGHT
    small_team_h = SMALL_TEAM_NAME_SIZE + 4
    stat_label_h = STAT_LABEL_SIZE + 4
    value_h = VALUE_SIZE + 8
    text_block_h = name_block_h + PLAYER_NAME_GAP + small_team_h + SMALL_TEAM_NAME_GAP + stat_label_h + STAT_LABEL_GAP + value_h

    content_h = max(text_block_h, PLAYER_PHOTO_SIZE)

    pill_w = pill_h = 0
    if period_text:
        bbox = measure.textbbox((0, 0), period_text, font=_PERIOD_FONT)
        pad_x, pad_y = 8, 3
        pill_w, pill_h = (bbox[2] - bbox[0]) + pad_x * 2, (bbox[3] - bbox[1]) + pad_y * 2

    height = TOP_PADDING + (pill_h + PILL_GAP if period_text else 0) + content_h + BOTTOM_PADDING

    img = Image.new("RGBA", (WIDTH, int(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    card_inset = 3
    draw.rounded_rectangle(
        [card_inset, card_inset, WIDTH - card_inset, height - card_inset],
        radius=16,
        fill=_CARD_BG_COLOR,
        outline=_CARD_BORDER_COLOR,
        width=1,
    )

    content_top = TOP_PADDING
    if period_text:
        pill_center_y = content_top + pill_h / 2
        left = WIDTH / 2 - pill_w / 2
        draw.rounded_rectangle([left, content_top, left + pill_w, content_top + pill_h], radius=pill_h / 2, fill=pill_color)
        draw.text((WIDTH / 2, pill_center_y), period_text, font=_PERIOD_FONT, fill=_PILL_TEXT_COLOR, anchor="mm")
        content_top += pill_h + PILL_GAP

    photo_top = content_top + (content_h - PLAYER_PHOTO_SIZE) / 2
    if photo:
        img.paste(photo, (int(photo_x), int(photo_top)), photo)

    y = content_top + (content_h - text_block_h) / 2

    name_top = y
    for i, line in enumerate(name_lines):
        line_center_y = name_top + NAME_LINE_HEIGHT * (i + 0.5)
        draw.text((text_x, line_center_y), line, font=_NAME_FONT, fill=_NAME_COLOR, anchor="lm")
    y += name_block_h + PLAYER_NAME_GAP

    draw.text((text_x, y + small_team_h / 2), small_team_text, font=_SMALL_TEAM_FONT, fill=_NAME_COLOR, anchor="lm")
    y += small_team_h + SMALL_TEAM_NAME_GAP

    draw.text((text_x, y + stat_label_h / 2), stat_label_text, font=_STAT_LABEL_FONT, fill=_NAME_COLOR, anchor="lm")
    y += stat_label_h + STAT_LABEL_GAP

    draw.text((text_x, y + value_h / 2), value_line, font=_VALUE_FONT, fill=value_color, anchor="lm")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_score_card(
    home_name: str,
    away_name: str,
    home_logo_url: Optional[str],
    away_logo_url: Optional[str],
    home_cols: list[str],
    away_cols: list[str],
    period_text: str,
    status: str,
) -> bytes:
    """
    home_cols/away_cols are ordered left-to-right as they appear beside each
    team's name, with the FIRST entry being the main/aggregate score (drawn
    bigger and bold, closest to the name); anything after it is a smaller
    sub-tier stat - tennis's games/points, volleyball's current-set score.
    Both lists must be the same length.
    """
    home_logo = _fetch_circular_logo(home_logo_url)
    away_logo = _fetch_circular_logo(away_logo_url)

    # A throwaway draw context just for text measurement, before the final
    # image (whose height depends on that measurement) exists.
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    pill_color = _SCORE_COLOR.get(status, (255, 255, 255, 255))
    score_color = _SCORE_TEXT_COLOR.get(status, (255, 255, 255, 255))

    n = len(home_cols)
    col_fonts = [_MAIN_SCORE_FONT if i == 0 else _SUB_SCORE_FONT for i in range(n)]
    col_widths = [
        max(measure.textlength(home_cols[i], font=col_fonts[i]), measure.textlength(away_cols[i], font=col_fonts[i]))
        for i in range(n)
    ]
    scores_width = sum(col_widths) + COLUMN_GAP * (n - 1) if n else 0

    name_start_x = MARGIN + LOGO_SIZE + LOGO_TO_NAME_GAP
    name_max_width = WIDTH - name_start_x - MARGIN - scores_width - (NAME_TO_SCORES_GAP if n else 0)
    home_lines = _wrap_name(home_name, name_max_width, measure)
    away_lines = _wrap_name(away_name, name_max_width, measure)

    row1_height = max(len(home_lines) * NAME_LINE_HEIGHT, SCORE_ROW_MIN_HEIGHT, LOGO_SIZE)
    row2_height = max(len(away_lines) * NAME_LINE_HEIGHT, SCORE_ROW_MIN_HEIGHT, LOGO_SIZE)

    pill_w = pill_h = 0
    if period_text:
        bbox = measure.textbbox((0, 0), period_text, font=_PERIOD_FONT)
        pad_x, pad_y = 8, 3
        pill_w, pill_h = (bbox[2] - bbox[0]) + pad_x * 2, (bbox[3] - bbox[1]) + pad_y * 2

    height = TOP_PADDING + (pill_h + PILL_GAP if period_text else 0) + row1_height + ROW_GAP + row2_height + BOTTOM_PADDING

    img = Image.new("RGBA", (WIDTH, int(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    card_inset = 3
    draw.rounded_rectangle(
        [card_inset, card_inset, WIDTH - card_inset, height - card_inset],
        radius=16,
        fill=_CARD_BG_COLOR,
        outline=_CARD_BORDER_COLOR,
        width=1,
    )

    y = TOP_PADDING
    if period_text:
        pill_center_y = y + pill_h / 2
        left, top = WIDTH / 2 - pill_w / 2, y
        draw.rounded_rectangle([left, top, left + pill_w, top + pill_h], radius=pill_h / 2, fill=pill_color)
        draw.text((WIDTH / 2, pill_center_y), period_text, font=_PERIOD_FONT, fill=_PILL_TEXT_COLOR, anchor="mm")
        y += pill_h + PILL_GAP

    row1_y = y + row1_height / 2
    y += row1_height + ROW_GAP
    row2_y = y + row2_height / 2

    divider_y = (row1_y + row2_y) / 2
    draw.line([(MARGIN, divider_y), (WIDTH - MARGIN, divider_y)], fill=_CARD_BORDER_COLOR, width=1)

    if home_logo:
        img.paste(home_logo, (MARGIN, int(row1_y - LOGO_SIZE / 2)), home_logo)
    if away_logo:
        img.paste(away_logo, (MARGIN, int(row2_y - LOGO_SIZE / 2)), away_logo)

    _draw_lines_centered(draw, name_start_x, row1_y, home_lines)
    _draw_lines_centered(draw, name_start_x, row2_y, away_lines)

    def draw_row_scores(y: float, cols: list[str]):
        x = WIDTH - MARGIN
        for i in reversed(range(n)):
            draw.text((x, y), cols[i], font=col_fonts[i], fill=score_color, anchor="rm")
            x -= col_widths[i] + COLUMN_GAP

    draw_row_scores(row1_y, home_cols)
    draw_row_scores(row2_y, away_cols)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
