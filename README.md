# Scorebox

A Discord bot that posts live sports scores. Ask it about a team and it'll
find its live or today's match and either reply once, or post a message that
keeps updating itself with the live score until the match ends.

## Commands

- `/score team:<name>` — one-off lookup of a team's live/today match.
- `/track team:<name>` — posts a live-updating embed that refreshes
  automatically (every `UPDATE_INTERVAL_SECONDS`, default 60s) until the
  match finishes.
- `/tracked` — lists game IDs currently being auto-updated in the channel.
- `/untrack game_id:<id>` — stops auto-updating a specific match.

## Data source

Scores come from **365scores.com's public JSON API** (`webws.365scores.com`)
— the same unauthenticated endpoint their own website calls client-side, no
API key or signup required. It covers soccer, basketball, tennis, hockey,
NFL, baseball, volleyball, and rugby in one consistent shape and reflects
live state (not just a periodic snapshot), which is why it's used here
instead of a paid provider or a rate-limited free tier.

A team is matched fuzzily against the current games list (case/punctuation
-insensitive, tolerant of partial names) across all supported sports at once,
so `/score team:Lakers` doesn't need you to specify the sport.

## Setup

1. Create a Discord application + bot at
   https://discord.com/developers/applications, copy its token, and invite
   it to your server with the `bot` and `applications.commands` scopes
   (Send Messages, Embed Links permissions).
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`.
4. Run it:
   ```
   python bot.py
   ```

Slash commands sync automatically on startup; it can take a few minutes for
Discord to show them the first time.

## Deploying

A `Dockerfile` is included for deploying to Railway, Render, Fly.io, or any
container host. Set `DISCORD_TOKEN` (and optionally `UPDATE_INTERVAL_SECONDS`,
`MAX_TRACK_HOURS`) as environment variables on the host — don't commit your
`.env` file.

## Known limitations

- 365scores' "current games" endpoint only returns live games plus
  today's/recently-finished schedule — a match from several days ago or
  scheduled further in the future won't be found.
- Team search runs across up to 8 sport categories per lookup when no sport
  is specified; each is a separate (cached 8s) HTTP call, so a cold lookup
  can take a second or two.
- This is an unofficial, undocumented API (reverse-engineered from
  365scores' own frontend traffic) — it could change or start blocking
  requests without notice. There's no official fallback wired in; if it
  breaks, `scores365.py` is the one file to look at.
