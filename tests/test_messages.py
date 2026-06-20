"""Tests for message-based spam detection."""

import unittest

from antiraid.actions import ActionType
from antiraid.config import GuildConfig
from antiraid.engine import AntiRaidEngine, content_fingerprint, normalize_name
from tests import _util as u


def engine(**overrides) -> AntiRaidEngine:
    eng = AntiRaidEngine()
    eng.set_config(GuildConfig(guild_id=u.GUILD, **overrides))
    return eng


class TestFlood(unittest.TestCase):
    def test_flood_triggers_timeout_and_delete(self):
        eng = engine(msg_rate_threshold=5, msg_rate_window=5)
        m = u.member(1)
        actions = []
        for i in range(5):
            actions += eng.process_message(u.message(m, f"hi {i}", u.NOW + i, mid=i))
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))
        self.assertTrue(u.has(actions, ActionType.DELETE_MESSAGE))
        self.assertIn(1, u.targets(actions, ActionType.TIMEOUT_MEMBER))

    def test_slow_messages_no_flood(self):
        eng = engine(msg_rate_threshold=5, msg_rate_window=5)
        m = u.member(1)
        actions = []
        for i in range(10):
            actions += eng.process_message(u.message(m, f"hi {i}", u.NOW + i * 3, mid=i))
        self.assertFalse(u.has(actions, ActionType.TIMEOUT_MEMBER))


class TestDuplicate(unittest.TestCase):
    def test_self_duplicate(self):
        eng = engine(
            msg_rate_threshold=100, duplicate_threshold=4, duplicate_window=30
        )
        m = u.member(1)
        actions = []
        for i in range(4):
            actions += eng.process_message(u.message(m, "BUY CRYPTO NOW", u.NOW + i, mid=i))
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))

    def test_different_content_not_duplicate(self):
        eng = engine(msg_rate_threshold=100, duplicate_threshold=4)
        m = u.member(1)
        actions = []
        for i in range(6):
            actions += eng.process_message(u.message(m, f"msg number {i}", u.NOW + i, mid=i))
        self.assertFalse(u.has(actions, ActionType.TIMEOUT_MEMBER))


class TestCrossUserSpam(unittest.TestCase):
    def test_identical_message_many_users_is_raid(self):
        eng = engine(
            msg_rate_threshold=100,
            duplicate_threshold=100,
            cross_user_threshold=5,
            cross_user_window=10,
        )
        actions = []
        for uid in range(5):
            actions += eng.process_message(
                u.message(u.member(uid), "@everyone free nitro scam.link", u.NOW + uid, mid=uid)
            )
        self.assertTrue(eng.is_raid_active(u.GUILD))
        self.assertTrue(u.has(actions, ActionType.ENABLE_LOCKDOWN))

    def test_normalisation_catches_whitespace_variants(self):
        a = content_fingerprint("Free   NITRO\nclick")
        b = content_fingerprint("free nitro click")
        self.assertEqual(a, b)


class TestMentionSpam(unittest.TestCase):
    def test_single_message_mass_mention(self):
        eng = engine(msg_rate_threshold=100, mention_threshold=6)
        actions = eng.process_message(
            u.message(u.member(1), "ping", u.NOW, mid=1, mentions=8)
        )
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))

    def test_everyone_ping_flagged(self):
        eng = engine(msg_rate_threshold=100, mention_threshold=50)
        actions = eng.process_message(
            u.message(u.member(1), "hey", u.NOW, mid=1, mentions=0, everyone=True)
        )
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))

    def test_cumulative_mentions_across_messages(self):
        eng = engine(
            msg_rate_threshold=100,
            mention_threshold=50,
            mention_window_threshold=12,
            mention_window=15,
        )
        m = u.member(1)
        actions = []
        for i in range(4):  # 4 * 3 = 12 mentions inside the window
            actions += eng.process_message(
                u.message(m, "p", u.NOW + i, mid=i, mentions=3)
            )
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))


class TestLinkSpam(unittest.TestCase):
    def test_invite_link_spam(self):
        eng = engine(msg_rate_threshold=100, link_threshold=3, link_window=20)
        m = u.member(1)
        actions = []
        for i in range(3):
            actions += eng.process_message(
                u.message(m, f"join discord.gg/abc{i}", u.NOW + i, mid=i)
            )
        self.assertTrue(u.has(actions, ActionType.TIMEOUT_MEMBER))

    def test_single_link_is_fine(self):
        eng = engine(msg_rate_threshold=100, link_threshold=3)
        actions = eng.process_message(
            u.message(u.member(1), "check https://example.com", u.NOW, mid=1)
        )
        self.assertFalse(u.has(actions, ActionType.TIMEOUT_MEMBER))


class TestMessageExemptions(unittest.TestCase):
    def test_bot_messages_ignored(self):
        eng = engine(msg_rate_threshold=3)
        bot = u.member(1, is_bot=True)
        actions = []
        for i in range(10):
            actions += eng.process_message(u.message(bot, "spam", u.NOW + i, mid=i))
        self.assertEqual(actions, [])

    def test_trusted_user_ignored(self):
        eng = engine(msg_rate_threshold=3, trusted_roles=frozenset({5}))
        m = u.member(1, roles=(5,))
        actions = []
        for i in range(10):
            actions += eng.process_message(u.message(m, "spam", u.NOW + i, mid=i))
        self.assertEqual(actions, [])


class TestHelpers(unittest.TestCase):
    def test_normalize_name_basic(self):
        self.assertEqual(normalize_name("Raider_001"), "raider")
        self.assertEqual(normalize_name("RAIDER-99"), "raider")
        self.assertEqual(normalize_name("user"), "user")
        # all-digit / symbol names fall back to the raw lowercase string
        self.assertEqual(normalize_name("12345"), "12345")

    def test_normalize_name_homoglyphs_and_leet(self):
        self.assertEqual(normalize_name("R4id3r"), "raider")            # leetspeak
        self.assertEqual(normalize_name("Rаider"), "raider")       # Cyrillic 'а'
        self.assertEqual(normalize_name("ráider"), "raider")       # accented 'á'
        self.assertEqual(  # full-width "Ｒａｉｄｅｒ"
            normalize_name("Ｒａｉｄｅｒ"), "raider"
        )
        self.assertEqual(normalize_name("n00b"), "noob")


if __name__ == "__main__":
    unittest.main()
