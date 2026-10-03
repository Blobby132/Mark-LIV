"""
minecraft/skills/collect.py -- what the gathering skills share, and
break_block: the best-tool-before-each-swing mixin (_HoldsTheRightTool),
_Gatherer (walk to a block, aim, break it, pick up the drop -- the engine
under collect_logs, fell_tree and collect_blocks), and the helpers about
trees and drops. collect_logs.py, collect_blocks.py and aim.py build on it.
Moved here unchanged from skills.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft import (
    action_spec, navigation as nav, verification as verify_mod,
)
from minecraft import stuck as stuck_mod
from minecraft import aiming as aiming_mod
from minecraft import mining as mining_mod
from minecraft.state import UNKNOWN
from minecraft.task_runner import Step

from minecraft.skills.base import (
    CANNOT_SEE_TARGET, DROP_RADIUS, LOG_BLOCKS, MAX_LEAVES_PER_LOG,
    MAX_PICKUP_WALKS, MAX_SKIPPED_TARGETS, MAX_TREE_LOGS, MIN_USEFUL_MINE_S,
    _ARRIVED, _centre, _crosshair_at, _crosshair_text, _mine_params,
)
from minecraft.skills.hotbar import (
    FETCH_WORTH_S, HotbarFetch, hotbar_slot_to_fill,
)
from minecraft.skills.navigate import NavigateTo, block_label, nav_target


class _HoldsTheRightTool:
    """Hold the best hotbar tool before each swing, and put the slot back
    once the skill has finished -- the way EatFood._restore does.

    Nothing chose a tool before: a log was chopped with whatever was held,
    and stone was held at for ten seconds by hand, breaking it eventually
    and dropping nothing. A skill using this implements `_plan` instead of
    `plan`, and asks `_tool_for` before each mine step.

    A tool only in the main inventory is fetched into the hotbar first
    (HotbarFetch), once per task: when nothing in the hotbar harvests the
    block, or when it saves FETCH_WORTH_S a block. While the inventory is
    open the fetch keeps its own, stricter, danger watch and the runner's
    stands down -- see `watch_hostiles`."""

    MAX_TOOL_SELECTS = 2
    _tool_slot_before = None      # the slot to go back to, once we switched
    _tool_restoring = False
    _plan_done = False
    _tool_tries = None            # {(block, slot): selects asked for}
    _fetch = None                 # the trip into the inventory, once made
    _fetch_note = ""              # why it was not made, or failed

    @property
    def watch_hostiles(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def watch_health(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    def _with_fetch(self, text: str) -> str:
        """`text`, after what the trip into the inventory did, if it moved
        anything -- the user's hotbar changed and should hear so."""
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {text}"
        return text

    def plan(self, state, step_index: int, history: tuple):
        if self._plan_done:
            return None
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step
            if self._fetch.failed:
                self._fetch_note = (
                    f"I tried to move the "
                    f"{' '.join(self._fetch.item.split('_'))} into the hotbar "
                    f"and stopped: {self._fetch.done_reason}.")
        step = self._plan(state, step_index, history)
        if step is None:
            return self._finish(state)
        return step

    def _finish(self, state):
        """The skill is done: put the slot back once, then nothing more.
        Its own plan is not asked again -- it might start over."""
        self._plan_done = True
        if self._tool_restoring or self._tool_slot_before is None \
                or getattr(state, "selected_slot", None) \
                == self._tool_slot_before:
            return None
        self._tool_restoring = True
        slot = self._tool_slot_before + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    def _tool_for(self, state, block):
        """None when ready to swing; a Step to take up the better tool first
        (select it, or begin fetching it from the main inventory); or a
        sentence: why not to swing at all."""
        choice = mining_mod.best_hotbar_tool(state, block)
        trip = self._fetch_for(state, choice)
        if trip is not None:
            return trip
        if choice.refusal:
            return " ".join(t for t in (choice.refusal, self._fetch_note) if t)
        if choice.slot is None:
            return None
        item = " ".join((choice.item or "tool").split("_"))
        tries = self._tool_tries if self._tool_tries is not None else {}
        self._tool_tries = tries
        key = (choice.block, choice.slot)
        if tries.get(key, 0) >= self.MAX_TOOL_SELECTS:
            current = getattr(state, "selected_slot", None)
            shown = "?" if current is None else current + 1
            return (f"I selected hotbar slot {choice.slot + 1} for the {item} "
                    f"{tries[key]} times and the game still shows slot "
                    f"{shown}; I stopped rather than mine {choice.block} "
                    f"with the wrong tool.")
        tries[key] = tries.get(key, 0) + 1
        if self._tool_slot_before is None \
                and getattr(state, "selected_slot", None) is not None:
            self._tool_slot_before = state.selected_slot
        slot = choice.slot + 1
        note = f"hold the {item} for the {choice.block} (hotbar slot {slot})"
        if choice.better_in_inventory:
            note += (f"; a {' '.join(choice.better_in_inventory.split('_'))} "
                     f"in the main inventory would be faster")
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot), note=note)

    def _fetch_for(self, state, choice):
        """The first step of a trip to fetch the better tool, or None: none
        is better, the hotbar's is nearly as good, a trip was already made,
        or it cannot be (then `_fetch_note` says why)."""
        stored = choice.better_in_inventory
        if not stored or self._fetch is not None:
            return None
        if choice.harvests:
            now = getattr(choice.estimate, "seconds", None)
            if now is None or choice.stored_seconds is None \
                    or now - choice.stored_seconds < FETCH_WORTH_S:
                return None
        self._fetch = HotbarFetch(item=stored,
                                  hotbar=hotbar_slot_to_fill(state))
        step = self._fetch.plan(state)
        if step is None:
            self._fetch_note = (f"I could not fetch it: "
                                f"{self._fetch.done_reason}.")
        return step


@dataclass
class BreakBlock(_HoldsTheRightTool):
    """Attack whatever is under the crosshair until it is gone.

    THE POINT OF THIS SKILL
        It is the smallest task where "I did the action" and "I achieved the
        goal" genuinely come apart. One bounded attack does not break an oak
        log; several do. The skill therefore cannot report success from its own
        actions — it has to look at the block again between swings, and it
        stops when the block is gone, not when it has swung enough times.

        If the target cannot be read at all, every swing comes back
        UNVERIFIABLE and the task ends at the step limit without ever claiming
        the block broke."""

    expected: str = ""
    swings: int = 8
    swing_seconds: float = action_spec.DEFAULT_MINE_DURATION_S

    name = "break_block"
    verifiable_with = ("target_block",)

    @property
    def goal(self) -> str:
        return f"break the {self.expected}" if self.expected else "break the block"

    @property
    def done_reason(self) -> str:
        return self._with_fetch(self._reason)

    @property
    def failed(self) -> bool:
        """Anything short of a swing verified to have broken the block.

        Without this the runner reported every ending as "done" -- including
        "there was nothing there" and "I stopped rather than break the wrong
        block" -- and the assistant told the user the block was gone."""
        return not self._broken

    _reason: str = ""
    _broken: bool = False

    def _plan(self, state, step_index: int, history: tuple):
        block = state.target_block
        name = getattr(block, "name", None) if block else None

        # Gone: only on the word of a swing's own verification. The crosshair
        # reading air afterwards is not enough -- it read air in a real run
        # where there had never been a block, and "the block is gone" went
        # back to the user.
        mines = [r for r in history if r.step.get("action") == "mine"]
        if any(r.verification.get("status") == verify_mod.SUCCESS
               for r in mines):
            self._broken = True
            self._reason = "the block is gone"
            return None

        # Nothing there. Holding attack at air breaks nothing and would be
        # reported as trying; say so instead.
        if name in _AIR:
            if mines and mines[-1].verification.get("status") \
                    == verify_mod.UNVERIFIABLE:
                self._reason = ("the crosshair now sees nothing, but I could "
                                "not see what was there before the swing, so "
                                "I cannot say whether it broke")
            else:
                self._reason = ("there is no block under the crosshair within "
                                "reach, so there is nothing to break. For a "
                                "tree, collect_logs finds, walks to and aims "
                                "at a log by itself")
            return None

        # Aimed at the wrong thing. Stop rather than mine whatever happens to
        # be there: breaking the wrong block is worse than not breaking one.
        if self.expected and name and name != self.expected:
            self._reason = (f"the crosshair is on {name}, not "
                            f"{self.expected} — I stopped rather than break "
                            f"the wrong block")
            return None

        # Swings, not steps: taking up a tool is a step and not a swing.
        if len(mines) >= self.swings:
            self._reason = "ran out of swings"
            return None

        if name:
            ready = self._tool_for(state, name)
            if isinstance(ready, str):
                self._reason = ready
                return None
            if ready is not None:
                return ready

        return Step(
            action="mine",
            params=_mine_params(self.swing_seconds, block),
            expectation=verify_mod.block_broken(self.expected or None),
            note=f"swing {step_index + 1}",
        )


_AIR = frozenset({"air", "cave_air", "void_air"})


@dataclass
class _Gatherer(_HoldsTheRightTool):
    """Find a block, get to it, mine it, pick up what it drops, repeat.

    The machinery collect_logs was built with -- a route to somewhere to
    stand, an aim the bridge confirms block by block, a hold sized by the
    break-time model with the right hotbar tool, and a walk over to the
    drops -- for any set of blocks. Parameterised by what a subclass
    supplies: which blocks to break (`_wanted_names`), which items are
    their drops and so count (`_drop_names`), what to call one (`_noun`),
    how to pick the next one (`_next_target`), and `count`. collect_logs,
    fell_tree and collect_blocks are thin subclasses."""

    count: int = 4
    sweep_steps: int = 10
    delta_px: int | None = None
    aim_tolerance_deg: float = 6.0
    max_aim_steps: int = 6
    whole_tree: bool = False
    """fell_tree: every log of one tree it can reach, then stop -- rather
    than a count of logs from wherever they are nearest."""

    name = "gather"
    verifiable_with = ("surface", "target_block", "inventory")

    # ── what a subclass supplies ─────────────────────────────────────────

    def _wanted_names(self) -> frozenset:
        """The blocks to break."""
        return LOG_BLOCKS

    def _drop_names(self) -> frozenset:
        """The items those blocks drop: what is counted and fetched."""
        return LOG_BLOCKS

    def _noun(self, plural: bool = False) -> str:
        return "logs" if plural else "log"

    def _broken_noun(self) -> str:
        """What the broken count counts, in the pickup report."""
        return "block(s)"

    def _clears_leaves(self) -> bool:
        """Break leaves in the way of the aim? Only round a tree."""
        return False

    def _next_target(self, state):
        """The next block to go for: the nearest wanted one there is a route
        to somewhere to stand by, not given up on."""
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()), reachable_only=True,
            exclude=self._skip)

    def _nearest_seen(self, state):
        """The nearest wanted block at all -- for saying why none will do."""
        return nav.nearest_of(state, nav.blocks_matching(
            state, self._wanted_names()))

    def _none_found_note(self) -> str:
        return ""

    def _unreachable_text(self, seen) -> str:
        """Why the nearest one seen will not do."""
        return (f"I can see {block_label(seen)} at {seen.position} but no "
                f"walkable route to it.")

    def _may_break(self, state, position, name) -> bool:
        """May this block be broken at all? Asked of the block under the
        crosshair too, not only of the one chosen."""
        return True

    def _aim_face(self, state, target):
        """Which face to aim at, or None for the one turned towards us."""
        return None

    def _work_without_the_map(self, state):
        """No terrain scan: nothing to plan over."""
        self._walk_failed = (f"Collecting {self._noun(True)} needs the "
                             f"terrain scan from the bridge mod, to see "
                             f"where they are.")
        return None

    _broken: int = 0
    _collected: int = 0
    _starting_logs: int | None = None
    _can_count: bool = False
    _last_target: str = ""
    _blind: bool = False
    _used_the_map: bool = False
    _aim_target: tuple | None = None
    _aim_tries: int = 0
    _aim_gave_up: str = ""
    _in_the_way: str = ""
    _cleared: set = field(default_factory=set)
    _leaves_for: dict = field(default_factory=dict)
    _aim_errors: list = field(default_factory=list)
    _skip: set = field(default_factory=set)
    _last_estimate: object = None
    _walker: object = None
    _walker_for: tuple | None = None      # the log the walker is walking to
    _fetching_at: tuple | None = None     # where the drop being fetched was
    _walk_failed: str = ""
    _broken_at: list = field(default_factory=list)
    _pickup_walker: object = None
    _pickup_walks: int = 0
    _pickup_note: str = ""
    _fetch_baseline: int | None = None
    _fetching: str = ""
    # The tree being worked on: positions of its logs, and for every log
    # this task has seen, which tree it belongs to -- (kind, trunk column).
    _tree: set = field(default_factory=set)
    _tree_of: dict = field(default_factory=dict)
    _tree_done: bool = False
    _tree_left: tuple = ()
    # The highest y the scan reported logs at, when this tree reached it:
    # there may be more of the trunk above, which the scan never looked at.
    _tree_ceiling: int | None = None

    @property
    def _done(self) -> int:
        return self._collected if self._can_count else self._broken

    @property
    def failed(self) -> bool:
        """Blind, or short of the count. Either way this is not a success,
        and the runner reports it as incomplete rather than done.

        Felling a tree has no count: it failed if it broke nothing, or broke
        logs it then could not pick up."""
        if self.whole_tree:
            return (self._blind or self._broken == 0
                    or (self._can_count and self._collected < self._broken))
        return self._blind or self._done < self.count

    @property
    def goal(self) -> str:
        if self.whole_tree:
            return "fell the tree"
        return f"break {self.count} log(s)"

    @property
    def done_reason(self) -> str:
        if self._blind:
            return CANNOT_SEE_TARGET
        how = (" (found by the terrain scan)" if self._used_the_map
               else " (found by sweeping the crosshair)")
        # Why it stopped breaking, and why drops were left, are different
        # facts; the second used to hide the first.
        trouble = " ".join(t for t in (self._walk_failed or self._aim_gave_up,
                                       self._pickup_note) if t)
        # Later logs skipped for reach overwrite the reason, and the first
        # one -- the log it pointed straight at and could not hit -- is the
        # one that explains the rest.
        if trouble and self._in_the_way and self._in_the_way not in trouble:
            trouble = f"{trouble} Before that: {self._in_the_way}"
        if trouble and self.failed:
            return self._with_fetch(f"{self._progress_text()}{how}. {trouble}")
        return self._with_fetch(f"{self._progress_text()}{how}")

    def _progress_text(self) -> str:
        what = self._noun(True)
        if self._can_count:
            return (f"broke {self._broken} block(s) and collected "
                    f"{self._collected} of {self.count} {what}, counted in "
                    f"the inventory")
        return (f"broke {self._broken} block(s) for {self.count} {what} — I "
                f"cannot see the inventory, so I am reporting blocks that "
                f"disappeared, not items picked up")

    # ── planning ─────────────────────────────────────────────────────────

    def _plan(self, state, step_index: int, history: tuple):
        local = nav.LocalMap.from_state(state)
        crosshair_readable = state.confidence_of("target_block") != UNKNOWN

        # Neither the scan nor the crosshair: every step would be a blind
        # swing and every verdict unverifiable. Say so on the first step
        # instead of spending the whole budget discovering it.
        if not local.usable and not crosshair_readable:
            self._blind = True
            return None

        self._count_progress(state, history)
        if self.whole_tree:
            if not local.usable:
                self._walk_failed = ("Felling a whole tree needs the terrain "
                                     "scan from the bridge mod, to tell one "
                                     "tree's logs from the next.")
                return None
            if self._tree_done or self._broken >= MAX_TREE_LOGS:
                return self._wind_up(state, local, history)
        else:
            if self._done >= self.count:
                return None

            # Enough broken. Never break more than was asked for: counting
            # only the inventory, a log that broke and fell out of pickup
            # range looked like no progress at all, and "collect one log"
            # broke three. What is left is fetching the drops -- which needs
            # the inventory to see.
            if self._broken >= self.count:
                if not self._can_count:
                    return None
                return self._pick_up(state, local, history)

        if local.usable:
            self._used_the_map = True
            step = self._work_from_the_map(state, local, crosshair_readable,
                                           history)
            if step is not None:
                return step
            if self._tree_done or self._walk_failed:
                # Finished, or giving up on the rest -- either way what has
                # been broken is picked up first. Giving up used to walk away
                # from logs lying on the ground a few blocks off.
                return self._wind_up(state, local, history)
            # The map had nothing useful to add; fall through to the sweep,
            # which at least checks what is right in front of us.

        if not crosshair_readable:
            return None
        return self._work_without_the_map(state)

    def account_for(self, state, history) -> None:
        """Count the final step before reporting.

        The runner calls this when it runs out of steps. Progress is tallied
        at the start of each plan(), so the last step's verdict would
        otherwise never be counted — and a run that broke its fourth log
        would say it broke three."""
        self._count_progress(state, history)

    def _count_progress(self, state, history) -> None:
        self._can_count = state.confidence_of("inventory") != UNKNOWN
        # Broken: from the record, always. A mine step's verdict says whether
        # THAT block went, whatever the inventory later says about the drop.
        broken = [r for r in history
                  if r.step.get("action") == "mine"
                  and r.verification.get("status") == verify_mod.SUCCESS
                  and not self._was_clearing(r)]
        self._broken = len(broken)
        self._broken_at = []
        for record in broken:
            where = (record.step.get("params") or {}).get("expect_at")
            if where:
                self._broken_at.append(tuple(where))
        if self._can_count:
            # Collected: what is actually in the bag, against where it started.
            if self._starting_logs is None:
                self._starting_logs = _item_total(state, self._drop_names())
            self._collected = _item_total(state, self._drop_names()) - self._starting_logs

    def _work_from_the_map(self, state, local, crosshair_readable,
                           history=()):
        """Next log: walk to it if it is far, aim and mine if it is close."""
        target = self._next_target(state)
        if target is None and self._tree_done:
            return None
        if target is None:
            seen = self._nearest_seen(state)
            if seen is not None and seen.position in self._skip \
                    and self._aim_gave_up:
                # The logs left are ones this task already gave up aiming at.
                # Saying "no walkable route" here would be false — they are
                # reachable; the crosshair would not land on them.
                self._walk_failed = self._aim_gave_up
                return None
            if seen is None:
                self._walk_failed = (
                    f"The scan covered {local.known_columns} columns within "
                    f"{local.radius or '?'} blocks and found no "
                    f"{self._noun(True)}{self._none_found_note()}.")
            else:
                self._walk_failed = self._unreachable_text(seen)
            return None

        # Reach is measured the way the game does -- from the eyes, to the
        # face -- and the same way the crosshair gate below measures it. Two
        # different "in reach" tests is what left the last real run standing
        # at the trunk, "arrived" by one and "too far" by the other, turning
        # on the spot until it gave up.
        if not aiming_mod.SHARED.within_reach(state.position, target.position):
            step = self._walk_towards(state, target, history)
            if step is not _ARRIVED:
                return step
            # Walked as close as the ground allows and it is still out of
            # reach -- high up the trunk, or across something. Skip it.
            self._skip.add(target.position)
            self._aim_target = None
            self._aim_gave_up = (f"The {target.name} at {target.position} is "
                                 f"out of reach from anywhere I can stand "
                                 f"next to it.")
            if len(self._skip) > MAX_SKIPPED_TARGETS:
                self._walk_failed = self._aim_gave_up
                return None
            return self._work_from_the_map(state, local, crosshair_readable,
                                           history)

        # Close enough to hit.
        self._walker = None

        # The crosshair is the authority, not the angle. If the bridge says
        # it is ALREADY on a log within reach, that is the log to mine —
        # whichever one it is. A person standing at a trunk hits the log in
        # front of them, not the one the planner happened to pick first.
        under = self._log_under_crosshair(state)
        if under is not None:
            return self._mine(state, under)

        # Aim at the chosen block, on the face turned towards us -- or the
        # one face that is open, for a block set in the ground.
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   target.position,
                                   face=self._aim_face(state, target))
        if target.position != self._aim_target:
            self._aim_target = target.position
            self._aim_tries = 0
            self._aim_errors = []
        self._aim_errors.append(error)

        converging = stuck_mod.diagnose_aim(self._aim_errors)
        # Keep correcting until the BRIDGE says the crosshair is on a log --
        # not until the computed angle looks small. Reaching this line means
        # it is not (a log under the crosshair was mined above), and "within
        # six degrees, crosshair on a leaf" is precisely the case that used to
        # stop correcting and swing. The bound on tries, the convergence check
        # and a zero-pixel correction are what end it -- and ending it never
        # mines; see below.
        if ((dx or dy) and self._aim_tries < self.max_aim_steps
                and converging is None):
            self._aim_tries += 1
            return Step(
                action="look", params={"dx": dx, "dy": dy},
                expectation=verify_mod.turned(min_degrees=1.0),
                note=(f"aim at {target.name} {target.position} "
                      f"({error:.0f}° off, try {self._aim_tries} of "
                      f"{self.max_aim_steps}; {_crosshair_text(state)})"))

        # Pointed as well as we are going to get, and the crosshair is NOT on
        # a log. Leaves in front of it are broken on purpose, a few at most;
        # anything else -- the trunk's own edge, another block -- or an aim
        # that stopped converging, and holding attack now would break
        # whatever IS under the crosshair, which is exactly the thing not to
        # do. Try a different log instead.
        if converging is None and self._clears_leaves():
            clearing = self._clear_leaves(state, target)
            if clearing is not None:
                return clearing
        seen = getattr(state, "target_block", None)
        seen_name = getattr(seen, "name", None) or "nothing"
        if converging is not None:
            self._aim_gave_up = (f"I could not settle the crosshair on "
                                 f"{target.name} at {target.position}: "
                                 f"{converging.describe()}.")
        else:
            self._aim_gave_up = (
                f"I aimed at the {target.name} at {target.position} but the "
                f"crosshair lands on {seen_name} — something is in the way.")
            if not self._in_the_way:
                self._in_the_way = self._aim_gave_up
        self._skip.add(target.position)
        self._aim_target = None
        if len(self._skip) > MAX_SKIPPED_TARGETS:
            self._walk_failed = self._aim_gave_up
            return None
        return self._work_from_the_map(state, local, crosshair_readable,
                                       history)

    def _tally(self) -> str:
        if self.whole_tree:
            return f"{self._broken} broken from this tree so far"
        return f"{self._done}/{self.count}"

    def _wind_up(self, state, local, history):
        """Nothing more to break for now: pick up what fell, then stop.

        Not a one-way door. A tree that comes into view on the walk to a
        drop is still worth going for; only when there is nothing left to
        break does picking up become the last thing it does."""
        if self._can_count and self._collected < self._broken:
            return self._pick_up(state, local, history)
        return None

    def _clear_leaves(self, state, target):
        """A step breaking the leaf block between the eye and `target`, or
        None if the crosshair is not on one worth breaking."""
        seen = _crosshair_at(state)
        if seen is None:
            return None
        position, name = seen
        if not str(name).endswith("_leaves") or position in self._skip:
            return None
        cleared = self._leaves_for.get(target.position, 0)
        if cleared >= MAX_LEAVES_PER_LOG:
            return None
        if not aiming_mod.SHARED.within_reach(state.position, position):
            return None
        # The tool first, before anything below counts this leaf as cleared.
        ready = self._tool_for(state, name)
        if isinstance(ready, str):
            return None
        if ready is not None:
            return ready
        # In the way means NEARER than the log. A leaf behind it would mean
        # the aim is off, and breaking it would fix nothing.
        try:
            eye = (state.position[0], state.position[1] + aiming_mod.EYE_HEIGHT,
                   state.position[2])
        except (TypeError, IndexError):
            return None
        if aiming_mod.distance_to(eye, _centre(position)) >= \
                aiming_mod.distance_to(eye, _centre(target.position)):
            return None

        self._leaves_for[target.position] = cleared + 1
        self._cleared.add(position)
        # A fresh aim at the same log once the leaf is gone.
        self._aim_tries = 0
        self._aim_errors = []
        estimate = mining_mod.estimate_break_duration(name, state=state)
        return Step(action="mine",
                    params={"duration": self._mine_seconds(estimate),
                            "expect_at": list(position)},
                    expectation=verify_mod.broke_block_at(position, name),
                    note=(f"clear {name} at {position}, in the way of the "
                          f"{target.name} at {target.position} "
                          f"({cleared + 1} of {MAX_LEAVES_PER_LOG})"))

    def _was_clearing(self, record) -> bool:
        """Did this mine step break leaves in the way, not a log?"""
        where = (record.step.get("params") or {}).get("expect_at")
        try:
            return tuple(int(v) for v in where) in self._cleared
        except (TypeError, ValueError):
            return False

    def _log_under_crosshair(self, state):
        """The log the bridge says the crosshair is on, if it is within reach.

        Returns a NearbyBlock-like object with a position and a name, or
        None. Reach matters: a crosshair on a log eight blocks away is a
        perfectly good aim that breaks nothing."""
        target = getattr(state, "target_block", None)
        if target is None or getattr(target, "name", None) \
                not in self._wanted_names():
            return None
        try:
            position = (int(target.x), int(target.y), int(target.z))
        except (TypeError, ValueError):
            return None
        if position in self._skip:
            return None
        if not self._may_break(state, position, target.name):
            return None
        # Once a tree is started, a log of ANOTHER tree that happens to be
        # under the crosshair is not the next one: finishing this tree is.
        if self._tree and position not in self._tree:
            return None
        if not aiming_mod.SHARED.within_reach(state.position, position):
            return None
        return nav_target(position, target.name)

    def _mine(self, state, target):
        """Hold attack on a block the crosshair is CONFIRMED to be on --
        with the best hotbar tool for it in hand first."""
        ready = self._tool_for(state, target.name)
        if isinstance(ready, str):
            self._walk_failed = ready
            return None
        if ready is not None:
            return ready
        self._last_target = target.name
        estimate = mining_mod.estimate_break_duration(target.name, state=state)
        self._last_estimate = estimate
        # The step is judged on what it did: did THIS block go. Whether its
        # drop reached the inventory is a separate question, answered by the
        # count and the pickup that follows -- judging a mine by the bag
        # called a broken log a failure and sent the task to break another.
        check = verify_mod.broke_block_at(target.position, target.name)
        # `expect_at` makes the controller read the crosshair once more at
        # the instant before the button goes down, and press nothing unless
        # it is still on this exact coordinate.
        return Step(action="mine",
                    params={"duration": self._mine_seconds(estimate),
                            "expect_at": list(target.position)},
                    expectation=check,
                    note=(f"mine {target.name} at {target.position} "
                          f"({self._tally()}; {estimate.describe()})"))

    def _walk_towards(self, state, target, history=()):
        """Delegate the walking to the navigation skill.

        Delegated rather than reimplemented: a second movement loop would be a
        second set of stuck rules, a second idea of what counts as progress,
        and a second thing to get wrong. NavigateTo already refuses routes it
        cannot see and gives up honestly, and those are exactly the properties
        this needs.

        One walker per target. _next_log re-picks the nearest log every
        step, and a walker kept from an earlier pick walked to THAT log's
        column -- then "arrived", and the new log was written off as out of
        reach from anywhere I could stand. A new pick whose standing column
        is the same keeps the walk, and the stall count in it."""
        if self._walker is not None and self._walker_for != target.position \
                and nav.approach_column(state, target) \
                != self._walker.destination:
            self._walker = None
        self._walker_for = target.position
        if self._walker is None:
            column = nav.approach_column(state, target)
            if column is None:
                self._walk_failed = (
                    f"I can see {block_label(target)} at {target.position} "
                    f"but there is nowhere next to it I can stand.")
                return None
            self._walker = NavigateTo(destination=column)

        # The real history, not an empty tuple: NavigateTo reads the last
        # record to decide whether it is stuck, and handing it a blank slate
        # every call would switch its stall detection off entirely. It
        # ignores records for actions it did not ask for, so mining steps in
        # the middle of the trail cost it nothing.
        step = self._walker.plan(state, 0, history)
        if step is None:
            if self._walker.failed:
                self._walk_failed = self._walker.done_reason
                return None
            self._walker = None
            return _ARRIVED
        return step

    # ── fetching the drops ───────────────────────────────────────────────

    def _pick_up(self, state, local, history):
        """Walk onto the logs this task broke, so they reach the inventory.

        A broken log drops an item where it falls, and the player only picks
        up what it walks within about a block of. Mining from a few blocks
        away -- which reach allows -- leaves the drop lying there. The bridge
        reports item entities with positions, so the drop can be walked to.
        Bounded: MAX_PICKUP_WALKS walks, then an honest account of where the
        rest are."""
        drops = self._drops(state)
        drop = drops[0] if drops else None
        if drop is None:
            self._pickup_walker = None
            self._pickup_note = (
                f"I broke {self._broken} {self._broken_noun()} but only {self._collected} "
                f"reached the inventory, and I cannot see the rest lying "
                f"anywhere near where they fell.")
            return None
        where = _drop_text(drop)
        if not local.usable:
            self._pickup_note = (f"The dropped {self._noun()} is on the ground at "
                                 f"{where}, but I cannot see the terrain to "
                                 f"walk to it.")
            return None

        walker = self._pickup_walker
        if walker is not None and not _still_there(drops, self._fetching_at):
            # Gone before we got there: picked up on the way, merged into
            # another stack, or carried off. Walking on to where it was
            # fetches nothing and spends a walk; go for what is left.
            self._pickup_walker = None
            self._fetching_at = None
            return self._pick_up(state, local, history)
        if walker is None:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and I could not get to it after {self._pickup_walks} "
                    f"tries.")
                return None
            # Nearest first, but one that cannot be reached is no reason to
            # leave the others lying there.
            target = None
            for candidate in drops:
                target = _pickup_column(state, local, candidate)
                if target is not None:
                    drop, where = candidate, _drop_text(candidate)
                    break
            if target is None:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is on the ground at {where} "
                    f"and there is nowhere I can stand close enough to pick "
                    f"it up.")
                return None
            column, within = target
            self._pickup_walks += 1
            walker = NavigateTo(destination=column, arrive_within=within)
            self._pickup_walker = walker
            self._fetch_baseline = _item_total(state, self._drop_names())
            self._fetching = where
            self._fetching_at = drop.position

        step = walker.plan(state, 0, history)
        if step is not None:
            return step
        self._pickup_walker = None
        # Picked up on the way: pickup happens on contact, often a step
        # before "arriving". Checking the bag against the moment AFTER that
        # reads as nothing gained -- the real run's "no more birch_log than
        # before", for an oak log that had just been collected.
        if self._fetch_baseline is not None \
                and _item_total(state, self._drop_names()) > self._fetch_baseline:
            self._fetch_baseline = None
            return self._pick_up(state, local, history)
        if walker.failed:
            if self._pickup_walks >= MAX_PICKUP_WALKS:
                self._pickup_note = (
                    f"I broke {self._broken} {self._broken_noun()} and picked up "
                    f"{self._collected}; the rest is at {where} and I could "
                    f"not walk there: {walker.done_reason}")
                return None
            return self._pick_up(state, local, history)
        # Standing on it. Pickup happens on contact within a tick or two;
        # one observed pause lets the inventory catch up -- and is CHECKED,
        # against the bag, so it is never a filler step.
        return Step(action="look", params={"dx": 1, "dy": 0},
                    expectation=verify_mod.collected(
                        self._drop_names(), label=self._noun(True)),
                    note=(f"standing where the dropped {self._noun()} is "
                          f"({self._fetching or where})"))

    def _could_be_a_drop(self, entity) -> bool:
        """A dropped item that is one of ours -- or might be, when an older
        jar does not say what the item is."""
        stack = getattr(entity, "item", None)
        return stack is None or getattr(stack, "name", None) \
            in self._drop_names()

    def _drops(self, state):
        """Logs on the ground near blocks this task broke, nearest first.

        Saplings, sticks and apples fall out of the leaves round a felled
        tree; walking to those collects nothing this task counts. A jar that
        says what each item is lets them be left; an older one does not, and
        then every item near a break is still worth a look."""
        items = [e for e in (getattr(state, "nearby_entities", None) or ())
                 if getattr(e, "category", None) == "item"
                 and getattr(e, "position", None) is not None
                 and self._could_be_a_drop(e)]
        if not items or state.position is None:
            return []
        anchors = self._broken_at or [tuple(state.position)]

        def near_a_break(entity):
            ex, ez = entity.position[0], entity.position[2]
            return any(math.hypot(ex - (a[0] + 0.5), ez - (a[2] + 0.5))
                       <= DROP_RADIUS for a in anchors)

        return sorted((e for e in items if near_a_break(e)),
                      key=lambda e: math.hypot(
                          e.position[0] - state.position[0],
                          e.position[2] - state.position[2]))

    @staticmethod
    def _mine_seconds(estimate=None) -> float:
        """How long to hold, from the break-time model when there is one.

        Never below MIN_USEFUL_MINE_S when the estimate is missing, because
        Minecraft discards breaking progress the moment the button comes up
        and a too-short hold breaks nothing however often it is repeated.
        With an estimate the hold can be shorter — dirt with a shovel is a
        fifth of a second — and the controller still lets go early when the
        bridge sees the block go."""
        if estimate is None or not estimate.breakable:
            return max(MIN_USEFUL_MINE_S, action_spec.DEFAULT_MINE_DURATION_S)
        return max(0.25, estimate.hold_seconds(
            action_spec.MAX_MINE_DURATION_S))

    def replan(self, state, reason, history):
        """Stuck: stop mining this log and look for another.

        Only offered when the last thing tried was mining. Offering it while
        already sweeping hands back the action that is failing, which resets
        the stuck detector without changing anything — the loop that made this
        task spin twenty times. The runner caps replans as a second defence,
        but a replan that is not an alternative should not be offered at all.

        Blindness gets no alternative: turning does not make an unreadable
        screen readable."""
        if reason != "no_progress":
            return None
        last = history[-1].step.get("action") if history else ""
        if last != "mine":
            return None
        return "sweep for a different log"


def _pickup_column(state, local, drop):
    """Where to stand to pick `drop` up: (column, arrive_within), or None.

    The drop's own column first. But a log broken at the bottom of a trunk
    drops INTO the trunk's column, under the logs still standing above it,
    where nobody fits -- and Minecraft collects from a box about 1.4 blocks
    either side of the player, so standing in the next column over does it.
    Hence the neighbours, nearest to the player first, each only if there is
    a walkable route to it. Stopping closer to a neighbour's centre keeps the
    item inside that box."""
    try:
        cx = int(math.floor(drop.position[0]))
        cz = int(math.floor(drop.position[2]))
        px, pz = state.position[0], state.position[2]
    except (TypeError, IndexError, ValueError, AttributeError):
        return None
    here = (int(math.floor(px)), int(math.floor(pz)))
    neighbours = sorted(((cx + 1, cz), (cx - 1, cz), (cx, cz + 1), (cx, cz - 1)),
                        key=lambda c: math.hypot(c[0] + 0.5 - px,
                                                 c[1] + 0.5 - pz))
    # Logs still standing over the drop mean it is under the trunk. The scan
    # lists every log, so this does not depend on how the ground there was
    # read: a real run with an older mod routed into a trunk's column for a
    # pickup and walked into the tree.
    candidates = [(c, 0.35) for c in neighbours]
    if not _under_the_trunk(state, (cx, cz), drop.position[1]):
        candidates.insert(0, ((cx, cz), 0.6))
    for column, within in candidates:
        if column == here:
            return column, within
        if not local.standable(*column):
            continue
        if nav.find_path(state, column).found:
            return column, within
    return None


def _under_the_trunk(state, column, drop_y) -> bool:
    """Is there a log in this column above a drop lying at `drop_y`?"""
    for block in (getattr(state, "notable_blocks", None) or ()):
        if (block.name in LOG_BLOCKS and (block.x, block.z) == column
                and block.y >= math.floor(drop_y)):
            return True
    return False


FETCHED_DROP_DRIFT = 1.5
"""How far a dropped item may move and still be the one being fetched.
Items slide a little after they fall; further than this, the walk to where
it was would no longer pick it up anyway."""


def _still_there(drops, where) -> bool:
    """Is the drop a pickup walk set out for still (about) where it was?"""
    if where is None:
        return True
    return any(math.hypot(d.position[0] - where[0], d.position[2] - where[2])
               <= FETCHED_DROP_DRIFT for d in drops)


def _drop_text(entity) -> str:
    try:
        x, y, z = entity.position
        return f"({x:.0f}, {y:.0f}, {z:.0f})"
    except (TypeError, ValueError):
        return "somewhere nearby"


def _item_total(state, names) -> int:
    """Every stack of any of `names` in the inventory, added up."""
    total = 0
    for stack in (state.inventory or ()):
        if getattr(stack, "name", None) in names:
            total += int(getattr(stack, "count", 0) or 0)
    return total
