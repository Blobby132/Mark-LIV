"""
Repository hygiene: the rules that keep the repository from clogging up as
it grows.

1. A size budget. No Python file outside tests/ is over MAX_LINES (2,000).
   ui.py and main.py are over it already; OVER_BUDGET lists them with
   today's length as a ceiling. A ceiling may only come down -- lower it
   as the file shrinks, and take the file off the list once it is under
   the budget (the test says so). Never raise one: split the file.
2. No test module imports another. What tests share lives in
   tests/support/.
3. No import cycle in minecraft/ -- module-level imports or lazy ones.
4. Every relative link in readme.md and the docs resolves: moving or
   renaming a document cannot leave a dead link behind. External links
   (http, https, mailto) are not fetched, and a fragment (`#section`) is
   not checked -- only that the file it points into exists. Links inside
   fenced code blocks are not links, and are skipped.
5. Every path in docs/project-structure.md's tree exists. The tree is
   generated from `git ls-files`; its format is one path per line, nested
   with the box-drawing prefixes, comments after `#`. A line in a tree
   block that does not follow it -- several names on a line, `...` -- fails
   too, so the check cannot be quietly sidestepped.
"""

from __future__ import annotations

import ast
import re
import unittest
from collections import defaultdict

from tests.support.paths import REPO_ROOT

INLINE = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\"[^\"]*\")?\s*$")
EXTERNAL = ("http://", "https://", "mailto:")

MAX_LINES = 2000
OVER_BUDGET = {"ui.py": 5434, "main.py": 2958}
"""Files already over MAX_LINES, with a ceiling that may only come down."""

SKIP_DIRS = {".git", ".venv", "venv", "env", "ENV", "build", "dist",
             "fabric-mod", "__pycache__", ".pytest_cache", ".ruff_cache"}


def app_python_files():
    """Every .py file outside tests/ (and outside build or tool output)."""
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] == "tests" or set(rel.parts[:-1]) & SKIP_DIRS:
            continue
        yield rel


def line_count(path):
    return len((REPO_ROOT / path).read_text(encoding="utf-8").splitlines())


def imports_of_test_modules():
    """(file, line, imported module) for each import of a test module
    found under tests/."""
    found = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                names = [base] + [f"{base}.{a.name}".lstrip(".")
                                  for a in node.names]
            for name in names:
                if any(part.startswith("test_") for part in name.split(".")):
                    found.append((path.relative_to(REPO_ROOT).as_posix(),
                                  node.lineno, name))
    return found


def minecraft_import_cycles():
    """Groups of minecraft/ modules that import each other, directly or
    round a loop -- counting imports inside functions too."""
    package = REPO_ROOT / "minecraft"
    modules = {}
    for path in package.rglob("*.py"):
        name = ".".join(path.relative_to(REPO_ROOT).with_suffix("").parts)
        modules[name.removesuffix(".__init__")] = path

    def resolve(name):
        while name and name not in modules:
            name = name.rpartition(".")[0]
        return name

    graph = defaultdict(set)
    for name, path in modules.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module] + [f"{node.module}.{a.name}"
                                           for a in node.names]
            for target in targets:
                hit = resolve(target)
                if hit and hit != name and hit != "minecraft":
                    graph[name].add(hit)

    index, low, stack, on_stack, cycles = {}, {}, [], set(), []
    counter = [0]

    def visit(v):                     # Tarjan's strongly connected components
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            group = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                group.append(w)
                if w == v:
                    break
            if len(group) > 1:
                cycles.append(sorted(group))

    for v in sorted(modules):
        if v not in index:
            visit(v)
    return cycles


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


class SizeBudgetTests(unittest.TestCase):

    def test_no_file_outside_tests_is_over_the_budget(self):
        over = [f"{p.as_posix()}: {line_count(p)} lines"
                for p in app_python_files()
                if p.as_posix() not in OVER_BUDGET
                and line_count(p) > MAX_LINES]
        self.assertEqual(over, [], f"over {MAX_LINES} lines -- split them")

    def test_the_files_already_over_it_do_not_grow(self):
        for name, ceiling in OVER_BUDGET.items():
            with self.subTest(file=name):
                lines = line_count(name)
                self.assertLessEqual(
                    lines, ceiling,
                    f"{name} grew from {ceiling} to {lines} lines: split "
                    f"what you added out of it instead")

    def test_the_over_budget_list_only_holds_files_still_over(self):
        for name in OVER_BUDGET:
            with self.subTest(file=name):
                self.assertGreater(
                    line_count(name), MAX_LINES,
                    f"{name} is under {MAX_LINES} lines now: take it off "
                    f"OVER_BUDGET")


class TestIsolationTests(unittest.TestCase):

    def test_no_test_module_imports_another(self):
        self.assertEqual(imports_of_test_modules(), [],
                         "import shared test code from tests.support")


class ImportCycleTests(unittest.TestCase):

    def test_minecraft_has_no_import_cycle(self):
        self.assertEqual(minecraft_import_cycles(), [])


STRUCTURE_DOC = REPO_ROOT / "docs" / "project-structure.md"
TREE_ENTRY = re.compile(r"^((?:│   |    )*)(?:├── |└── )(\S+?)(?:\s+#.*)?$")


def tree_paths(path):
    """(line number, repository path or a parse error) for every entry of
    every fenced tree block -- a fence whose lines draw a tree."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks, current = [], None
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append((number, line))
    out = []
    for block in blocks:
        if not any("├── " in l or "└── " in l for _n, l in block):
            continue                          # code, not a tree
        stack = []
        for number, line in block:
            if not line.strip() or (not stack and line.rstrip().endswith("/")
                                    and "── " not in line):
                continue                      # blank, or the root line
            match = TREE_ENTRY.match(line)
            if not match or match.group(2) in ("...", "…") or "," in match.group(2):
                out.append((number, f"not one path per line: {line.strip()!r}"))
                continue
            depth = len(match.group(1)) // 4
            del stack[depth:]
            stack.append(match.group(2).rstrip("/"))
            out.append((number, "/".join(stack)))
    return out


class StructureDocTests(unittest.TestCase):

    def test_the_tree_has_entries(self):
        """If the parser finds nothing, the test below passes for nothing."""
        self.assertGreater(len(tree_paths(STRUCTURE_DOC)), 50)

    def test_every_path_in_the_tree_exists(self):
        missing = [f"line {n}: {p}" for n, p in tree_paths(STRUCTURE_DOC)
                   if not (REPO_ROOT / p).exists()]
        self.assertEqual(missing, [])


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
