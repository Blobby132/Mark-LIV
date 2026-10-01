"""
tests/support/ -- what several test modules share: simulated worlds, fakes,
payload builders, and where the repository is (paths.py). Test modules
import from here, never from each other.
"""

# Temporary, removed once gui_world.py and build_world.py have moved in here
# by a pure `git mv`: until then `tests.support.gui_world` and
# `tests.support.build_world` are also looked for one level up, in tests/.
from pathlib import Path as _Path

__path__.append(str(_Path(__file__).resolve().parent.parent))
