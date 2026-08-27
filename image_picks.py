#!/usr/bin/env python3
"""
Reads a picks-slate GRAPHIC (an image, not text) and transcribes it into the
same bracket-tagged plain-text format picks.parse_picks_message already
understands - lets GreenFox's image-based "Today's Free Plays" graphics get
auto-tracked exactly like a text message would, without a from-scratch
image-specific parser. Uses Claude's own vision (the Anthropic API) to do
the actual reading; this module's job is just the prompt and cleaning up
the response into something parse_picks_message can consume directly.

Text and images can arrive in the same message - bot.py's on_message
appends this module's output to message.content before ever calling
parse_picks_message, so a slate split across a caption and a graphic still
tracks as one combined message.
"""

import asyncio
import logging
from typing import Optional

import anthropic

import config
import picks

log = logging.getLogger("scorebox.image_picks")

_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY) if config.ANTHROPIC_API_KEY else None

# Every section header picks.py's own _HEADER_SPORT_MAP recognizes - kept
# in sync manually (small, stable list) rather than importing picks.py's
# private map directly, so this module stays a thin, replaceable adapter
# in front of the parser rather than reaching into its internals.
_SECTION_EXAMPLES = "MLB, NBA, WNBA, NFL, NHL, Soccer, Tennis, Rugby, Volleyball, UFC, Boxing, KBO, Dota 2, CS2"

_PROMPT = f"""You are transcribing a sports-picks graphic into plain text lines a downstream parser will read. Output ONLY the transcribed lines - no commentary, no markdown, no code fences, no blank lines.

Every single line you output MUST start with a sport name wrapped in literal square brackets, immediately followed by the pick text on the SAME line. This is a strict, non-negotiable format - never put the category on its own line, never omit the brackets, even if the image itself shows the sport/category as a separate heading above a list of picks.

Correct:
[MLB] Cincinnati Reds F5 ML
[WNBA] Atlanta Dream ML

Wrong (never do this):
MLB
Cincinnati Reds F5 ML

Wrong (never do this either):
MLB Cincinnati Reds F5 ML

SportCategory (the text inside the brackets) must be one of: {_SECTION_EXAMPLES} (use whichever this image's own section/sport actually is - if the image groups several picks under one heading like "MLB", repeat "[MLB]" on every one of those picks' own lines, not just the first).

Use EXACTLY these pick-text wordings, matching whatever bet each line in the image shows:
- Moneyline: "Team Name ML"
- First-5-innings moneyline (baseball only): "Team Name F5 ML"
- Team spread/handicap: "Team Name -3.5" or "Team Name +3.5"
- A team's OWN total (not the combined game total): "Team Name Over 3.5" or "Team Name Under 3.5"
- The COMBINED game total (both teams together): "Team A vs Team B - Over 9.5" or "Team A vs Team B - Under 9.5"
- A player prop: "Player Name Over 21.5 Points" (swap in the actual stat name: Points, Rebounds, Assists, Strikeouts, Total Bases, Runs, Goals, etc.)
- Tennis win-a-set: "Player Name to Win a Set"
- Tennis games handicap: "Player Name +5.5 Games"

Rules:
- Use each team/player's real, full name (e.g. "Tampa Bay Rays" not "Tampa Rays", "San Francisco Giants" not "SF Giants") - correct obvious abbreviations to the real name.
- Ignore decorative-only text: titles, section dividers, logos, watermarks, branding, footers.
- Ignore odds, win probabilities, confidence percentages, and "(ALT LINE)"/"(Incl. Overtime)" style annotations - do not include them in the output line.
- If a line's bet type doesn't match any wording above, transcribe it as literally and simply as you can in the same "[Category] Team/Player + market" shape rather than omitting it.
- If you cannot read a line with reasonable confidence, skip it entirely rather than guessing.

Before finishing, check every line you are about to output starts with "[" - if any line doesn't, fix it before responding.
"""


class ImagePicksError(Exception):
    pass


def _normalize_line(line: str) -> str:
    """Defensive safety net for when the model drops the bracket tag
    despite the prompt's explicit instruction not to (confirmed live on
    a real test image: output "MLB Cincinnati Reds F5 ML" with no
    brackets at all, which parse_picks_message silently can't use at
    all). If a line starts with a recognized section keyword (picks.py's
    own _HEADER_SPORT_MAP - tries the 2-word prefix first so "Dota 2 ..."
    matches before falling back to just "Dota"), wraps it in brackets
    instead of silently losing that pick."""
    line = line.strip()
    if not line or line.startswith("["):
        return line
    words = line.split(" ")
    for prefix_len in (2, 1):
        if len(words) <= prefix_len:
            continue
        prefix, rest = " ".join(words[:prefix_len]), " ".join(words[prefix_len:])
        if prefix.lower() in picks._HEADER_SPORT_MAP and rest:
            return f"[{prefix}] {rest}"
    return line


def _sniff_media_type(image_bytes: bytes, fallback: str) -> str:
    """Anthropic's vision API validates the declared media_type against the
    image bytes' own actual format and rejects a mismatch outright with a
    400 - confirmed live, Discord's own attachment.content_type reported
    "image/jpeg" for a real .png upload (filename and all), and every
    single image from that source failed to transcribe as a result, with
    no way to tell from the botlog alone that this was the cause rather
    than a missing/broken API key. Trusts the bytes' own magic number over
    whatever Discord claims; only falls back to Discord's value for a
    format this doesn't recognize (Anthropic's API only supports jpeg/png/
    gif/webp anyway, so an unrecognized magic number would fail either
    way)."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _extract_sync(image_bytes: bytes, media_type: str) -> str:
    import base64

    response = _client.messages.create(
        model=config.IMAGE_PICKS_MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


async def extract_picks_text(image_bytes: bytes, media_type: str) -> Optional[str]:
    """None if ANTHROPIC_API_KEY isn't configured (image-based picks are
    silently skipped, not an error - see bot.py's on_message) or the API
    call itself fails; "" is a valid result (the model found nothing
    parseable in the image, distinct from a failure)."""
    if _client is None:
        return None
    media_type = _sniff_media_type(image_bytes, media_type)
    try:
        raw_text = await asyncio.to_thread(_extract_sync, image_bytes, media_type)
    except anthropic.APIError as e:
        log.warning("Image picks extraction failed: %s", e)
        return None
    return "\n".join(_normalize_line(line) for line in raw_text.splitlines())
