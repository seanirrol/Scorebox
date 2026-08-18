#!/usr/bin/env python3
"""One-off: resolves the bot's own effective permissions in a channel via
raw REST calls (no gateway login, safe to run alongside the live bot
process) - role union + @everyone/role/member overwrite precedence, same
manual resolution Discord's client applies. Usage:

    python3 scripts/check_channel_perms.py <channel_id>

Reads DISCORD_TOKEN from .env - never printed."""

import sys
import urllib.request
import urllib.error
import json
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

API = "https://discord.com/api/v10"

REQUIRED_BITS = {
    "View Channel": 1 << 10,
    "Send Messages": 1 << 11,
    "Embed Links": 1 << 14,
    "Attach Files": 1 << 15,
    "Read Message History": 1 << 16,
    "Add Reactions": 1 << 6,
    "Use External Emojis": 1 << 18,
}


def _get(path):
    # Cloudflare (in front of Discord's API) blocks urllib's default
    # "Python-urllib/3.x" User-Agent outright (HTTP 403, Cloudflare error
    # 1010) - a real header, not auth, fixes it.
    req = urllib.request.Request(
        f"{API}{path}",
        headers={
            "Authorization": f"Bot {config.DISCORD_TOKEN}",
            "User-Agent": "DiscordBot (https://github.com/seanirrol/Scorebox, 1.0)",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 scripts/check_channel_perms.py <channel_id>")
        sys.exit(1)
    channel_id = sys.argv[1]

    channel = _get(f"/channels/{channel_id}")
    guild_id = channel["guild_id"]
    overwrites = {ow["id"]: ow for ow in channel.get("permission_overwrites", [])}

    me = _get(f"/users/@me")
    bot_id = me["id"]
    member = _get(f"/guilds/{guild_id}/members/{bot_id}")
    role_ids = set(member.get("roles", [])) | {guild_id}  # guild_id doubles as @everyone's role id

    roles = _get(f"/guilds/{guild_id}/roles")
    base_allow = 0
    for role in roles:
        if role["id"] in role_ids:
            base_allow |= int(role["permissions"])

    perms = base_allow

    # @everyone overwrite first
    everyone_ow = overwrites.get(guild_id)
    if everyone_ow:
        perms &= ~int(everyone_ow["deny"])
        perms |= int(everyone_ow["allow"])

    # role overwrites (deny then allow, across every applicable role)
    role_deny = role_allow = 0
    for rid in role_ids:
        if rid == guild_id:
            continue
        ow = overwrites.get(rid)
        if ow:
            role_deny |= int(ow["deny"])
            role_allow |= int(ow["allow"])
    perms &= ~role_deny
    perms |= role_allow

    # member-specific overwrite last
    member_ow = overwrites.get(bot_id)
    if member_ow:
        perms &= ~int(member_ow["deny"])
        perms |= int(member_ow["allow"])

    print(f"Channel {channel_id} (guild {guild_id}) - bot permissions:")
    all_ok = True
    for name, bit in REQUIRED_BITS.items():
        ok = bool(perms & bit)
        all_ok &= ok
        print(f"  {'OK ' if ok else 'MISSING'}  {name}")
    print("ALL REQUIRED PERMISSIONS PRESENT" if all_ok else "MISSING ONE OR MORE REQUIRED PERMISSIONS")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"Discord API error {e.code}: {e.read().decode()}")
        sys.exit(1)
