#!/usr/bin/env python3
"""One-off: manually corrects a single already-graded leg from "lost" to
"void" - built for the Xiyu Wang vs Elina Svitolina "Win a Set" walkover
pick (Xiyu Wang won the match outright via walkover, but graded LOST
before scores365.is_walkover existed). Corrects three places:

  1. The parlay group's own leg entry + recomputed won/voided/lost/
     resolved_legs/total_legs counters (parlay_state.json).
  2. The matching dailylog entry (daily_log_state.json) - the same pick's
     own permanent /summary /performance /winlossgraph record.
  3. The live posted parlay summary card in Discord (edited in place via
     REST, no gateway login needed - safe alongside the running bot).

Not meant to be reused for a different leg without re-checking the ids
below - this is a targeted one-off, not a general "void any leg" tool.
"""

import json
import urllib.request
import urllib.error
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
import parlaytracker

GROUP_KEY = "1531085780959891508:25000"
LEG_ID = "settracker:1531085780959891508:4816209:win_a_set:Xiyu Wang"
CHANNEL_ID = "1531085780959891508"


def fix_parlay_state():
    with open("parlay_state.json") as f:
        data = json.load(f)
    group = data[GROUP_KEY]
    leg = group["legs"][LEG_ID]
    print("Before:", leg)
    leg["status"] = "void"
    leg["detail"] = "VOID"
    parlaytracker._recompute_group_counts(group)
    print("After:", leg)
    print("Group counters:", {k: group[k] for k in ("won", "voided", "lost", "resolved_legs", "total_legs")})
    with open("parlay_state.json", "w") as f:
        json.dump(data, f, indent=2)
    return group


def fix_dailylog():
    with open("daily_log_state.json") as f:
        data = json.load(f)
    matches = [
        (k, e) for k, e in data.items()
        if e.get("module") == "settracker" and "Xiyu Wang" in (e.get("label") or "") and e.get("status") == "lost"
    ]
    if len(matches) != 1:
        print(f"Expected exactly 1 matching dailylog entry, found {len(matches)} - not touching dailylog. Matches: {matches}")
        return
    key, entry = matches[0]
    print("Dailylog before:", entry)
    entry["status"] = "void"
    print("Dailylog after:", entry)
    with open("daily_log_state.json", "w") as f:
        json.dump(data, f, indent=2)


def fix_discord_card(group):
    embed = parlaytracker._build_summary_embed(group)
    message_id = group["summary_message_id"]
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages/{message_id}",
        method="PATCH",
        data=json.dumps({"embeds": [embed.to_dict()]}).encode(),
        headers={
            "Authorization": f"Bot {config.DISCORD_TOKEN}",
            "User-Agent": "DiscordBot (https://github.com/seanirrol/Scorebox, 1.0)",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        json.loads(resp.read())
    print(f"Edited Discord message {message_id} in channel {CHANNEL_ID}")


if __name__ == "__main__":
    group = fix_parlay_state()
    fix_dailylog()
    try:
        fix_discord_card(group)
    except urllib.error.HTTPError as e:
        print(f"Discord API error {e.code}: {e.read().decode()}")
