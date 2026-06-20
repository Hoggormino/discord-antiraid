"""Per-guild configuration with safe, opinionated defaults.

Every threshold is tunable per guild and persisted to JSON by the adapter.
Defaults are chosen to be aggressive enough to stop a real raid but lenient
enough not to nuke a healthy, active community.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Dict, FrozenSet


class RaidAction(str, Enum):
    """What to do with members caught in a raid."""

    BAN = "ban"
    KICK = "kick"
    QUARANTINE = "quarantine"
    ALERT = "alert"  # detect only, take no destructive action


class SpamResponse(str, Enum):
    """How to respond to a member caught spamming."""

    SLOWMODE = "slowmode"  # delete the spam + put the channel in slowmode
    TIMEOUT = "timeout"    # delete the spam + time the member out


@dataclass
class GuildConfig:
    guild_id: int = 0
    enabled: bool = True

    # ---- exemptions (checked before any enforcement) -------------------
    #: role ids that are fully trusted (mods, boosters, verified, ...).
    trusted_roles: FrozenSet[int] = field(default_factory=frozenset)
    #: user ids that bypass all enforcement.
    allowlist_users: FrozenSet[int] = field(default_factory=frozenset)
    #: bot ids allowed to perform bulk admin actions (anti-nuke bypass).
    allowlist_bots: FrozenSet[int] = field(default_factory=frozenset)

    # ---- mass-join / raid detection -----------------------------------
    join_rate_threshold: int = 8          # joins ...
    join_window_seconds: float = 10.0     # ... within this window => raid
    #: similar/duplicate usernames joining together (coordinated raid).
    similar_name_threshold: int = 4
    #: account younger than this (days) is "new".
    min_account_age_days: float = 7.0
    #: members with no custom avatar are mildly suspicious.
    flag_default_avatar: bool = True

    #: what to do with raiders once a raid is active.
    raid_action: RaidAction = RaidAction.QUARANTINE
    #: if True, only *suspicious* joiners are actioned during a raid;
    #: if False, everyone who joined inside the raid window is actioned.
    raid_only_suspicious: bool = True
    #: how long the guild stays in raid mode with no new signals (seconds).
    raid_cooldown_seconds: float = 120.0
    #: outside an active raid, also enforce the account-age gate on joiners.
    enforce_account_age_outside_raid: bool = False

    # ---- message spam --------------------------------------------------
    msg_rate_threshold: int = 6           # messages ...
    msg_rate_window: float = 5.0          # ... within this window => spam
    duplicate_threshold: int = 4          # same content from one user
    duplicate_window: float = 12.0
    #: identical content from N distinct users => coordinated spam.
    cross_user_threshold: int = 5
    cross_user_window: float = 10.0
    mention_threshold: int = 6            # mentions in a single message
    mention_window_threshold: int = 12    # cumulative mentions per user
    mention_window: float = 15.0
    link_threshold: int = 3               # invite/url messages per user
    link_window: float = 20.0
    #: when True, repeat offenders escalate warn -> slowmode -> timeout ->
    #: quarantine (counted within escalation_window). When False, every spam
    #: trigger gets the flat spam_response below.
    escalating_spam: bool = True
    escalation_window: float = 300.0
    #: warnings given before escalation begins (0 = skip straight to slowmode).
    spam_warnings: int = 2
    #: flat response when escalating_spam is False.
    spam_response: SpamResponse = SpamResponse.SLOWMODE
    #: channel slowmode (seconds) applied for the slowmode response.
    slowmode_seconds: int = 10
    #: auto-clear a channel's slowmode after this many quiet seconds.
    slowmode_cooldown: float = 120.0
    #: timeout duration (seconds) applied for the timeout response.
    spam_timeout_seconds: float = 600.0

    # ---- username filtering (AutoMod-style, acts on the name itself) ---
    #: regex patterns (case-insensitive); a joiner whose name OR normalised
    #: skeleton matches any pattern is actioned immediately, no raid required.
    banned_name_patterns: FrozenSet[str] = field(default_factory=frozenset)
    name_filter_action: RaidAction = RaidAction.QUARANTINE

    # ---- anti-nuke (compromised admin / rogue bot) --------------------
    nuke_threshold: int = 4               # destructive audit actions ...
    nuke_window: float = 10.0             # ... within this window by one actor
    antinuke_enabled: bool = True

    # ---- alerting ------------------------------------------------------
    alert_channel_id: int = 0

    # ---------------------------------------------------------------
    # (de)serialisation helpers
    # ---------------------------------------------------------------
    _SET_FIELDS = ("trusted_roles", "allowlist_users", "allowlist_bots")
    _STR_SET_FIELDS = ("banned_name_patterns",)
    #: enum field name -> its enum class, for (de)serialisation and `!ar set`.
    _ENUM_FIELDS = {
        "raid_action": RaidAction,
        "name_filter_action": RaidAction,
        "spam_response": SpamResponse,
    }

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        for key in self._SET_FIELDS + self._STR_SET_FIELDS:
            data[key] = sorted(getattr(self, key))
        for key in self._ENUM_FIELDS:
            data[key] = getattr(self, key).value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GuildConfig":
        known = {f.name for f in fields(cls)}
        clean: Dict[str, Any] = {k: v for k, v in data.items() if k in known}
        for key in cls._SET_FIELDS:
            if key in clean and clean[key] is not None:
                clean[key] = frozenset(int(x) for x in clean[key])
        for key in cls._STR_SET_FIELDS:
            if key in clean and clean[key] is not None:
                clean[key] = frozenset(str(x) for x in clean[key])
        for key, enum_cls in cls._ENUM_FIELDS.items():
            if key in clean and clean[key] is not None:
                clean[key] = enum_cls(clean[key])
        return cls(**clean)

    def validate(self) -> "GuildConfig":
        """Guard against foot-gun configs that would disable protection."""
        positive_ints = {
            "join_rate_threshold": self.join_rate_threshold,
            "similar_name_threshold": self.similar_name_threshold,
            "msg_rate_threshold": self.msg_rate_threshold,
            "duplicate_threshold": self.duplicate_threshold,
            "cross_user_threshold": self.cross_user_threshold,
            "mention_threshold": self.mention_threshold,
            "nuke_threshold": self.nuke_threshold,
        }
        for name, value in positive_ints.items():
            if value < 1:
                raise ValueError(f"{name} must be >= 1, got {value}")
        positive_windows = {
            "join_window_seconds": self.join_window_seconds,
            "msg_rate_window": self.msg_rate_window,
            "nuke_window": self.nuke_window,
        }
        for name, value in positive_windows.items():
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        return self
