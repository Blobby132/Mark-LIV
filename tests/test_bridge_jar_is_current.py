"""
The committed mod jar must match the mod's source.

WHY THIS EXISTS
    mods/markliv-bridge-1.0.0.jar is built by hand and copied into the repo,
    because the people installing it do not have a Java toolchain. That
    leaves one easy mistake: change the Java source, forget to rebuild or
    copy, and ship the OLD jar. install_mod.bat would then install it without
    complaint, the Python side would quietly fall back for every field the
    old jar does not send, and the symptom would be features that simply do
    not work — with nothing pointing at the jar.

    So these tests read the compiled class out of the committed jar and
    check it carries what the source says it should: the same schema
    version, and the strings the newest fields are written under.
"""

from __future__ import annotations

import re
import sys
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JAR = ROOT / "mods" / "markliv-bridge-1.0.0.jar"
SOURCE = (ROOT / "fabric-mod" / "src" / "main" / "java" / "com" / "markliv"
          / "bridge" / "MarkLivBridge.java")
SCHEMA = re.compile(r"markliv\.minecraft\.state/\d+")


def _class_bytes() -> bytes:
    with zipfile.ZipFile(JAR) as jar:
        name = next(n for n in jar.namelist()
                    if n.endswith("MarkLivBridge.class"))
        return jar.read(name)


class CommittedJarTests(unittest.TestCase):

    def test_the_jar_and_the_source_declare_the_same_schema(self):
        source = SCHEMA.findall(SOURCE.read_text(encoding="utf-8"))
        compiled = SCHEMA.findall(_class_bytes().decode("latin-1"))
        self.assertEqual(sorted(set(source)), sorted(set(compiled)),
                         "mods/ holds a jar built from older source — rebuild "
                         "fabric-mod and copy build/libs/*.jar into mods/")

    def test_the_python_side_expects_what_the_jar_sends(self):
        from minecraft import mod_bridge
        compiled = set(SCHEMA.findall(_class_bytes().decode("latin-1")))
        self.assertIn(mod_bridge.SCHEMA, compiled,
                      "the reader's newest schema is not what the jar writes")

    def test_every_payload_key_in_the_source_is_in_the_jar(self):
        """Catches a field added to the source and not rebuilt, even when
        the schema number was not bumped."""
        source = SOURCE.read_text(encoding="utf-8")
        keys = set(re.findall(r'out\.raw\("([a-z_]+)"', source))
        compiled = _class_bytes()
        missing = sorted(k for k in keys if k.encode() not in compiled)
        self.assertEqual(missing, [],
                         f"the committed jar predates these fields: {missing}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
