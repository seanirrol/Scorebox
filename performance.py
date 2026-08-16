#!/usr/bin/env python3
"""
Renders /performance's chart as a transparent PNG - one bold header bar per
sport (its own combined win%), followed by an indented, smaller sub-bar per
tournament/competition/league within it - a single win% bar per row (no
loss segment, see _draw_bar) against a faint full-width track, ordered
highest win% to lowest throughout. Every bet type within a tournament is
combined into that tournament's own single bar (see dailylog.
sport_tournament_win_loss).

Push/void/pending picks aren't part of this at all - dailylog.
sport_tournament_win_loss already excludes them before this module ever
sees the data (see that function's own docstring for why).
"""

import io

from PIL import Image, ImageDraw

from winlossgraph import _LABEL_FONT, _LEGEND_FONT, _PCT_FONT, _TITLE_FONT

WIDTH = 760
RIGHT_PADDING = 20
SPORT_LABEL_WIDTH = 130
SUB_LABEL_WIDTH = 190
SPORT_BAR_HEIGHT = 28
SUB_BAR_HEIGHT = 20
ROW_GAP = 8
SPORT_GAP = 16
TOP_PADDING = 44
BOTTOM_PADDING = 20

_SPORT_BAR_WIDTH = WIDTH - SPORT_LABEL_WIDTH - RIGHT_PADDING
_SUB_BAR_WIDTH = WIDTH - SUB_LABEL_WIDTH - RIGHT_PADDING

_WIN_COLOR = (46, 204, 113, 255)  # matches scoreimage._PALETTE["won"]
_TRACK_COLOR = (255, 255, 255, 40)  # faint full-width baseline behind the win bar
_LABEL_COLOR = (220, 221, 222, 255)
_SUB_LABEL_COLOR = (185, 187, 190, 255)
_TITLE_COLOR = (220, 221, 222, 255)
_PCT_TEXT_COLOR = (60, 60, 60, 255)  # matches scoreimage._PILL_TEXT_COLOR

# Minimum bar width the percentage label is drawn centered inside - any
# narrower and it's drawn just outside the bar's own end instead, so a low
# win% doesn't get a label crammed into (or overflowing) a tiny sliver.
_MIN_LABEL_SEGMENT_WIDTH = 30


def _win_pct(won: int, lost: int) -> float:
    decided = won + lost
    return won / decided if decided else 0.0


def _draw_bar(draw: ImageDraw.ImageDraw, x: float, y: float, width: int, height: int, won: int, lost: int):
    pct = _win_pct(won, lost)
    win_w = round(width * pct)
    draw.rectangle([x, y, x + width, y + height], fill=_TRACK_COLOR)
    if win_w > 0:
        draw.rectangle([x, y, x + win_w, y + height], fill=_WIN_COLOR)
    label = f"{round(100 * pct)}%"
    if win_w >= _MIN_LABEL_SEGMENT_WIDTH:
        draw.text((x + win_w / 2, y + height / 2), label, font=_PCT_FONT, fill=_PCT_TEXT_COLOR, anchor="mm")
    else:
        draw.text((x + win_w + 6, y + height / 2), label, font=_PCT_FONT, fill=_LABEL_COLOR, anchor="lm")


def render_chart(title: str, data: dict[str, dict[str, tuple[int, int]]]) -> bytes:
    """data is dailylog.sport_tournament_win_loss's output:
    {sport: {tournament: (won, lost)}}. Sports are ordered by win%
    descending (best-performing first), tournaments within a sport the
    same way. A sport with exactly one tournament sharing its own name
    (e.g. "NBA" under sport "NBA" - a league with no real sub-tournament
    concept, see sport_tournament_win_loss's own fallback) skips the
    redundant sub-row entirely rather than showing the same bar twice."""
    def _sport_pct(item):
        _sport, tournaments = item
        return _win_pct(sum(w for w, _l in tournaments.values()), sum(l for _w, l in tournaments.values()))

    sports = sorted(data.items(), key=_sport_pct, reverse=True)

    def _visible_tournaments(sport: str, tournaments: dict[str, tuple[int, int]]) -> dict[str, tuple[int, int]]:
        if len(tournaments) == 1 and sport in tournaments:
            return {}
        return tournaments

    # Sport header rows are taller than tournament sub-rows (SPORT_BAR_HEIGHT
    # vs. SUB_BAR_HEIGHT) - confirmed live, treating every row as sub-row
    # height undercounted the total and clipped the last row off the bottom
    # of the image entirely once there were enough sports for that deficit
    # to add up.
    sub_row_count = sum(len(_visible_tournaments(sport, tournaments)) for sport, tournaments in sports)
    height = (
        TOP_PADDING + len(sports) * (SPORT_BAR_HEIGHT + SPORT_GAP) + sub_row_count * (SUB_BAR_HEIGHT + ROW_GAP)
        + BOTTOM_PADDING
    )

    img = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.text((WIDTH / 2, TOP_PADDING / 2), title, font=_TITLE_FONT, fill=_TITLE_COLOR, anchor="mm")

    y = TOP_PADDING
    for sport, tournaments in sports:
        sport_won = sum(w for w, _l in tournaments.values())
        sport_lost = sum(l for _w, l in tournaments.values())
        draw.text((SPORT_LABEL_WIDTH - 10, y + SPORT_BAR_HEIGHT / 2), sport, font=_LABEL_FONT, fill=_LABEL_COLOR, anchor="rm")
        _draw_bar(draw, SPORT_LABEL_WIDTH, y, _SPORT_BAR_WIDTH, SPORT_BAR_HEIGHT, sport_won, sport_lost)
        y += SPORT_BAR_HEIGHT + ROW_GAP

        visible = _visible_tournaments(sport, tournaments)
        for tournament, (won, lost) in sorted(visible.items(), key=lambda item: _win_pct(*item[1]), reverse=True):
            draw.text((SUB_LABEL_WIDTH - 10, y + SUB_BAR_HEIGHT / 2), tournament, font=_LEGEND_FONT, fill=_SUB_LABEL_COLOR, anchor="rm")
            _draw_bar(draw, SUB_LABEL_WIDTH, y, _SUB_BAR_WIDTH, SUB_BAR_HEIGHT, won, lost)
            y += SUB_BAR_HEIGHT + ROW_GAP

        y += SPORT_GAP - ROW_GAP

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
