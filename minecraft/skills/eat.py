"""
minecraft/skills/eat.py -- eat_food, and how long eating takes. Moved here
unchanged from skills.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from minecraft import action_spec, navigation as nav, verification as verify_mod
from minecraft.state import UNKNOWN
from minecraft.task_runner import Step

from minecraft.skills.base import FOODS, UNSAFE_FOODS, _interactive
from minecraft.skills.hotbar import HotbarFetch, hotbar_slot_to_fill


# ── Eating ───────────────────────────────────────────────────────────────────

EAT_TICKS = {"dried_kelp": 16, "honey_bottle": 40}
"""How long the game takes to eat or drink these, in ticks. Every other food
takes 32."""

EAT_SLOW_SERVER = 1.25
"""Allowance for a server running behind: 16 ticks a second instead of 20."""

EAT_LATENCY_S = 0.25
"""The press reaching the game, and the tick the eating starts on."""


def eat_ticks(food) -> int:
    return EAT_TICKS.get(food, 32)


def eat_seconds(food) -> float:
    """How long to hold right-click to eat ONE `food`: its use time, with
    room for a slow server and the press arriving -- and short of a second
    one, which a flat 2 seconds ate of dried kelp."""
    wanted = eat_ticks(food) / 20.0 * EAT_SLOW_SERVER + EAT_LATENCY_S
    return min(action_spec.MAX_EAT_DURATION_S, wanted)

MAX_HUNGER = 20


@dataclass
class EatFood:
    """Eat from the hotbar until not hungry, or `count` items.

    FOOD IN THE MAIN INVENTORY IS FETCHED FIRST
        Choosing a hotbar slot is a key press; food anywhere else needs the
        inventory screen. With no food on the hotbar it moves the best food
        from the main inventory into it (HotbarFetch: one number-key swap,
        through the click gate) and eats that. Without a mod that reports
        screens, or in creative, it names the food and asks instead.

    RIGHT CLICK IS NOT ONLY EATING
        With a chest, door or furnace under the crosshair, holding right
        click uses that instead. It looks up at the sky first, and will not
        eat while something that reacts to a click is still in front of it.

    Health is not watched while it runs: a starving player loses health,
    which is the reason to eat, not a reason to stop. Hostile mobs still
    stop it. The slot that was selected before is selected again after."""

    count: int = 1

    name = "eat_food"
    verifiable_with = ("hunger", "inventory", "selected_slot")
    watch_health = False

    _previous_slot: int | None = None
    _eaten: int = 0
    _failed_bites: int = 0
    _looks: int = 0
    _restoring: bool = False
    _reason: str = ""
    _blind: bool = False
    _fetch: object = None

    MAX_LOOKS = 3

    @property
    def watch_hostiles(self) -> bool:
        """Handed to the fetch's own, stricter watch while the inventory is
        open -- it closes the screen before stopping; the runner cannot."""
        return not (self._fetch is not None and self._fetch.in_screen)

    @property
    def goal(self) -> str:
        return "eat something" if self.count == 1 \
            else f"eat up to {self.count} things"

    @property
    def failed(self) -> bool:
        return self._blind or (self._eaten == 0 and not self._full)

    @property
    def done_reason(self) -> str:
        if self._fetch is not None and self._fetch.landed:
            return f"{self._fetch.done_reason}; {self._reason}"
        return self._reason

    _full: bool = False

    def plan(self, state, step_index: int, history: tuple):
        if state.confidence_of("inventory") == UNKNOWN \
                or not isinstance(state.hunger, (int, float)) \
                or state.selected_slot is None:
            self._blind = True
            self._reason = ("eating needs the bridge mod: without it I "
                            "cannot see hunger or what is on the hotbar")
            return None

        eats = [r for r in history if r.step.get("action") == "eat"]
        self._eaten = sum(1 for r in eats if r.verification.get("status")
                          == verify_mod.SUCCESS)
        self._failed_bites = len(eats) - self._eaten
        if self._previous_slot is None:
            self._previous_slot = state.selected_slot
        if self._fetch is not None and not self._fetch.finished:
            step = self._fetch.plan(state, step_index, history)
            if step is not None:
                return step

        hunger = state.hunger
        finished = (self._eaten >= self.count or hunger >= MAX_HUNGER
                    or self._failed_bites >= 2)
        food = None if finished else self._choose(state)
        if food is None and not finished and self._fetch is None:
            stored = self._choose(state, slots=range(9, 36))
            if stored is not None:
                self._fetch = HotbarFetch(item=stored[1],
                                          hotbar=hotbar_slot_to_fill(state),
                                          hurt_is_expected=True)
                step = self._fetch.plan(state, step_index, history)
                if step is not None:
                    return step
        if food is None:
            if not finished:
                self._reason = self._no_food(state)
            elif hunger >= MAX_HUNGER and self._eaten == 0:
                self._full = True
                self._reason = (f"not hungry ({hunger:.0f}/20) — Minecraft "
                                f"does not let you eat when full")
            elif self._failed_bites >= 2 and self._eaten == 0:
                self._reason = ("I held the food but it was not eaten, "
                                "twice — something may be in the way of "
                                "the right click")
            else:
                self._reason = (f"ate {self._eaten}; hunger is now "
                                f"{hunger:.0f}/20")
            return self._restore(state)

        slot, name = food
        if state.selected_slot != slot:
            return Step(action="hotbar_select", params={"slot": slot + 1},
                        expectation=verify_mod.holding_slot(slot + 1),
                        note=f"hold the {name} (hotbar slot {slot + 1})")

        blocker = self._in_front(state)
        if blocker:
            if self._looks >= self.MAX_LOOKS:
                self._reason = (f"{blocker} is in front of me even looking "
                                f"up, and right-clicking would use it "
                                f"instead of eating")
                return self._restore(state)
            self._looks += 1
            return self._look_up(state, blocker)

        return Step(action="eat",
                    params={"duration": eat_seconds(name)},
                    expectation=verify_mod.ate(name),
                    note=(f"eat the {name} (hunger {hunger:.0f}/20)"))

    def _choose(self, state, slots=range(9)):
        """(slot, name) of the food to eat from `slots` -- the hotbar unless
        told otherwise -- or None.

        The most filling one that does not overshoot what is missing, or if
        every one would, the smallest -- a steak at 18 of 20 is mostly
        wasted, a cookie is not."""
        hotbar = [(s.slot, s.name) for s in (state.inventory or ())
                  if s.slot is not None and s.slot in slots
                  and s.name in FOODS and (s.count or 0) > 0]
        if not hotbar:
            return None
        missing = MAX_HUNGER - state.hunger
        fits = [f for f in hotbar if FOODS[f[1]] <= missing]
        if fits:
            return max(fits, key=lambda f: (FOODS[f[1]], -f[0]))
        return min(hotbar, key=lambda f: (FOODS[f[1]], f[0]))

    def _no_food(self, state) -> str:
        elsewhere = sorted({s.name for s in (state.inventory or ())
                            if s.name in FOODS and s.slot is not None
                            and s.slot > 8})
        risky = sorted({s.name for s in (state.inventory or ())
                        if s.slot is not None and 0 <= s.slot <= 8
                        and s.name in UNSAFE_FOODS})
        if risky and not elsewhere:
            return (f"the only food on the hotbar is "
                    f"{', '.join(' '.join(n.split('_')) for n in risky)}, "
                    f"which I will not eat on my own — "
                    f"{UNSAFE_FOODS[risky[0]]}")
        if elsewhere:
            why = self._fetch.done_reason if self._fetch is not None \
                else "no reason"
            return (f"there is no food on the hotbar. There is "
                    f"{', '.join(' '.join(n.split('_')) for n in elsewhere)} "
                    f"in the rest of the inventory, and I could not move it "
                    f"to the hotbar: {why}. Move it there and I can eat it")
        return "there is no food anywhere in the inventory"

    def _in_front(self, state) -> str:
        """What a right click would use instead of eating, or ""."""
        if getattr(state, "target_entity", None) is not None:
            name = getattr(state.target_entity, "name", None) or "a mob"
            return f"the {str(name).split(':')[-1]}"
        block = getattr(state, "target_block", None)
        name = getattr(block, "name", None)
        if name and _interactive(name):
            return f"a {' '.join(str(name).split('_'))}"
        return ""

    def _look_up(self, state, blocker):
        pitch = (state.rotation or (0.0, 0.0))[1] or 0.0
        wanted = -60.0                              # up at the sky
        dy = int(round((wanted - pitch) * nav.pixels_per_degree()))
        dy = max(-action_spec.MAX_LOOK_DELTA_PX, min(-40, dy))
        return Step(action="look", params={"dx": 0, "dy": dy},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=(f"look up — {blocker} is under the crosshair and "
                          f"right-clicking it would not eat"))

    def _restore(self, state):
        """Put the slot back the way it was, once, then finish."""
        if self._restoring or self._previous_slot is None \
                or state.selected_slot == self._previous_slot:
            return None
        self._restoring = True
        slot = self._previous_slot + 1
        return Step(action="hotbar_select", params={"slot": slot},
                    expectation=verify_mod.holding_slot(slot),
                    note=f"back to hotbar slot {slot}")
