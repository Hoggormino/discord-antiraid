"""Tests for the pure action planner and the token-bucket rate limiter."""

import unittest

from antiraid.actions import Action, ActionType
from antiraid.executor import TokenBucket, plan_actions
from tests import _util as u


def ban(target, guild=u.GUILD):
    return Action(ActionType.BAN_MEMBER, guild, target_id=target, reason="raid")


class TestPlanActions(unittest.TestCase):
    def test_bans_coalesced_into_one_chunk(self):
        plan = plan_actions([ban(i) for i in range(50)])
        self.assertEqual(len(plan.ban_chunks), 1)
        self.assertEqual(len(plan.ban_chunks[0].target_ids), 50)
        self.assertEqual(plan.others, [])

    def test_bans_chunked_at_200(self):
        plan = plan_actions([ban(i) for i in range(450)])
        sizes = sorted(len(c.target_ids) for c in plan.ban_chunks)
        self.assertEqual(sizes, [50, 200, 200])

    def test_custom_chunk_size(self):
        plan = plan_actions([ban(i) for i in range(10)], max_per_ban_call=4)
        self.assertEqual([len(c.target_ids) for c in plan.ban_chunks], [4, 4, 2])

    def test_duplicate_bans_deduped(self):
        plan = plan_actions([ban(7), ban(7), ban(8)])
        self.assertEqual(len(plan.ban_chunks), 1)
        self.assertEqual(sorted(plan.ban_chunks[0].target_ids), [7, 8])

    def test_bans_grouped_per_guild(self):
        plan = plan_actions([ban(1, guild=10), ban(2, guild=20), ban(3, guild=10)])
        by_guild = {c.guild_id: c.target_ids for c in plan.ban_chunks}
        self.assertEqual(sorted(by_guild[10]), [1, 3])
        self.assertEqual(sorted(by_guild[20]), [2])

    def test_urgent_separated_and_ordered(self):
        actions = [
            Action(ActionType.ENABLE_LOCKDOWN, u.GUILD, reason="r"),
            ban(1),
            Action(ActionType.ALERT, u.GUILD, reason="r"),
        ]
        plan = plan_actions(actions)
        self.assertEqual(
            [a.type for a in plan.urgent],
            [ActionType.ENABLE_LOCKDOWN, ActionType.ALERT],
        )
        self.assertEqual(len(plan.ban_chunks), 1)

    def test_other_member_actions_preserved(self):
        actions = [
            Action(ActionType.TIMEOUT_MEMBER, u.GUILD, target_id=1, reason="r"),
            Action(ActionType.DELETE_MESSAGE, u.GUILD, target_id=2, reason="r"),
        ]
        plan = plan_actions(actions)
        self.assertEqual(len(plan.others), 2)
        self.assertEqual(plan.ban_chunks, [])

    def test_invalid_chunk_size_rejected(self):
        with self.assertRaises(ValueError):
            plan_actions([ban(1)], max_per_ban_call=0)


class TestTokenBucket(unittest.IsolatedAsyncioTestCase):
    def _bucket(self, rate, capacity):
        clock = [0.0]

        async def fake_sleep(d):
            clock[0] += d

        tb = TokenBucket(
            rate, capacity, time_func=lambda: clock[0], sleep_func=fake_sleep
        )
        return tb, clock

    async def test_burst_up_to_capacity_is_instant(self):
        tb, clock = self._bucket(rate=5, capacity=5)
        for _ in range(5):
            await tb.acquire()
        self.assertEqual(clock[0], 0.0)  # no sleeping within capacity

    async def test_paces_beyond_capacity(self):
        tb, clock = self._bucket(rate=5, capacity=5)
        for _ in range(5):
            await tb.acquire()
        await tb.acquire()  # 6th must wait one token: 1/5 = 0.2s
        self.assertAlmostEqual(clock[0], 0.2, places=6)

    async def test_sustained_rate(self):
        tb, clock = self._bucket(rate=10, capacity=1)
        for _ in range(11):  # 1 free + 10 paced at 0.1s each
            await tb.acquire()
        self.assertAlmostEqual(clock[0], 1.0, places=6)

    async def test_acquire_above_capacity_does_not_hang(self):
        # Regression: asking for more than a full bucket must not spin forever.
        tb, clock = self._bucket(rate=5, capacity=2)
        await tb.acquire(10)  # capped to capacity internally
        self.assertGreaterEqual(clock[0], 0.0)

    async def test_many_acquisitions_terminate(self):
        # Regression for the float-dust infinite loop: a long run must finish.
        tb, _ = self._bucket(rate=7, capacity=3)
        for _ in range(200):
            await tb.acquire()  # would hang pre-fix

    async def test_invalid_rate_rejected(self):
        with self.assertRaises(ValueError):
            TokenBucket(0)


if __name__ == "__main__":
    unittest.main()
