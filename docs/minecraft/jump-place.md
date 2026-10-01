# Jump, then place: pillar_up and bridge

Status: design note, written before any code. Nothing here is built yet.
`NOT_YET_POSSIBLE["pillar_up_or_bridge"]` points here.

## Why it does not work today

Building upwards past two blocks means standing on what you build:

- **Pillar up:** look straight down, jump, and at the top of the jump place a
  block into the cell your feet just left. You land on it one block higher.
- **Bridge:** sneak backwards to the edge of a block, look down and back at
  the block's side, and place against it. The new block extends the floor.

Both need two inputs at a fixed interval, inside one action. The controller
runs one hold at a time. The keys and buttons it presses go down together,
stay down for `duration`, and come up together. Nothing can be scheduled at
an offset inside a hold.

Sending the jump and the place as two runner steps does not work either:

- Between steps the runner reads a fresh state and waits out its pacing,
  about 0.2 to 0.6 s in practice.
- The bridge mod writes every 200 ms, so the read alone can add a tick.
- The window to place (below) is about 0.3 s wide, so two separate steps miss
  it more often than they hit it. A miss places nothing, or worse, places the
  block against whatever the crosshair drifted to.

## The timing, from the game's physics

At 20 ticks a second, a jump starts at 0.42 blocks/tick upwards. Each tick
gravity takes 0.08 off, then drag keeps 98 %. The feet's height above the
start, tick by tick:

| tick | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| height | 0.42 | 0.75 | 1.00 | 1.17 | 1.25 | 1.25 | 1.18 | 1.02 | 0.80 | 0.50 | 0.12 |

The cell below is free once the feet are above 1.0: from tick 3 (0.15 s) to
tick 8 (0.40 s). The middle of that is about **0.27 s**. A place pressed then
lands the block in the cell, and the player comes down onto it.

These numbers are from the vanilla physics constants, not from a measurement.
The first in-game check is to log the bridge's `position` y around a jump and
compare it with the table. The second is the delay between `SendInput` and the
game seeing the key: one frame, but under load it could be two.

## Proposal: one composite action, fixed shape

`jump_place`, a new entry in `action_spec`, with the timeline fixed in source:

1. `t = 0`: jump, a tap (space down 0.05 s).
2. `t = delay`: use, a tap (right button down 0.05 s). `delay` is bounded to
   0.20–0.35 s; default 0.27.
3. Released by `t = delay + 0.05`. The whole action is at most 0.4 s.

The only parameter is `delay`, inside that bound. There is no list of
(offset, input) pairs from the model. A timeline the caller could write would
turn the controller into a macro engine, and this does not.

Preconditions, checked as `place` checks them, before anything is pressed:

- the held item passes the A4 deny-list;
- the crosshair is on the block under the feet, face `up` (`expect_at` /
  `expect_face`), with the pitch at least 80° down;
- the cell two above the floor is free (the scan's headroom). Jumping into a
  ceiling places nothing and costs a block-height of nothing.

### Controller change

`_hold_inputs` gains an optional, internal `taps` schedule, built only by
`action_spec.parse_jump_place`:

- The hold loop already ticks every 40 ms. A scheduled tap fires on the first
  tick at or after its offset. A tap's own press is timed with a short sleep,
  so the 40 ms grain does not stretch it.
- Every tap goes through the InputLedger: recorded before it goes down, and
  released by `release_all()` like everything else.
- Before each tap the guard runs again: focus, F12, the deadman, the
  session. A guard failure releases everything and presses nothing more.
- The use tap re-checks the held item. It does not re-check the crosshair
  mid-jump: the eye rises 1.25 blocks during the jump, so the angle to the
  block below changes, but the block under the feet is still the one under
  the crosshair when looking straight down. Verification decides whether it
  worked.

Only one action at a time, as now, and F12 is as immediate as it is for any
hold.

### Verification

`pillar_up` as a skill: after the action, the feet are one block higher, the
block under them has the item's name, and the held stack is one smaller. It
does a few blocks at most, each a separate action, and stops on the first
failure.

## Bridge: later, and harder

Bridging needs feedback in the middle of the hold. Sneak and walk backwards,
stop when the heels are at the edge, then place. "At the edge" is a position,
and a fixed delay cannot know it. That needs either the progress probe to end
a hold on a position condition, which `mine` already does for a changed block,
or a short sneak-back step followed by a separate place. The second is safe
because sneaking stops you at the edge, so the timing stops mattering. The
second is simpler and needs no controller change. It should be tried first.

## Tests to write with it

- `parse_jump_place`: the delay is bounded; nothing else is accepted.
- The controller with FakeInputBackend: space down at ~0, right button down
  at ~`delay`, everything up by the end; the ledger empty afterwards.
- A guard failure between the two taps: the second never goes down.
- The held-item deny-list and `expect_at` refuse with nothing pressed.
- The skill in a simulated world with the jump curve above: the block lands
  under the feet and the player ends a block higher.

## What has to be measured in the game

1. The feet's height against time in a jump, from the bridge's position
   at 200 ms. That is coarse, so take several jumps.
2. Whether 0.27 s lands the block in the real game at the user's frame rate,
   and how wide the working delay really is.
3. Whether the server-side check (the player must not intersect the new
   block) refuses placements late in the window.
