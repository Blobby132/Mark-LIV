"""
The mod says what it can do: "features" in the payload.

WHY THIS EXISTS
    From a real run. An older jar was loaded and nothing said so. It wrote
    the same schema, /4, because the GUI fields, near_blocks, the names of
    dropped items and the taller tree scan were all added under /4 -- and
    outdated() compared only the schema, so the old jar looked current.
    craft_item stopped with "I cannot see the game's screens" because
    game_mode was None, and inventory_close said no screen was open while
    the inventory was.

    The mod now lists what it reports, by name. A payload without the list,
    or with one missing a feature this Jarvis needs, is outdated.

"Before" below is a payload shaped like that old jar: schema /4, terrain,
no features, no game_mode, no near_blocks, no tree_up in the scan.
"""

from __future__ import annotations

import json
import re
import unittest
import zipfile

from minecraft import mod_bridge
from minecraft.mod_bridge import ModBridgeStateSource, SCHEMA
from minecraft.state import WorldState
from tests.support.paths import REPO_ROOT as ROOT

NOW_MS = 1_700_000_000_000
JAR = ROOT / "mods" / "markliv-bridge-1.0.0.jar"
SOURCE = (ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv"
          / "bridge" / "MarkLivBridge.java")


def old_jar_payload(**extra) -> dict:
    """What the jar from the real run wrote: the current schema, without the
    fields added under it since."""
    payload = {
        "schema": SCHEMA, "written_at_ms": NOW_MS, "in_game": True,
        "position": [0.5, 64.0, 0.5], "rotation": [0.0, 0.0],
        "health": 20.0, "hunger": 20, "on_ground": True,
        "scan": {"radius": 10, "up": 4, "down": 5},
        "surface": [], "notable_blocks": [],
    }
    payload.update(extra)
    return payload


def new_jar_payload(**extra) -> dict:
    payload = old_jar_payload(
        features=list(mod_bridge.REQUIRED_FEATURES),
        game_mode="survival",
        scan={"radius": 10, "up": 4, "down": 5, "tree_up": 12})
    payload.update(extra)
    return payload


def source_of(payload) -> ModBridgeStateSource:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ModBridgeStateSource(path="(test)", reader=lambda: text,
                                clock=lambda: NOW_MS / 1000.0)


class BeforeAndAfterTests(unittest.TestCase):

    def test_the_old_jar_is_outdated_though_its_schema_is_current(self):
        source = source_of(old_jar_payload())
        self.assertEqual(source.schema(), SCHEMA)
        self.assertTrue(source.outdated())
        self.assertEqual(source.features(), frozenset())
        self.assertEqual(source.missing_features(),
                         mod_bridge.REQUIRED_FEATURES)

    def test_the_new_jar_is_not(self):
        source = source_of(new_jar_payload())
        self.assertFalse(source.outdated())
        self.assertEqual(source.missing_features(), ())
        self.assertEqual(source.features(),
                         frozenset(mod_bridge.REQUIRED_FEATURES))

    def test_one_missing_feature_is_enough(self):
        have = [f for f in mod_bridge.REQUIRED_FEATURES if f != "gui"]
        source = source_of(new_jar_payload(features=have))
        self.assertTrue(source.outdated())
        self.assertEqual(source.missing_features(), ("gui",))

    def test_a_newer_jar_with_more_features_is_not_outdated(self):
        more = list(mod_bridge.REQUIRED_FEATURES) + ["something_new"]
        source = source_of(new_jar_payload(features=more))
        self.assertFalse(source.outdated())
        self.assertIn("something_new", source.features())

    def test_an_older_schema_is_still_outdated(self):
        source = source_of(new_jar_payload(
            schema="markliv.minecraft.state/3"))
        self.assertTrue(source.outdated())

    def test_a_malformed_list_counts_as_no_list(self):
        for bad in ("gui,near_blocks", {"gui": True}, 7, [1, None]):
            with self.subTest(features=bad):
                source = source_of(new_jar_payload(features=bad))
                self.assertTrue(source.outdated())
                self.assertEqual(source.missing_features(),
                                 mod_bridge.REQUIRED_FEATURES)

    def test_nothing_readable_is_not_called_outdated(self):
        """No payload is "not running", which says so in its own words."""
        source = source_of("not json")
        self.assertIsNone(source.features())
        self.assertEqual(source.missing_features(), ())
        self.assertFalse(source.outdated())

    def test_the_menu_payload_already_says(self):
        """A session can start at the title screen: the list is sent there
        too, so the notice need not wait for a world."""
        for payload, outdated in (
                ({"schema": SCHEMA, "written_at_ms": NOW_MS,
                  "in_game": False}, True),
                ({"schema": SCHEMA, "written_at_ms": NOW_MS,
                  "in_game": False,
                  "features": list(mod_bridge.REQUIRED_FEATURES)}, False)):
            with self.subTest(outdated=outdated):
                self.assertEqual(source_of(payload).outdated(), outdated)

    def test_every_required_feature_says_what_goes_without_it(self):
        for name in mod_bridge.REQUIRED_FEATURES:
            with self.subTest(feature=name):
                self.assertTrue(mod_bridge.FEATURES[name].strip())


class WorldStateTests(unittest.TestCase):

    def test_the_state_carries_what_the_mod_reported(self):
        old = source_of(old_jar_payload()).read()
        new = source_of(new_jar_payload()).read()
        self.assertEqual(old.features, ())
        self.assertEqual(set(new.features), set(mod_bridge.REQUIRED_FEATURES))

    def test_lacks_names_a_feature_the_mod_did_not_report(self):
        old = source_of(old_jar_payload()).read()
        new = source_of(new_jar_payload()).read()
        self.assertTrue(old.lacks("gui"))
        self.assertFalse(new.lacks("gui"))

    def test_a_state_from_elsewhere_lacks_nothing(self):
        """A simulated world or the F3 reader reports no feature list. That
        is not an old jar, and must not be called one."""
        self.assertIsNone(WorldState().features)
        self.assertFalse(WorldState().lacks("gui"))


class TheJarSaysSoTests(unittest.TestCase):
    """The list only helps if the jar people install sends it."""

    def java_features(self):
        text = SOURCE.read_text(encoding="utf-8")
        block = re.search(r"String\[\] FEATURES = \{(.*?)\};", text, re.S)
        self.assertIsNotNone(block, "MarkLivBridge.java declares no FEATURES")
        return re.findall(r'"([a-z_]+)"', block.group(1))

    def test_the_source_sends_every_feature_this_jarvis_needs(self):
        self.assertIn('out.raw("features"',
                      SOURCE.read_text(encoding="utf-8"))
        missing = [f for f in mod_bridge.REQUIRED_FEATURES
                   if f not in self.java_features()]
        self.assertEqual(missing, [])

    def test_the_committed_jar_carries_them(self):
        with zipfile.ZipFile(JAR) as jar:
            name = next(n for n in jar.namelist()
                        if n.endswith("MarkLivBridge.class"))
            compiled = jar.read(name)
        missing = [f for f in ("features",) + mod_bridge.REQUIRED_FEATURES
                   if f.encode() not in compiled]
        self.assertEqual(missing, [],
                         "mods/ holds a jar built before the feature list -- "
                         "rebuild fabric-mod and copy the jar into mods/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
