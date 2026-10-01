"""
A4: `place` right-clicks whatever is in the hand.

Placing is a tap of the use button, and the use button does what the held
item does: a lava bucket pours lava, flint and steel starts a fire, an
ender pearl teleports you, a bow draws. parse_place and the PlaceBlock skill
never looked. Now the controller asks an injected `held_item_probe` what is
held and refuses before pressing anything when it is on the deny-list -- or
when it cannot tell. `use_item` is the explicit "use what I am holding"
action: it is checked only for a lava, water or powder-snow bucket and fire
starters, which need the item named (item 3, test_minecraft_interact_held_item).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from minecraft import action_spec                                      # noqa: E402
from minecraft.controller import MinecraftController                   # noqa: E402
from minecraft.input_backend import FakeInputBackend                   # noqa: E402
from minecraft.session import SessionManager                           # noqa: E402
from minecraft.state import EXACT, ItemStack, WorldState               # noqa: E402
from tests.support.fakes import FakeLocator, FakeProcess  # noqa: E402
from tests.support.held_items import DENIED  # noqa: E402
BLOCKS = ("cobblestone", "oak_planks", "dirt", "stone", "torch",
          "crafting_table", "glass", "oak_log", "sand")


class DenyListTests(unittest.TestCase):

    def test_every_listed_item_is_refused_with_a_reason(self):
        for name in DENIED:
            with self.subTest(item=name):
                reason = action_spec.place_refusal(name)
                self.assertTrue(reason, f"{name} would be right-clicked")
                self.assertIn(" ".join(name.split("_")), reason)

    def test_ordinary_blocks_are_placed(self):
        for name in BLOCKS:
            with self.subTest(item=name):
                self.assertIsNone(action_spec.place_refusal(name))

    def test_an_unknown_hand_is_refused(self):
        self.assertIn("cannot see", action_spec.place_refusal(None))

    def test_an_empty_hand_is_refused(self):
        self.assertIn("empty", action_spec.place_refusal(""))

    def test_a_namespaced_name_is_matched(self):
        self.assertTrue(action_spec.place_refusal("minecraft:lava_bucket"))


class ControllerTests(unittest.TestCase):

    def controller(self, held="cobblestone", **kwargs):
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0,
            held_item_probe=(lambda: held), **kwargs)
        controller.start_session(duration_s=60)
        self.addCleanup(controller.stop, "test")
        return controller

    def test_a_lava_bucket_is_never_right_clicked(self):
        result = self.controller("lava_bucket").place({})
        self.assertFalse(result.ok)
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertIn("lava bucket", result.error)
        self.assertEqual(self.backend.events, [], "something was pressed")

    def test_a_block_is_placed(self):
        result = self.controller("cobblestone").place({})
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.backend.button_downs(), ["right"])

    def test_no_probe_means_no_place(self):
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0)
        controller.start_session(duration_s=60)
        self.addCleanup(controller.stop, "test")
        result = controller.place({})
        self.assertEqual(result.stopped_reason, "held_item")
        self.assertEqual(self.backend.events, [])

    def test_a_probe_that_breaks_means_no_place(self):
        def broken():
            raise RuntimeError("bridge hiccup")
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0, held_item_probe=broken)
        controller.start_session(duration_s=60)
        self.addCleanup(controller.stop, "test")
        self.assertEqual(controller.place({}).stopped_reason, "held_item")
        self.assertEqual(self.backend.events, [])

    def test_use_item_is_explicit_and_not_checked(self):
        result = self.controller("bow").use_item({"duration": 0.05})
        self.assertTrue(result.ok, result.error)

    def test_authorisation_still_speaks_first(self):
        self.backend = FakeInputBackend()
        controller = MinecraftController(
            backend=self.backend, locator=FakeLocator(),
            sessions=SessionManager(), process_module=FakeProcess(),
            start_watchers=False, focus_wait_s=0.0,
            held_item_probe=lambda: "lava_bucket")
        controller.sessions.start(duration_s=60, authorized=False)
        self.addCleanup(controller.stop, "test")
        self.assertEqual(controller.place({}).stopped_reason,
                         "not_authorized")


class ProbeTests(unittest.TestCase):
    """actions/minecraft.py's _held_item: the bridge's selected slot."""

    def setUp(self):
        from actions import minecraft as mc_actions
        from minecraft.mod_bridge import ModBridgeStateSource
        self.mc = mc_actions
        self.state = None

        class Bridge(ModBridgeStateSource):
            def __init__(inner):
                pass

            def read(inner):
                return self.state
        self.bridge = Bridge()
        original = mc_actions._get_state_source
        mc_actions._get_state_source = lambda: self.bridge
        self.addCleanup(setattr, mc_actions, "_get_state_source", original)

    def holding(self, slot, stacks):
        self.state = WorldState(selected_slot=slot, inventory=tuple(
            ItemStack(slot=s, name=n, count=c) for s, n, c in stacks),
            source="bridge", confidence=EXACT)
        return self.mc._held_item()

    def test_the_selected_stack(self):
        self.assertEqual(self.holding(2, [(0, "dirt", 5),
                                          (2, "lava_bucket", 1)]),
                         "lava_bucket")

    def test_an_empty_slot_is_an_empty_hand(self):
        self.assertEqual(self.holding(4, [(0, "dirt", 5)]), "")

    def test_no_slot_reported_is_unknown(self):
        self.assertIsNone(self.holding(None, [(0, "dirt", 5)]))

    def test_without_the_bridge_it_is_unknown(self):
        self.mc._get_state_source = lambda: object()
        self.assertIsNone(self.mc._held_item())

    def test_the_controller_is_built_with_it(self):
        import inspect
        self.assertIn("held_item_probe=_held_item",
                      inspect.getsource(self.mc._get_controller))


if __name__ == "__main__":
    unittest.main(verbosity=2)
