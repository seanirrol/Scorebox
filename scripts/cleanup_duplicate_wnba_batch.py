#!/usr/bin/env python3
"""One-off: cleans up the duplicate-posting incident in channel
1536429372217761833 (2026-08-19 ~22:44-23:11 EDT) caused by the on_message
race condition fixed in commit 423b629. Untracks the 12 picks that actually
won their registration race (dailylog void + removed from their tracker's
own state file) and deletes all 27 posted score cards (12 real + 15
orphaned duplicate copies that never had a tracker behind them at all) so
the picks can be reposted cleanly. Confirmed none of the 12 are part of an
active parlay group before writing this.

Run once, then restart the service so the removed entries aren't resumed
and any lingering in-memory tracker tasks for them are gone.
"""

import sys
import os
import json
import urllib.request
import urllib.error
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import dailylog

API = "https://discord.com/api/v10"
CHANNEL_ID = 1536429372217761833

# (module, state_file, track_key)
LIVE_ENTRIES = [
    ("tracker", "tracks_state.json", "1536429372217761833:4614731:ml:Tampa Bay Rays"),
    ("tracker", "tracks_state.json", "1536429372217761833:4714917:total:under:42.5"),
    ("tracker", "tracks_state.json", "1536429372217761833:4714918:tt:Chargers:spread:3.5"),
    ("tracker", "tracks_state.json", "1536429372217761833:4555907:total:over:8"),
    ("proptracker", "props_state.json", "1536429372217761833:401873285:4383351:YDS:RTG:over:104.5"),
    ("proptracker", "props_state.json", "1536429372217761833:401873286:4837248:YDS:RTG:over:74.5"),
    ("proptracker", "props_state.json", "1536429372217761833:401816602:4345076:K:IP:over:4.5"),
    ("proptracker", "props_state.json", "1536429372217761833:401816609:32796:K:IP:over:4.5"),
    ("proptracker", "props_state.json", "1536429372217761833:401816602:32801:__computed__:total_bases:over:0.5"),
    ("inningtracker", "innings_state.json", "1536429372217761833:401816603:NRFI"),
    ("f5tracker", "f5_state.json", "1536429372217761833:4614842:ml:Cleveland Guardians"),
    ("f5tracker", "f5_state.json", "1536429372217761833:4614728:ml:Texas Rangers"),
]

ALL_MESSAGE_IDS = [
    1539837698880315395, 1539837261900816464, 1539836827282841641, 1539836463615844444,
    1539836175831924866, 1539835591330365441, 1539835091373531208, 1539834655212314718,
    1539834291393925125, 1539833855056420865, 1539833488532967435, 1539832694328922205,
    1539832331924144210, 1539832188676083783, 1539831824790982659, 1539831460897234993,
    1539831170827689987, 1539830662901661719, 1539830228480827394, 1539829867015831616,
    1539829361312796774, 1539829358825443452, 1539828778618851400, 1539828777419415563,
    1539828127629582356, 1539827622056304640, 1539827620487893105,
]


def _delete_message(message_id: int):
    req = urllib.request.Request(
        f"{API}/channels/{CHANNEL_ID}/messages/{message_id}",
        method="DELETE",
        headers={
            "Authorization": f"Bot {config.DISCORD_TOKEN}",
            "User-Agent": "DiscordBot (https://github.com/seanirrol/Scorebox, 1.0)",
        },
    )
    try:
        urllib.request.urlopen(req)
        print(f"Deleted {message_id}")
    except urllib.error.HTTPError as e:
        print(f"Failed to delete {message_id}: {e.code} {e.read().decode()}")


def main():
    for module, state_file, key in LIVE_ENTRIES:
        dailylog.record_result(CHANNEL_ID, module, key, "void", "Manually untracked (duplicate-posting cleanup)")
        data = json.load(open(state_file))
        removed = data.pop(key, None)
        json.dump(data, open(state_file, "w"), indent=2)
        print(f"Removed {module}:{key} from {state_file} (existed: {removed is not None})")

    for message_id in ALL_MESSAGE_IDS:
        _delete_message(message_id)
        time.sleep(0.3)


if __name__ == "__main__":
    main()
