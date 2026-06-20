"""Durable record of in-progress incidents (active lockdowns / raids).

If the bot restarts while a server is locked down, this is what lets it pick up
where it left off: it remembers that the lockdown is active, the original
per-channel ``send_messages`` overwrites needed to restore them, and the raid
timers so the auto-lift cooldown keeps counting.

Kept pure and dependency-free (JSON, atomic writes) so it can be unit tested.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Incident:
    guild_id: int
    lockdown_active: bool = False
    raid_active: bool = False
    raid_started_at: float = 0.0
    last_raid_signal: float = 0.0
    #: channel_id -> the @everyone ``send_messages`` value before lockdown
    #: (True / False / None), so unlock restores the exact prior state.
    channel_snapshot: Dict[int, Optional[bool]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "guild_id": self.guild_id,
            "lockdown_active": self.lockdown_active,
            "raid_active": self.raid_active,
            "raid_started_at": self.raid_started_at,
            "last_raid_signal": self.last_raid_signal,
            "channel_snapshot": {str(k): v for k, v in self.channel_snapshot.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Incident":
        snap = data.get("channel_snapshot") or {}
        return cls(
            guild_id=int(data["guild_id"]),
            lockdown_active=bool(data.get("lockdown_active", False)),
            raid_active=bool(data.get("raid_active", False)),
            raid_started_at=float(data.get("raid_started_at", 0.0)),
            last_raid_signal=float(data.get("last_raid_signal", 0.0)),
            channel_snapshot={int(k): v for k, v in snap.items()},
        )


class IncidentStore:
    def __init__(self, path: str = "incidents.json") -> None:
        self.path = path
        self._cache: Dict[int, Incident] = {}
        self.load()

    def load(self) -> None:
        self._cache = {}
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return  # corrupt store -> start clean rather than crash-loop
        for gid, data in raw.items():
            try:
                self._cache[int(gid)] = Incident.from_dict({**data, "guild_id": int(gid)})
            except (ValueError, TypeError, KeyError):
                continue

    def get(self, guild_id: int) -> Optional[Incident]:
        return self._cache.get(guild_id)

    def active_guild_ids(self) -> List[int]:
        return [gid for gid, inc in self._cache.items() if inc.lockdown_active]

    def set(self, incident: Incident) -> None:
        self._cache[incident.guild_id] = incident
        self.save()

    def clear(self, guild_id: int) -> None:
        if guild_id in self._cache:
            del self._cache[guild_id]
            self.save()

    def save(self) -> None:
        payload = {str(gid): inc.to_dict() for gid, inc in self._cache.items()}
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
