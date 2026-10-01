# Cleanup status

Temporary: deleted when the cleanup round is finished. If a session stops
part-way, the next one starts here.

## Baseline (at 17a472c, before any cleanup commit)

- mods/markliv-bridge-1.0.0.jar sha256:
  bfa29e4da5c130306d39ca3a16c5d4b9dd0b0257b0a42bc84f0bcbf9695ad279
- actions.minecraft.TOOL without the handler (sorted JSON, callables by
  qualified name) sha256:
  1b052ba88c4c803ad5b0363bf0c5e7b36254910dddcafd0c863bd98785fa240a
- Collected test ids: 1519.
- pytest: 3.11 1506 passed / 13 skipped; 3.12 and 3.13 1482 / 37.
- unittest (`discover -s tests -t .`): 1519 run; 13 / 37 / 37 skipped.
- tests/minecraft/test_minecraft_boundary.py blob:
  1a38e53f650999eece833ce99678afc85d63fba3 (must not change).
- pyflakes: 8 findings, all `# noqa` keeps.

## Progress

| Item | State | Commit |
|---|---|---|
| 1 project-structure doc | | |
| 2 move test_install_mod | | |
| 3 two readme lines | | |
| 4 unused public names | | |
| 5 remaining lint | | |
| 6 stale Java comment | | |
| 7 first-run docs | | |

## Next

Item 1.
