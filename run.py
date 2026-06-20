"""Entry point for the live bot.

Usage:
    pip install -r requirements.txt
    cp .env.example .env   # then edit DISCORD_TOKEN
    python run.py
"""

from __future__ import annotations

import logging
import os
import sys


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass  # dotenv optional; env vars may be set another way

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN is not set (see .env.example).", file=sys.stderr)
        return 1

    # Imported here so the engine/tests never require discord.py.
    from antiraid.bot import AntiRaidBot, register_commands
    from antiraid.config_store import ConfigStore
    from antiraid.incident_store import IncidentStore

    store = ConfigStore(os.getenv("ANTIRAID_CONFIG_PATH", "guild_configs.json"))
    incidents = IncidentStore(os.getenv("ANTIRAID_INCIDENTS_PATH", "incidents.json"))
    bot = AntiRaidBot(store=store, incidents=incidents)
    register_commands(bot)
    bot.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
