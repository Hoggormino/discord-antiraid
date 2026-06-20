# Deploying the bot (24/7)

The bot is a long-running gateway client (no web server / no exposed port). It
needs exactly one secret: `DISCORD_TOKEN`, set as an environment variable on the
host. **Never commit the token or paste it into chat** — set it in the host's
secrets UI directly.

A host-agnostic [`Dockerfile`](Dockerfile) is included, plus a [`Procfile`](Procfile)
for buildpack hosts.

## Recommended: Railway (easiest, ~$5/mo after trial credit)

1. Sign in at <https://railway.app> (you create the account / handle billing).
2. **New Project → Deploy from GitHub repo →** select `Hoggormino/discord-antiraid`.
   Railway detects the `Dockerfile` and builds it.
3. Open the service → **Variables** → add `DISCORD_TOKEN` = your token
   (do a final **Reset Token** in the Developer Portal and paste the new value
   here directly — this is the token that should never have been in a chat).
4. Deploy. Watch **Deploy Logs** for `Logged in as AntiRaid Bot#... (guilds=N)`.
5. *(Optional, for persistence across redeploys)* add a **Volume** mounted at
   `/data` and set `ANTIRAID_CONFIG_PATH=/data/guild_configs.json` and
   `ANTIRAID_INCIDENTS_PATH=/data/incidents.json`. Without a volume the per-guild
   config and active-lockdown state reset on each redeploy (usually fine).

## Alternative: Fly.io (small free allowance, CLI/Docker)

```bash
fly launch --no-deploy        # generates fly.toml from the Dockerfile
fly secrets set DISCORD_TOKEN=your-token
fly deploy
fly logs                      # look for the "Logged in as ..." line
```
For persistence: `fly volumes create antiraid_data --size 1`, mount it at `/data`
in `fly.toml`, and set the two `ANTIRAID_*` env vars as above.

## Alternative: a VPS you control (Docker or systemd)

Docker:
```bash
docker build -t antiraid .
docker run -d --restart=unless-stopped \
  -e DISCORD_TOKEN=your-token \
  -v /opt/antiraid:/data \
  -e ANTIRAID_CONFIG_PATH=/data/guild_configs.json \
  -e ANTIRAID_INCIDENTS_PATH=/data/incidents.json \
  --name antiraid antiraid
```
Bare systemd: create `/etc/antiraid.env` with `DISCORD_TOKEN=...` (chmod 600), a
venv with `pip install -r requirements.txt`, and a unit running
`python run.py` with `Restart=always` and `EnvironmentFile=/etc/antiraid.env`.

## Security reminder

The tokens used while wiring this up locally were exposed in chat and are
compromised. Before going live, **reset the token one final time and set it only
in the host's secrets** — that production token must never touch a chat, a
commit, or the image.
