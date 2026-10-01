"""
B3c: the controller's screen actions, and the gate in front of them.

gui_point moves the pointer (the same relative move `look` sends; inside a
screen it moves the pointer, not the view); gui_click taps a mouse button,
optionally with shift, on a NAMED slot; gui_swap taps a number key over a
named slot, which swaps it with that hotbar slot. Each asks an injected
`gui_probe` for a fresh bridge reading and presses nothing unless
minecraft/gui.py's rules all hold. They are for tasks: the model cannot
call them one at a time.
"""

from __future__ import annotations

import time
import unittest


from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.session import SessionManager                           # noqa: E402
from minecraft.state import EXACT, GuiSlot, GuiView, WorldState        # noqa: E402
from tests.support.fakes import FakeLocator, FakeProcess  # noqa: E402
from tests.support.paths import REPO_ROOT

SLOT = GuiSlot(i=0, role="craft_out", x=500.0, y=300.0)


def reading(cursor=(500.0, 300.0), kind="inventory", health=20.0,
            mode="survival"):
    return WorldState(position=(0.0, 64.0, 0.0), health=health,
                      screen=kind, game_mode=mode,
                      gui=GuiView(scale=3.0, window_px=(1920, 1080),
                                  cursor_px=cursor),
                      slots=(SLOT,), captured_at=time.time(),
                      source="bridge", confidence=EXACT)


class Probe:
    def __init__(self, *states):
        self.states = list(states)

    def __call__(self):
        state = self.states[0]
        if len(self.states) > 1:
            self.states.pop(0)
        return state


class GuiActionTests(unittest.TestCase):

    def controller(self, probe, authorized=True):
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0, gui_probe=probe)
        if authorized:
            controller.start_session(duration_s=60)
        else:
            controller.sessions.start(duration_s=60, authorized=False)
        self.addCleanup(controller.stop, "test")
        return controller

    def test_a_click_on_the_slot_under_the_pointer(self):
        result = self.controller(Probe(reading())).execute_action(
            "gui_click", {"slot": 0})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.button_downs(), ["left"])

    def test_a_shift_click_holds_shift(self):
        result = self.controller(Probe(reading())).execute_action(
            "gui_click", {"slot": 0, "shift": True})
        self.assertTrue(result.ok, result.error)
        self.assertIn("shift", self.backend.downs())
        self.assertEqual(self.backend.held, set())

    def test_a_right_click(self):
        self.controller(Probe(reading())).execute_action(
            "gui_click", {"slot": 0, "button": "right"})
        self.assertEqual(self.backend.button_downs(), ["right"])

    def test_the_pointer_elsewhere_presses_nothing(self):
        result = self.controller(Probe(reading(cursor=(400.0, 300.0)))) \
            .execute_action("gui_click", {"slot": 0})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertIn("not over slot 0", result.error)
        self.assertEqual(self.backend.events, [])

    def test_a_chest_screen_presses_nothing(self):
        result = self.controller(Probe(reading(kind="chest"))) \
            .execute_action("gui_click", {"slot": 0})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertEqual(self.backend.events, [])

    def test_no_probe_presses_nothing(self):
        result = self.controller(None).execute_action("gui_click",
                                                      {"slot": 0})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertEqual(self.backend.events, [])

    def test_health_lost_since_the_screen_opened_stops_clicks(self):
        controller = self.controller(Probe(reading(health=20.0),
                                           reading(health=17.0)))
        self.assertTrue(controller.execute_action("gui_click",
                                                  {"slot": 0}).ok)
        result = controller.execute_action("gui_click", {"slot": 0})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertIn("health", result.error)

    def test_authorisation_speaks_first(self):
        result = self.controller(Probe(reading()), authorized=False) \
            .execute_action("gui_click", {"slot": 0})
        self.assertEqual(result.stopped_reason, "not_authorized")

    def test_a_pointer_move_inside_an_allowed_screen(self):
        result = self.controller(Probe(reading())).execute_action(
            "gui_point", {"dx": 30, "dy": -12})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.mouse_moves(), [(30, -12)])

    def test_no_pointer_move_in_the_pause_screen(self):
        result = self.controller(Probe(reading(kind="pause"))) \
            .execute_action("gui_point", {"dx": 30, "dy": 0})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertEqual(self.backend.mouse_moves(), [])

    def test_a_swap_taps_the_number_key_over_the_slot(self):
        result = self.controller(Probe(reading())).execute_action(
            "gui_swap", {"slot": 0, "hotbar": 4})
        self.assertTrue(result.ok, result.error)
        self.assertIn("4", self.backend.downs())

    def test_a_swap_away_from_the_slot_presses_nothing(self):
        result = self.controller(Probe(reading(cursor=(10.0, 10.0)))) \
            .execute_action("gui_swap", {"slot": 0, "hotbar": 4})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertEqual(self.backend.events, [])

    def test_hotbar_select_is_refused_with_a_screen_open(self):
        """A number key over a slot swaps it: hotbar_select must not."""
        result = self.controller(Probe(reading())).hotbar_select({"slot": 2})
        self.assertEqual(result.stopped_reason, "gui_gate")
        self.assertEqual(self.backend.events, [])

    def test_hotbar_select_works_with_no_screen(self):
        none_open = WorldState(position=(0, 64, 0), source="bridge",
                               confidence=EXACT, captured_at=time.time())
        result = self.controller(Probe(none_open)).hotbar_select({"slot": 2})
        self.assertTrue(result.ok, result.error)


class NotForTheModelTests(unittest.TestCase):

    def test_the_model_cannot_click_one_at_a_time(self):
        from actions import minecraft as mc_actions
        for action in ("gui_point", "gui_click", "gui_swap"):
            with self.subTest(action=action):
                answer = mc_actions.minecraft_control({"action": action,
                                                       "slot": 0})
                self.assertIn("craft_item", answer)

    def test_they_are_inventory_actions_for_the_broker(self):
        from actions.minecraft import _CAPABILITY_BY_ACTION
        from core import capabilities
        for action in ("gui_point", "gui_click", "gui_swap"):
            self.assertEqual(_CAPABILITY_BY_ACTION[action],
                             capabilities.MINECRAFT_INVENTORY)


class ConsentTests(unittest.TestCase):
    """What one confirmation covers changed: the banner, the grant summary,
    the README and NOT_YET_POSSIBLE say so together."""

    def test_the_grant_says_it(self):
        from minecraft.session import GRANT_SUMMARY
        self.assertIn("crafting-table", GRANT_SUMMARY)
        self.assertIn("pointer", GRANT_SUMMARY)

    def test_the_banner_says_what_it_never_clicks(self):
        from actions import minecraft as mc_actions
        detail = mc_actions._mc_guard({"action": "start_session"})["detail"]
        self.assertIn("chest", detail)
        self.assertIn("creative", detail)

    def test_the_readme_no_longer_says_it_cannot_click(self):
        readme = (REPO_ROOT / "readme.md") \
            .read_text(encoding="utf-8")
        self.assertNotIn("Click inside the inventory screen, so it cannot "
                         "craft yet", readme)
        self.assertIn("never a chest", readme)


if __name__ == "__main__":
    unittest.main(verbosity=2)
