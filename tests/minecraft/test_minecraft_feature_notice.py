"""
An older mod jar is named once, early, in one line, with the fix.

From a real run: the jar loaded was older than the Python side, nothing
said so, and Jarvis guessed at causes ("I cannot see the game's screens").
Now, when a session starts -- or, if nothing could be said then, the first
time an action or task needs a feature the jar lacks -- one line says which
features are missing and what to do. It goes to the HUD and into the text
the model reads, so the model reports the real cause.

The controller is the real one with the recording backend; the state source
is the real bridge reader over a payload the test can change.
"""

from __future__ import annotations

import json
import unittest

from minecraft import mod_bridge
from minecraft.controller import MinecraftController
from minecraft.input_backend import FakeInputBackend
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from tests.support.fakes import FakeLocator, FakeProcess

NOW_MS = 1_700_000_000_000
EVERYTHING = list(mod_bridge.REQUIRED_FEATURES)


def payload(features=None, schema=SCHEMA):
    """A player standing in a world. `features` None is a jar from before
    the list."""
    data = {"schema": schema, "written_at_ms": NOW_MS, "in_game": True,
            "position": [0.5, 64.0, 0.5], "rotation": [0.0, 0.0],
            "health": 20.0, "hunger": 20, "on_ground": True,
            "nearby_entities": []}
    if features is not None:
        data["features"] = list(features)
    return data


class Player:
    def __init__(self):
        self.lines = []

    def write_log(self, line):
        self.lines.append(line)


class FeatureNoticeTests(unittest.TestCase):

    def setUp(self):
        import actions.minecraft as adapter
        self.adapter = adapter
        self.current = None                  # no payload: nothing to read

        def read():
            if self.current is None:
                raise FileNotFoundError("no state file")
            return json.dumps(self.current)

        self.bridge = ModBridgeStateSource(path="(test)", reader=read,
                                           clock=lambda: NOW_MS / 1000.0)
        self.controller = MinecraftController(
            backend=FakeInputBackend(), locator=FakeLocator(),
            process_module=FakeProcess(), start_watchers=False,
            focus_wait_s=0)
        adapter._reset_for_tests(controller=self.controller,
                                 state_source=self.bridge)
        self.player = Player()

    def tearDown(self):
        self.adapter._slot.cancel("test over")
        self.adapter._slot.wait(3)
        self.adapter._reset_for_tests()

    def call(self, **params):
        return self.adapter.minecraft_control(params, player=self.player)

    def notices(self, text):
        return [line for line in text.splitlines()
                if "older than this Jarvis" in line]

    def hud_notices(self):
        """The notice itself. Each task's log also starts by naming the
        source it read from, and that line names an older jar too."""
        return [line for line in self.player.lines
                if line.startswith("[minecraft] The installed Minecraft mod")]

    # ── at session start ─────────────────────────────────────────────────

    def test_starting_a_session_with_the_old_jar_says_so_once(self):
        self.current = payload()                       # the real run's jar
        answer = self.call(action="start_session")
        lines = self.notices(answer)
        self.assertEqual(len(lines), 1, answer)
        line = lines[0]
        for words in ("crafting and screens", "building checks",
                      "install_mod.bat", "Quit Minecraft"):
            self.assertIn(words, line)
        self.assertEqual(len(self.hud_notices()), 1)
        self.assertIn(line, self.hud_notices()[0])

        again = self.call(action="start_session")
        self.assertEqual(self.notices(again), [], "said twice")
        self.assertEqual(len(self.hud_notices()), 1, "said twice on the HUD")

    def test_the_current_jar_says_nothing(self):
        self.current = payload(EVERYTHING)
        answer = self.call(action="start_session")
        self.assertEqual(self.notices(answer), [])
        self.assertEqual(self.hud_notices(), [])

    def test_an_older_schema_names_the_ground_under_trees(self):
        self.current = payload(schema="markliv.minecraft.state/3")
        answer = self.call(action="start_session")
        self.assertIn("ground under trees", self.notices(answer)[0])

    def test_only_what_is_missing_is_named(self):
        self.current = payload([f for f in EVERYTHING if f != "gui"])
        line = self.notices(self.call(action="start_session"))[0]
        self.assertIn("crafting and screens", line)
        self.assertNotIn("building checks", line)

    # ── the first time something needs it ────────────────────────────────

    def test_unreadable_at_the_start_then_named_when_a_task_needs_it(self):
        answer = self.call(action="start_session")     # nothing to read yet
        self.assertEqual(self.notices(answer), [])
        self.current = payload([f for f in EVERYTHING if f != "gui"])

        walk = self.call(action="run_task", task="walk_forward", seconds=1)
        self.assertEqual(self.notices(walk), [],
                         "walking does not need the screens")
        self.adapter._slot.cancel("next")
        self.adapter._slot.wait(3)

        craft = self.call(action="run_task", task="craft_item", item="stick")
        self.assertEqual(len(self.notices(craft)), 1, craft)
        self.assertIn("crafting and screens", self.notices(craft)[0])
        self.assertEqual(len(self.hud_notices()), 1)

    def test_an_inventory_action_needs_the_screens(self):
        self.call(action="start_session")
        self.current = payload([f for f in EVERYTHING if f != "gui"])
        answer = self.call(action="inventory", state="close")
        self.assertEqual(len(self.notices(answer)), 1, answer)

    def test_every_task_needs_the_mob_categories(self):
        """The danger watch between steps only sees a mob the mod calls
        hostile: without the categories it sees none."""
        self.call(action="start_session")
        self.current = payload([f for f in EVERYTHING
                                if f != "mob_categories"])
        answer = self.call(action="run_task", task="walk_forward", seconds=1)
        self.assertEqual(len(self.notices(answer)), 1, answer)

    def test_a_source_that_keeps_no_list_says_nothing(self):
        class Still:
            name = "still"

            def read(self):
                from minecraft.state import WorldState
                return WorldState()

            def available(self):
                return True

        self.adapter._reset_for_tests(controller=self.controller,
                                      state_source=Still())
        answer = self.call(action="start_session")
        self.assertEqual(self.notices(answer), [])

    def test_the_task_log_names_the_cause_too(self):
        """The line every task log starts with, and status, say what is
        missing instead of a guess."""
        self.current = payload([f for f in EVERYTHING if f != "gui"])
        label = self.adapter._source_label(self.bridge)
        self.assertIn("OLDER", label)
        self.assertIn("crafting and screens", label)
        self.assertNotIn("ground under trees", label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
