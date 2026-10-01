"""
minecraft/skills/combat.py -- flee and fight. Moved here unchanged from
skills.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from minecraft import navigation as nav, verification as verify_mod
from minecraft import aiming as aiming_mod
from minecraft import mining as mining_mod
from minecraft import building as building_mod
from minecraft.task_runner import Step

from minecraft.skills.base import _words
from minecraft.skills.navigate import NavigateTo


# ── Getting away ─────────────────────────────────────────────────────────────

FLEE_SAFE_DISTANCE = 12.0
"""Run until the nearest hostile mob is further than this."""

FLEE_KEEP_CLEAR = 2
"""Columns kept clear round each hostile mob on the way."""

FLEE_REPLAN_EVERY = 4
"""Steps on one leg before choosing again: the mobs move too."""

MAX_FLEE_SECONDS = 60.0


def _hostiles(state):
    return [e for e in (getattr(state, "nearby_entities", None) or ())
            if e.hostile is True and e.distance is not None
            and e.position is not None]


def _mob_words(entity) -> str:
    return _words(str(entity.name or "hostile mob").split(":")[-1])


@dataclass
class Flee:
    """flee: get away from hostile mobs.

    Walks -- sprinting where the ground ahead is straight and clear -- to
    the reachable column of the scan furthest from every hostile it can
    see, on a route that keeps two columns clear of each, and chooses again
    every few steps as they move. Done when the nearest is further than
    FLEE_SAFE_DISTANCE, or when `seconds` run out.

    NEVER STOPPED BY A MOB OR BY DAMAGE
        A mob close by and health going down are why it runs. The runner's
        danger watch is off for it (watch_hostiles, watch_health), and the
        controller has no hazard check on movement (A1)."""

    seconds: float = 30.0

    name = "flee"
    verifiable_with = ("position", "nearby_entities")
    watch_hostiles = False
    watch_health = False

    _start: float | None = None
    _walker: object = None
    _leg_steps: int = 0
    _reason: str = ""
    _safe: bool = False
    _done: bool = False
    _last: object = None            # the nearest hostile at the last look
    _reached: set = field(default_factory=set)

    @property
    def goal(self) -> str:
        return (f"get more than {FLEE_SAFE_DISTANCE:.0f} blocks from hostile "
                f"mobs")

    @property
    def failed(self) -> bool:
        return not self._safe

    @property
    def done_reason(self) -> str:
        if self._reason:
            return self._reason
        if self._last is not None:
            return (f"still running: a {_mob_words(self._last)} is "
                    f"{self._last.distance:.0f} blocks away")
        return "stopped before getting clear"

    def _stop(self, reason, safe=False):
        self._reason, self._safe, self._done = reason, safe, True
        return None

    def plan(self, state, step_index: int, history: tuple):
        if self._done:
            return None
        if getattr(state, "nearby_entities", None) is None:
            return self._stop("I cannot see the mobs around me -- that "
                              "needs the bridge mod -- so I cannot tell "
                              "which way is away.")
        hostiles = _hostiles(state)
        if not hostiles:
            return self._stop("there is no hostile mob in sight.", safe=True)
        nearest = min(hostiles, key=lambda e: e.distance)
        self._last = nearest
        if nearest.distance > FLEE_SAFE_DISTANCE:
            return self._stop(
                f"clear: the nearest hostile, a {_mob_words(nearest)}, is "
                f"{nearest.distance:.0f} blocks away -- more than "
                f"{FLEE_SAFE_DISTANCE:.0f}.", safe=True)
        if self._start is None:
            self._start = state.captured_at
        limit = min(float(self.seconds or 30.0), MAX_FLEE_SECONDS)
        if state.captured_at - self._start >= limit:
            return self._stop(
                f"time ran out after {limit:.0f}s: a {_mob_words(nearest)} "
                f"is still {nearest.distance:.0f} blocks away.")
        for _attempt in range(2):
            if self._walker is None or self._leg_steps >= FLEE_REPLAN_EVERY:
                walker = self._leg(state, hostiles)
                if isinstance(walker, str):
                    return self._stop(walker)
                self._walker, self._leg_steps = walker, 0
            step = self._walker.plan(state, step_index, history)
            if step is not None:
                self._leg_steps += 1
                return step
            # Arrived, or that way is shut: choose again, once.
            self._reached.add(self._walker._destination)
            self._walker = None
        return self._stop(
            f"there is nowhere further to run within what I can see: a "
            f"{_mob_words(nearest)} is {nearest.distance:.0f} blocks away.")

    def _leg(self, state, hostiles):
        """A NavigateTo to the best place to be, or why there is none."""
        local = nav.LocalMap.from_state(state)
        here = (math.floor(state.position[0]), math.floor(state.position[2]))
        mobs = [(e.position[0], e.position[2]) for e in hostiles]
        now = min(math.dist((state.position[0], state.position[2]), m)
                  for m in mobs)
        # Two columns clear of each mob -- less when one is closer than
        # that already, or the clear ring would wall the player in with it.
        ring = max(0.5, min(float(FLEE_KEEP_CLEAR), now - 1.0))
        keep = frozenset(
            (math.floor(mx) + dx, math.floor(mz) + dz)
            for mx, mz in mobs
            for dx in range(-FLEE_KEEP_CLEAR, FLEE_KEEP_CLEAR + 1)
            for dz in range(-FLEE_KEEP_CLEAR, FLEE_KEEP_CLEAR + 1)
            if math.dist((math.floor(mx) + dx + 0.5,
                          math.floor(mz) + dz + 0.5), (mx, mz)) <= ring)

        def clearance(column):
            centre = (column[0] + 0.5, column[1] + 0.5)
            return min(math.dist(centre, m) for m in mobs)
        candidates = sorted(
            (c for c in nav.reachable_columns(local)
             if c not in keep and c != here and c not in self._reached
             and local.standable(*c) and clearance(c) > now + 1.0),
            key=lambda c: (-clearance(c), abs(c[0] - here[0])
                           + abs(c[1] - here[1]), c))
        for column in candidates[:6]:
            if nav.find_path(state, column, avoid=set(keep)).found:
                return NavigateTo(destination=column, arrive_within=1.0,
                                  keep_off=keep, sprint=True)
        nearest = min(hostiles, key=lambda e: e.distance)
        return (f"there is no reachable ground further from the "
                f"{_mob_words(nearest)} within what I can see; it is "
                f"{nearest.distance:.0f} blocks away.")


# ── Fighting ─────────────────────────────────────────────────────────────────

FIGHT_MAX_SECONDS = 20.0
FIGHT_RETREAT_HEALTH = 8.0
FIGHT_START_HEALTH = 12.0
"""Below this a fight does not start: it costs health, and starting lower
leaves next to nothing to spend before FIGHT_RETREAT_HEALTH."""
FIGHT_WEAPON_SELECTS = 2
FIGHT_RANGE = 16.0
"""Only mobs this close are fought."""
ENTITY_REACH = 3.0
"""How far a survival player can hit a mob."""
ATTACK_TAP_S = 0.1

MOB_HEIGHTS = {
    "zombie": 1.95, "husk": 1.95, "drowned": 1.95, "zombie_villager": 1.95,
    "skeleton": 1.99, "stray": 1.99, "bogged": 1.99,
    "wither_skeleton": 2.4, "spider": 0.9, "cave_spider": 0.5,
    "enderman": 2.9, "witch": 1.95, "slime": 1.04, "magma_cube": 1.04,
    "silverfish": 0.3, "endermite": 0.3, "pillager": 1.95,
    "vindicator": 1.95, "evoker": 1.95, "piglin": 1.95,
    "piglin_brute": 1.95, "zombified_piglin": 1.95, "blaze": 1.8,
    "hoglin": 1.4, "zoglin": 1.4, "phantom": 0.5, "guardian": 0.85,
    "breeze": 1.77,
}
"""Hit-box heights, to aim at the middle of the body, not the feet."""

NEVER_MELEE = {
    "creeper": "it explodes when it is close -- flee instead",
    "warden": "it is far stronger than anything I can hold -- flee instead",
    "enderman": "it leaves you alone until it is hit or looked at, then "
                "teleports about and hits hard -- leave it be",
    "zombified_piglin": "hitting one turns every one nearby against you",
    "piglin": "hitting one turns the whole group against you",
    "piglin_brute": "it hits far harder than I can trade blows with",
    "ravager": "it has far more health than I can wear down, and hits hard",
    "wither": "it is a boss: it explodes as it forms and fires wither skulls",
    "ender_dragon": "it is a boss, mostly out of reach in the air, and "
                    "knocks you flying",
    "ghast": "it flies out of reach and fires explosive fireballs",
    "elder_guardian": "it lives under water, saps your mining, and lasers "
                      "you from out of reach",
    "guardian": "it lives under water and lasers you from out of reach",
    "shulker": "it hides in its shell, and its bullets make you float up",
    "evoker": "it summons fangs and vexes from where it stands",
    "vex": "it flies through walls; I cannot keep it in reach",
    "hoglin": "it throws you into the air -- off a ledge, often",
    "zoglin": "it throws you into the air and goes for anything near",
    "blaze": "it flies and shoots fireballs from out of reach",
    "wither_skeleton": "its hits wither you, and it outreaches a skeleton",
}
"""Hostile mobs fight never walks up to, each with the reason it says."""


def best_hotbar_weapon(state):
    """(slot, item) of the best sword in the hotbar -- or, with no sword,
    the best axe -- by material; what is held already wins a tie. None
    when there is neither, or the inventory cannot be read.

    A sword before any axe: an axe hits harder once, but recovers so
    slowly that the sword's taps, every other step, do more."""
    inventory = getattr(state, "inventory", None)
    if inventory is None:
        return None
    current = getattr(state, "selected_slot", None)
    best = None
    for stack in inventory:
        slot = getattr(stack, "slot", None)
        if slot not in mining_mod.HOTBAR_SLOTS \
                or not (getattr(stack, "count", 0) or 0):
            continue
        item = building_mod.short(stack.name)
        tool = mining_mod.tool_from_item(item)
        if tool.kind not in (mining_mod.SWORD, mining_mod.AXE):
            continue
        rank = (tool.kind == mining_mod.SWORD, tool.tier, slot == current,
                -slot)
        if best is None or rank > best[0]:
            best = (rank, slot, item)
    return None if best is None else (best[1], best[2])


@dataclass
class Fight:
    """fight -- opt-in: run only when the user asks to fight.

    The nearest hostile mob within FIGHT_RANGE (or the nearest of the kind
    named in `target`): walk into reach, aim at the middle of its body,
    and attack in short taps, every other step -- Minecraft's attack
    cooldown makes a swing at once after another a weak one.

    WHAT IT NEVER HITS
        Every swing carries `expect_hostile`: the controller presses
        nothing unless the game reports a hostile mob under the crosshair
        at that moment. A player or a passive mob is never chosen, and a
        player or passive mob stepping in front is never hit. A mob in
        NEVER_MELEE -- a creeper, a warden, an enderman, a piglin, a
        ghast, a boss and the like -- is not walked up to at all, and the
        refusal says why.
    WHEN IT DOES NOT START
        Health below FIGHT_START_HEALTH (12), or health it cannot read.
    THE WEAPON
        Before the first swing it takes up the best sword in the hotbar,
        else the best axe (best_hotbar_weapon), and puts the old slot back
        when it ends -- after a win, a retreat or a refusal alike, the way
        mining puts its tool slot back. No sword or axe: it says so, and
        fights with what is in hand.
    WHEN IT STOPS
        The mob is gone from sight (killed, most likely -- the bridge does
        not report a mob's health, so that is not proven); health below
        FIGHT_RETREAT_HEALTH, when it runs (flee); or FIGHT_MAX_SECONDS
        (20) are up. The runner's danger watch is off for it: a mob close
        by is the point, and it keeps its own health rule."""

    target: str = ""
    seconds: float = FIGHT_MAX_SECONDS

    name = "fight"
    verifiable_with = ("nearby_entities", "target_entity", "health")
    watch_hostiles = False
    watch_health = False

    _start: float | None = None
    _done: bool = False
    _won: bool = False
    _reason: str = ""
    _swings: int = 0
    _mob: object = None                  # EntityRef being fought
    _retreat: object = None              # a Flee once health is low
    _retreat_note: str = ""
    _last_was_swing: bool = False
    _weapon_done: bool = False
    _weapon_tries: int = 0
    _weapon_note: str = ""
    _slot_before: int | None = None      # the slot to go back to
    _slot_restored: bool = False
    _finished: bool = False

    @property
    def goal(self) -> str:
        what = _words(self.target) if self.target else "the nearest hostile mob"
        return f"fight {what}"

    @property
    def failed(self) -> bool:
        return not self._won

    @property
    def done_reason(self) -> str:
        if self._retreat is not None:
            text = f"{self._retreat_note} {self._retreat.done_reason}"
        else:
            text = self._reason or "stopped before fighting"
        return f"{text} {self._weapon_note}".strip()

    def _stop(self, reason, won=False):
        self._reason, self._won, self._done = reason, won, True
        return None

    def plan(self, state, step_index: int, history: tuple):
        """The fight's next step; once it has none, the step putting the
        old hotbar slot back -- once -- then nothing."""
        if self._finished:
            return None
        step = self._plan(state, step_index, history)
        if step is not None:
            return step
        self._finished = True
        if self._slot_before is None or self._slot_restored \
                or getattr(state, "selected_slot", None) == self._slot_before:
            return None
        self._slot_restored = True
        slot = self._slot_before + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")

    def _take_up_weapon(self, state):
        """A hotbar_select to the best weapon, once, before the first
        swing; None when it is held already, or there is none to take."""
        if self._weapon_done:
            return None
        if getattr(state, "inventory", None) is None:
            self._weapon_done = True
            self._weapon_note = ("(I could not read the hotbar, so I fought "
                                 "with whatever was in hand.)")
            return None
        choice = best_hotbar_weapon(state)
        if choice is None:
            self._weapon_done = True
            self._weapon_note = ("(There was no sword or axe in the hotbar, "
                                 "so I fought with whatever was in hand.)")
            return None
        slot, item = choice
        current = getattr(state, "selected_slot", None)
        words = " ".join(item.split("_"))
        if current == slot:
            self._weapon_done = True
            return None
        if self._weapon_tries >= FIGHT_WEAPON_SELECTS:
            self._weapon_done = True
            self._weapon_note = (
                f"(I selected hotbar slot {slot + 1} for the {words} "
                f"{self._weapon_tries} times and the game did not show it, "
                f"so I fought with whatever was in hand.)")
            return None
        self._weapon_tries += 1
        if self._slot_before is None and current is not None:
            self._slot_before = current
        return Step(action="hotbar_select", params={"slot": slot + 1},
                    expectation=verify_mod.holding_slot(slot + 1),
                    note=f"take up the {words} (hotbar slot {slot + 1})")

    def _plan(self, state, step_index: int, history: tuple):
        if self._done:
            return None
        last = history[-1] if history else None
        if last is not None and last.step.get("action") == "attack" \
                and last.action_result.get("ok", True):
            self._swings += 1
        if self._retreat is not None:
            return self._retreat.plan(state, step_index, history)
        if getattr(state, "nearby_entities", None) is None \
                or state.position is None or state.rotation is None:
            return self._stop("I cannot see the mobs or where I am facing -- "
                              "that needs the bridge mod -- so I did not "
                              "fight.")
        health = getattr(state, "health", None)
        if self._start is None:
            if not isinstance(health, (int, float)):
                return self._stop("I cannot read my health, so I did not "
                                  "start a fight.")
            if health < FIGHT_START_HEALTH:
                return self._stop(
                    f"I have {health:.0f} health, and I start a fight only "
                    f"at {FIGHT_START_HEALTH:.0f} or more -- eat or get "
                    f"away first. I did not fight.")
        if isinstance(health, (int, float)) and health < FIGHT_RETREAT_HEALTH:
            self._retreat_note = (
                f"I retreated at {health:.0f} health, after {self._swings} "
                f"swing(s){self._at_mob()}:")
            self._retreat = Flee(seconds=FIGHT_MAX_SECONDS)
            return self._retreat.plan(state, step_index, history)
        if self._start is None:
            self._start = state.captured_at
        limit = min(float(self.seconds or FIGHT_MAX_SECONDS),
                    FIGHT_MAX_SECONDS)
        if state.captured_at - self._start >= limit:
            return self._stop(f"time is up ({limit:.0f}s, the most I fight "
                              f"for): {self._swings} swing(s)"
                              f"{self._at_mob()}, and it is still there.")
        mob = self._choose(state)
        if isinstance(mob, str):
            return self._stop(mob, won=self._swings > 0)
        self._mob = mob
        weapon = self._take_up_weapon(state)
        if weapon is not None:
            return weapon
        if mob.distance > ENTITY_REACH - 0.3:
            self._last_was_swing = False
            column = (math.floor(mob.position[0]),
                      math.floor(mob.position[2]))
            walker = NavigateTo(destination=column, arrive_within=2.0)
            step = walker.plan(state, 0, ())
            if step is not None:
                return step
        point = (mob.position[0],
                 mob.position[1] + MOB_HEIGHTS.get(_mob_key(mob), 1.8) / 2,
                 mob.position[2])
        dx, dy, error = aiming_mod.SHARED.delta_for(
            state.rotation, aiming_mod.yaw_to(state.position, point),
            aiming_mod.pitch_to(state.position, point))
        seen = getattr(state, "target_entity", None)
        on_it = (seen is not None and seen.category == "hostile"
                 and _mob_key(seen) == _mob_key(mob))
        if on_it and not self._last_was_swing:
            self._last_was_swing = True
            return Step(action="attack",
                        params={"duration": ATTACK_TAP_S,
                                "expect_hostile": True},
                        expectation=verify_mod.knocked_back(_mob_key(mob)),
                        note=f"hit the {_mob_words(mob)} "
                             f"({mob.distance:.1f} blocks)")
        self._last_was_swing = False
        if dx == 0 and dy == 0:
            dx = 1                     # a breath between swings, on target
        return Step(action="look", params={"dx": dx, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=0.05),
                    note=(f"aim at the {_mob_words(mob)}'s body "
                          f"({error:.0f}° off)"))

    def _at_mob(self) -> str:
        return f" at the {_mob_words(self._mob)}" if self._mob else ""

    def _choose(self, state):
        """The mob to fight -- the one fought so far, if it is still here --
        or why there is none."""
        wanted = building_mod.short(self.target) if self.target else ""
        mobs = [e for e in (state.nearby_entities or ())
                if e.category == "hostile" and e.hostile is True
                and e.position is not None and e.distance is not None
                and e.distance <= FIGHT_RANGE
                and (not wanted or _mob_key(e) == wanted)]
        meleeable = [e for e in mobs if _mob_key(e) not in NEVER_MELEE]
        if not meleeable:
            if self._swings and self._mob is not None:
                return (f"the {_mob_words(self._mob)} is gone after "
                        f"{self._swings} swing(s) -- killed, most likely; "
                        f"the game does not tell me a mob's health.")
            if mobs:
                worst = min(mobs, key=lambda e: e.distance)
                return (f"the only hostile close by is a "
                        f"{_mob_words(worst)}, and I will not walk up to it: "
                        f"{NEVER_MELEE[_mob_key(worst)]}.")
            return (f"there is no hostile mob "
                    f"{'called ' + _words(wanted) + ' ' if wanted else ''}"
                    f"within {FIGHT_RANGE:.0f} blocks to fight.")
        if self._mob is not None:
            same = [e for e in meleeable
                    if _mob_key(e) == _mob_key(self._mob)]
            if same:
                return min(same, key=lambda e: math.dist(
                    e.position, self._mob.position))
        return min(meleeable, key=lambda e: e.distance)


def _mob_key(entity) -> str:
    return building_mod.short(getattr(entity, "name", "") or "")
