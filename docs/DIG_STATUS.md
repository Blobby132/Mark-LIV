# Ore finding and digging: progress

Working file for the ore-finding and digging round; deleted when the round
ends. Each phase is one or more commits on `claude/trusting-curie-tjgwfd`.

| Phase | What | State |
|---|---|---|
| 0 | Screen open/close judged on a reading newer than the key | done |
| 1a | Mod: `singleplayer`, `ores`, deeper `near_blocks` with fluids | done |
| 1b | Python: `state.ores`, `state.singleplayer`, `find_ores` (read-only) | done |
| 2 | `minecraft/digging.py`: the planner and its eight hard rules | next |
| 3 | `dig_to` skill | |
| 4 | `mine_ore` and the tool text | |
| 5 | Simulated dig worlds, docs, report | |

Nothing digs yet: `NOT_YET_POSSIBLE` still says so until phase 4.

Owed: the "Known stale" comment in `MarkLivBridge.java` (ARCHITECTURE.md)
should have been fixed at the 1a rebuild; it goes with the next one.
