# Reorganisation status

Temporary: deleted when the reorganisation is finished. If a session stops
part-way, the next one starts here.

## Baseline (before any reorg commit, at e36be83)

- Tests collected: 1501 (pytest and unittest, every version).
- pytest 3.11: 1490 passed, 11 skipped. 3.12 and 3.13: 1466 passed, 35 skipped.
- unittest 3.11: 1501 run, 11 skipped. 3.12 and 3.13: 1501 run, 35 skipped.
  (Skips: Pillow, FastAPI, cryptography not installed for that version; one
  audit test skipped when running as root.)
- mods/markliv-bridge-1.0.0.jar sha256:
  bfa29e4da5c130306d39ca3a16c5d4b9dd0b0257b0a42bc84f0bcbf9695ad279
- pyflakes: 184 findings, 1 undefined name
  (tests/test_minecraft_observation.py:289 `Observation`).
- Action discovery: 17 tools load here (screen_processor and system_monitor
  need numpy and psutil, not installed in this environment).

## Progress

| Phase | State | Last commit |
|---|---|---|
| 0a line endings | done | normalise line endings, no content change |
| 0b pyproject | done | pyproject.toml and a lint test |
| 0c defects and lint | done | a01fdec skills/__init__.py: an explicit public API |
| 1 tests | done | 52cdfaf tests: drop the move's two fallbacks |
| 2 docs | done | d840fd7 ARCHITECTURE.md: the tests and docs layout |
| 3 oversized files | | |
| 4 root and tools | | |
| 5 guardrails | | |

## Test count rule

The 1501 baseline tests must all still be collected and pass or skip as
before. Tests this reorganisation adds are counted on top, listed here:

- tests/test_lint.py: 2 (skip without pyflakes)
- tests/test_minecraft_navigation.py: test_the_arithmetic_has_one_owner
- tests/test_minecraft_tasks.py: SkillsPublicNamesTests, 3

Expected after phase 0: 1507 collected (1501 + 6).

- tests/core/test_doc_links.py: 2 (phase 2)
- tests/minecraft/test_architecture_doc.py: 2 more (tests folders, support modules)

Expected after phase 2: 1511 collected (1501 + 10).

## Next

Phase 3: oversized Minecraft files. 3c first: the import graph of minecraft/;
then 3a (actions/minecraft.py probes and text) and 3b (collect.py) only as the
graph allows.

Left for the report: the MarkLivBridge.java comment still names
docs/minecraft-gui.md (changing mod source would mean a jar rebuild); the
plugins listed in docs/project-structure.md are not in this repository;
test_install_mod.py is in tests/core per the brief but tests tools/install_mod.py.
