"""The pure anti-raid decision engine.

Feed it domain events; it returns a list of :class:`Action` objects.  It holds
no Discord state, performs no I/O and reads no clock — every decision is a
deterministic function of the events and the per-guild config.

Detection layers
----------------
* **Mass-join**       : N joins inside a sliding window -> raid + lockdown.
* **Coordinated names**: a cluster of similar/identical usernames joining
  together -> raid (catches slow, scripted raids that stay under the rate).
* **Account-age gate** : new accounts are treated as raiders during a raid
  (and optionally gated outside one).
* **Message spam**     : per-user flood, self-duplicate, cross-user identical
  content, mention spam and link spam.
* **Anti-nuke**        : one actor performing many destructive audit-log
  actions (a compromised admin or rogue bot) gets stripped + alerted.
* **Lifecycle**        : ``tick`` auto-lifts raid/lockdown after a quiet
  cooldown.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Callable, Dict, List, Optional

from .actions import Action, ActionType, Severity
from .config import GuildConfig, RaidAction, SpamResponse
from .models import AuditEvent, JoinEvent, Member, MessageEvent
from .state import GuildState, StateStore, _prune_left

# Matches discord invite links and bare URLs (used for link-spam scoring).
_INVITE_RE = re.compile(
    r"(?:discord(?:\.gg|app\.com/invite|\.com/invite)/[A-Za-z0-9\-]+)", re.I
)
_URL_RE = re.compile(r"https?://[^\s]+", re.I)

# Cyrillic / Greek / look-alike letters mapped to their Latin twins, so a
# homoglyph raid ("Rаider" with a Cyrillic а) clusters with "Raider".
# NFKD (below) already folds full-width / accented forms; these are the
# cross-script confusables it does not.
_CONFUSABLES = {
    # Cyrillic (lowercase)
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "к": "k", "м": "m", "т": "t",
    "н": "h", "в": "b", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ґ": "r", "һ": "h",
    # Greek (lowercase)
    "α": "a", "ο": "o", "ε": "e", "ρ": "p", "τ": "t",
    "ν": "v", "χ": "x", "ι": "i", "κ": "k", "υ": "u",
    "η": "n", "β": "b",
    # misc symbol look-alikes
    "ı": "i",  # dotless i
}
_CONFUSABLE_TABLE = str.maketrans(_CONFUSABLES)

# Leet substitutions applied to *interior* characters only (after trailing /
# leading counter digits are removed), so "R4 id3r" -> "raider".
_LEET_TABLE = str.maketrans(
    {"4": "a", "3": "e", "1": "i", "0": "o", "5": "s", "7": "t",
     "$": "s", "@": "a", "8": "b", "9": "g", "6": "g"}
)


def normalize_name(name: str) -> str:
    """Collapse a username to its comparable skeleton.

    Designed so visually-identical names cluster together even when raiders
    use homoglyphs, full-width characters, accents, leetspeak or trailing
    counters.  ``Raider001`` / ``raider_002`` / ``RAIDER`` / ``Rаider`` (Cyrillic)
    / ``Ｒａｉｄｅｒ`` (full-width) / ``R4id3r`` all map to ``raider``.
    """
    # 1) compatibility-fold full-width/ligatures, then drop combining marks.
    decomposed = unicodedata.normalize("NFKD", name)
    no_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    # 2) case-fold, then map cross-script homoglyphs to Latin.
    folded = no_marks.lower().translate(_CONFUSABLE_TABLE)
    # 3) drop leading/trailing digit runs (join counters like Raider01).
    trimmed = re.sub(r"^\d+", "", re.sub(r"\d+$", "", folded))
    # 4) fold remaining interior leet characters to letters.
    leeted = trimmed.translate(_LEET_TABLE)
    # 5) keep only a-z.
    letters = re.sub(r"[^a-z]", "", leeted)
    return letters or folded  # fall back to the folded form if no a-z remain


def content_fingerprint(content: str) -> str:
    """Stable hash of normalised message content for duplicate detection."""
    squashed = re.sub(r"\s+", " ", content.strip().lower())
    return hashlib.sha1(squashed.encode("utf-8")).hexdigest()


class AntiRaidEngine:
    def __init__(
        self,
        config_provider: Optional[Callable[[int], GuildConfig]] = None,
        store: Optional[StateStore] = None,
    ) -> None:
        self._configs: Dict[int, GuildConfig] = {}
        self._config_provider = config_provider
        self.store = store or StateStore()
        self._name_pattern_cache: Dict[str, Optional["re.Pattern"]] = {}

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def set_config(self, config: GuildConfig) -> None:
        self._configs[config.guild_id] = config.validate()

    def config_for(self, guild_id: int) -> GuildConfig:
        cfg = self._configs.get(guild_id)
        if cfg is None:
            cfg = (
                self._config_provider(guild_id)
                if self._config_provider
                else GuildConfig(guild_id=guild_id)
            )
            self._configs[guild_id] = cfg
        return cfg

    # ------------------------------------------------------------------
    # exemption check — the single gate every enforcement path goes through
    # ------------------------------------------------------------------
    @staticmethod
    def is_exempt(member: Member, cfg: GuildConfig) -> bool:
        if member.is_guild_owner:
            return True
        if member.id in cfg.allowlist_users:
            return True
        if member.is_bot and member.id in cfg.allowlist_bots:
            return True
        if cfg.trusted_roles and any(r in cfg.trusted_roles for r in member.roles):
            return True
        return False

    # ==================================================================
    # JOINS
    # ==================================================================
    def process_join(self, event: JoinEvent) -> List[Action]:
        cfg = self.config_for(event.guild_id)
        if not cfg.enabled:
            return []
        state = self.store.get(event.guild_id)
        member = event.member
        now = event.timestamp

        # Trusted members never count toward raid thresholds and are never
        # actioned.  (A mod returning to the server shouldn't trip the alarm.)
        if self.is_exempt(member, cfg):
            return []

        # record + prune
        state.joins.append((now, member.id))
        state.recent_joiners.append((now, member))
        state.prune_joins(now - cfg.join_window_seconds)

        actions: List[Action] = []
        newly_triggered = False

        # ---- verification gate (front door for every new member) ------
        if cfg.verification_gate:
            actions.append(
                Action(
                    ActionType.GATE_MEMBER,
                    guild_id=event.guild_id,
                    target_id=member.id,
                    reason="Verification gate: new member must verify",
                    severity=Severity.LOW,
                )
            )

        # ---- mass-join detection --------------------------------------
        if not state.raid_active and len(state.joins) >= cfg.join_rate_threshold:
            newly_triggered = True
            reason = (
                f"Mass join: {len(state.joins)} joins in "
                f"{cfg.join_window_seconds:.0f}s (threshold {cfg.join_rate_threshold})"
            )
            self._begin_raid(state, now, actions, cfg, reason, Severity.CRITICAL)

        # ---- coordinated-username detection ---------------------------
        if not state.raid_active:
            cluster = self._name_cluster_size(state, member, cfg)
            if cluster >= cfg.similar_name_threshold:
                newly_triggered = True
                reason = (
                    f"Coordinated usernames: {cluster} members matching "
                    f"'{normalize_name(member.name)}*' joined together"
                )
                self._begin_raid(state, now, actions, cfg, reason, Severity.HIGH)

        if state.raid_active:
            state.last_raid_signal = now
            if newly_triggered:
                # Retroactively clean up everyone already inside the window.
                actions.extend(self._sweep_recent_joiners(state, cfg, now))
            else:
                # Ongoing raid: evaluate just this joiner.
                act = self._evaluate_raid_joiner(state, member, cfg, now)
                if act is not None:
                    actions.append(act)
        else:
            # Peace-time account-age gate (opt-in).
            if cfg.enforce_account_age_outside_raid:
                reasons = self._suspicion_reasons(member, cfg, now)
                if reasons:
                    actions.append(
                        Action(
                            ActionType.QUARANTINE_MEMBER,
                            guild_id=event.guild_id,
                            target_id=member.id,
                            reason="; ".join(reasons),
                            severity=Severity.LOW,
                        )
                    )

        # AutoMod-style username filter — acts on the name itself, regardless of
        # raid state. Skipped if the member was already actioned this raid (so a
        # raider isn't both banned and quarantined).
        if member.id not in state.actioned_members:
            bad = self._name_filter_match(member, cfg)
            if bad is not None:
                actions.append(
                    self._map_action(
                        member, cfg, cfg.name_filter_action,
                        f"Username filter: name matches /{bad}/", Severity.MEDIUM,
                    )
                )
                state.actioned_members.add(member.id)

        return actions

    def _begin_raid(
        self,
        state: GuildState,
        now: float,
        actions: List[Action],
        cfg: GuildConfig,
        reason: str,
        severity: Severity,
    ) -> None:
        state.raid_active = True
        state.raid_started_at = now
        state.last_raid_signal = now
        actions.append(
            Action(ActionType.ALERT, cfg.guild_id, reason=reason, severity=severity)
        )
        if not state.lockdown_active:
            state.lockdown_active = True
            actions.append(
                Action(
                    ActionType.ENABLE_LOCKDOWN,
                    cfg.guild_id,
                    reason=reason,
                    severity=severity,
                )
            )
            actions.append(
                Action(
                    ActionType.RAISE_VERIFICATION,
                    cfg.guild_id,
                    reason="Raid response: raising verification level",
                    severity=severity,
                )
            )

    def _name_cluster_size(
        self, state: GuildState, member: Member, cfg: GuildConfig
    ) -> int:
        skeleton = normalize_name(member.name)
        return sum(
            1
            for _, m in state.recent_joiners
            if normalize_name(m.name) == skeleton
        )

    def _suspicion_reasons(
        self, member: Member, cfg: GuildConfig, now: float
    ) -> List[str]:
        reasons: List[str] = []
        age_days = member.age_seconds(now) / 86400.0
        if age_days < cfg.min_account_age_days:
            reasons.append(
                f"account age {age_days:.2f}d < {cfg.min_account_age_days:.0f}d"
            )
        if cfg.flag_default_avatar and not member.has_avatar:
            reasons.append("no custom avatar")
        return reasons

    _ACTION_MAP = {
        RaidAction.BAN: ActionType.BAN_MEMBER,
        RaidAction.KICK: ActionType.KICK_MEMBER,
        RaidAction.QUARANTINE: ActionType.QUARANTINE_MEMBER,
    }

    def _map_action(
        self,
        member: Member,
        cfg: GuildConfig,
        kind: RaidAction,
        reason: str,
        severity: Severity,
    ) -> Action:
        if kind == RaidAction.ALERT:
            return Action(
                ActionType.ALERT, cfg.guild_id, target_id=member.id,
                reason=reason, severity=severity,
            )
        return Action(
            self._ACTION_MAP[kind], cfg.guild_id, target_id=member.id,
            reason=reason, severity=severity,
        )

    def _raid_action_for(
        self, member: Member, cfg: GuildConfig, reason: str
    ) -> Optional[Action]:
        if cfg.raid_action == RaidAction.ALERT:
            return self._map_action(
                member, cfg, RaidAction.ALERT,
                f"Raider (detect-only): {reason}", Severity.HIGH,
            )
        return self._map_action(
            member, cfg, cfg.raid_action, f"Raid response: {reason}", Severity.HIGH,
        )

    # ---- AutoMod-style username filter --------------------------------
    def _compiled_name_pattern(self, pattern: str):
        if pattern not in self._name_pattern_cache:
            try:
                self._name_pattern_cache[pattern] = re.compile(pattern, re.I)
            except re.error:
                self._name_pattern_cache[pattern] = None  # skip invalid patterns
        return self._name_pattern_cache[pattern]

    def _name_filter_match(self, member: Member, cfg: GuildConfig) -> Optional[str]:
        if not cfg.banned_name_patterns:
            return None
        skeleton = normalize_name(member.name)
        for pattern in cfg.banned_name_patterns:
            rx = self._compiled_name_pattern(pattern)
            if rx is not None and (rx.search(member.name) or rx.search(skeleton)):
                return pattern
        return None

    def _evaluate_raid_joiner(
        self, state: GuildState, member: Member, cfg: GuildConfig, now: float
    ) -> Optional[Action]:
        if member.id in state.actioned_members:
            return None
        reasons = self._suspicion_reasons(member, cfg, now)
        if cfg.raid_only_suspicious and not reasons:
            return None
        state.actioned_members.add(member.id)
        why = "; ".join(reasons) if reasons else "joined during active raid"
        return self._raid_action_for(member, cfg, why)

    def _sweep_recent_joiners(
        self, state: GuildState, cfg: GuildConfig, now: float
    ) -> List[Action]:
        actions: List[Action] = []
        for _, m in list(state.recent_joiners):
            if self.is_exempt(m, cfg):
                continue
            act = self._evaluate_raid_joiner(state, m, cfg, now)
            if act is not None:
                actions.append(act)
        return actions

    def _warn_action(self, event, uid, reason) -> Action:
        return Action(
            ActionType.WARN_MEMBER, guild_id=event.guild_id, target_id=uid,
            reason=reason, severity=Severity.LOW,
            meta={"channel_id": event.channel_id, "user_id": uid},
        )

    def _slowmode_action(self, cfg, state, event, uid, reason) -> Action:
        # record the channel so tick() can auto-clear it once spam subsides
        state.active_slowmodes[event.channel_id] = event.timestamp
        return Action(
            ActionType.SET_SLOWMODE, guild_id=event.guild_id, target_id=event.channel_id,
            reason=reason, severity=Severity.MEDIUM,
            duration=float(cfg.slowmode_seconds), meta={"user_id": uid},
        )

    def _timeout_action(self, cfg, event, uid, reason) -> Action:
        return Action(
            ActionType.TIMEOUT_MEMBER, guild_id=event.guild_id, target_id=uid,
            reason=reason, severity=Severity.HIGH, duration=cfg.spam_timeout_seconds,
        )

    def _quarantine_action(self, event, uid, reason) -> Action:
        return Action(
            ActionType.QUARANTINE_MEMBER, guild_id=event.guild_id, target_id=uid,
            reason=reason, severity=Severity.HIGH,
        )

    def _spam_enforcement(self, cfg, state, event, uid: int, reason: str) -> Action:
        """The per-spammer response.

        With escalation (default), repeat offenders within escalation_window
        climb the ladder: warn -> slowmode -> timeout -> quarantine. Without
        escalation, every trigger gets the flat spam_response. Discord slowmode
        is per-channel, so it rate-limits the spammed channel, not one member.
        """
        if not cfg.escalating_spam:
            if cfg.spam_response == SpamResponse.SLOWMODE:
                return self._slowmode_action(cfg, state, event, uid, reason)
            return self._timeout_action(cfg, event, uid, reason)

        offenses = state.user_offenses[uid]
        offenses.append((event.timestamp,))
        _prune_left(offenses, event.timestamp - cfg.escalation_window)
        n = len(offenses)
        warnings = cfg.spam_warnings
        # ladder: <=warnings warn(s) -> slowmode -> timeout -> quarantine
        if n <= warnings:
            return self._warn_action(event, uid, reason)
        if n == warnings + 1:
            return self._slowmode_action(cfg, state, event, uid, reason)
        if n == warnings + 2:
            return self._timeout_action(cfg, event, uid, reason)
        return self._quarantine_action(event, uid, reason)

    # ==================================================================
    # MESSAGES
    # ==================================================================
    def process_message(self, event: MessageEvent) -> List[Action]:
        cfg = self.config_for(event.guild_id)
        if not cfg.enabled:
            return []
        author = event.author
        if author.is_bot or self.is_exempt(author, cfg):
            return []

        state = self.store.get(event.guild_id)
        now = event.timestamp
        uid = author.id
        actions: List[Action] = []
        triggered_reasons: List[str] = []

        # ---- flood: messages-per-window -------------------------------
        msgs = state.user_msgs[uid]
        msgs.append((now, event.message_id))
        _prune_left(msgs, now - cfg.msg_rate_window)
        if len(msgs) >= cfg.msg_rate_threshold:
            triggered_reasons.append(
                f"flood: {len(msgs)} msgs in {cfg.msg_rate_window:.0f}s"
            )

        # ---- self-duplicate -------------------------------------------
        fp = content_fingerprint(event.content)
        if event.content.strip():
            dupes = state.user_dupes[uid]
            dupes.append((now, fp))
            _prune_left(dupes, now - cfg.duplicate_window)
            same = sum(1 for _, f in dupes if f == fp)
            if same >= cfg.duplicate_threshold:
                triggered_reasons.append(
                    f"duplicate: same message x{same} in {cfg.duplicate_window:.0f}s"
                )

            # ---- cross-user identical content (coordinated spam) ------
            authors = state.content_authors[fp]
            authors.append((now, uid, event.message_id))
            _prune_left(authors, now - cfg.cross_user_window)
            distinct = {u for _, u, _ in authors}
            if len(distinct) >= cfg.cross_user_threshold:
                reason = (
                    f"coordinated spam: identical message from "
                    f"{len(distinct)} users in {cfg.cross_user_window:.0f}s"
                )
                triggered_reasons.append(reason)
                # cross-user spam is a raid signal in its own right
                if not state.raid_active:
                    self._begin_raid(state, now, actions, cfg, reason, Severity.HIGH)
                state.last_raid_signal = now
                # Retroactively wipe the whole wave the first time we catch it:
                # delete every earlier identical message (+ timeout each author
                # only in TIMEOUT mode; in SLOWMODE the channel throttle below
                # covers the whole wave at once).
                if fp not in state.flagged_content:
                    state.flagged_content.add(fp)
                    for ts2, uid2, mid2 in list(authors):
                        if mid2 == event.message_id:
                            continue  # current msg handled by the block below
                        actions.append(
                            Action(
                                ActionType.DELETE_MESSAGE,
                                guild_id=event.guild_id,
                                target_id=mid2,
                                reason=reason,
                                severity=Severity.HIGH,
                                meta={"channel_id": event.channel_id, "user_id": uid2},
                            )
                        )
                        if not cfg.escalating_spam and cfg.spam_response == SpamResponse.TIMEOUT:
                            actions.append(
                                Action(
                                    ActionType.TIMEOUT_MEMBER,
                                    guild_id=event.guild_id,
                                    target_id=uid2,
                                    reason=reason,
                                    severity=Severity.HIGH,
                                    duration=cfg.spam_timeout_seconds,
                                )
                            )

        # ---- mention spam ---------------------------------------------
        mentions = event.mention_count + (50 if event.mentions_everyone else 0)
        if event.mention_count >= cfg.mention_threshold or event.mentions_everyone:
            triggered_reasons.append(
                f"mention spam: {event.mention_count} mentions"
                + (" + @everyone" if event.mentions_everyone else "")
                + " in one message"
            )
        mwin = state.user_mentions[uid]
        mwin.append((now, mentions))
        _prune_left(mwin, now - cfg.mention_window)
        total_mentions = sum(c for _, c in mwin)
        if total_mentions >= cfg.mention_window_threshold:
            triggered_reasons.append(
                f"mention spam: {total_mentions} mentions in {cfg.mention_window:.0f}s"
            )

        # ---- link spam ------------------------------------------------
        if _INVITE_RE.search(event.content) or _URL_RE.search(event.content):
            links = state.user_links[uid]
            links.append((now, 1))
            _prune_left(links, now - cfg.link_window)
            if len(links) >= cfg.link_threshold:
                triggered_reasons.append(
                    f"link spam: {len(links)} links in {cfg.link_window:.0f}s"
                )

        if triggered_reasons:
            why = "; ".join(triggered_reasons)
            actions.append(
                Action(
                    ActionType.DELETE_MESSAGE,
                    guild_id=event.guild_id,
                    target_id=event.message_id,
                    reason=why,
                    severity=Severity.MEDIUM,
                    meta={"channel_id": event.channel_id, "user_id": uid},
                )
            )
            actions.append(self._spam_enforcement(cfg, state, event, uid, why))
        return actions

    # ==================================================================
    # ANTI-NUKE (audit log)
    # ==================================================================
    def process_audit(self, event: AuditEvent) -> List[Action]:
        cfg = self.config_for(event.guild_id)
        if not cfg.enabled or not cfg.antinuke_enabled:
            return []
        actor = event.actor
        # Guild owner and explicitly-allowlisted actors/bots are exempt.
        if self.is_exempt(actor, cfg):
            return []

        state = self.store.get(event.guild_id)
        now = event.timestamp
        bucket = state.actor_audit[actor.id]
        bucket.append((now, event.action.value))
        _prune_left(bucket, now - cfg.nuke_window)

        if len(bucket) < cfg.nuke_threshold:
            return []
        if actor.id in state.stripped_actors:
            return []  # already neutralised

        state.stripped_actors.add(actor.id)
        kinds = ", ".join(sorted({a for _, a in bucket}))
        reason = (
            f"Anti-nuke: actor {actor.id} performed {len(bucket)} destructive "
            f"actions ({kinds}) in {cfg.nuke_window:.0f}s"
        )
        return [
            Action(
                ActionType.STRIP_ACTOR_PERMISSIONS,
                guild_id=event.guild_id,
                target_id=actor.id,
                reason=reason,
                severity=Severity.CRITICAL,
            ),
            Action(
                ActionType.ALERT,
                guild_id=event.guild_id,
                target_id=actor.id,
                reason=reason,
                severity=Severity.CRITICAL,
            ),
        ]

    # ==================================================================
    # LIFECYCLE
    # ==================================================================
    def tick(self, guild_id: int, now: float) -> List[Action]:
        """Auto-lift raid mode / lockdown once things have been quiet."""
        cfg = self.config_for(guild_id)
        state = self.store.get(guild_id)
        actions: List[Action] = []
        # Always reclaim stale tracking data, even when nothing is happening.
        state.housekeep(now, cfg)
        # Auto-clear channel slowmodes once the spam has subsided.
        for ch_id, last in list(state.active_slowmodes.items()):
            if now - last >= cfg.slowmode_cooldown:
                actions.append(
                    Action(
                        ActionType.SET_SLOWMODE,
                        guild_id,
                        target_id=ch_id,
                        reason="Spam subsided; slowmode cleared",
                        severity=Severity.INFO,
                        duration=0.0,
                    )
                )
                del state.active_slowmodes[ch_id]
        if not state.raid_active and not state.lockdown_active:
            return actions
        quiet_for = now - state.last_raid_signal
        if quiet_for >= cfg.raid_cooldown_seconds:
            if state.raid_active:
                state.clear_raid()
            if state.lockdown_active:
                state.lockdown_active = False
                actions.append(
                    Action(
                        ActionType.DISABLE_LOCKDOWN,
                        guild_id,
                        reason=f"No raid activity for {cfg.raid_cooldown_seconds:.0f}s",
                        severity=Severity.INFO,
                    )
                )
                actions.append(
                    Action(
                        ActionType.LOWER_VERIFICATION,
                        guild_id,
                        reason="Raid over: restoring verification level",
                        severity=Severity.INFO,
                    )
                )
                actions.append(
                    Action(
                        ActionType.ALERT,
                        guild_id,
                        reason="Raid appears to be over; lockdown lifted.",
                        severity=Severity.INFO,
                    )
                )
        return actions

    # ------------------------------------------------------------------
    def is_raid_active(self, guild_id: int) -> bool:
        return self.store.get(guild_id).raid_active

    def is_locked_down(self, guild_id: int) -> bool:
        return self.store.get(guild_id).lockdown_active
