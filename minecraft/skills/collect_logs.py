"""
minecraft/skills/collect_logs.py -- collect_logs and fell_tree (CollectLogs,
on _Gatherer). Moved here unchanged from collect.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from minecraft import navigation as nav, verification as verify_mod
from minecraft.task_runner import Step

from minecraft.skills.base import (
    LOG_BLOCKS, _mine_params, _sweep_pixels, _tree_label,
)

from minecraft.skills.collect import _Gatherer


@dataclass
class CollectLogs(_Gatherer):
    """Find a tree, get to it, mine it, repeat. The first genuinely useful goal.

    HOW IT LOOKS FOR A TREE DEPENDS ON WHAT IT CAN SEE
        With the terrain scan, it finds the nearest log in the scanned volume,
        checks there is a walkable route, walks there, aims at it and mines.
        That is the difference between looking for a tree and turning on the
        spot hoping one comes past — which is what the previous version did,
        and what it looked like it was doing.

        Without the scan it falls back to the crosshair sweep. Same skill,
        much weaker, and the report says which one ran.

    WHAT IT COUNTS DEPENDS ON WHAT CAN BE SEEN TOO
        With the inventory readable, "collect 4 logs" means four logs actually
        in the inventory — the real claim. Without it, only the block
        disappearing is observable, and that is a weaker thing: an item that
        fell in lava or landed out of reach was broken and never picked up.
        So the result says which claim it is making.

    ONE HOLD PER LOG, NOT SEVERAL TAPS
        Minecraft discards breaking progress the moment the button comes up,
        so a mining step asks for one continuous hold of at least
        MIN_USEFUL_MINE_S. A skill that asks for one second mines forever and
        breaks nothing, which is indistinguishable from broken input."""

    name = "collect_logs"

    def _broken_noun(self) -> str:
        return "log(s)"

    def _clears_leaves(self) -> bool:
        return True

    def _nearest_seen(self, state):
        return nav.nearest_block(state, "log")

    def _none_found_note(self) -> str:
        return ". There may be a forest past that; I cannot see it"

    def _next_target(self, state):
        return self._next_log(state)

    def _work_without_the_map(self, state):
        return self._work_from_the_crosshair(state)

    def _next_log(self, state):
        """The next log to go for.

        From the tree already started, while it has one within reach of
        somewhere to stand. Only then another tree: the nearest, or the one
        under the crosshair. "Nearest log" on its own hopped between trees
        whenever a pickup walk left another trunk a little closer."""
        if self._tree:
            logs = nav.tree_logs(state, self._tree)
            self._adopt(logs)
            self._note_ceiling(state, logs)
            target = nav.nearest_of(state, logs, reachable_only=True,
                                    exclude=self._skip)
            if target is not None:
                return target
            if self.whole_tree:
                self._tree_done = True
                self._tree_left = logs
                return None
            self._tree = set()            # this one is done; the next tree

        start = self._log_under_crosshair(state)
        if start is None:
            start = nav.nearest_block(state, "log", reachable_only=True,
                                      exclude=self._skip)
        if start is None:
            return None
        logs = nav.tree_logs(state, {start.position}) or (start,)
        self._adopt(logs)
        self._note_ceiling(state, logs)
        return start

    def _note_ceiling(self, state, logs) -> None:
        """Remember when this tree reaches the highest row the scan reports
        logs in: past that, the scan cannot say whether the trunk goes on."""
        ceiling = getattr(state, "log_ceiling", None)
        if ceiling is not None and any(b.y >= ceiling for b in logs):
            self._tree_ceiling = ceiling

    def _adopt(self, logs) -> None:
        """Make `logs` the tree being worked on, keeping the name it was
        first given -- its trunk moves up as the bottom logs go."""
        if not logs:
            return
        label = next((self._tree_of[b.position] for b in logs
                      if b.position in self._tree_of), None)
        if label is None:
            lowest = min(logs, key=lambda b: b.y)
            label = (lowest.name, (lowest.x, lowest.z))
        for block in logs:
            self._tree.add(block.position)
            self._tree_of.setdefault(block.position, label)

    def _progress_text(self) -> str:
        if self.whole_tree:
            return self._tree_progress_text()
        if self._can_count:
            return (f"broke {self._broken} and collected {self._collected} "
                    f"of {self.count} log(s), counted in the inventory"
                    f"{self._trees_text()}")
        return (f"broke {self._broken} of {self.count} log(s){self._trees_text()}"
                f" — I cannot see the inventory, so I am reporting blocks "
                f"that disappeared, not items picked up")

    def _tree_progress_text(self) -> str:
        labels = [self._tree_of.get(tuple(w)) for w in self._broken_at]
        labels = [l for l in labels if l is not None]
        tree = _tree_label(labels[0]) if labels else "the tree"
        text = f"broke {self._broken} log(s) from {tree}"
        if self._can_count:
            text += (f" and collected {self._collected}, counted in the "
                     f"inventory")
        else:
            text += (" — I cannot see the inventory, so these are blocks "
                     "that disappeared, not items picked up")
        if self._tree_left:
            heights = sorted({b.y for b in self._tree_left})
            span = (f"y {heights[0]}" if len(heights) == 1
                    else f"y {heights[0]}–{heights[-1]}")
            text += (f"; {len(self._tree_left)} more log(s) of it are still "
                     f"standing ({span}) where I cannot reach or hit them")
        elif self._tree_done and self._tree_ceiling is not None:
            # Not "none left": the tree reached the top of what the scan
            # reports, so the rest of the trunk may stand above it unseen.
            text += (f"; I cannot tell whether any of it is still standing: "
                     f"the scan reports logs only up to y "
                     f"{self._tree_ceiling}, and this tree reached that "
                     f"height, so there may be more of it above")
        elif self._tree_done:
            text += "; none of it is left standing that I can see"
        return text

    def _trees_text(self) -> str:
        """Which trees the broken logs came from, so "why did you mine that
        tree" has a true answer rather than an invented one."""
        counts: dict = {}
        for where in self._broken_at:
            label = self._tree_of.get(tuple(where))
            if label is not None:
                counts[label] = counts.get(label, 0) + 1
        if not counts:
            return ""
        if len(counts) == 1:
            return f", all from {_tree_label(next(iter(counts)))}"
        parts = [f"{n} from {_tree_label(label)}"
                 for label, n in counts.items()]
        return f" — {', '.join(parts)}"

    def _work_from_the_crosshair(self, state):
        """The old behaviour, kept for when the mod is not running."""
        block = state.target_block
        name = getattr(block, "name", None) if block else None
        done = self._done

        if name in LOG_BLOCKS:
            ready = self._tool_for(state, name)
            if isinstance(ready, Step):
                return ready
            self._last_target = name
            # Judged on the block, never the bag -- see _mine.
            check = verify_mod.block_broken(name)
            return Step(action="mine",
                        params=_mine_params(self._mine_seconds(), block),
                        expectation=check,
                        note=f"mine {name} ({done}/{self.count})")

        # Nothing wooden under the crosshair: sweep the view looking for some.
        return Step(action="look",
                    params={"dx": self.delta_px or _sweep_pixels(), "dy": 0},
                    expectation=verify_mod.turned(min_degrees=2.0),
                    note=(f"sweeping for a log ({done}/{self.count}) — no "
                          f"terrain scan, so I can only check the crosshair"))
