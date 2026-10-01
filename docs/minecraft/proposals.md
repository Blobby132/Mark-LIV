# Proposals: water, stairs, distance

Status: proposals (B6). Nothing here is built. Each one says what the bridge
would have to report, what the task would do, where it must stop, and how to
test it before it touches a real world.

## 1. Crossing shallow water

**Today.** Water is in `LIQUIDS`, and `LocalMap.standable` refuses a column
whose floor is water. A stream one block wide makes the far bank unreachable.
`navigate_to` says so rather than wade.

**What is missing is the depth.** The scan reports one floor per column. For
water, that floor is the water's surface block, so a puddle over grass and a
ten-deep lake look the same. Wading is safe only where the bottom is close:
standing on the bottom with your head out (one block of water) is a walk.
Two blocks is a swim, and a swim is a different, slower movement that can
drown you under an overhang.

**Mod (additive, schema /4).** For a column whose floor is water, add
`depth`: the number of water blocks straight down to the first block with
collision, capped at 4. The bridge already reads the column top-down, so this
is one more pass over rows it has, with no new block lookups. `null` for a
column that is not water.

**Python.**
- `LocalMap.standable`: a water column with `depth == 1` and a solid bottom
  is standable. The player stands on the bottom, at the bottom's height plus
  one.
- `step_cost`: wading costs 3×, because walking in water is slower. A route
  prefers the bank.
- Anything deeper, flowing water (a current pushes you), and lava stay
  refused exactly as now.
- `navigate_to` names a wade in its plan: "across the stream at (x, z), one
  block deep".

**Stop conditions.** Never into a column with `depth` unknown, an old jar, or
2 or more. Never where the next column is lava. Stop if the player's y drops
below the planned bottom (a hole under the water).

**Tests.** A world with a one-deep stream between two banks: crossed. A
two-deep one: refused, with the depth named. Flowing water: refused. An old
jar without `depth`: refused, as today.

## 2. Digging a staircase

**Today.** Nothing digs. `collect_blocks` never breaks the block under the
player and never digs to reach a block, and NOT_YET_POSSIBLE says so.

**Shape.** A staircase down is a fixed pattern. Facing the way to go, break
the two blocks in front at head and feet height, plus the one below the feet
block. Step forward and down. Repeat. Every step lowers the player one
block and moves them one block forward. It never breaks the block the player
stands on, because each new floor is the block under the one stepped into,
and that one is never broken.

**Rules, checked before every swing.**
- Never the block under the feet. The pattern never asks for it, and the
  controller's `expect_at` makes the swing refuse if the crosshair lands on
  it.
- Read every block in the next step's three cells and their neighbours from
  `near_blocks` (B4d). If any of them, or any block beside or below the step
  to come, is lava or water, stop before breaking anything. Liquids flow into
  the space a break opens, and lava kills.
- Stop at a block nothing in hand can harvest (B1 already refuses it), at
  bedrock, or at a fall: the cell below the next floor is air or unknown.
- Bounded: at most N steps down per task (8). Each step is verified: the
  player's y went down by one and x/z moved by one.

**Light.** A staircase in the dark spawns mobs. Proposal: one torch every
four steps, placed with `place_block_at` against the side wall, but only if
torches are in the inventory. Torches are not in BUILDING_BLOCKS: the
crosshair proof for a wall torch names `wall_torch`, so it needs its own
verification.

**Tests.** A simulated column of stone with a lava pocket three steps down:
the staircase stops one step before it, having broken nothing next to it. A
staircase into an air gap: stops, saying it would fall. The block under the
feet is never in the swings.

## 3. Long-distance travel as chained legs

**Today.** `navigate_to` refuses a destination outside the scan, on purpose:
it plans only over ground it has seen. The scan radius is 10.

**Proposal.** `travel_to(x, z)`, a chain of `navigate_to` legs:
1. Pick the point at the edge of the current scan that is nearest the
   destination and reachable: a scanned, standable column with a route to it.
2. Walk there with `navigate_to`, which plans only over seen ground, as now.
3. Re-read. The scan has moved with the player. Repeat.

**Stop conditions.**
- A leg that gains less than 3 blocks on the destination, twice in a row:
  something is in the way (water, a cliff, a wall of trees). Say which, and
  where.
- Water or lava across the way (with proposal 1, shallow water is crossed).
- Night with mobs about (B5c), damage, or the step and time limits. A long
  trip is several tasks: as with `build_blueprint`, the destination is kept
  in memory and "carry on" resumes it.
- A cap on the straight-line distance per request (say 200 blocks), so a
  mistyped coordinate is not a walk across the world.

**Report.** The legs walked, how far is left, and what stopped it. It never
claims arrival without the position to show it.

**Tests.** A long simulated corridor of terrain revealed 10 blocks at a time
around the player: reached in legs. A river across the way: stopped at the
bank, with the river named. A dead end: stopped after two legs that gained
nothing.
