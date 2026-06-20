"""Discord adapter: wires the pure engine to the gateway and executes actions.

This is the *only* layer that performs side effects, and every destructive
action goes through :meth:`_can_act` first (hierarchy + permission + self/owner
guards) so the bot can never be tricked into banning the wrong member or acting
above its own role.

Requires ``discord.py`` (see requirements.txt).  The engine and the test-suite
do **not** import this module, so they run without it installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, List, Optional, Tuple

try:
    import discord
    from discord.ext import commands, tasks
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only at runtime
    raise SystemExit(
        "discord.py is not installed. Run:  pip install -r requirements.txt"
    ) from exc

from .actions import Action, ActionType, Severity
from .config import GuildConfig, RaidAction
from .config_store import ConfigStore
from .engine import AntiRaidEngine
from .executor import TokenBucket, plan_actions
from .incident_store import Incident, IncidentStore
from .models import AuditAction, AuditEvent, JoinEvent, Member, MessageEvent

log = logging.getLogger("antiraid")

QUARANTINE_ROLE_NAME = "Quarantined"

#: permissions that make a role "dangerous" for anti-nuke stripping.
_DANGEROUS_PERMS = (
    "administrator",
    "ban_members",
    "kick_members",
    "manage_guild",
    "manage_channels",
    "manage_roles",
    "manage_webhooks",
    "mention_everyone",
)

_AUDIT_MAP = {
    "channel_delete": AuditAction.CHANNEL_DELETE,
    "channel_create": AuditAction.CHANNEL_CREATE,
    "role_delete": AuditAction.ROLE_DELETE,
    "role_create": AuditAction.ROLE_CREATE,
    "ban": AuditAction.BAN,
    "kick": AuditAction.KICK,
    "webhook_create": AuditAction.WEBHOOK_CREATE,
    "member_role_update": AuditAction.MEMBER_ROLE_UPDATE,
}


def to_domain_member(obj, guild: "discord.Guild") -> Member:
    """Translate a discord member/user into the engine's :class:`Member`."""
    roles = tuple(r.id for r in getattr(obj, "roles", []) if r.id != guild.id)
    return Member(
        id=obj.id,
        name=obj.name,
        created_at=obj.created_at.timestamp(),
        is_bot=bool(getattr(obj, "bot", False)),
        roles=roles,
        has_avatar=getattr(obj, "avatar", None) is not None,
        is_guild_owner=(guild.owner_id == obj.id),
    )


class AntiRaidBot(commands.AutoShardedBot):
    """Auto-sharded so the bot keeps working past Discord's 2,500-guild
    sharding requirement without code changes."""

    def __init__(self, store: ConfigStore, action_rate: float = 25.0,
                 incidents: Optional[IncidentStore] = None, **kwargs):
        intents = discord.Intents.default()
        intents.members = True          # member joins
        intents.message_content = True  # spam/link content
        intents.moderation = True       # audit-log entries (anti-nuke)
        super().__init__(command_prefix="!ar ", intents=intents,
                         help_command=None, **kwargs)
        self.store = store
        self.engine = AntiRaidEngine(config_provider=store.get)
        self.incidents = incidents if incidents is not None else IncidentStore()
        self._lockdown_snapshots: Dict[int, Dict[int, Optional[bool]]] = {}
        self._restored = False
        # Paced action execution: cap API ops well under Discord's 50 req/s
        # global ceiling, leaving headroom for the library's own requests.
        self._actions: "asyncio.Queue[Tuple[discord.Guild, Action]]" = asyncio.Queue()
        self._rate = TokenBucket(rate=action_rate, capacity=action_rate)
        self._executor_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    async def setup_hook(self) -> None:
        self.housekeeping_loop.start()
        self._executor_task = self.loop.create_task(self._executor_worker())

    async def on_ready(self) -> None:
        if not self._restored:
            self._restored = True
            self._restore_incidents()
        log.info("Logged in as %s (guilds=%d)", self.user, len(self.guilds))

    def _restore_incidents(self) -> None:
        """Re-arm any lockdown that was active when the bot was last shut down.

        Restores the channel snapshot (so unlock works) and the engine's raid
        timers (so the auto-lift cooldown keeps counting from where it was).
        """
        for guild_id in self.incidents.active_guild_ids():
            inc = self.incidents.get(guild_id)
            if inc is None:
                continue
            self._lockdown_snapshots[guild_id] = dict(inc.channel_snapshot)
            st = self.engine.store.get(guild_id)
            st.lockdown_active = True
            st.raid_active = inc.raid_active
            st.raid_started_at = inc.raid_started_at
            st.last_raid_signal = inc.last_raid_signal
            log.warning("restored active lockdown for guild %s", guild_id)

    def _persist_incident(self, guild_id: int) -> None:
        st = self.engine.store.get(guild_id)
        self.incidents.set(
            Incident(
                guild_id=guild_id,
                lockdown_active=st.lockdown_active,
                raid_active=st.raid_active,
                raid_started_at=st.raid_started_at,
                last_raid_signal=st.last_raid_signal,
                channel_snapshot=dict(self._lockdown_snapshots.get(guild_id, {})),
            )
        )

    # ==================================================================
    # gateway -> engine
    # ==================================================================
    async def on_member_join(self, member: "discord.Member") -> None:
        ev = JoinEvent(
            guild_id=member.guild.id,
            member=to_domain_member(member, member.guild),
            timestamp=member.joined_at.timestamp() if member.joined_at
            else discord.utils.utcnow().timestamp(),
        )
        await self._dispatch(member.guild, self.engine.process_join(ev))

    async def on_message(self, message: "discord.Message") -> None:
        if message.guild is None or message.author.bot:
            await self.process_commands(message)
            return
        ev = MessageEvent(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            author=to_domain_member(message.author, message.guild),
            content=message.content or "",
            timestamp=message.created_at.timestamp(),
            message_id=message.id,
            mention_count=len(message.mentions),
            mentions_everyone=message.mention_everyone,
            attachment_count=len(message.attachments),
        )
        await self._dispatch(message.guild, self.engine.process_message(ev))
        await self.process_commands(message)

    async def on_audit_log_entry_create(self, entry: "discord.AuditLogEntry") -> None:
        action = _AUDIT_MAP.get(entry.action.name)
        if action is None or entry.user is None:
            return
        guild = entry.guild
        ev = AuditEvent(
            guild_id=guild.id,
            actor=to_domain_member(entry.user, guild),
            action=action,
            target_id=getattr(entry.target, "id", 0) or 0,
            timestamp=entry.created_at.timestamp(),
        )
        await self._dispatch(guild, self.engine.process_audit(ev))

    @tasks.loop(seconds=5.0)
    async def housekeeping_loop(self) -> None:
        for guild in list(self.guilds):
            try:
                actions = self.engine.tick(guild.id, discord.utils.utcnow().timestamp())
                await self._dispatch(guild, actions)
            except Exception:  # pragma: no cover - defensive
                log.exception("tick failed for guild %s", guild.id)

    @housekeeping_loop.before_loop
    async def _before_loop(self) -> None:
        await self.wait_until_ready()

    # ==================================================================
    # engine actions -> Discord (with safety checks)
    # ==================================================================
    async def _dispatch(self, guild: "discord.Guild", actions) -> None:
        """Hand engine actions to the paced executor (non-blocking enqueue)."""
        for action in actions:
            await self._actions.put((guild, action))

    async def _executor_worker(self) -> None:
        """Drain queued actions in batches, coalescing bans into bulk requests."""
        await self.wait_until_ready()
        while not self.is_closed():
            first = await self._actions.get()
            batch = [first]
            while True:  # opportunistically grab everything already queued
                try:
                    batch.append(self._actions.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._run_batch(batch)
            except Exception:  # pragma: no cover - defensive
                log.exception("executor batch failed")
            finally:
                for _ in batch:
                    self._actions.task_done()

    async def _run_batch(self, batch: List[Tuple["discord.Guild", Action]]) -> None:
        guild_by_id = {g.id: g for g, _ in batch}
        plan = plan_actions([a for _, a in batch])

        # 1) urgent guild-wide actions first, immediately (lockdown, alerts, ...)
        for action in plan.urgent:
            guild = guild_by_id.get(action.guild_id)
            if guild is not None:
                await self._safe_apply(guild, action)

        # 2) coalesced bans — one API call per (up to) 200 raiders
        for chunk in plan.ban_chunks:
            guild = guild_by_id.get(chunk.guild_id)
            if guild is None:
                continue
            await self._rate.acquire()
            await self._bulk_ban(guild, chunk.target_ids, chunk.reason)

        # 3) remaining per-member actions, paced
        for action in plan.others:
            guild = guild_by_id.get(action.guild_id)
            if guild is None:
                continue
            await self._rate.acquire()
            await self._safe_apply(guild, action)

    async def _safe_apply(self, guild: "discord.Guild", action: Action) -> None:
        try:
            await self._apply(guild, action)
        except discord.Forbidden:
            log.warning("missing permissions for %s in %s", action, guild.id)
        except discord.HTTPException as exc:
            log.warning("HTTP error applying %s: %s", action, exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("unexpected error applying %s", action)

    async def _single_ban(self, guild: "discord.Guild", uid: int, reason: str) -> None:
        await guild.ban(
            discord.Object(id=uid),
            reason=reason[:512],
            delete_message_seconds=3600,
        )

    async def _bulk_ban(
        self, guild: "discord.Guild", ids: Tuple[int, ...], reason: str
    ) -> None:
        # Filter using the local cache only (no per-id fetch — that would defeat
        # the point of bulk banning). Members not in cache are banned by id,
        # matching the single-ban path.
        safe = [
            uid
            for uid in ids
            if (m := guild.get_member(uid)) is None or self._can_act(guild, m)
        ]
        if not safe:
            return
        if len(safe) == 1:
            await self._safe_apply_ban(guild, safe[0], reason)
            return
        try:
            await guild.bulk_ban(
                [discord.Object(id=uid) for uid in safe],
                reason=reason[:512],
                delete_message_seconds=3600,
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("bulk_ban failed (%s); falling back to single bans", exc)
            for uid in safe:
                await self._rate.acquire()
                await self._safe_apply_ban(guild, uid, reason)

    async def _safe_apply_ban(self, guild: "discord.Guild", uid: int, reason: str) -> None:
        try:
            await self._single_ban(guild, uid, reason)
        except discord.HTTPException as exc:
            log.warning("ban of %s failed: %s", uid, exc)

    def _can_act(self, guild: "discord.Guild", member: "discord.Member") -> bool:
        """Hierarchy + self/owner guard shared by all member-targeted actions."""
        if member is None:
            return False
        if member.id == guild.owner_id:
            return False
        if self.user and member.id == self.user.id:
            return False
        me = guild.me
        if me is None or member.top_role >= me.top_role:
            return False
        return True

    async def _apply(self, guild: "discord.Guild", action: Action) -> None:
        t = action.type
        if t is ActionType.ALERT:
            await self._alert(guild, action)
            return
        if t is ActionType.ENABLE_LOCKDOWN:
            await self._set_lockdown(guild, True, action.reason)
            return
        if t is ActionType.DISABLE_LOCKDOWN:
            await self._set_lockdown(guild, False, action.reason)
            return
        if t in (ActionType.RAISE_VERIFICATION, ActionType.LOWER_VERIFICATION):
            await self._set_verification(guild, raise_=t is ActionType.RAISE_VERIFICATION)
            return
        if t is ActionType.DELETE_MESSAGE:
            await self._delete_message(guild, action)
            return
        if t is ActionType.SET_SLOWMODE:
            await self._set_slowmode(guild, action)
            return
        if t is ActionType.WARN_MEMBER:
            await self._warn(guild, action)
            return
        if t is ActionType.STRIP_ACTOR_PERMISSIONS:
            await self._strip(guild, action)
            return

        # --- member enforcement actions (ban/kick/timeout/quarantine) ---
        member = guild.get_member(action.target_id)
        if member is None:
            try:
                member = await guild.fetch_member(action.target_id)
            except discord.HTTPException:
                member = None
        if t is ActionType.BAN_MEMBER:
            if member is None or self._can_act(guild, member):
                await self._single_ban(guild, action.target_id, action.reason)
        elif t is ActionType.KICK_MEMBER:
            if member and self._can_act(guild, member):
                await member.kick(reason=action.reason[:512])
        elif t is ActionType.TIMEOUT_MEMBER:
            if member and self._can_act(guild, member):
                await member.timeout(
                    discord.utils.utcnow() + _timedelta(action.duration or 600),
                    reason=action.reason[:512],
                )
        elif t is ActionType.QUARANTINE_MEMBER:
            if member and self._can_act(guild, member):
                role = await self._quarantine_role(guild)
                if role:
                    await member.add_roles(role, reason=action.reason[:512])

    # ------------------------------------------------------------------
    async def _alert(self, guild: "discord.Guild", action: Action) -> None:
        channel = self._alert_channel(guild)
        if channel is None:
            return
        colour = {
            Severity.CRITICAL: 0xE74C3C,
            Severity.HIGH: 0xE67E22,
            Severity.MEDIUM: 0xF1C40F,
        }.get(action.severity, 0x95A5A6)
        embed = discord.Embed(
            title=f"🛡️ Anti-Raid · {action.severity.name}",
            description=action.reason,
            colour=colour,
        )
        if action.target_id:
            embed.add_field(name="Target", value=f"<@{action.target_id}> (`{action.target_id}`)")
        await channel.send(embed=embed)

    def _alert_channel(self, guild: "discord.Guild"):
        cfg = self.store.get(guild.id)
        if cfg.alert_channel_id:
            ch = guild.get_channel(cfg.alert_channel_id)
            if ch is not None:
                return ch
        env = os.getenv("DEFAULT_ALERT_CHANNEL_ID")
        if env and env.isdigit():
            ch = guild.get_channel(int(env))
            if ch is not None:
                return ch
        # fall back to the first text channel the bot can post in
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch
        return None

    async def _set_lockdown(self, guild: "discord.Guild", on: bool, reason: str) -> None:
        everyone = guild.default_role
        if on:
            snapshot = self._lockdown_snapshots.setdefault(guild.id, {})
            for ch in guild.text_channels:
                ow = ch.overwrites_for(everyone)
                if ch.id not in snapshot:
                    snapshot[ch.id] = ow.send_messages
                if ow.send_messages is not False:
                    ow.send_messages = False
                    await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            # remember the lockdown so a restart can restore + auto-lift it
            self._persist_incident(guild.id)
        else:
            snapshot = self._lockdown_snapshots.pop(guild.id, {})
            for ch in guild.text_channels:
                ow = ch.overwrites_for(everyone)
                ow.send_messages = snapshot.get(ch.id, None)
                await ch.set_permissions(everyone, overwrite=ow, reason=reason)
            self.incidents.clear(guild.id)  # incident resolved

    async def _set_verification(self, guild: "discord.Guild", raise_: bool) -> None:
        try:
            level = (
                discord.VerificationLevel.highest
                if raise_
                else discord.VerificationLevel.medium
            )
            await guild.edit(verification_level=level, reason="Anti-raid response")
        except discord.HTTPException:
            pass

    async def _set_slowmode(self, guild: "discord.Guild", action: Action) -> None:
        channel = guild.get_channel(action.target_id)
        if channel is None:
            return
        try:
            await channel.edit(
                slowmode_delay=int(action.duration or 0), reason=action.reason[:512]
            )
        except discord.HTTPException:
            pass

    async def _warn(self, guild: "discord.Guild", action: Action) -> None:
        channel = guild.get_channel(action.meta.get("channel_id"))
        if channel is None:
            return
        try:
            await channel.send(
                f"👋 Hey <@{action.target_id}>, please ease up on the messages — "
                "just an automated heads-up. Keep it chill and you're all good. 🙂",
                allowed_mentions=discord.AllowedMentions(
                    users=True, everyone=False, roles=False
                ),
                delete_after=30,
            )
        except discord.HTTPException:
            pass

    async def _delete_message(self, guild: "discord.Guild", action: Action) -> None:
        channel_id = action.meta.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            msg = await channel.fetch_message(action.target_id)
            await msg.delete()
        except discord.HTTPException:
            pass

    async def _strip(self, guild: "discord.Guild", action: Action) -> None:
        member = guild.get_member(action.target_id)
        if member is None or not self._can_act(guild, member):
            return
        bad = [
            r
            for r in member.roles
            if r != guild.default_role
            and r < guild.me.top_role
            and any(getattr(r.permissions, p) for p in _DANGEROUS_PERMS)
        ]
        if bad:
            await member.remove_roles(*bad, reason=action.reason[:512])

    async def _quarantine_role(self, guild: "discord.Guild"):
        role = discord.utils.get(guild.roles, name=QUARANTINE_ROLE_NAME)
        if role is None:
            try:
                role = await guild.create_role(
                    name=QUARANTINE_ROLE_NAME,
                    reason="Anti-raid quarantine role",
                    permissions=discord.Permissions.none(),
                )
                for ch in guild.channels:
                    try:
                        await ch.set_permissions(
                            role, send_messages=False, add_reactions=False,
                            connect=False, speak=False,
                        )
                    except discord.HTTPException:
                        continue
            except discord.HTTPException:
                return None
        return role


def _timedelta(seconds: float):
    import datetime

    return datetime.timedelta(seconds=float(seconds))


# ----------------------------------------------------------------------
# admin commands (require Manage Server)
# ----------------------------------------------------------------------
def register_commands(bot: AntiRaidBot) -> None:
    def admin():
        return commands.has_guild_permissions(manage_guild=True)

    @bot.command(name="status")
    @admin()
    async def status(ctx: "commands.Context"):
        cfg = bot.store.get(ctx.guild.id)
        raid = bot.engine.is_raid_active(ctx.guild.id)
        lock = bot.engine.is_locked_down(ctx.guild.id)
        await ctx.send(
            f"**Anti-Raid** · enabled=`{cfg.enabled}` raid=`{raid}` lockdown=`{lock}`\n"
            f"join threshold=`{cfg.join_rate_threshold}/{cfg.join_window_seconds:.0f}s` "
            f"raid_action=`{cfg.raid_action.value}` "
            f"min_age=`{cfg.min_account_age_days}d`"
        )

    @bot.command(name="enable")
    @admin()
    async def enable(ctx: "commands.Context"):
        cfg = bot.store.get(ctx.guild.id)
        cfg.enabled = True
        bot.store.set(cfg)
        await ctx.send("✅ Anti-raid enabled.")

    @bot.command(name="disable")
    @admin()
    async def disable(ctx: "commands.Context"):
        cfg = bot.store.get(ctx.guild.id)
        cfg.enabled = False
        bot.store.set(cfg)
        await ctx.send("⚠️ Anti-raid disabled.")

    @bot.command(name="lockdown")
    @admin()
    async def lockdown(ctx: "commands.Context"):
        await bot._set_lockdown(ctx.guild, True, "Manual lockdown")
        await ctx.send("🔒 Server locked down.")

    @bot.command(name="unlock")
    @admin()
    async def unlock(ctx: "commands.Context"):
        await bot._set_lockdown(ctx.guild, False, "Manual unlock")
        st = bot.engine.store.get(ctx.guild.id)
        st.lockdown_active = False
        st.clear_raid()
        await ctx.send("🔓 Server unlocked.")

    @bot.command(name="trust")
    @admin()
    async def trust(ctx: "commands.Context", role: "discord.Role"):
        cfg = bot.store.get(ctx.guild.id)
        cfg.trusted_roles = frozenset(cfg.trusted_roles | {role.id})
        bot.store.set(cfg)
        await ctx.send(f"✅ `{role.name}` is now trusted (exempt from enforcement).")

    @bot.command(name="release")
    @admin()
    async def release(ctx: "commands.Context", member: "discord.Member"):
        role = discord.utils.get(ctx.guild.roles, name=QUARANTINE_ROLE_NAME)
        if role is not None and role in member.roles:
            await member.remove_roles(role, reason=f"Released by {ctx.author}")
            await ctx.send(f"✅ Released {member.mention} from quarantine.")
        else:
            await ctx.send(f"{member.mention} isn't quarantined.")

    @bot.command(name="help")
    @admin()
    async def help_cmd(ctx: "commands.Context"):
        await ctx.send(
            "**Anti-Raid** (prefix `!ar `, needs Manage Server):\n"
            "`status` · `enable` / `disable` · `lockdown` / `unlock`\n"
            "`trust @role` — exempt a role · `release @user` — undo a quarantine\n"
            "`blockname <regex>` — ban a username pattern on join\n"
            "`set <key> <value>` — tune anything, e.g. `set raid_action ban`, "
            "`set spam_warnings 1`, `set escalating_spam false`"
        )

    @bot.command(name="blockname")
    @admin()
    async def blockname(ctx: "commands.Context", *, pattern: str):
        import re as _re

        try:
            _re.compile(pattern)
        except _re.error as exc:
            await ctx.send(f"❌ Invalid regex: {exc}")
            return
        cfg = bot.store.get(ctx.guild.id)
        cfg.banned_name_patterns = frozenset(cfg.banned_name_patterns | {pattern})
        bot.store.set(cfg)
        await ctx.send(
            f"✅ Username pattern `{pattern}` will be actioned "
            f"(`{cfg.name_filter_action.value}`) on join."
        )

    @bot.command(name="set")
    @admin()
    async def set_value(ctx: "commands.Context", key: str, value: str):
        cfg = bot.store.get(ctx.guild.id)
        unsupported = key in cfg._SET_FIELDS or key in cfg._STR_SET_FIELDS
        if not hasattr(cfg, key) or key.startswith("_") or unsupported:
            await ctx.send(f"❌ Unknown or unsupported key `{key}`.")
            return
        current = getattr(cfg, key)
        try:
            if key in cfg._ENUM_FIELDS:
                coerced = cfg._ENUM_FIELDS[key](value)
            elif isinstance(current, bool):
                coerced = value.lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int):
                coerced = int(value)
            elif isinstance(current, float):
                coerced = float(value)
            else:
                await ctx.send(f"❌ Cannot set `{key}`.")
                return
            setattr(cfg, key, coerced)
            cfg.validate()
        except (ValueError, TypeError) as exc:
            await ctx.send(f"❌ Invalid value: {exc}")
            return
        bot.store.set(cfg)
        await ctx.send(f"✅ `{key}` = `{coerced}`")
