#!/usr/bin/env python3
"""Configuration loaded from environment variables."""

import os
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# Used by image_picks.py to read a picks-slate graphic (an image, not text)
# via Claude's vision - unset means image-based picks messages are silently
# skipped (only their text content, if any, still gets parsed normally).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IMAGE_PICKS_MODEL = os.environ.get("IMAGE_PICKS_MODEL", "claude-sonnet-4-6")

# How often (seconds) a tracked match's embed is refreshed.
UPDATE_INTERVAL_SECONDS = int(os.environ.get("UPDATE_INTERVAL_SECONDS", "30"))

# Safety cap so a match that never reports "finished" doesn't poll forever.
MAX_TRACK_HOURS = int(os.environ.get("MAX_TRACK_HOURS", "6"))

# How long a postponed/canceled event is given to publish a new schedule
# before its pick is voided outright - a rain delay or a same-day
# reschedule shouldn't torch a pick the instant ESPN marks the event
# postponed, but a game that's still sitting postponed after a full day is
# reliably dead.
POSTPONED_VOID_HOURS = int(os.environ.get("POSTPONED_VOID_HOURS", "24"))

# If set, all commands are restricted to these channels only (comma-separated,
# empty = no restriction).
_allowed_channel_ids = os.environ.get("ALLOWED_CHANNEL_ID", "").strip()
ALLOWED_CHANNEL_IDS = (
    {int(cid.strip()) for cid in _allowed_channel_ids.split(",") if cid.strip()}
    if _allowed_channel_ids
    else None
)

# Maps each picks channel to its own target scores channel - messages posted
# in a mapped channel are auto-parsed for picks, which get tracked
# automatically (via the same logic as /track/.../playerprops) and posted
# into that specific target channel. The bot can post cross-server as long
# as it's a member of both, so this also covers e.g. a test-server picks
# channel that posts into the live server's scores channel, alongside a
# separate pair fully contained within the test server. Format (comma-
# separated "picks_id:target_id" pairs):
#   PICKS_CHANNEL_MAP=111111111111111111:222222222222222222,333...:444...
_picks_channel_map_raw = os.environ.get("PICKS_CHANNEL_MAP", "").strip()
PICKS_CHANNEL_MAP: dict[int, int] = {}
for _pair in _picks_channel_map_raw.split(","):
    _pair = _pair.strip()
    if not _pair:
        continue
    _picks_id, _, _target_id = _pair.partition(":")
    if _picks_id.strip() and _target_id.strip():
        PICKS_CHANNEL_MAP[int(_picks_id.strip())] = int(_target_id.strip())

# /summary is restricted to server admins (guild_permissions.administrator)
# plus these specific user IDs, comma-separated - lets a non-admin (e.g. in
# a server they don't administer) still use it without granting them admin
# outright. Empty = admins only.
_summary_allowed_user_ids = os.environ.get("SUMMARY_ALLOWED_USER_IDS", "").strip()
SUMMARY_ALLOWED_USER_IDS: set[int] = {
    int(uid.strip()) for uid in _summary_allowed_user_ids.split(",") if uid.strip()
}

class ParlayRoute(NamedTuple):
    scores_channel_id: int
    post_channel_id: int


# Lets /parlay's summary card post to a different channel than the one its
# legs' own score cards live in - e.g. a dedicated "parlays" channel kept
# separate from the noisier scores channel. Legs are still only ever
# matched against scores_channel_id (that's where tracker.py/
# doublechancetracker.py/settracker.py actually post their cards -
# unaffected by this setting); only the summary card's own posting
# location is redirected. /parlay can be invoked from EITHER channel in a
# pair - both are keyed to the same ParlayRoute below so the lookup is a
# single dict access regardless of which one was used. A channel with no
# pair configured here keeps behaving exactly as before (summary posts
# wherever /parlay was invoked, gated by ALLOWED_CHANNEL_ID as usual).
# Format (comma-separated "scores_id:post_id" pairs):
#   PARLAY_ROUTES=111111111111111111:222222222222222222
_parlay_routes_raw = os.environ.get("PARLAY_ROUTES", "").strip()
PARLAY_ROUTES: dict[int, ParlayRoute] = {}
for _proute in _parlay_routes_raw.split(","):
    _proute = _proute.strip()
    if not _proute:
        continue
    _scores_id, _, _post_id = _proute.partition(":")
    if _scores_id.strip() and _post_id.strip():
        _route = ParlayRoute(int(_scores_id.strip()), int(_post_id.strip()))
        PARLAY_ROUTES[_route.scores_channel_id] = _route
        PARLAY_ROUTES[_route.post_channel_id] = _route


class SummaryRoute(NamedTuple):
    origins: tuple[int, ...]
    post_channel_id: int


# Groups one or more picks-source channels' reports into a single combined
# /summary report, invokable from (and previewed in) one "invoke" channel per
# route, then posted to that route's post_channel_id - usually the same
# channel, but not necessarily (e.g. two channels that should both be able to
# run/preview /summary while every actual post lands in one shared channel).
# /summary refuses to run in any channel that isn't an invoke channel here -
# there's no separate "which channels can this command run in" setting the
# way ALLOWED_CHANNEL_ID works for every other command. A route's post
# channel can be its own invoke channel (a picks channel that reports on
# itself). Format (comma-separated "origin1|origin2|...:invoke[:post]"
# routes - post defaults to invoke when omitted):
#   SUMMARY_ROUTES=111:111,222|333:222,222|333:444:555
_summary_routes_raw = os.environ.get("SUMMARY_ROUTES", "").strip()
SUMMARY_ROUTES: dict[int, SummaryRoute] = {}
for _route in _summary_routes_raw.split(","):
    _route = _route.strip()
    if not _route:
        continue
    _route_parts = _route.split(":")
    if len(_route_parts) == 2:
        _origins_part, _invoke_part = _route_parts
        _post_part = _invoke_part
    elif len(_route_parts) == 3:
        _origins_part, _invoke_part, _post_part = _route_parts
    else:
        continue
    _invoke_part, _post_part = _invoke_part.strip(), _post_part.strip()
    _origin_ids = tuple(int(o.strip()) for o in _origins_part.split("|") if o.strip())
    if _invoke_part and _post_part and _origin_ids:
        SUMMARY_ROUTES[int(_invoke_part)] = SummaryRoute(_origin_ids, int(_post_part))
