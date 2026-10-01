# Documentation

The [readme](../readme.md) covers what Jarvis is, installing and running it,
and safety in brief. Everything else is here.

## Using Jarvis

| | |
|---|---|
| [Capabilities](capabilities.md) | Everything it can do, feature by feature |
| [What's new in Mark LIV](whats-new.md) | The face, talking to it, how it understands itself |
| [The Foundation Update](foundation-update.md) | Memory, undo, a confirmation the model can't forge, audio devices, reconnection |
| [Roadmap](roadmap.md) | The Marks so far and what comes next |

## Minecraft

| | |
|---|---|
| [Minecraft](minecraft/README.md) | Playing Minecraft Java inside a bounded, revocable session; what it can and deliberately cannot do |
| [Architecture](ARCHITECTURE.md) | The subsystem's modules, the skills package, and every safety layer |
| [Inventory and crafting screens](minecraft/gui.md) | What the bridge reports about an open screen, and the click gate |
| [Jumping and placing](minecraft/jump-place.md) | Design for building upwards and over gaps (not built) |
| [Proposals](minecraft/proposals.md) | Shallow water, a staircase down, long trips in legs (not built) |
| [First real run](minecraft/FIRST_RUN.md) | The checklist for the first test in the game, in order, with what to write down when a step fails |
| [Not tested in the game](minecraft/NOT_TESTED_IN_GAME.md) | Every capability verified only by unit tests or simulation, with its source and tests |

## Working on it

| | |
|---|---|
| [Project structure](project-structure.md) | What each file and folder is for |
| [Architecture](ARCHITECTURE.md) | Where new code, tests and docs go |
| [Splitting ui.py and main.py](proposals/ui-main-split.md) | A proposal: the seams, how to test each cut, what cannot be tested headless |
