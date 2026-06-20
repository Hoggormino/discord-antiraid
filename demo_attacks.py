"""Focused attack showcase: feeds specific attacks through the real engine and
prints the decisions it returns. Pure engine, no network — run with:

    python demo_attacks.py
"""

from __future__ import annotations

from antiraid.config import GuildConfig, RaidAction
from antiraid.engine import AntiRaidEngine
from antiraid.models import JoinEvent, Member, MessageEvent

BASE = 1_700_000_000.0
DAY = 86400.0
GUILD = 5000


def member(uid, name, age_days=400.0, avatar=True):
    return Member(id=uid, name=name, created_at=BASE - age_days * DAY, has_avatar=avatar)


def engine(**cfg):
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=GUILD, **cfg))
    return eng


def show(actions):
    if not actions:
        print("       (no action — looks fine to the bot)")
        return
    for a in actions:
        tgt = f" target={a.target_id}" if a.target_id is not None else ""
        print(f"       -> {a.type.value}{tgt}  [{a.severity.name}]  {a.reason}")


def header(title):
    print("\n" + "=" * 72 + f"\n  {title}\n" + "=" * 72)


# ---------------------------------------------------------------------------
# A) Scripted raid that stays UNDER the join-rate limit, using disguised names
# ---------------------------------------------------------------------------
header("A) Scripted raid under the rate limit — disguised 'raider' names")
print("  join-rate trigger = 10 joins / 10s (deliberately NOT reached);")
print("  similar-name trigger = 4. Names use leet / full-width / dotless-i.\n")
eng = engine(
    join_rate_threshold=10, join_window_seconds=10, similar_name_threshold=4,
    raid_action=RaidAction.BAN, raid_only_suspicious=False,
)
disguised = ["Raider", "R4ider", "RAIDER_99", "Ｒａｉｄｅｒ", "raıder"]
t = BASE
for i, name in enumerate(disguised):
    acts = eng.process_join(JoinEvent(GUILD, member(1000 + i, name), t))
    print(f"  join #{i + 1}: {name!r}")
    show(acts)
    t += 2  # 5 joins across 8s -> only 5 in the 10s window, well under the rate of 10


# ---------------------------------------------------------------------------
# A2) The genuinely-slow drip — caught by the account-age gate, not clustering
# ---------------------------------------------------------------------------
header("A2) Very slow drip (1 brand-new account) — account-age gate")
print("  A drip slow enough to evade windowed clustering is caught instead by")
print("  the opt-in account-age gate (min age 7d), with NO raid required.\n")
eng = engine(
    join_rate_threshold=100, min_account_age_days=7,
    enforce_account_age_outside_raid=True,
)
acts = eng.process_join(JoinEvent(GUILD, member(2000, "totally_legit", age_days=0.3, avatar=False), BASE))
print("  brand-new account (age 0.3d, no avatar) joins quietly:")
show(acts)


# ---------------------------------------------------------------------------
# B) @everyone / mass-mention ping spam from a regular member
# ---------------------------------------------------------------------------
header("B) @everyone + mass-mention ping spam")
eng = engine(msg_rate_threshold=100, mention_threshold=6)
m = member(3000, "pinger")
acts = eng.process_message(
    MessageEvent(GUILD, 42, m, "everyone get in here", BASE, 1, mentions_everyone=True)
)
print("  message that pings @everyone:")
show(acts)
acts = eng.process_message(
    MessageEvent(GUILD, 42, m, "gg", BASE + 1, 2, mention_count=8)
)
print("  message tagging 8 users at once:")
show(acts)


# ---------------------------------------------------------------------------
# C) Invite-link flooding from a regular member
# ---------------------------------------------------------------------------
header("C) Invite-link flooding")
print("  link trigger = 3 link messages / 20s.\n")
eng = engine(msg_rate_threshold=100, link_threshold=3, link_window=20)
m = member(4000, "advertiser")
t = BASE
for i in range(3):
    acts = eng.process_message(
        MessageEvent(GUILD, 42, m, f"join the best server discord.gg/free{i}", t, i)
    )
    print(f"  invite link #{i + 1}:")
    show(acts)
    t += 2


# ---------------------------------------------------------------------------
# D) Scam / impersonation usernames — AutoMod-style name filter (+ leet folding)
# ---------------------------------------------------------------------------
header("D) Scam / impersonation usernames — name filter")
print("  banned patterns: free.?nitro , discord.?(staff|mod|admin) , giveaway")
print("  (matched against the raw name AND the normalised skeleton, so leet")
print("   spellings can't dodge it). Acts on join, no raid required.\n")
eng = engine(
    join_rate_threshold=100, similar_name_threshold=100,
    banned_name_patterns=frozenset(
        {r"free.?nitro", r"discord.?(staff|mod|admin)", r"giveaway"}
    ),
    name_filter_action=RaidAction.BAN,
)
suspects = ["FreeNitro Giveaway", "Discord Staff", "alice", "G1veaway_bot"]
for i, name in enumerate(suspects):
    acts = eng.process_join(JoinEvent(GUILD, member(5000 + i, name), BASE + i))
    print(f"  join: {name!r}")
    show(acts)


# ---------------------------------------------------------------------------
# E) Repeat offender — escalation ladder (warn -> slowmode -> timeout -> quarantine)
# ---------------------------------------------------------------------------
from antiraid.models import MessageEvent  # noqa: E402

header("E) Repeat offender — escalation ladder")
print("  flood trigger = 3 msgs. One member keeps flooding; with the default of")
print("  2 warnings the ladder is: warn -> warn -> slowmode -> timeout -> quarantine.\n")
eng = engine(msg_rate_threshold=3, msg_rate_window=30, escalation_window=300)
m = member(6000, "repeatoffender")
for i in range(7):
    acts = eng.process_message(
        MessageEvent(GUILD, 42, m, f"spam wave {i}", BASE + i, i)
    )
    print(f"  message #{i + 1}:")
    show(acts)

print("\nDone.\n")
