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

## Free forever: Oracle Cloud Always Free (best $0 option)

Oracle Cloud's **Always Free** tier gives a genuinely free-forever VM (Ampere
ARM, generous specs) that runs this image 24/7. Signup needs a credit card for
identity verification only — Always Free resources are never charged.

1. Create an **Always Free** account at <https://www.oracle.com/cloud/free/>,
   launch a VM (Ubuntu, **Ampere A1** shape, within the Always Free allowance),
   allow SSH, and connect to it.
2. Install Docker, clone, and run the container — the token lives in a
   locked-down file, never in shell history:

```bash
sudo apt update && sudo apt install -y docker.io git
sudo systemctl enable --now docker
git clone https://github.com/Hoggormino/discord-antiraid.git && cd discord-antiraid

echo "DISCORD_TOKEN=your-freshly-reset-token" > antiraid.env && chmod 600 antiraid.env

sudo docker build -t antiraid .
sudo docker run -d --name antiraid --restart=unless-stopped \
  --env-file antiraid.env \
  -e ANTIRAID_CONFIG_PATH=/data/guild_configs.json \
  -e ANTIRAID_INCIDENTS_PATH=/data/incidents.json \
  -v /opt/antiraid-data:/data \
  antiraid

sudo docker logs -f antiraid    # look for "Logged in as AntiRaid Bot#... (guilds=N)"
```

**Update later:**
```bash
cd discord-antiraid && git pull
sudo docker build -t antiraid .
sudo docker rm -f antiraid
# then re-run the `docker run ...` command above
```
The `/opt/antiraid-data` host folder is the persistent `/data` volume — per-guild
config and active-lockdown state survive restarts and rebuilds. `--restart=unless-stopped`
brings the bot back automatically after a crash or VM reboot.

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
