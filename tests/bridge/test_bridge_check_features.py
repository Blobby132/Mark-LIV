"""
bridge_check shows which features arrived, not just the schema.

From a real run: an older jar was loaded, and bridge_check's verdict was
the schema alone -- /4, so "Ground under trees: yes" and no warning, while
the jar sent no game_mode, no screens, no near_blocks and no tree_up.

Now it prints a table of the features seen in the live payload, each
present or missing, says "open your inventory first" for the screen rows
when no screen is open, and for an older jar gives the one-line notice and
the existing explanation of why the old jar is still running.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from minecraft import mod_bridge
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from tools import bridge_check

NOW_MS = 1_700_000_000_000
DROP_OLD = {"name": "minecraft:item", "distance": 2.0,
            "position": [1.5, 64.0, 0.5]}
DROP_NEW = dict(DROP_OLD, category="item",
                item={"name": "minecraft:oak_log", "count": 1})
ZOMBIE_NEW = {"name": "minecraft:zombie", "distance": 9.0,
              "category": "hostile", "position": [9.5, 64.0, 0.5]}


def old_jar(**extra):
    """The real run's jar: schema /4 and terrain, nothing added since."""
    data = {"schema": SCHEMA, "written_at_ms": NOW_MS, "in_game": True,
            "position": [0.5, 64.0, 0.5], "rotation": [0.0, 0.0],
            "health": 20.0, "hunger": 20, "on_ground": True,
            "mouse_sensitivity": 0.5,
            "scan": {"radius": 10, "up": 4, "down": 5},
            "surface": [[0, 63, 0, "minecraft:grass_block", True, 4, None]],
            "notable_blocks": [], "nearby_entities": [DROP_OLD]}
    data.update(extra)
    return data


def new_jar(**extra):
    data = old_jar(
        features=list(mod_bridge.REQUIRED_FEATURES), game_mode="survival",
        scan={"radius": 10, "up": 4, "down": 5, "tree_up": 12},
        near_blocks={"origin": [0, 64, 0], "radius": 4, "below": 4,
                     "above": 4, "complete_within": 4, "blocks": []},
        nearby_entities=[DROP_NEW, ZOMBIE_NEW])
    data.update(extra)
    return data


INVENTORY_OPEN = dict(
    screen={"kind": "inventory"},
    slots=[{"i": 36, "role": "hotbar", "x": 100, "y": 200,
            "item": "minecraft:oak_log", "count": 4}])


def source_of(payload):
    text = json.dumps(payload)
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: NOW_MS / 1000.0)


def rows(payload):
    source = source_of(payload)
    return {row: verdict for row, verdict, _detail
            in bridge_check.feature_rows(source.payload(), source.read())}


class FeatureRowTests(unittest.TestCase):

    def test_the_old_jar_shows_what_it_does_not_send(self):
        seen = rows(old_jar())
        for row in ("game_mode", "screen (when open)", "slots",
                    "near_blocks", "item names on dropped items", "tree_up"):
            with self.subTest(row=row):
                self.assertEqual(seen[row], bridge_check.MISSING)

    def test_the_new_jar_with_nothing_open(self):
        seen = rows(new_jar())
        for row in ("game_mode", "near_blocks",
                    "item names on dropped items", "tree_up"):
            with self.subTest(row=row):
                self.assertEqual(seen[row], bridge_check.PRESENT)
        self.assertEqual(seen["screen (when open)"],
                         bridge_check.OPEN_INVENTORY)
        self.assertEqual(seen["slots"], bridge_check.OPEN_INVENTORY)

    def test_the_new_jar_with_the_inventory_open(self):
        seen = rows(new_jar(**INVENTORY_OPEN))
        self.assertEqual(seen["screen (when open)"], bridge_check.PRESENT)
        self.assertEqual(seen["slots"], bridge_check.PRESENT)

    def test_no_dropped_item_nearby_is_not_seen_not_missing(self):
        seen = rows(new_jar(nearby_entities=[ZOMBIE_NEW]))
        self.assertEqual(seen["item names on dropped items"],
                         bridge_check.NOT_SEEN)

    def test_the_older_rows_are_kept(self):
        seen = rows(new_jar())
        for row in ("head clearance", "ground under trees",
                    "mouse sensitivity"):
            with self.subTest(row=row):
                self.assertEqual(seen[row], bridge_check.PRESENT)


class MainOutputTests(unittest.TestCase):

    def run_main(self, payload):
        source = source_of(payload)
        game = {"found": False, "pid": None, "game_dir": None,
                "fabric": None, "version": None, "cmdline_readable": False}
        out = io.StringIO()
        with mock.patch.object(bridge_check, "ModBridgeStateSource",
                               return_value=source), \
                mock.patch.object(bridge_check, "inspect_running_game",
                                  return_value=game), \
                contextlib.redirect_stdout(out):
            bridge_check.main()
        return out.getvalue()

    @staticmethod
    def flat(text):
        """Long lines are wrapped for the console."""
        return " ".join(text.split())

    def test_the_old_jar_is_called_old_and_why_is_explained(self):
        text = self.run_main(old_jar())
        self.assertIn("older than this Jarvis", self.flat(text))
        self.assertIn("WHY IS THE OLD MOD STILL RUNNING?", text)
        self.assertIn("install_mod.bat", text)
        self.assertNotIn("Ground under trees:        yes", text)

    def test_the_new_jar_asks_for_the_inventory_to_check_the_screen(self):
        text = self.run_main(new_jar())
        self.assertIn("open your inventory first", text)
        self.assertNotIn("older than this Jarvis", self.flat(text))
        self.assertNotIn("WHY IS THE OLD MOD STILL RUNNING?", text)

    def test_the_explanation_names_the_features_too(self):
        text = self.run_main(old_jar())
        why = text[text.index("WHY IS THE OLD MOD STILL RUNNING?"):]
        self.assertIn("gui", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
