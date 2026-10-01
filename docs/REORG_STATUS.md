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
| 0a line endings | in progress | |
| 0b pyproject | | |
| 0c defects and lint | | |
| 1 tests | | |
| 2 docs | | |
| 3 oversized files | | |
| 4 root and tools | | |
| 5 guardrails | | |

## Next

Phase 0a: normalise line endings in one isolated commit.
