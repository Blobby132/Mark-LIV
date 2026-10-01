"""
Every relative link in readme.md and the docs resolves to a file that
exists, so moving or renaming a document cannot leave a dead link behind.

External links (http, https, mailto) are not fetched, and a fragment
(`#section`) is not checked -- only that the file it points into exists.
Links inside fenced code blocks are not links, and are skipped.
"""

from __future__ import annotations

import re
import unittest

from tests.support.paths import REPO_ROOT

INLINE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\"[^\"]*\")?\s*$")
EXTERNAL = ("http://", "https://", "mailto:")


def documents():
    yield REPO_ROOT / "readme.md"
    yield from sorted((REPO_ROOT / "docs").rglob("*.md"))


def links(path):
    """(line number, target) for each relative link in a markdown file."""
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        targets = INLINE.findall(line)
        ref = REFERENCE.match(line)
        if ref:
            targets.append(ref.group(1))
        for target in targets:
            if target.startswith(EXTERNAL) or target.startswith("#"):
                continue
            yield number, target


def dead_links():
    dead = []
    for doc in documents():
        for number, target in links(doc):
            file_part = target.split("#", 1)[0]
            if not (doc.parent / file_part).exists():
                dead.append(f"{doc.relative_to(REPO_ROOT)}:{number}: {target}")
    return dead


class DocLinkTests(unittest.TestCase):

    def test_there_are_links_to_check(self):
        """The index links every topic file: if this finds none, the
        pattern broke, and the test below would pass for nothing."""
        found = [t for d in documents() for _n, t in links(d)]
        self.assertGreater(len(found), 10)

    def test_every_relative_link_resolves(self):
        self.assertEqual(dead_links(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
