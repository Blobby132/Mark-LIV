"""
METHODS: main.py's voice methods, lifted out and compiled on
their own, because main.py cannot be imported in a test.

Shared by several test modules, so they import it from here instead of
from each other. Moved here unchanged from tests/voice/test_voice_main_paths.py,
except that ROOT, the repository root, now comes from tests.support.paths
(computed from the test file's own location, it would be wrong one level
down).
"""

from __future__ import annotations

import ast
import asyncio
import time

from tests.support.paths import REPO_ROOT as ROOT


MAIN = ROOT / "main.py"


def _lift(method_names):
    """Compile the named methods of main.py's classes into plain functions."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in method_names:
                    found[item.name] = item
    missing = set(method_names) - set(found)
    if missing:
        raise AssertionError(f"main.py no longer defines {sorted(missing)}")
    module = ast.Module(body=list(found.values()), type_ignores=[])
    namespace = {"asyncio": asyncio, "time": time,
                 "get_proactive_audio_enabled": lambda: False}
    exec(compile(module, str(MAIN), "exec"), namespace)
    return {name: namespace[name] for name in method_names}


METHODS = _lift(["_enqueue_audio", "_report_voice_state", "_on_text_command"])
