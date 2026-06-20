# Discord Anti-Raid Bot

[![tests](https://github.com/Hoggormino/discord-antiraid/actions/workflows/tests.yml/badge.svg)](https://github.com/Hoggormino/discord-antiraid/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A secure, thoroughly-tested anti-raid bot for Discord. Tests run on every push
via GitHub Actions (`.github/workflows/tests.yml`).

The project is deliberately split into two layers:

| Layer | Modules | Dependencies | Tested by |
|-------|---------|--------------|-----------|
| **Pure decision engine** | `models`, `actions`, `config`, `config_store`, `incident_store`, `state`, `engine`, `executor` | standard library only | 112 unit/simulation tests |
| **Discord adapter** | `bot`, `run` | `discord.py` | adapter safety tests + manual run |

The engine **never** touches the network or reads the clock — every decision is
a deterministic function of the events fed in and the timestamps carried on
those events. That is what lets the entire detection logic be exhaustively
tested offline, with no bot token and no live server.

---

## What it protects against

| Threat | Detection | Default response |
|--------|-----------|------------------|
| **Mass-join raid** | N joins inside a sliding window | Lockdown + raise verification + ban/kick raiders (incl. **retroactive** sweep of everyone already in the window) |
| **Scripted/slow raid** | cluster of similar usernames — robust to homoglyphs (`Rаider` w/ Cyrillic), full-width (`Ｒaider`), accents and leetspeak (`R4id3r`) | Same as above — catches raids that stay *under* the join-rate bar |
| **Throwaway accounts** | account age below threshold | Treated as raiders during a raid; optional gate in peacetime |
| **Malicious usernames** | AutoMod-style regex filter on join (matches raw name *and* normalised skeleton) | Quarantine/kick/ban on join, no raid required |
| **Message flood** | per-user message rate | Delete + timeout |
| **Copy-paste spam** | same content repeated by one user | Delete + timeout |
| **Coordinated spam wave** | identical message from many users | Lockdown + **retroactive** delete of the whole wave + timeout every author |
| **Mention/ping spam** | per-message and cumulative mentions, `@everyone` | Delete + timeout |
| **Invite/link spam** | repeated invite/URL posts | Delete + timeout |
| **Compromised admin / rogue bot ("nuke")** | one actor doing many destructive audit-log actions | Strip the actor's dangerous roles + critical alert |

After a configurable quiet period, raid mode and lockdown **lift themselves
automatically**.

---

## Security design

- **Single enforcement gate.** Every destructive path runs through
  `AntiRaidEngine.is_exempt()` first. The guild owner, allowlisted users,
  allowlisted bots and trusted roles are always spared.
- **Hierarchy & self/owner guards in the adapter.** `_can_act()` refuses to
  ban/kick/timeout the owner, the bot itself, or anyone at or above the bot's
  top role — so a crafted username or role can never turn the bot on the wrong
  target.
- **Side effects are isolated.** The engine only *describes* actions; the
  adapter is the one auditable place where they're executed, each wrapped in
  permission/HTTP error handling.
- **No secrets in code.** The token is read from the environment / `.env`,
  which is git-ignored.
- **Least-privilege intents** and bounded, self-pruning state (no unbounded
  memory growth even on busy servers).
- **Fail-safe config.** Invalid thresholds are rejected; a corrupt config or
  incident file degrades to safe defaults instead of crash-looping.
- **Crash-safe lockdowns.** Active lockdowns are persisted (`incident_store`):
  if the bot restarts mid-raid it re-arms the lockdown, restores the exact
  pre-lockdown channel permissions, and keeps counting the auto-lift cooldown.

---

## Performance & scale

The detection engine is not the bottleneck — Discord's API is. The adapter is
built around that reality:

- **Bulk banning.** A raid that flags hundreds of accounts is coalesced into
  Discord's *Bulk Guild Ban* endpoint — up to **200 bans per request**, each
  request costing a single rate-limit slot instead of 200.
- **Rate-limited action queue.** All enforcement drains through a token-bucket
  limiter (default 25 ops/s, well under Discord's 50 req/s global ceiling) so a
  mass-ban storm can never trip the *10,000-invalid-requests / 10 min*
  Cloudflare ban. Urgent guild-wide actions (lockdown, rogue-admin neutralise)
  jump the queue and run immediately.
- **Auto-sharding.** The bot subclasses `AutoShardedBot`, so it keeps working
  past Discord's mandatory 2,500-guild sharding threshold with no code changes.

The engine itself runs at ~30k–100k events/sec on a single core (≈3 orders of
magnitude above the action ceiling), so a language rewrite would optimise a
non-bottleneck. If raw CPU ever did matter, the pure engine is isolated enough
to port to a Rust extension (PyO3) without touching the adapter.

---

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then edit DISCORD_TOKEN
python run.py
```

In the [Discord Developer Portal](https://discord.com/developers/applications)
enable **Server Members Intent** and **Message Content Intent**, and invite the
bot with permissions: Ban Members, Kick Members, Moderate Members (timeout),
Manage Roles, Manage Channels, Manage Server, Read Message History, View Audit
Log. The bot's own role must sit **above** the roles it needs to action.

To run it 24/7 on a host (Railway / Fly.io / a VPS), see [DEPLOY.md](DEPLOY.md).

---

## Admin commands

Prefix `!ar ` (requires **Manage Server**):

| Command | Effect |
|---------|--------|
| `!ar status` | Show current config and raid/lockdown state |
| `!ar enable` / `!ar disable` | Toggle protection |
| `!ar lockdown` / `!ar unlock` | Manual lockdown / restore |
| `!ar trust @role` | Mark a role exempt from enforcement |
| `!ar blockname <regex>` | Add a banned-username pattern (actioned on join) |
| `!ar set <key> <value>` | Tune any threshold (e.g. `!ar set join_rate_threshold 10`, `!ar set raid_action kick`, `!ar set name_filter_action ban`) |

## Configuration reference

All keys live in `antiraid/config.py` (`GuildConfig`) and persist per-guild to
JSON. Highlights:

```
join_rate_threshold / join_window_seconds   mass-join trigger
similar_name_threshold                      coordinated-username trigger
min_account_age_days                        "new account" definition
raid_action                                 ban | kick | quarantine | alert
raid_only_suspicious                        sweep only new/suspicious vs everyone
raid_cooldown_seconds                       auto-lift delay
msg_rate_threshold / msg_rate_window        flood
duplicate_threshold / cross_user_threshold  copy-paste / coordinated spam
mention_threshold / mention_window_threshold mention spam
nuke_threshold / nuke_window                anti-nuke sensitivity
banned_name_patterns / name_filter_action   AutoMod-style username filter
trusted_roles / allowlist_users / allowlist_bots  exemptions
```

---

## Testing

```bash
python -m unittest discover -s tests -p "test_*.py"
# with coverage:
python -m coverage run --source=antiraid -m unittest discover -s tests
python -m coverage report -m
```

112 tests cover join detection (incl. homoglyph/leet username folding), the
AutoMod-style username filter, message spam, anti-nuke, lifecycle, config &
incident persistence (incl. lockdown restore across restarts), bulk-ban
coalescing, the rate limiter, the adapter's hierarchy guards, **and**
false-positive scenarios (a busy-but-normal community must never trip
enforcement). Pure modules sit at 99% line coverage. The adapter tests are
skipped automatically if `discord.py` is not installed.

### End-to-end environment simulation

```bash
python simulate.py
```

[`simulate.py`](simulate.py) stands up an in-memory Discord world and drives the
**real** bot pipeline through it (gateway handlers → engine → action queue →
token-bucket executor → bulk-ban/lockdown/anti-nuke). It runs six scenarios —
including a visible demonstration of the rate limiter pacing a burst — and
asserts the outcomes. The same simulation also runs automatically inside the
test suite (`tests/test_integration_env.py`).

---

## Known limitations / future work

- Username similarity folds the common confusables (Cyrillic/Greek look-alikes,
  full-width, accents, leetspeak), but the Unicode confusables table is curated,
  not exhaustive — exotic mixed-script spoofs may still slip through.
- Detection thresholds are heuristics — tune them per community and prefer
  `raid_action = quarantine` or `alert` while calibrating.
```

---

## License

Released under the [MIT License](LICENSE).
