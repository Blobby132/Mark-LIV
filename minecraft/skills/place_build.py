"""
minecraft/skills/place_build.py -- placing and building: place_block,
place_block_at, build_line and build_blueprint (with the blueprint memory
that lets "carry on" resume one). Moved here unchanged from skills.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft import navigation as nav, verification as verify_mod
from minecraft import aiming as aiming_mod
from minecraft import building as building_mod
from minecraft.state import EXACT
from minecraft.task_runner import MAX_TASK_STEPS, Step

from minecraft.skills.base import _interactive, _words
from minecraft.skills.hotbar import HotbarFetch, hotbar_slot_to_fill
from minecraft.skills.navigate import NavigateTo


@dataclass
class PlaceBlock:
    """Select a slot, then place one block against what the crosshair is on.

    Two steps rather than one because they fail differently: the wrong slot
    means the wrong block, and no valid surface means nothing happens at all.
    Reporting "placed" for either would be a lie of a different kind.

    HONEST LIMIT ON VERIFICATION
        Placement is confirmed by the targeted block changing — the new block
        is now what the crosshair sees. That works when aiming at the face a
        block lands on and fails when it lands out of view, so the result can
        be UNVERIFIABLE even when the block really was placed. Confirming it
        properly needs the inventory count, which is not on the F3 overlay."""

    slot: int = 1
    _placed: bool = False

    name = "place_block"
    verifiable_with = ("target_block",)

    @property
    def goal(self) -> str:
        return f"place the block in slot {self.slot}"

    @property
    def done_reason(self) -> str:
        return ("placed, as far as the crosshair can tell" if self._placed
                else "nothing was placed")

    def plan(self, state, step_index: int, history: tuple):
        if step_index == 0:
            return Step(action="hotbar_select", params={"slot": self.slot},
                        expectation=verify_mod.holding_slot(self.slot),
                        note=f"select slot {self.slot}")
        if step_index == 1:
            return Step(action="place", params={},
                        expectation=verify_mod.target_changed(),
                        note="place one block")
        self._placed = any(
            r.verification.get("status") == verify_mod.SUCCESS
            for r in history)
        return None


# ── One block into one cell ──────────────────────────────────────────────────

MAX_PLACE_AIMS = 8
"""Corrections to get the crosshair onto the face before giving up."""

MAX_PLACE_WALKS = 2
"""Walks to a place to stand before giving up."""

PLACE_REACH_MARGIN = 0.3
"""How far inside the game's reach the face must be. The aim point is not
exactly where the ray will touch the face."""


def _place_from(position, cell, ref) -> bool:
    """Could a player whose feet are at `position` place into `cell`
    against `ref`: out of the cell, on the outer side of the face, and
    within reach of it?"""
    if position is None:
        return False
    eye = (position[0], position[1] + aiming_mod.EYE_HEIGHT, position[2])
    point = aiming_mod.target_point(ref.position, position, ref.face)
    return (not building_mod.body_overlaps(position, cell)
            and building_mod.faces_towards(eye, ref.position, ref.face)
            and aiming_mod.distance_to(eye, point)
            <= aiming_mod.REACH - PLACE_REACH_MARGIN)


def _in_view(local, position, cell, ref) -> bool:
    """Does the map show nothing between the eye at `position` and the
    face? A block placed a moment ago is in the map: a row is not built by
    looking through its own first block."""
    eye = (position[0], position[1] + aiming_mod.EYE_HEIGHT, position[2])
    point = aiming_mod.target_point(ref.position, position, ref.face)
    own = (math.floor(position[0]), math.floor(position[2]))
    return local.sight_is_clear(
        eye, point, skip=(own, (cell[0], cell[2]),
                          (ref.position[0], ref.position[2])))


def _can_place_here(state, cell, ref) -> bool:
    if not _place_from(state.position, cell, ref):
        return False
    return _in_view(nav.LocalMap.from_state(state), state.position, cell, ref)


def _stand_for(state, cell, ref, avoid=()):
    """The nearest column with a route to it from which `_place_from` holds
    and the map shows nothing between the eye and the face -- or None."""
    local = nav.LocalMap.from_state(state)
    if not local.usable:
        return None
    here = local.origin
    best = None
    for column in nav.reachable_columns(local):
        if column in avoid or abs(column[0] - cell[0]) > 4 \
                or abs(column[1] - cell[2]) > 4:
            continue
        if not local.standable(*column) or not local.fits(*column):
            continue
        feet = (column[0] + 0.5, local.ground_at(*column) + 1,
                column[1] + 0.5)
        if not _place_from(feet, cell, ref) \
                or not _in_view(local, feet, cell, ref):
            continue
        cost = (abs(column[0] - here[0]) + abs(column[1] - here[2]),
                column)
        if best is None or cost < best[0]:
            best = (cost, column)
    return None if best is None else best[1]


def _face_words(ref) -> str:
    side = {"up": "top", "down": "underside"}.get(ref.face,
                                                  f"{ref.face} face")
    return f"the {side} of the {_words(ref.name)} at {ref.position}"


@dataclass
class PlaceBlockAt:
    """place_block_at: put one `item` block into the cell (x, y, z).

    THE CELL FIRST
        Refused unless the scan saw the cell empty or holding a plant the
        game replaces (minecraft/building.py) -- never into a block, never
        into space nobody looked at, never with anything but a plain
        building block.
    AGAINST A KNOWN BLOCK, ON A NAMED FACE
        A block cannot float: it goes against a solid neighbour the scan or
        the task itself confirmed, never one a right-click would use instead
        (a chest, a door, a crafting table). It stands within reach of that
        face, on its outer side and out of the cell, aims, and presses
        nothing until the game reports the crosshair on that block and that
        face -- which the controller checks once more as it presses
        (expect_at, expect_face). A plant in the cell is under the
        crosshair instead, and the game puts the block in its place.
    PROVEN TWICE
        The crosshair on a block of that name at the cell AND the held stack
        one smaller. In creative the stack does not go down, and the report
        says the crosshair is the only proof.

    One press at most: a press whose result cannot be proven is reported,
    never repeated -- a second could put a block somewhere else. Used on its
    own and, cell by cell, by the build tasks, which pass the cells they
    placed as `known`."""

    x: int = 0
    y: int = 0
    z: int = 0
    item: str = ""
    known: dict = field(default_factory=dict)
    restore_slot: bool = True
    avoid: frozenset = frozenset()   # (x, z) columns never to stand in
    allowed: frozenset = building_mod.BUILDING_BLOCKS
    """What it may place. craft_item passes {"crafting_table"}: a table is
    a full block of its own name -- a fine thing to place, only never a
    thing to place AGAINST."""

    name = "place_block_at"
    verifiable_with = ("target_block", "inventory")

    _phase: str = "check"
    _reason: str = ""
    _placed: bool = False
    _ref: object = None
    _fetch: object = None
    _walker: object = None
    _walks: int = 0
    _aims: int = 0
    _misses: int = 0
    _selects: int = 0
    _previous_slot: int | None = None
    _restored: bool = False
    _tried: tuple = ()
    _fatal: bool = False         # no point trying another cell either

    @property
    def cell(self) -> tuple:
        return (int(self.x), int(self.y), int(self.z))

    @property
    def goal(self) -> str:
        return f"place {_words(self.item)} at {self.cell}"

    @property
    def failed(self) -> bool:
        return not self._placed

    @property
    def placed(self) -> bool:
        return self._placed

    @property
    def done_reason(self) -> str:
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {self._reason}"
        return self._reason

    @property
    def watch_hostiles(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def watch_health(self) -> bool:
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def fatal(self) -> bool:
        """Failed for a reason another cell would fail for too: nothing left
        to place, or the item could not be brought to hand."""
        return self._fatal

    def _stop(self, reason, placed=False, fatal=False):
        self._reason = reason
        self._placed = placed
        self._fatal = fatal
        self._phase = "restore"
        return None

    @property
    def finished(self) -> bool:
        return self._phase == "done"

    @property
    def awaiting_verdict(self) -> bool:
        return self._phase == "placed"

    def plan(self, state, step_index: int, history: tuple):
        last = history[-1] if history else None
        if self._phase == "done":
            return None
        if self._phase == "restore":
            return self._restore(state)
        step = self._step(state, step_index, history, last)
        if step is None and self._phase == "restore":
            return self._restore(state)
        return step

    def _restore(self, state):
        self._phase = "done"
        if not self.restore_slot or self._restored \
                or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restored = True
        self._phase = "restore"          # one more look, to finish
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    def _step(self, state, step_index, history, last):
        if self._phase == "placed":
            return self._judge(state, last)
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step
            if self._fetch.failed:
                return self._stop(f"The {_words(self.item)} is in the main "
                                  f"inventory and I could not move it to "
                                  f"the hotbar: {self._fetch.done_reason}.",
                                  fatal=True)
        if self._phase == "check":
            problem = self._check(state)
            if problem:
                return self._stop(problem, fatal=self._fatal)
            self._phase = "work"
            if self._previous_slot is None:
                self._previous_slot = state.selected_slot
        if self._walker is not None:
            step = self._walker.plan(state, step_index, history)
            if step is not None:
                return step
            failed = self._walker.failed
            why = self._walker.done_reason
            self._walker = None
            if failed and not self._here_will_do(state) \
                    and self._walks >= MAX_PLACE_WALKS:
                return self._stop(f"I could not get to a place to stand: "
                                  f"{why}")
        held = self._hold(state)
        if held is not None:
            return held
        if not self._here_will_do(state):
            return self._walk(state)
        return self._aim_or_place(state)

    def _here_will_do(self, state) -> bool:
        column = (math.floor(state.position[0]), math.floor(state.position[2]))
        return column not in self.avoid \
            and _can_place_here(state, self.cell, self._ref)

    # ── before anything ──────────────────────────────────────────────────

    def _check(self, state):
        item = building_mod.short(self.item)
        self.item = item
        if item not in self.allowed:
            return (f"I only build with plain blocks -- "
                    f"{', '.join(sorted(n for n in building_mod.BUILDING_BLOCKS if not n.endswith('_planks')))}"
                    f" and any planks -- not {_words(item) or 'nothing'}.")
        if state.inventory is None or state.position is None \
                or state.confidence_of("target_block") != EXACT:
            return ("Placing a block at a coordinate needs the bridge mod: "
                    "without it I cannot see the cell, the crosshair's "
                    "block and face, or the inventory.")
        if verify_mod._count_of(state, {item}) <= 0:
            self._fatal = True
            return f"There is no {_words(item)} in the inventory."
        local = nav.LocalMap.from_state(state)
        verdict, name = building_mod.what_is_at(local, state, self.cell,
                                                self.known)
        if verdict == building_mod.OCCUPIED:
            return (f"{self.cell} already holds "
                    f"{'a ' + _words(name) if name else 'a block'}; I only "
                    f"place into empty space or grass the game replaces.")
        if verdict == building_mod.UNKNOWN:
            return (f"I cannot see that {self.cell} is empty -- it is below "
                    f"the ground, above the headroom the scan measures, or "
                    f"outside it -- so I placed nothing.")
        refs = building_mod.references(local, state, self.cell, self.known,
                                       unusable=_interactive)
        if not refs:
            blocked = [n for n in (building_mod.solid_at(
                local, state, building_mod.offset(self.cell, d), self.known)
                for d, _f in building_mod.NEIGHBOURS) if n]
            if blocked:
                return (f"The only blocks next to {self.cell} are "
                        f"{', '.join(_words(b) for b in blocked)}; "
                        f"right-clicking would use them instead of placing "
                        f"against them.")
            return (f"There is nothing solid I know of next to {self.cell} "
                    f"to place against -- a block cannot float.")
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        for ref in refs:
            if (here not in self.avoid
                    and _can_place_here(state, self.cell, ref)) \
                    or _stand_for(state, self.cell, ref,
                                  avoid=self.avoid) is not None:
                self._ref = ref
                return None
        return (f"There is nowhere I can reach to stand and place at "
                f"{self.cell} -- every block next to it is out of reach or "
                f"out of sight from where I can walk.")

    # ── the block in hand ────────────────────────────────────────────────

    def _hold(self, state):
        """A step to get the item into the hand, or None when it is."""
        stacks = [s for s in (state.inventory or ())
                  if s.name == self.item and (s.count or 0) > 0
                  and s.slot is not None]
        selected = state.selected_slot
        if any(s.slot == selected for s in stacks):
            return None
        hotbar = [s for s in stacks if 0 <= s.slot <= 8]
        if hotbar:
            if self._selects >= 2:
                return self._stop(f"I selected the {_words(self.item)} twice "
                                  f"and the game still shows another slot.")
            self._selects += 1
            slot = max(hotbar, key=lambda s: (s.count, -s.slot)).slot + 1
            return Step(action="hotbar_select", params={"slot": slot},
                        expectation=verify_mod.holding_slot(slot),
                        note=f"hold the {_words(self.item)} (hotbar slot "
                             f"{slot})")
        if self._fetch is None and stacks:
            self._fetch = HotbarFetch(item=self.item,
                                      hotbar=hotbar_slot_to_fill(state))
            step = self._fetch.plan(state)
            if step is not None:
                return step
            return self._stop(f"The {_words(self.item)} is only in the main "
                              f"inventory and I could not move it to the "
                              f"hotbar: {self._fetch.done_reason}.",
                              fatal=True)
        return self._stop(f"There is no {_words(self.item)} left to hold.",
                          fatal=True)

    # ── where to stand ───────────────────────────────────────────────────

    def _walk(self, state):
        if self._walks >= MAX_PLACE_WALKS:
            return self._stop(f"I could not find a spot to stand within "
                              f"reach of {_face_words(self._ref)} and out of "
                              f"the way of {self.cell}.")
        here = building_mod.cell_of((math.floor(state.position[0]), 0,
                                     math.floor(state.position[2])))
        column = _stand_for(state, self.cell, self._ref,
                            avoid=set(self._tried) | {(here[0], here[2])}
                            | set(self.avoid))
        if column is None:
            return self._stop(f"There is nowhere I can walk to that reaches "
                              f"{_face_words(self._ref)} without standing "
                              f"in {self.cell}.")
        self._walks += 1
        self._tried += (column,)
        self._walker = NavigateTo(destination=column, arrive_within=0.4,
                                  keep_off=frozenset(self.avoid))
        step = self._walker.plan(state, 0, ())
        if step is None:
            self._walker = None
            return self._stop(f"I could not walk to {column}.")
        return step

    # ── aim, then one press ──────────────────────────────────────────────

    def _on_target(self, state):
        """The block to require under the crosshair as the button goes
        down, and its face (None: any face) -- or None, not on it yet."""
        seen = state.target_block
        try:
            where = (int(seen.x), int(seen.y), int(seen.z))
        except (AttributeError, TypeError, ValueError):
            return None
        face = str(getattr(seen, "face", "") or "").lower()
        if where == tuple(self._ref.position) and face == self._ref.face:
            return where, face
        if where == self.cell and building_mod.short(seen.name) \
                in building_mod.REPLACEABLE:
            return where, None
        return None

    def _aim_or_place(self, state):
        target = self._on_target(state)
        if target is not None:
            local = nav.LocalMap.from_state(state)
            verdict, name = building_mod.what_is_at(local, state, self.cell,
                                                    self.known)
            if verdict not in (building_mod.EMPTY, building_mod.REPLACE):
                return self._stop(f"{self.cell} no longer looks empty "
                                  f"({_words(name or 'something')} or "
                                  f"unknown); I did not press.")
            params = {"expect_at": list(target[0])}
            if target[1] is not None:
                params["expect_face"] = target[1]
            creative = getattr(state, "game_mode", None) == "creative"
            self._phase = "placed"
            return Step(action="place", params=params,
                        expectation=verify_mod.placed_block(
                            self.cell, self.item, state.selected_slot,
                            count_proof=not creative),
                        note=(f"place {_words(self.item)} at {self.cell} "
                              f"against {_face_words(self._ref)}"))
        dx, dy, error = nav.aim_at(state.position, state.rotation,
                                   self._ref.position, face=self._ref.face)
        if self._aims >= MAX_PLACE_AIMS or (dx == 0 and dy == 0):
            # Aimed as well as it can be and still not on the face:
            # something the map did not show is in the way from here. Try
            # once more from somewhere else.
            if self._walks < MAX_PLACE_WALKS:
                self._aims = 0
                return self._walk(state)
            return self._stop(self._missed_text(state))
        self._aims += 1
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.3),
                    note=(f"aim at {_face_words(self._ref)} ({error:.1f}° "
                          f"off; correction {self._aims} of "
                          f"{MAX_PLACE_AIMS})"))

    def _missed_text(self, state):
        seen = state.target_block
        on = (f"{_words(seen.name)} at ({seen.x}, {seen.y}, {seen.z}), "
              f"{getattr(seen, 'face', None) or 'no'} face"
              if seen is not None and getattr(seen, "x", None) is not None
              else "nothing in reach")
        return (f"I could not get the crosshair onto {_face_words(self._ref)}"
                f" -- it is on {on}. Something may be in the way. I placed "
                f"nothing.")

    def _judge(self, state, last):
        if last is None or last.step.get("action") != "place":
            return self._stop("I lost track of the place step.")
        result = last.action_result or {}
        if result.get("stopped_reason") == "target_not_confirmed":
            # Nothing was pressed: the crosshair moved off at the last
            # moment. Aim again, twice at most.
            self._misses += 1
            if self._misses > 2:
                return self._stop("The crosshair kept slipping off the face "
                                  "as I went to press. I placed nothing.")
            self._phase = "work"
            return self._aim_or_place(state)
        if not result.get("ok", True):
            return self._stop(f"The place was refused: "
                              f"{result.get('error') or 'no reason given'}")
        reason = last.verification.get("reason", "")
        if last.verification.get("status") == verify_mod.SUCCESS:
            self.known[self.cell] = self.item
            return self._stop(f"placed {_words(self.item)} at {self.cell} "
                              f"against {_face_words(self._ref)}: {reason}",
                              placed=True)
        return self._stop(f"I pressed place once and could not prove a "
                          f"{_words(self.item)} went into {self.cell}: "
                          f"{reason} I did not press again.")


# ── Several blocks ───────────────────────────────────────────────────────────

MAX_LINE_BLOCKS = 16
"""The longest build_line. A task's 45 steps place roughly fifteen."""

MAX_FAILURES_IN_A_ROW = 3
"""Cells failed one after another before a build gives up on the rest."""

_LINE_DIRECTIONS = {"north": (0, 0, -1), "south": (0, 0, 1),
                    "west": (-1, 0, 0), "east": (1, 0, 0), "up": (0, 1, 0)}


def _cells_text(cells) -> str:
    return ", ".join(str(tuple(c)) for c in cells) or "none"


@dataclass
class _Builder:
    """Places a list of cells one at a time with PlaceBlockAt, sharing what
    it has placed (`known`) so each block can be placed against the last.

    A cell that fails is reported and the next is tried -- three failures
    in a row, or one that would fail every cell (no blocks left), end it.
    The report names every cell placed and every cell that failed, why, and
    what the inventory says about the total. Subclasses give `_targets`
    (the cells, in order) and may override `_next_cell`."""

    item: str = ""

    verifiable_with = ("target_block", "inventory")

    _phase: str = "prepare"
    _reason: str = ""
    _cells: list = field(default_factory=list)
    _done: set = field(default_factory=set)
    _placed: list = field(default_factory=list)
    _failed: list = field(default_factory=list)
    _known: dict = field(default_factory=dict)
    _current: object = None
    _start_count: int | None = None
    _creative: bool = False
    _previous_slot: int | None = None
    _restored: bool = False
    _in_a_row: int = 0
    _last_state: object = None
    _stopped: str = ""
    _lists_untried = True

    @property
    def failed(self) -> bool:
        return not self._cells or len(self._placed) < len(self._cells)

    @property
    def watch_hostiles(self) -> bool:
        return not self._in_screen()

    @property
    def watch_health(self) -> bool:
        return not self._in_screen()

    def _in_screen(self) -> bool:
        fetch = getattr(self._current, "_fetch", None)
        return fetch is not None and fetch.in_screen

    # ── the report ───────────────────────────────────────────────────────

    @property
    def done_reason(self) -> str:
        if self._reason:
            return self._reason
        return self._report()

    def _report(self) -> str:
        total = len(self._cells)
        text = (f"placed {len(self._placed)} of {total} "
                f"{_words(self.item)}: {_cells_text(self._placed)}.")
        if self._failed:
            text += " Failed: " + "; ".join(
                f"{tuple(c)} -- {why}" for c, why in self._failed) + "."
        left = [c for c in self._cells if c not in self._done]
        if left and self._lists_untried:
            text += f" Not tried: {_cells_text(left)}."
        if self._stopped:
            text += f" {self._stopped}"
        state = self._last_state
        if self._start_count is not None and state is not None \
                and getattr(state, "inventory", None) is not None:
            now = verify_mod._count_of(state, {self.item})
            text += (f" Counted in the inventory: {self.item} "
                     f"{self._start_count} → {now}")
            used = self._start_count - now
            if self._creative:
                text += " (creative: blocks are not used up)"
            elif used != len(self._placed):
                text += (f" -- {used} used, which does not match the "
                         f"{len(self._placed)} I proved")
            text += "."
        return text

    # ── the loop ─────────────────────────────────────────────────────────

    def account_for(self, state, history) -> None:
        """The runner stopped us (danger, the step limit) right after a
        place: judge it, so the report counts it."""
        self._last_state = state
        current = self._current
        if current is None or not current.awaiting_verdict or not history:
            return
        current.plan(state, len(history), tuple(history))
        self._record(current)

    def _record(self, current) -> None:
        cell = current.cell
        if cell in self._done:
            return
        self._done.add(cell)
        if current.placed:
            self._placed.append(cell)
            self._known[cell] = self.item
            self._in_a_row = 0
        else:
            self._failed.append((cell, current.done_reason))
            self._in_a_row += 1

    def plan(self, state, step_index: int, history: tuple):
        self._last_state = state
        if self._phase == "done":
            return None
        if self._phase == "restore":
            return self._restore(state)
        if self._phase == "prepare":
            problem = self._prepare(state)
            if problem:
                self._reason = problem
                self._phase = "done"
                return None
            self._phase = "build"
        while True:
            if self._current is not None:
                step = self._current.plan(state, step_index, history)
                if step is not None:
                    return step
                current, self._current = self._current, None
                self._record(current)
                if current.fatal:
                    self._stopped = "I stopped there: no other cell would do better."
                    return self._restore(state)
                if self._in_a_row >= MAX_FAILURES_IN_A_ROW:
                    self._stopped = (f"I stopped after {self._in_a_row} "
                                     f"cells in a row failed.")
                    return self._restore(state)
            if step_index >= MAX_TASK_STEPS - 2:
                self._stopped = ("I ran out of steps for this task; ask "
                                 "again to carry on.")
                return self._restore(state)
            cell = self._next_cell(state)
            if cell is None:
                self._finished(state)
                return self._restore(state)
            self._current = PlaceBlockAt(x=cell[0], y=cell[1], z=cell[2],
                                         item=self.item, known=self._known,
                                         restore_slot=False,
                                         avoid=self._keep_out())

    def _prepare(self, state) -> str | None:
        self.item = building_mod.short(self.item)
        if self.item not in building_mod.BUILDING_BLOCKS:
            return (f"I only build with plain blocks such as dirt, "
                    f"cobblestone, stone or planks -- not "
                    f"{_words(self.item) or 'nothing'}.")
        if state.inventory is None or state.position is None:
            return ("Building needs the bridge mod: without it I cannot see "
                    "the inventory, the cells or the crosshair.")
        problem = self._targets(state)
        if isinstance(problem, str):
            return problem
        have = verify_mod._count_of(state, {self.item})
        if have <= 0:
            return f"There is no {_words(self.item)} in the inventory."
        self._start_count = have
        self._creative = getattr(state, "game_mode", None) == "creative"
        self._previous_slot = state.selected_slot
        return None

    def _targets(self, state):
        """Fill self._cells, or return why not."""
        raise NotImplementedError

    def _finished(self, state) -> None:
        """Every cell has been tried. A hook for a last check."""

    def _keep_out(self) -> frozenset:
        """Columns never to stand in while building: the structure's own.
        Standing on a half-built wall or inside a half-built room is how a
        player ends up boxed in by what they are building."""
        return frozenset((c[0], c[2]) for c in self._cells)

    def _next_cell(self, state):
        for cell in self._cells:
            if cell not in self._done:
                return cell
        return None

    def _restore(self, state):
        self._phase = "done"
        if self._restored or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restored = True
        self._phase = "restore"
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")


@dataclass
class BuildLine(_Builder):
    """build_line: up to `count` blocks of `item` in a straight line from
    (x, y, z) -- north, south, east, west, or up. Each one by
    place_block_at, against the ground or the block before it.

    Up is a column, and from the ground a column is two blocks high: the
    third block goes on a top face above a standing player's eyes, which
    cannot be seen without jumping (B4d). It says so, by cell."""

    x: int = 0
    y: int = 0
    z: int = 0
    direction: str = ""
    count: int = 1

    name = "build_line"

    @property
    def goal(self) -> str:
        return (f"build a line of {self.count} {_words(self.item)} "
                f"{self.direction} from {(self.x, self.y, self.z)}")

    def _targets(self, state):
        step = _LINE_DIRECTIONS.get(str(self.direction or "").strip().lower())
        if step is None:
            return (f"A line goes north, south, east, west or up -- not "
                    f"{self.direction!r}.")
        try:
            count = int(self.count)
        except (TypeError, ValueError):
            count = 0
        if not 1 <= count <= MAX_LINE_BLOCKS:
            return (f"A line is 1 to {MAX_LINE_BLOCKS} blocks; ask for "
                    f"{MAX_LINE_BLOCKS} or fewer at a time.")
        start = (int(self.x), int(self.y), int(self.z))
        self._cells = [building_mod.offset(start, (step[0] * i, step[1] * i,
                                                   step[2] * i))
                       for i in range(count)]
        return None


_BLUEPRINTS: dict = {}
"""Blueprints under way, so asking again carries on: key -> {"cells": the
plan's cells, "placed": cells proven placed}. In memory only. A task has 45
steps and a shelter needs about seventy."""

_LAST_BLUEPRINT: dict = {}
"""(plan, item) -> the key of the last one started, for "carry on" without
coordinates."""


def forget_blueprints() -> None:
    _BLUEPRINTS.clear()
    _LAST_BLUEPRINT.clear()


def _cardinal(dx, dz) -> str:
    if abs(dx) > abs(dz):
        return "east" if dx > 0 else "west"
    return "south" if dz > 0 else "north"


@dataclass
class BuildBlueprint(_Builder):
    """build_blueprint: a named plan -- platform, wall, or a hollow 3x3x3
    shelter with a doorway -- built bottom-up, each block by
    place_block_at.

    THE LIMITS, CHECKED BEFORE ANYTHING IS PRESSED
        At most 64 blocks; every cell within 6 blocks of where the task
        began; every cell empty or holding grass the game replaces; flat
        ground under the bottom layer; plain building blocks only; and
        enough of them in the inventory for the whole plan.
    ONE SENTENCE
        The plan is one sentence (`goal`, which run_task's answer repeats):
        what, of which block, where, which way the door faces, how many.
    ORDER
        The lowest unfinished layer first. Within it, the first cell in the
        plan that can be placed from where it stands (its face in reach and
        in view), else the nearest that has something to go against,
        walking to it.
    NEVER INSIDE IT
        For walls and the shelter it never stands in, or routes through,
        the structure's own footprint -- the step aside, which is there to
        be stood on for the roof. Standing on a half-built wall or inside
        the half-built room is how a player boxes themselves in.
    MORE THAN ONE TASK
        45 steps place about fifteen blocks. The plan and what has been
        proven placed are kept in memory: asking again carries on, with the
        same limits checked for what is left. Every report says what this
        task placed and failed, and what is left."""

    design: str = ""             # the plan's name; `plan` is the method
    x: int | None = None
    y: int | None = None
    z: int | None = None
    direction: str | None = None
    size: int | None = None

    name = "build_blueprint"

    _key: tuple = None
    _blueprint: object = None
    _before: int = 0                # cells placed by earlier tasks
    _checked: str = ""
    _found: list = field(default_factory=list)
    _lists_untried = False          # "Still to place" says it

    @property
    def goal(self) -> str:
        if None not in (self.x, self.y, self.z) and self.direction:
            bp = building_mod.blueprint(self.design, (self.x, self.y, self.z),
                                        self.direction, self.size,
                                        building_mod.short(self.item))
            if not isinstance(bp, str):
                return f"build {bp.sentence}"
        return (f"build a {self.design or 'blueprint'} of "
                f"{_words(building_mod.short(self.item))}")

    @property
    def failed(self) -> bool:
        record = _BLUEPRINTS.get(self._key)
        return record is None or len(record["placed"]) \
            < len(record["cells"])

    def _report(self) -> str:
        record = _BLUEPRINTS.get(self._key)
        if record is None or self._blueprint is None:
            return super()._report()
        total = len(record["cells"])
        done = len(record["placed"])
        text = super()._report()
        head = f"{self._blueprint.sentence}. "
        if self._found:
            head += (f"Already in place when I started, though not proven "
                     f"by me: {_cells_text(self._found)}. ")
        if done >= total:
            return (f"{head}Built: all {total} placed. This task {text}"
                    f"{self._checked}")
        left = [c for c in record["cells"] if c not in record["placed"]]
        return (f"{head}{done} of {total} placed so far. This task "
                f"{text}{self._checked} Still to place: {_cells_text(left)}. "
                f"Ask again (build_blueprint {self._blueprint.plan}) to "
                f"carry on.")

    def _record(self, current) -> None:
        super()._record(current)
        record = _BLUEPRINTS.get(self._key)
        if record is not None and current.placed:
            record["placed"].add(current.cell)

    def _targets(self, state):
        item = self.item
        start = building_mod.cell_of((math.floor(state.position[0]),
                                      math.floor(state.position[1]),
                                      math.floor(state.position[2])))
        local = nav.LocalMap.from_state(state)
        key = self._resume_key()
        if key is None:
            made = self._new_plan(state, local, start)
            if isinstance(made, str):
                return made
            key = made
        record = _BLUEPRINTS[key]
        self._key = key
        self._blueprint = record["blueprint"]
        _LAST_BLUEPRINT[(self._blueprint.plan, item)] = key
        left = [c for c in record["cells"] if c not in record["placed"]]
        self._before = len(record["placed"])
        far = building_mod.within(left, start)
        if far:
            anchor = record["anchor"]
            return (f"Every cell has to be within "
                    f"{building_mod.MAX_BLUEPRINT_REACH} blocks of where I "
                    f"start, and {_cells_text(far[:3])} "
                    f"{'is' if len(far) == 1 else 'are'} not. Walk closer "
                    f"to {anchor} and ask again.")
        have = verify_mod._count_of(state, {item})
        if getattr(state, "game_mode", None) != "creative" and have < len(left):
            return (f"{self._blueprint.sentence[0].upper()}"
                    f"{self._blueprint.sentence[1:]} needs {len(left)} more "
                    f"{_words(item)}, and I have {have}.")
        for cell in left:
            verdict, name = building_mod.what_is_at(local, state, cell,
                                                    record["known"])
            if verdict == building_mod.OCCUPIED and name == item:
                # The scan shows the block already there: a press an
                # earlier task could not prove, most likely. Counted as
                # found, not as placed by this task.
                record["placed"].add(cell)
                record["known"][cell] = item
                self._found.append(cell)
            elif verdict == building_mod.OCCUPIED:
                return (f"{cell} holds {'a ' + _words(name) if name else 'a block'}"
                        f", in the way of the {self._blueprint.plan}. Pick "
                        f"another spot, or clear it first.")
        left = [c for c in left if c not in record["placed"]]
        self._cells = left
        self._known = record["known"]
        return None

    def _finished(self, state) -> None:
        """With the plan placed, check it against the game's own list of
        the blocks around the player (near_blocks, B4d), where it reaches.
        A cell that does not hold the block now is taken off the placed
        list -- asking again puts it back -- and the report names it."""
        record = _BLUEPRINTS.get(self._key)
        near = getattr(state, "near", None)
        if record is None or len(record["placed"]) < len(record["cells"]):
            return
        if near is None:
            self._checked = (" Not checked block by block: the bridge mod "
                             "does not report the blocks around me "
                             "(install_mod.bat updates it).")
            return
        seen, wrong, out_of_range = 0, [], 0
        for cell in record["cells"]:
            if not near.covers(cell):
                out_of_range += 1
                continue
            block = near.block_at(cell)
            name = building_mod.short(block.name) if block else "air"
            if name == self.item:
                seen += 1
            else:
                wrong.append((cell, name))
        text = (f" Checked against the game's own list of the blocks around "
                f"me: {seen} of {len(record['cells'])} confirmed")
        if out_of_range:
            text += f", {out_of_range} too far from where I stand to check"
        if wrong:
            text += ("; not as built: " + "; ".join(
                f"{c} holds {_words(n)}" for c, n in wrong))
            for cell, _name in wrong:
                record["placed"].discard(cell)
                record["known"].pop(cell, None)
        self._checked = text + "."

    def _keep_out(self) -> frozenset:
        """For walls and a shelter, the footprint -- every column inside
        the outline, the hollow too -- except the step, which is there to
        be stood on. A platform is a floor: standing on it is the point,
        and its middle cannot be reached from outside it."""
        bp = self._blueprint
        if bp is None:
            return super()._keep_out()
        steps = {(c[0], c[2]) for c, role in bp.roles if role == "step"}
        body = [c for c, role in bp.roles if role in ("wall", "roof")]
        if not body:
            return frozenset()
        xs = [c[0] for c in body]
        zs = [c[2] for c in body]
        return frozenset((x, z) for x in range(min(xs), max(xs) + 1)
                         for z in range(min(zs), max(zs) + 1)) - steps

    def _next_cell(self, state):
        """The lowest unfinished layer; in it, the first cell in the plan
        that can be placed from where it stands (its face in reach and in
        view -- a block that would hide another's face is caught by that
        view check, not by guessing an order), else the nearest one with
        something to go against, walking to it."""
        left = [c for c in self._cells if c not in self._done]
        if not left:
            return None
        layer = min(c[1] for c in left)
        here = [c for c in left if c[1] == layer]
        local = nav.LocalMap.from_state(state)
        ready = []
        for cell in here:
            refs = building_mod.references(local, state, cell, self._known,
                                           unusable=_interactive)
            if refs:
                ready.append((cell, refs))
        if not ready:
            return here[0]           # it fails, and the report says why
        pos = state.position
        inside = (math.floor(pos[0]), math.floor(pos[2])) in self._keep_out()
        for cell, refs in ready:
            if not inside and any(_can_place_here(state, cell, r)
                                  for r in refs):
                return cell
        return min((c for c, _refs in ready),
                   key=lambda c: (math.dist(
                       (pos[0], pos[1], pos[2]),
                       (c[0] + 0.5, c[1] + 0.5, c[2] + 0.5)), c))

    def _resume_key(self):
        """The blueprint under way to carry on, or None for a new one."""
        item = self.item
        if None in (self.x, self.y, self.z):
            key = _LAST_BLUEPRINT.get((str(self.design).lower(), item))
            record = _BLUEPRINTS.get(key)
            if record is not None and \
                    len(record["placed"]) < len(record["cells"]):
                return key
            return None
        for key, record in _BLUEPRINTS.items():
            if record["anchor"] == (int(self.x), int(self.y), int(self.z)) \
                    and record["blueprint"].plan == str(self.design).lower() \
                    and key[-1] == item \
                    and len(record["placed"]) < len(record["cells"]):
                return key
        return None

    def _new_plan(self, state, local, start):
        item = self.item
        if None in (self.x, self.y, self.z):
            yaw = math.radians((state.rotation or (0.0, 0.0))[0] or 0.0)
            ahead = _cardinal(-math.sin(yaw), math.cos(yaw))
            step = building_mod.FACE_OFFSETS[{"north": "north",
                                              "south": "south",
                                              "east": "east",
                                              "west": "west"}[ahead]]
            column = (start[0] + 3 * step[0], start[2] + 3 * step[2])
            ground = local.ground_at(*column)
            if ground is None:
                return ("I cannot see the ground three blocks in front of "
                        "me. Give me x, y and z from look_around.")
            anchor = (column[0], ground + 1, column[1])
        else:
            anchor = (int(self.x), int(self.y), int(self.z))
        facing = (str(self.direction).strip().lower() if self.direction
                  else _cardinal(start[0] - anchor[0], start[2] - anchor[2]))
        bp = building_mod.blueprint(self.design, anchor, facing, self.size,
                                    item)
        if isinstance(bp, str):
            return bp
        if not building_mod.supported_in_order(bp.cells, anchor[1] - 1):
            return f"The {bp.plan} cannot be built bottom-up."
        for cell in bp.cells:
            verdict, name = building_mod.what_is_at(local, state, cell)
            if verdict == building_mod.OCCUPIED:
                return (f"{cell} holds "
                        f"{'a ' + _words(name) if name else 'a block'}, in "
                        f"the way of the {bp.plan}. Pick another spot, or "
                        f"clear it first.")
        bottom = [c for c in bp.cells if c[1] == anchor[1]]
        for cell in bottom:
            ground = local.ground_at(cell[0], cell[2])
            if ground != anchor[1] - 1:
                return (f"The ground under the {bp.plan} is not flat: at "
                        f"{(cell[0], cell[2])} it is "
                        f"{'unseen' if ground is None else f'at y={ground}'}"
                        f", not y={anchor[1] - 1}. Pick flatter ground.")
        for cell in bp.cells:
            verdict, name = building_mod.what_is_at(local, state, cell)
            if verdict == building_mod.UNKNOWN:
                return (f"I cannot see that {cell} is empty, so I will not "
                        f"start the {bp.plan} there.")
        key = (bp.plan, anchor, facing, self.size, item)
        _BLUEPRINTS[key] = {"blueprint": bp, "cells": bp.cells,
                            "anchor": anchor, "placed": set(), "known": {}}
        return key
