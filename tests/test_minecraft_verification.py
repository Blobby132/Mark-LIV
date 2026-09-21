"""
Tests for minecraft/verification.py.

The property under test is the one the whole phase turns on:

    "I did the action" and "the goal was achieved" are different answers,
    and neither one may be inferred from the other.

The third status, UNVERIFIABLE, gets the most attention here. It is what
stops an agent that cannot see from reporting a confident failure — or worse,
a confident success — and it is the status most likely to be "simplified
away" by someone later who finds two statuses tidier.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import verification as v                                # noqa: E402
from minecraft.state import (                                          # noqa: E402
    BlockRef, EXACT, INFERRED, WorldState,
)


def seen(**kwargs) -> WorldState:
    """A state whose given fields are readable."""
    return WorldState(source="test", confidence=INFERRED, **kwargs)


BLIND = WorldState()          # nothing readable at all


class TestTheThreeStatuses(unittest.TestCase):

    def test_an_observed_change_is_success(self):
        result = v.moved(0.5).check(seen(position=(0, 64, 0)),
                                    seen(position=(3, 64, 0)), delivered=True)
        self.assertEqual(result.status, v.SUCCESS)
        self.assertTrue(result.succeeded)
        self.assertTrue(result.conclusive)

    def test_an_absent_change_is_failure(self):
        result = v.moved(0.5).check(seen(position=(0, 64, 0)),
                                    seen(position=(0, 64, 0)), delivered=True)
        self.assertEqual(result.status, v.FAILED)
        self.assertFalse(result.succeeded)
        self.assertTrue(result.conclusive)

    def test_an_unreadable_field_is_unverifiable_not_failure(self):
        """The heart of it. Not being able to see is not evidence of failure,
        and reporting FAILED would send a planner off fixing the wrong thing."""
        result = v.moved(0.5).check(BLIND, BLIND, delivered=True)
        self.assertEqual(result.status, v.UNVERIFIABLE)
        self.assertFalse(result.succeeded)
        self.assertFalse(result.conclusive)
        self.assertIn("position", result.missing_fields)

    def test_unverifiable_when_only_the_before_state_is_blind(self):
        """A change is a comparison. Knowing where you ended up proves
        nothing if you never knew where you started."""
        result = v.moved(0.5).check(BLIND, seen(position=(3, 64, 0)),
                                    delivered=True)
        self.assertEqual(result.status, v.UNVERIFIABLE)

    def test_unverifiable_when_only_the_after_state_is_blind(self):
        result = v.moved(0.5).check(seen(position=(0, 64, 0)), BLIND,
                                    delivered=True)
        self.assertEqual(result.status, v.UNVERIFIABLE)


class TestDeliveredIsNotAccomplished(unittest.TestCase):

    def test_a_delivered_action_with_no_effect_is_not_success(self):
        result = v.block_broken("oak_log").check(
            seen(target_block=BlockRef(name="oak_log")),
            seen(target_block=BlockRef(name="oak_log")),
            delivered=True)
        self.assertTrue(result.delivered)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.as_dict()["action_delivered"], True)
        self.assertEqual(result.as_dict()["goal_accomplished"], False)

    def test_the_two_are_separate_keys_in_the_payload(self):
        payload = v.moved().check(BLIND, BLIND, delivered=True).as_dict()
        self.assertIn("action_delivered", payload)
        self.assertIn("goal_accomplished", payload)

    def test_the_sentence_does_not_claim_success_when_unverifiable(self):
        text = v.block_broken("oak_log").check(BLIND, BLIND, True).describe()
        self.assertIn("cannot tell", text.lower())
        self.assertNotIn("confirmed —", text)


class TestBlockBroken(unittest.TestCase):

    def _check(self, before_name, after_name, expected="oak_log"):
        return v.block_broken(expected).check(
            seen(target_block=BlockRef(name=before_name) if before_name else None),
            seen(target_block=BlockRef(name=after_name) if after_name else None),
            delivered=True)

    def test_block_replaced_by_air_is_success(self):
        self.assertEqual(self._check("oak_log", "air").status, v.SUCCESS)

    def test_an_unreadable_target_afterwards_is_unverifiable(self):
        """None means "could not read", never "nothing there" — the F3 source
        emits an explicit air block for the sky. So this must not be scored as
        a broken block."""
        self.assertEqual(self._check("oak_log", None).status, v.UNVERIFIABLE)

    def test_a_different_block_behind_it_is_success(self):
        """Breaking a log reveals whatever was behind it, which is still a
        broken log."""
        self.assertEqual(self._check("oak_log", "dirt").status, v.SUCCESS)

    def test_the_same_block_still_there_is_failure(self):
        self.assertEqual(self._check("oak_log", "oak_log").status, v.FAILED)

    def test_breaking_the_wrong_block_is_not_success(self):
        """Aimed at stone while asked to break a log: even if the stone
        vanishes, the goal was not accomplished."""
        result = self._check("stone", "air", expected="oak_log")
        self.assertEqual(result.status, v.FAILED)
        self.assertIn("stone", result.reason)

    def test_nothing_there_to_begin_with_is_failure(self):
        self.assertEqual(self._check("air", "air").status, v.FAILED)

    def test_an_unreadable_target_beforehand_is_unverifiable(self):
        self.assertEqual(self._check(None, "air").status, v.UNVERIFIABLE)


class TestTurned(unittest.TestCase):

    def test_a_real_turn_is_success(self):
        result = v.turned(5.0).check(seen(rotation=(0.0, 0.0)),
                                     seen(rotation=(45.0, 0.0)), True)
        self.assertEqual(result.status, v.SUCCESS)

    def test_a_tiny_drift_is_not_a_turn(self):
        result = v.turned(5.0).check(seen(rotation=(0.0, 0.0)),
                                     seen(rotation=(0.4, 0.0)), True)
        self.assertEqual(result.status, v.FAILED)

    def test_yaw_wrapping_is_handled(self):
        """179 to -179 is a two degree turn, not 358. Getting this wrong makes
        every wrap-around look like a huge successful turn."""
        result = v.turned(5.0).check(seen(rotation=(179.0, 0.0)),
                                     seen(rotation=(-179.0, 0.0)), True)
        self.assertEqual(result.status, v.FAILED)

    def test_pitch_alone_counts_as_turning(self):
        result = v.turned(5.0).check(seen(rotation=(0.0, 0.0)),
                                     seen(rotation=(0.0, -30.0)), True)
        self.assertEqual(result.status, v.SUCCESS)

    def test_malformed_rotation_is_a_failure_not_a_crash(self):
        result = v.turned(5.0).check(seen(rotation=("north",)),
                                     seen(rotation=("south",)), True)
        self.assertEqual(result.status, v.FAILED)


class TestMoved(unittest.TestCase):

    def test_a_sub_threshold_shuffle_is_not_movement(self):
        result = v.moved(0.5).check(seen(position=(0, 64, 0)),
                                    seen(position=(0.05, 64, 0)), True)
        self.assertEqual(result.status, v.FAILED)

    def test_stayed_within_is_the_inverse(self):
        result = v.stayed_within(0.5).check(seen(position=(0, 64, 0)),
                                            seen(position=(0.05, 64, 0)), True)
        self.assertEqual(result.status, v.SUCCESS)


class TestHonestlyImpossibleChecks(unittest.TestCase):

    def test_hotbar_selection_is_unverifiable_with_current_sources(self):
        """`selected_slot` is not on the F3 overlay. The expectation exists so
        it works the day the mod bridge lands; today it must say it cannot
        tell rather than pass."""
        result = v.holding_slot(3).check(seen(position=(0, 0, 0)),
                                         seen(position=(0, 0, 0)), True)
        self.assertEqual(result.status, v.UNVERIFIABLE)
        self.assertIn("selected_slot", result.missing_fields)

    def test_hotbar_selection_verifies_once_the_field_exists(self):
        before = seen(selected_slot=0)
        after = WorldState(selected_slot=2, source="mod", confidence=EXACT)
        self.assertEqual(v.holding_slot(3).check(before, after, True).status,
                         v.SUCCESS)

    def test_an_action_with_no_expectation_is_unverifiable(self):
        result = v.unverifiable("jump", "nothing checks this yet", True)
        self.assertEqual(result.status, v.UNVERIFIABLE)
        self.assertFalse(result.succeeded)


if __name__ == "__main__":
    unittest.main()
