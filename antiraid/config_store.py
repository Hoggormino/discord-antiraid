"""JSON-backed per-guild config persistence.

Kept dependency-free and separate from the Discord adapter so it can be unit
tested.  Writes are atomic (temp file + replace) to avoid corrupting the store
if the process dies mid-write.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Dict

from .config import GuildConfig


class ConfigStore:
    def __init__(self, path: str = "guild_configs.json") -> None:
        self.path = path
        self._cache: Dict[int, GuildConfig] = {}
        self.load()

    def load(self) -> None:
        self._cache = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable store -> start clean rather than crash-loop.
            return
        for gid, data in raw.items():
            try:
                cfg = GuildConfig.from_dict({**data, "guild_id": int(gid)}).validate()
                self._cache[int(gid)] = cfg
            except (ValueError, TypeError):
                continue  # skip a single bad entry, keep the rest

    def get(self, guild_id: int) -> GuildConfig:
        cfg = self._cache.get(guild_id)
        if cfg is None:
            cfg = GuildConfig(guild_id=guild_id)
            self._cache[guild_id] = cfg
        return cfg

    def set(self, config: GuildConfig) -> None:
        self._cache[config.guild_id] = config.validate()
        self.save()

    def save(self) -> None:
        payload = {str(gid): cfg.to_dict() for gid, cfg in self._cache.items()}
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
