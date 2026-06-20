"""End-to-end simulations.

These drive the engine through realistic scenarios and assert both that real
attacks are stopped *and* that ordinary activity is left alone (no false
positives).
"""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig, RaidAction
from antiraid.engine import AntiRaidEngine
from antiraid.models import AuditAction
from tests import _util as u


def production_engine() -> AntiRaidEngine:
    """An engine wired with realistic, default-ish thresholds."""
    eng = AntiRaidEngine()
    eng.set_config(
        GuildConfig(
            guild_id=u.GUILD,
            join_rate_threshold=8,
            join_window_seconds=10,
            min_account_age_days=7,
            raid_action=RaidAction.BAN,
            raid_only_suspicious=True,
            msg_rate_threshold=6,
            msg_rate_window=5,
            cross_user_threshold=5,
            cross_user_window=10,
            nuke_threshold=4,
            nuke_window=10,
            raid_cooldown_seconds=120,
        )
    )
    return eng


class TestFullRaidScenario(unittest.TestCase):
    def test_classic_mass_join_raid_is_contained(self):
        eng = production_engine()
        t = u.NOW
        banned = set()
        locked = False

        # 20 freshly-created bot-like accounts flood in over 8 seconds.
        for i in range(20):
            acts = eng.process_join(
                u.join(u.member(1000 + i, name=f"Raider{i}", age_days=0.2,
                                has_avatar=False), t)
            )
            t += 0.4
            for a in acts:
                if a.type is ActionType.BAN_MEMBER:
                    banned.add(a.target_id)
                if a.type is ActionType.ENABLE_LOCKDOWN:
                    locked = True

        self.assertTrue(locked, "lockdown should engage")
        self.assertTrue(eng.is_raid_active(u.GUILD))
        # Every raider account should have been banned exactly once.
        self.assertEqual(len(banned), 20)

        # After 3 minutes of calm the lockdown auto-lifts.
        lift = eng.tick(u.GUILD, t + 200)
        self.assertTrue(u.has(lift, ActionType.DISABLE_LOCKDOWN))
        self.assertFalse(eng.is_raid_active(u.GUILD))

    def test_coordinated_spam_wave(self):
        eng = production_engine()
        t = u.NOW
        # 6 compromised accounts post the identical scam in quick succession.
        all_acts = []
        for uid in range(6):
            all_acts += eng.process_message(
                u.message(
                    u.member(2000 + uid, age_days=400),
                    "FREE NITRO -> http://scam.example/claim",
                    t,
                    mid=uid,
                )
            )
            t += 0.5
        # Lockdown engages and the entire wave is cleaned up retroactively:
        # all six messages deleted, every author timed out.
        self.assertTrue(eng.is_locked_down(u.GUILD))
        self.assertTrue(u.has(all_acts, ActionType.ENABLE_LOCKDOWN))
        self.assertEqual(set(u.targets(all_acts, ActionType.DELETE_MESSAGE)),
                         {0, 1, 2, 3, 4, 5})
        self.assertEqual(set(u.targets(all_acts, ActionType.TIMEOUT_MEMBER)),
                         {2000, 2001, 2002, 2003, 2004, 2005})

    def test_compromised_admin_nuke_is_stopped(self):
        eng = production_engine()
        t = u.NOW
        rogue = u.member(3000, name="hackedmod")
        stripped = False
        for i in range(5):
            acts = eng.process_audit(
                u.audit(rogue, AuditAction.CHANNEL_DELETE, t, target=i)
            )
            t += 1
            if u.has(acts, ActionType.STRIP_ACTOR_PERMISSIONS):
                stripped = True
        self.assertTrue(stripped, "rogue admin should be neutralised")


class TestNoFalsePositives(unittest.TestCase):
    def test_healthy_server_day(self):
        """A busy-but-normal community must never trip any enforcement."""
        eng = production_engine()
        actions = []
        t = u.NOW

        # Organic joins: ~1 every 30s, all established accounts.
        for i in range(40):
            actions += eng.process_join(
                u.join(u.member(i, name=f"person{i}", age_days=120 + i), t)
            )
            t += 30

        # Lively chat: 30 users each sending varied messages a few seconds apart.
        mid = 0
        for round_no in range(5):
            for uid in range(30):
                mid += 1
                actions += eng.process_message(
                    u.message(
                        u.member(uid, name=f"person{uid}", age_days=120 + uid),
                        f"hello everyone this is message {mid} from me",
                        t,
                        mid=mid,
                    )
                )
                t += 2

        offenders = [
            a for a in actions
            if a.type in {
                ActionType.BAN_MEMBER,
                ActionType.KICK_MEMBER,
                ActionType.TIMEOUT_MEMBER,
                ActionType.DELETE_MESSAGE,
                ActionType.ENABLE_LOCKDOWN,
            }
        ]
        self.assertEqual(offenders, [], f"unexpected enforcement: {offenders}")
        self.assertFalse(eng.is_raid_active(u.GUILD))

    def test_one_enthusiastic_user_not_a_raid(self):
        """A single chatty (not flooding) user shouldn't trigger lockdown."""
        eng = production_engine()
        m = u.member(1, age_days=300)
        actions = []
        t = u.NOW
        for i in range(20):
            actions += eng.process_message(u.message(m, f"unique thought {i}", t, mid=i))
            t += 2  # 1 msg / 2s — under the 6-in-5s flood bar
        self.assertFalse(u.has(actions, ActionType.TIMEOUT_MEMBER))
        self.assertFalse(eng.is_raid_active(u.GUILD))


if __name__ == "__main__":
    unittest.main()
