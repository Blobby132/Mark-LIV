"""
actions/minecraft.py — the one tool that reaches the Minecraft subsystem.

WHERE THIS SITS
    Gemini → action_loader → core/permissions.guard → THIS FILE
           → minecraft/capabilities (phase gate) → minecraft/controller

    The broker decides. This file's job is to resolve which capability a call
    needs, validate the parameters, refuse anything this phase has not built,
    and turn a structured `ActionResult` back into something a model can say
    out loud without overstating it.

WHY THE CAPABILITY IS RESOLVED PER CALL
    `observe` and `move` are not the same risk, and `start_session` is the only
    one that needs a human. One capability for the whole tool would mean either
    confirming every step of a walk or confirming none of it.

WHAT THIS FILE DELIBERATELY DOES NOT DO
    It does not decide whether something is safe — `core/capabilities.py` does.
    It does not send input — `minecraft/input_backend.py` does, and only for
    sixteen keys. It does not import subprocess, os.system, or any other action
    module. `tests/minecraft/test_minecraft_boundary.py` fails the build if that changes.
"""

from __future__ import annotations

import time

from core import capabilities
from core import interrupts
from core import ocr as core_ocr

from minecraft import aiming as mc_aiming
from minecraft import capabilities as mc_phase
from minecraft import navigation as mc_nav
from minecraft import ores as mc_ores
from minecraft import perception as mc_perception
from minecraft import skills as mc_skills
from minecraft.skills import dig as mc_dig
from minecraft import danger as mc_danger
from minecraft.controller import MinecraftController
from minecraft.debug_overlay import DebugOverlayStateSource
from minecraft.mod_bridge import ModBridgeStateSource
from minecraft.mod_bridge import SCHEMA
from minecraft.mod_bridge import outdated_notice as _jar_notice
from minecraft.errors import (
    CapabilityDisabled, InvalidAction, MinecraftError, TaskAlreadyRunning,
)
from minecraft.observation import Observer
from minecraft.session import (
    GRANT_SUMMARY, MAX_SESSION_SECONDS, MIN_SESSION_SECONDS, UNLIMITED,
)
from minecraft.task_runner import MAX_TASK_SECONDS, MAX_TASK_STEPS, TaskRunner
from minecraft.task_slot import TaskSlot

from actions._minecraft_text import TOOL_DESCRIPTION, TOOL_PARAMETERS

# One controller for the process. Minecraft is one game with one window and one
# session, and a second controller would mean a second ledger — two records of
# which keys are held, neither of them complete.
_controller: MinecraftController | None = None
_observer: Observer | None = None
_state_source = None
_state_source_pinned = False

# The one bridge reader, kept for the life of the process so what it caches
# -- the last parse, the last good read -- survives between calls, and when
# it last answered. A bridge that answered within BRIDGE_STICKY_SECONDS is
# kept through a failed check: that is a read racing the mod's rename, not
# the game closing, and the task runner keeps whatever source it is handed
# for the whole task.
_bridge = None
_bridge_ok_at = None
BRIDGE_STICKY_SECONDS = 3.0

# The outdated-mod notice last said (see _feature_notice): said once, early,
# and again only if what the jar lacks changes.
_feature_notice_said = None

# The one background task, if any. See minecraft/task_slot.py: run_task starts
# a task here and returns at once, so the conversation -- including "stop" --
# is not held up for the length of the task.
_slot = TaskSlot()

# Actions that press something. While a task is running these are refused,
# so a direct command cannot fight the task for the keyboard. Reading, status,
# stopping and cancelling are never refused.
_INPUT_ACTIONS = frozenset({
    "move", "move_and_jump", "jump", "look", "sneak", "sprint", "attack",
    "mine", "place", "build", "interact", "use_item", "eat", "drop",
    "hotbar_select", "inventory", "toggle_debug", "run_task",
})


def _entity_probe():
    """The mob under the crosshair as (name, category), or None -- for an
    attack that must only ever hit something hostile (fight)."""
    try:
        entity = _get_state_source().read().target_entity
    except Exception:
        return None
    if entity is None:
        return None
    return (entity.name, getattr(entity, "category", None))


def _target_probe():
    """What the crosshair is on -- (name, x, y, z, face) -- for the mining
    loop to watch, and for mine and place to confirm before pressing.

    Returns None when nothing can read it, which the controller treats as "no
    probe" and falls back to a plain timed hold. Deliberately cheap: it runs
    every 40ms while a block is being broken."""
    try:
        block = _get_state_source().read().target_block
    except Exception:
        return None
    if block is None:
        return None
    return (block.name, block.x, block.y, block.z,
            getattr(block, "face", None))


# Danger inside a hold, per kind of action: (hostile radius, health lost).
# Working holds -- mining for up to ten seconds, eating, using a block --
# let go for a zombie three blocks off or a heart lost.
#
# Movement (move, move_and_jump, sprint, sneak) has NO check, and that is
# the point: walking is how you get away. A "looser" check here refused the
# step back from a zombie at arm's length -- exactly when it was needed --
# and cut navigate_to off the same way, since it is built from moves.
# Attacking and using an item (a shield, a bow) are how you fight, so the
# mob being there is the point: no check either.
_HAZARD_THRESHOLDS = {
    "mine": (3.0, 2.0), "eat": (3.0, 2.0), "interact": (3.0, 2.0),
    "place": (3.0, 2.0),
}


def _hazard_probe(action):
    """The controller's `hazard_probe`: a check for one hold, or None.

    The check compares against the health at the start of the hold, so
    damage taken before it started -- already reported by the task's own
    danger watch -- does not stop it.

    Only with the mod's bridge, resolved once per hold. The check runs
    inside the hold loop, between the 40ms focus-guard ticks: the bridge is
    one small file read, but the OCR route is a screenshot and OCR -- 300ms
    and more, with keys down and the guard waiting -- and the overlay
    reports no health or mobs, so the check could never have fired there."""
    thresholds = _HAZARD_THRESHOLDS.get(str(action))
    if thresholds is None:
        return None
    try:
        source = _get_state_source()
    except Exception:
        return None
    if not isinstance(source, ModBridgeStateSource):
        return None
    read = source.read
    radius, hurt_by = thresholds
    watch = mc_danger.DangerWatch(hostile_radius=radius, hurt_by=hurt_by)
    try:
        watch.check(read())                             # the starting health
    except Exception:
        return None

    def check():
        return watch.check(read())
    return check


def _held_item():
    """The controller's `held_item_probe`: what is in the main hand.

    The held item's name, "" for an empty hand, or None when it cannot be
    read -- which `place` treats as a refusal. Bridge only: nothing else
    can see the hand. The mod reports held_item as null for an empty hand,
    so an absent one is resolved from the selected slot and the inventory,
    which tell "empty" from "unknown"."""
    try:
        source = _get_state_source()
        if not isinstance(source, ModBridgeStateSource):
            return None
        state = source.read()
    except Exception:
        return None
    held = getattr(state, "held_item", None)
    if held is not None and getattr(held, "name", None):
        return held.name
    slot = getattr(state, "selected_slot", None)
    inventory = getattr(state, "inventory", None)
    if slot is None or inventory is None:
        return None
    for stack in inventory:
        if stack.slot == slot and (stack.count or 0) > 0:
            return stack.name
    return ""


def _gui_state():
    """The controller's `gui_probe`: a fresh bridge reading for the screen
    rules, or None. Bridge only -- nothing else reports the screen."""
    try:
        source = _get_state_source()
        if not isinstance(source, ModBridgeStateSource):
            return None
        return source.read()
    except Exception:
        return None


def _get_controller() -> MinecraftController:
    global _controller, _observer
    if _controller is None:
        _controller = MinecraftController(progress_probe=_target_probe,
                                          hazard_probe=_hazard_probe,
                                          held_item_probe=_held_item,
                                          gui_probe=_gui_state,
                                          entity_probe=_entity_probe)
        _observer = Observer(_controller._locator)
    return _controller


def _minecraft_busy() -> bool:
    """Is anything running that a spoken "stop" should reach?"""
    if _slot.busy():
        return True
    controller = _controller
    return bool(controller is not None and getattr(controller, "busy", False))


def cancel_running(reason: str = "cancelled") -> dict:
    """Stop the running task and any action in progress; keep the session.

    What a spoken "stop" and a withdrawn tool call both mean. Never refused,
    never blocks: the task is asked to stop, and the controller releases
    every input right now rather than waiting for it to."""
    job = _slot.cancel(reason)
    report = {"task_cancelled": job.name if job is not None else None}
    controller = _controller
    if controller is not None:
        try:
            report.update(controller.cancel_current(reason))
        except Exception as e:                    # pragma: no cover
            report["error"] = f"{type(e).__name__}: {e}"
    return report


# A spoken "stop" reaches this directly, without waiting for the model to
# decide to call a tool -- see core/interrupts.py. Registered at import, which
# happens once, when the action loader discovers this file.
interrupts.register("minecraft", _minecraft_busy, cancel_running,
                    tools=("minecraft_control",))


def _get_observer() -> Observer:
    _get_controller()
    return _observer


def _get_state_source():
    """The best state source available RIGHT NOW.

    Three implementations, in order of how much they can actually tell us:

        mod bridge  — the game's own numbers. Everything, `exact`, including
                      the terrain around the player, which is what makes
                      navigating possible at all.
        F3 overlay  — OCR of the debug screen. Position and the targeted
                      block, `inferred`, and NOTHING about the terrain.
        vision      — an honest empty state. Reads nothing.

    THE PREFERENCE IS RE-EVALUATED EVERY CALL, NOT JUST THE AVAILABILITY
        An earlier version kept whatever it had picked for as long as that
        source still worked. That reads as caching and is really a trap: the
        ordinary sequence is JARVIS first, Minecraft second, mod third, so
        the overlay is very often available BEFORE the bridge is, gets
        picked, and then keeps being picked forever because it never stopped
        working. The result is an assistant with the mod installed and
        running that quietly cannot see the terrain, sweeping the crosshair
        and reporting no trees.

        So the bridge is asked first on every call. Cheap — one small file
        read — and it is only called once per task, not once per step.

    Everything above this function is written against `StateSource` and never
    learns which one it got; that seam is why the bridge could be added
    without touching the planner, the task runner or verification."""
    global _state_source, _bridge, _bridge_ok_at
    if _state_source_pinned:
        return _state_source

    if _bridge is None:
        _bridge = ModBridgeStateSource()
    bridge = _bridge
    now = time.monotonic()
    if bridge.available():
        _bridge_ok_at = now
        _state_source = bridge
        return _state_source
    if _state_source is bridge and _bridge_ok_at is not None \
            and now - _bridge_ok_at <= BRIDGE_STICKY_SECONDS:
        return _state_source

    if isinstance(_state_source, DebugOverlayStateSource) \
            and _state_source.available():
        return _state_source

    overlay = DebugOverlayStateSource(observer=_get_observer(),
                                      reader=core_ocr.create_reader())
    if overlay.available():
        _state_source = overlay
        return _state_source

    # Nothing can read the game. Keep the bridge as the reported source so the
    # reason names the thing worth installing rather than the OCR the user may
    # have already decided against.
    _state_source = bridge
    return _state_source


def _reset_for_tests(controller=None, observer=None, state_source=None) -> None:
    """Swap in fakes. Only the tests call this; it exists so they can exercise
    the real adapter rather than a copy of its logic."""
    global _controller, _observer, _state_source, _state_source_pinned, _slot
    global _bridge, _bridge_ok_at, _feature_notice_said
    _feature_notice_said = None
    _controller = controller
    _observer = observer
    _state_source = state_source
    _bridge = None
    _bridge_ok_at = None
    _slot = TaskSlot()
    # A fake handed in here is used exactly as given; the real resolution
    # order would otherwise replace it with whatever this machine has.
    _state_source_pinned = state_source is not None


# ── Capability resolution ────────────────────────────────────────────────────

_CAPABILITY_BY_ACTION = {
    # Reading. No session needed.
    "status":        capabilities.MINECRAFT_OBSERVE,
    "observe":       capabilities.MINECRAFT_OBSERVE,
    "read_state":    capabilities.MINECRAFT_READ_STATE,
    # Reading the terrain scan. Same capability as read_state because it is
    # the same file read, summarised -- it presses nothing and needs no
    # session.
    "look_around":   capabilities.MINECRAFT_READ_STATE,
    # The ore the bridge's scan lists. A read like look_around: it presses
    # nothing. Single-player worlds only -- see minecraft/ores.py.
    "find_ores":     capabilities.MINECRAFT_READ_STATE,
    "toggle_debug":  capabilities.MINECRAFT_READ_STATE,

    # THE confirmation, and the way out of it.
    "start_session": capabilities.MINECRAFT_CONTROL,
    "end_session":   capabilities.MINECRAFT_STOP,
    "stop":          capabilities.MINECRAFT_STOP,

    # Gameplay. All covered by the one session grant.
    "move":          capabilities.MINECRAFT_MOVEMENT,
    "jump":          capabilities.MINECRAFT_MOVEMENT,
    # Walking and jumping at once. The same capability as each half: it
    # presses a movement key and the jump key, both already covered.
    "move_and_jump": capabilities.MINECRAFT_MOVEMENT,
    "sneak":         capabilities.MINECRAFT_MOVEMENT,
    "sprint":        capabilities.MINECRAFT_MOVEMENT,
    "look":          capabilities.MINECRAFT_LOOK,
    "attack":        capabilities.MINECRAFT_COMBAT,
    "mine":          capabilities.MINECRAFT_MINING,
    "place":         capabilities.MINECRAFT_BUILD,
    "build":         capabilities.MINECRAFT_BUILD,
    "interact":      capabilities.MINECRAFT_INTERACT,
    "use_item":      capabilities.MINECRAFT_ITEMS,
    "eat":           capabilities.MINECRAFT_ITEMS,
    "drop":          capabilities.MINECRAFT_ITEMS,
    "hotbar_select": capabilities.MINECRAFT_ITEMS,
    "inventory":     capabilities.MINECRAFT_INVENTORY,
    # Moving items inside the inventory and crafting-table screens. Part of
    # "use the inventory"; the tool still refuses them one at a time below,
    # and every click passes minecraft/gui.py's gate in the controller.
    "gui_point":     capabilities.MINECRAFT_INVENTORY,
    "gui_click":     capabilities.MINECRAFT_INVENTORY,
    "gui_swap":      capabilities.MINECRAFT_INVENTORY,
    "run_task":      capabilities.MINECRAFT_TASK,
    # About the background task. Status is a read; cancelling is a stop, and
    # like every stop it is never refused.
    "task_status":   capabilities.MINECRAFT_READ_STATE,
    "cancel_task":   capabilities.MINECRAFT_STOP,

    # Named so they resolve to their real capability and are refused by the
    # phase gate with an explanation, rather than falling through to the
    # unknown-action branch and getting a vaguer answer.
    "chat":          capabilities.MINECRAFT_CHAT,
    "say":           capabilities.MINECRAFT_CHAT,
    "command":       capabilities.MINECRAFT_COMMAND,
    "launch":        capabilities.MINECRAFT_LAUNCH,
}


def _mc_capability(params: dict) -> str:
    """Which capability this call needs.

    An unrecognised action resolves to `minecraft.command`, which is DENY. That
    is deliberate: the alternative is a permissive default, and an action name
    nobody wrote a handler for is exactly when you want the strictest answer."""
    action = str((params or {}).get("action", "")).lower().strip()
    return _CAPABILITY_BY_ACTION.get(action, capabilities.MINECRAFT_COMMAND)


def _mc_guard(params: dict) -> dict:
    """What the confirmation banner says.

    Only `start_session` shows one, and it is the only confirmation in this
    whole subsystem. So it has to actually inform: it names every kind of
    action it is buying, in the user's words, and it is explicit about how
    long it lasts — because session-level consent is only better than
    per-action consent when the person knows what they agreed to. A vague
    banner here would make this worse, not better."""
    action = str((params or {}).get("action", "")).lower().strip()
    if action != "start_session":
        return {"summary": f"Minecraft: {action or 'unknown'}"}

    seconds = _requested_seconds(params)
    if seconds is None:
        summary = "Allow JARVIS to play Minecraft until you stop it?"
        duration = (
            "This lasts until you say stop or press F12 — there is no timer. "
            "It also ends if you close the game, and I let go of every key "
            "the moment you Alt-Tab away or Minecraft stops being the window "
            "in front."
        )
    else:
        summary = f"Allow JARVIS to play Minecraft for {int(seconds)} seconds?"
        duration = (
            f"It lasts {int(seconds)} seconds and ends early if you press "
            f"F12, Alt-Tab away, or close the game."
        )

    return {
        "summary": summary,
        "detail": (
            f"This ONE approval lets me {GRANT_SUMMARY} — without asking "
            f"again for each action.\n\n"
            f"{duration}\n\n"
            f"Mining and placing change your world and I cannot undo them, so "
            f"use a world you do not mind changing.\n\n"
            f"This does NOT let me type in chat, run slash commands, or touch "
            f"anything outside Minecraft. I never attack a player, a pet, a "
            f"villager or an animal -- only a mob the game calls hostile. "
            f"I never right-click with a bucket, flint and steel, TNT or a "
            f"spawn egg to open something, and I pour lava or water or "
            f"start a fire only when you name the item. "
            f"When you ask me to dig or to mine ore, and only in a "
            f"single-player world, "
            f"I dig a staircase through natural stone, earth and ore: never "
            f"the block under you, never beside water or lava or within "
            f"three blocks of lava, never under sand or gravel, and at most "
            f"40 blocks, 16 down and 32 across a dig, stopping at once if "
            f"you are hurt or anything moves that should not. For ore I go "
            f"only for ore the scan lists, never one with water or lava "
            f"beside it, and take the vein I can reach from beside it. "
            f"Inside screens I only ever click in "
            f"your inventory and a crafting table -- never a chest, a "
            f"furnace or the creative inventory -- and only when the game "
            f"reports the pointer over the slot I mean."
        ),
    }


def _requested_seconds(params: dict):
    """How long the caller asked for, or None for "until stopped".

    Unlimited is the default. The clock was always the weakest of the stops —
    it protects against forgetting, not against anything going wrong — and
    expiring mid-conversation trains people to re-approve without reading,
    which is the habit the one-confirmation design exists to avoid."""
    raw = (params or {}).get("duration_s")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(MIN_SESSION_SECONDS, min(value, MAX_SESSION_SECONDS))


# ── Result shaping ───────────────────────────────────────────────────────────

def _result_line(result, player=None) -> str:
    """One sentence for the model, plus the structured detail it needs.

    The sentence never claims more than happened: a move cut short by focus
    loss reads as stopped, not done.

    It is also written to the HUD. Without that, a refused action was visible
    only to the model: the user saw a burst of tool calls and a character
    standing still, with no way to find out that every one of them had been
    turned down for a reason that was sitting in a return value. A safety
    refusal nobody can see is indistinguishable from a bug."""
    if player:
        if result.ok:
            note = getattr(result, "note", "")
            player.write_log(f"[minecraft] {result.action}: ok "
                             f"({result.actual_duration_ms}ms)"
                             + (f" -- {note}" if note else ""))
        else:
            player.write_log(f"[minecraft] {result.action} REFUSED: "
                             f"{result.stopped_reason or result.error_class}")
            if result.error:
                player.write_log(f"[minecraft] {result.error}")
    return f"{result.describe()}\n{result.as_dict()}"


# ── The handler ──────────────────────────────────────────────────────────────

_TASK_NEEDS = {
    "craft_item": ("gui",),
    "place_block_at": ("near_blocks",),
    "build_line": ("near_blocks",),
    "build_blueprint": ("near_blocks",),
    "collect_logs": ("tree_up", "item_names"),
    "fell_tree": ("tree_up", "item_names"),
    "collect_blocks": ("item_names",),
    "dig_to": ("singleplayer", "near_grid"),
    "mine_ore": ("singleplayer", "ores", "near_grid"),
}
"""The mod's features each task depends on (mod_bridge.FEATURES), besides
mob_categories, which every task needs: the danger watch between steps sees
only a mob the mod calls hostile, so without the categories it sees none."""

_ACTION_NEEDS = {
    "inventory": ("gui",),
    "attack": ("mob_categories",),
    "find_ores": ("singleplayer", "ores"),
}


def _needs_of(params: dict):
    """The features an action depends on. None for start_session, where any
    missing feature is worth saying; () for one that needs none."""
    action = str(params.get("action", "")).lower().strip()
    if action == "start_session":
        return None
    if action == "run_task":
        task = str(params.get("task", "")).strip().lower()
        return ("mob_categories",) + _TASK_NEEDS.get(task, ()) if task else ()
    return _ACTION_NEEDS.get(action, ())


def _bridge_for_notice():
    """The bridge reader, whichever source is answering: the notice is about
    the installed jar. A pinned test source is used only if it can say."""
    global _bridge
    if _state_source_pinned:
        source = _state_source
    else:
        if _bridge is None:
            _bridge = ModBridgeStateSource()
        source = _bridge
    return source if callable(getattr(source, "missing_features", None)) \
        else None


def _feature_notice(params: dict) -> str:
    """One line when the installed mod is older than this Jarvis, or "".

    Said once, early: when a session starts, or -- if nothing could be read
    then -- the first time an action or task needs a feature the jar lacks.
    Again only if what it lacks changes."""
    global _feature_notice_said
    needs = _needs_of(params)
    if needs == ():
        return ""
    bridge = _bridge_for_notice()
    if bridge is None:
        return ""
    try:
        schema = bridge.schema()
        missing = tuple(bridge.missing_features())
    except Exception:
        return ""
    if schema is None:
        return ""
    old_schema = schema != SCHEMA
    if needs is not None and not old_schema and not set(needs) & set(missing):
        return ""
    notice = _jar_notice(missing, old_schema=old_schema)
    if not notice or notice == _feature_notice_said:
        return ""
    _feature_notice_said = notice
    return notice


def minecraft_control(parameters: dict = None, player=None,
                      session_memory=None, response=None, speak=None) -> str:
    """The tool. An older mod jar is named here, once, in one line on the
    HUD and at the top of the reply, so the model reports the real cause of
    what it cannot do instead of guessing."""
    reply = _minecraft_control(parameters, player=player,
                               session_memory=session_memory,
                               response=response, speak=speak)
    notice = _feature_notice(parameters or {})
    if not notice:
        return reply
    if player:
        player.write_log(f"[minecraft] {notice}")
    return f"{notice}\n{reply}"


def _minecraft_control(parameters: dict = None, player=None,
                       session_memory=None, response=None,
                       speak=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if not action:
        return ("Tell me what to do in Minecraft: status, observe, "
                "read_state, start_session, move, look, jump, stop.")

    capability = _mc_capability(params)

    # The phase gate. Narrowing only — it can refuse something the central
    # table allows, and can never permit something the table refuses.
    if not mc_phase.is_enabled(capability):
        reason = mc_phase.why_disabled(capability)
        if player:
            player.write_log(f"[minecraft] refused {action}: not in this phase")
        return reason

    try:
        controller = _get_controller()

        # One thing at a time on the keyboard. While a task runs, anything
        # that presses keys is refused -- not queued -- and says how to stop
        # the task. Reading, status and every kind of stop stay available.
        running = _slot.current()
        if running is not None and action in _INPUT_ACTIONS:
            if player:
                player.write_log(f"[minecraft] refused {action}: the "
                                 f"{running.name} task is running")
            return (f"I did not do that: the {running.name} task is still "
                    f"running ({running.elapsed:.0f}s so far) and it has the "
                    f"controls. Say stop, or use cancel_task, to end it "
                    f"first.\n{running.as_dict()}")

        if action == "status":
            status = controller.status()
            status["state_source"] = _source_label(_get_state_source())
            status["task"] = (running.as_dict() if running is not None
                              else None)
            return (f"{_status_line(status)}\n"
                    f"Reading the world from: {status['state_source']}\n"
                    f"{status}")

        if action == "task_status":
            if running is not None:
                return (f"The {running.name} task is running "
                        f"({running.elapsed:.0f}s so far).\n"
                        f"{running.as_dict()}")
            last = _slot.last()
            if last is None:
                return "No Minecraft task has run yet."
            return (f"No task is running. The last one was {last.name}: "
                    f"{_job_summary(last)}\n{last.as_dict()}")

        if action == "cancel_task":
            outcome = cancel_running("cancelled by request")
            if player:
                player.write_log("[minecraft] task cancelled, all input "
                                 "released; session still open")
            what = (f"Cancelled the {outcome['task_cancelled']} task."
                    if outcome.get("task_cancelled")
                    else "No task was running; I released anything held.")
            return (f"{what} The control session is still open.\n{outcome}")

        if action == "observe":
            observation = _get_observer().capture()
            if player:
                player.write_log(f"[minecraft] observe: "
                                 f"{'ok' if observation.ok else 'failed'}")
            return (f"{observation.describe()}\n"
                    f"{observation.as_dict(include_frame=False)}")

        if action == "read_state":
            source = _get_state_source()
            state = source.read()
            extra = ""
            if state.is_empty and not getattr(source, "available", lambda: True)():
                extra = f"\n{source.unavailable_reason()}"
            return f"{state.describe()}{extra}\n{state.as_dict()}"

        if action == "look_around":
            state = _get_state_source().read()
            summary = mc_nav.summarise(state)
            seen = _what_is_under_the_crosshair(state)
            summary["crosshair"] = seen.as_dict()
            return (f"{_around_line(state, summary)} Under the crosshair: "
                    f"{seen.describe()}.\n{summary}")

        if action == "find_ores":
            return _find_ores(params)

        if action == "toggle_debug":
            return _result_line(controller.toggle_debug_overlay(), player)

        if action == "start_session":
            seconds = _requested_seconds(params)
            info = controller.start_session(
                duration_s=UNLIMITED if seconds is None else seconds,
                owner=str(params.get("owner", "user"))[:40],
            )
            if player:
                player.write_log("[minecraft] control session started")
            warning = ""
            if not info.get("input_available"):
                warning = ("\nNOTE: I cannot actually send input on this "
                           "machine — " + info.get("input_backend", ""))
            session = info["session"]
            if session.get("unlimited"):
                lifetime = "until you tell me to stop"
            else:
                lifetime = f"for {session['granted_seconds']:.0f}s"

            if info.get("reused_existing"):
                left = ("" if session.get("unlimited")
                        else f" — {session['remaining_seconds']:.0f}s left")
                return (f"I already have control{left}. No need to start "
                        f"another session; just tell me what to do."
                        f"{warning}\n{info}")
            return (f"Minecraft session open {lifetime} — I can now "
                    f"{GRANT_SUMMARY} without asking again. "
                    f"{info['emergency_stop']}{warning}\n{info}")

        if action in ("end_session", "stop"):
            # The task first, so no further step starts; then the controller,
            # which releases everything now and ends the session.
            _slot.cancel("stopped by request" if action == "stop"
                         else "ended by request")
            outcome = controller.stop(
                "stopped by request" if action == "stop" else "ended by request")
            if action == "end_session":
                outcome = controller.end_session("ended by request")
            if player:
                player.write_log("[minecraft] stopped, all input released")
            released = outcome.get("released") or []
            detail = (f" Released: {', '.join(released)}." if released
                      else " Nothing was being held.")
            if not outcome.get("clean", True):
                detail += (f" WARNING: could not release "
                           f"{', '.join(outcome.get('failed_to_release', []))}.")
            return f"Minecraft control stopped.{detail}\n{outcome}"

        if action == "move":
            return _result_line(controller.move(params), player)

        if action == "move_and_jump":
            return _result_line(controller.move_and_jump(params), player)

        if action == "jump":
            return _result_line(controller.jump(params), player)

        if action == "look":
            return _result_line(controller.look(params), player)

        if action == "attack":
            return _result_line(controller.attack(params), player)

        if action == "mine":
            return _result_line(controller.mine(params), player)

        if action in ("place", "build"):
            return _result_line(controller.place(params), player)

        if action == "interact":
            return _result_line(controller.interact(params), player)

        if action == "use_item":
            return _result_line(controller.use_item(params), player)

        if action == "eat":
            return _result_line(controller.eat(params), player)

        if action == "drop":
            return _result_line(controller.drop(params), player)

        if action == "inventory":
            return _result_line(controller.inventory(params), player)

        if action == "hotbar_select":
            return _result_line(controller.hotbar_select(params), player)

        if action == "sneak":
            return _result_line(controller.sneak(params), player)

        if action == "sprint":
            return _result_line(controller.sprint(params), player)

        if action in ("gui_point", "gui_click", "gui_swap"):
            # Not one at a time: a click is only ever part of a task that
            # knows what every click is for. The controller would gate a
            # single click anyway; this keeps the model from steering the
            # pointer by hand.
            return (f"{action} is not something to do step by step. To "
                    f"craft, use run_task craft_item; it moves the pointer "
                    f"and clicks itself, each click checked against the game.")

        if action == "run_task":
            return _run_task(controller, params, player, speak)

        # Reachable only if someone adds a name to _CAPABILITY_BY_ACTION and
        # to ENABLED without writing the handler.
        raise InvalidAction(f"'{action}' is not something I can do yet.")

    except CapabilityDisabled as e:
        return str(e)
    except InvalidAction as e:
        # Nothing was attempted — say so, so the planner retries with better
        # parameters rather than assuming a partial action happened.
        return f"I did not do that: {e}"
    except MinecraftError as e:
        return f"{e}"
    except Exception as e:                            # pragma: no cover
        # Release held input, but do NOT stop the controller.
        #
        # This used to call stop(), which ends the session and sets the sticky
        # cancel flag. That turned any unexpected error -- including a merely
        # redundant request -- into a dead controller: every later action was
        # refused with the stale reason, and only a new session could clear it.
        # Releasing the keys is the part that matters for safety; tearing down
        # a valid session is not, and it cost the user their working session.
        try:
            _get_controller().release_inputs()
        except Exception:
            pass
        if player:
            player.write_log(f"[minecraft] error in {action}: "
                             f"{type(e).__name__}")
        return (f"Minecraft control failed ({type(e).__name__}: {e}). "
                f"I released any keys I was holding; the control session is "
                f"still open.")


def _run_task(controller, params: dict, player=None, speak=None) -> str:
    """Start one bounded skill in the background, and return at once.

    The model chooses WHICH skill and with what parameters. It does not get to
    describe the steps: a skill is ordinary Python, so the sequence of actions
    is decided in source and the runner validates every one of them again
    against `action_spec` before it reaches the controller.

    WHY IT DOES NOT WAIT FOR THE TASK
        A task can take two minutes, and this function runs inside the tool
        call the voice session is waiting on. Waiting here meant nothing the
        user said in those two minutes could be acted on -- "stop" included.
        So the task goes to the background slot and the tool call answers
        "started"; the real result is reported into the conversation through
        `speak` when the task ends, and to the HUD step by step as before."""
    name = str(params.get("task", "")).strip().lower()
    if not name:
        return (f"Which task? I have: {', '.join(mc_skills.available())}. "
                f"Some things I cannot do yet: "
                f"{', '.join(sorted(mc_skills.NOT_YET_POSSIBLE))}.")

    options = {}
    for key in ("seconds", "direction", "expected", "swings", "steps",
                "count", "slot", "target", "item", "plan", "size", "ore",
                "radius"):
        if key in params:
            options[key] = params[key]
    if name in ("aim_at_block", "mine_block", "place_block_at",
                "build_line", "dig_to"):
        block = _block_from(params)
        if isinstance(block, str):
            return block
        options.update(x=block[0], y=block[1], z=block[2])
        options.pop("target", None)
    if name == "build_blueprint":
        if any(k in params for k in ("x", "y", "z")):
            block = _block_from(params)
            if isinstance(block, str):
                return block
            options.update(x=block[0], y=block[1], z=block[2])
    if name == "navigate_to":
        column = _destination_from(params)
        if isinstance(column, str):
            return column
        if column is not None:
            options["destination"] = column
        if not options.get("destination") and not options.get("target"):
            return ("Where to? navigate_to needs either a destination "
                    "(x and z) or a target such as 'log', 'stone' or "
                    "'water'. Call look_around first to see what is there.")

    try:
        skill = mc_skills.create(name, **options)
    except KeyError as e:
        return str(e).strip('"')
    except TypeError as e:
        return (f"'{name}' does not take those options ({e}). "
                f"Try it without them.")

    # Refuse NOW what the task would only discover on its first step, so the
    # answer arrives in this turn rather than as a report a moment later.
    # Focus is deliberately not checked: every action waits briefly for the
    # game to come back to the front, because the confirmation banner is
    # always in front of it the moment a session starts.
    blocked = _session_refusal(controller)
    if blocked:
        return blocked

    plan = ""
    if name == "mine_ore":
        # The same choice the task will make on its first step, said now:
        # the model tells the user the plan, and nothing starts without one.
        plan = _mine_ore_plan(_get_state_source().read(), options)
        if not plan.startswith("Plan:"):
            return plan

    runner = TaskRunner(controller, _get_state_source(),
                        observer=_get_observer())

    def _finished(job):
        _log_task(player, job)
        if speak is not None:
            try:
                speak(_completion_notice(job))
            except Exception:
                pass

    try:
        job = _slot.start(runner, skill, name=name,
                          max_steps=params.get("max_steps", MAX_TASK_STEPS),
                          on_done=_finished)
    except TaskAlreadyRunning as e:
        return f"I did not start {name}: {e}"

    if player:
        player.write_log(f"[minecraft] task {name} started in the background "
                         f"(say stop to cancel it)")
    if plan:
        plan = f" {plan} Tell the user this plan."
    return (f"Started {name} — {job.goal}.{plan} It is running now, in the "
            f"background, for at most {MAX_TASK_SECONDS:.0f} seconds. It is "
            f"NOT finished: do not tell the user it worked. When it ends, a "
            f"message beginning [Minecraft task] will say what actually "
            f"happened; relay that. If the user says stop, it stops at once.\n"
            f"{job.as_dict()}")


_SESSION_REFUSALS = {
    "no_session": "There is no Minecraft control session. Ask me to start "
                  "one — one confirmation covers all ordinary gameplay.",
    "session_expired": "The Minecraft session has run out. Start another if "
                       "you want me to keep playing.",
    "session_cancelled": "The Minecraft session was stopped. Start another "
                         "if you want me to keep playing.",
    "session_inactive": "The Minecraft session has ended. Start another if "
                        "you want me to keep playing.",
    "process_gone": "Minecraft is not running.",
    "window_gone": "I cannot find the Minecraft window.",
}


def _session_refusal(controller) -> str:
    """Why a task cannot start at all right now, or ''."""
    guard = getattr(controller, "_guard", None)
    if not callable(guard):
        return ""
    try:
        reason = guard()
    except Exception:
        return ""
    # Focus is the one failure a task may start through: see the caller.
    if not reason or reason == "focus_lost":
        return ""
    explanation = _SESSION_REFUSALS.get(
        reason,
        f"Minecraft control is stopped ({reason}). Start a new session to "
        f"keep playing.")
    return f"I did not start the task: {explanation}"


def _job_summary(job) -> str:
    """One honest sentence about a finished task."""
    if job.error:
        return f"it ended with an internal error ({job.error})."
    result = job.result
    if result is None:
        return "it produced no result."
    return result.describe()


def _completion_notice(job) -> str:
    """What the model is told when a task ends. Built not to overstate it.

    Sent into the conversation, so it is short: the task's own verdict, which
    already separates what was verified from what was attempted -- never the
    step-by-step record, which goes to the HUD."""
    if job.cancel_reason:
        session = _controller.sessions.current if _controller else None
        still_open = bool(session is not None and session.active)
        return (f"[Minecraft task] {job.name} was stopped ({job.cancel_reason})."
                f" {_job_summary(job)} Every key and button is released"
                + ("; the control session is still open."
                   if still_open else ".")
                + " Tell the user in one short sentence.")
    return (f"[Minecraft task] {job.name} finished. {_job_summary(job)} Tell "
            f"the user in one or two sentences and do not claim more than "
            f"this says.")


def _log_task(player, job) -> None:
    """The step-by-step trail, on the HUD. Unchanged from when the task ran
    in the foreground -- only moved to when it finishes."""
    if not player:
        return
    try:
        _write_task_trail(player, job)
    except Exception:
        pass


def _write_task_trail(player, job) -> None:
    name = job.name
    if job.error:
        player.write_log(f"[minecraft] task {name}: error — {job.error}")
        return
    result = job.result
    if result is None:
        return
    # Every step, not just the summary. A task that ends "incomplete" tells
    # you nothing about WHY, and the per-step trail is the difference
    # between "it mined four times and nothing broke" and "it never mined
    # at all" -- which are opposite problems that both read as failure.
    source = _get_state_source()
    player.write_log(f"[minecraft] reading the world from: "
                     f"{_source_label(source)}")
    for entry in result.records:
        action = entry.step.get("action", "?")
        held = entry.action_result.get("actual_duration_ms", 0)
        verdict = entry.verification.get("status", "?")
        # The note is the only part that says WHAT it was doing --
        # "aim at oak_log (5, 64, 3)" and "looking for a log" are the
        # same `look` action and completely different situations.
        # Dropping it made these logs unreadable after the fact.
        note = entry.step.get("note", "")
        # Two different ways a hold ends short, and they mean opposite
        # things: the guard stopping it, and mining letting go because
        # the block went. Neither was visible in this log, which is why
        # a 126ms swing looked like nothing at all.
        cut = entry.action_result.get("stopped_reason") or ""
        if not cut:
            cut = (entry.action_result.get("requested") or {}).get(
                "stopped_early") or ""
        line = (f"[minecraft]   {entry.index + 1}. {action} "
                f"{held}ms -> {verdict}")
        if note:
            line += f"  | {note}"
        if cut:
            line += f"  | CUT SHORT: {cut}"
        player.write_log(line)
        why = entry.verification.get("reason") or ""
        if why and verdict != "success":
            player.write_log(f"[minecraft]        {why}")
    player.write_log(f"[minecraft] task {name}: {result.status} "
                     f"({result.steps_taken} steps)")
    if result.reason:
        player.write_log(f"[minecraft] {result.reason}")


def _what_is_under_the_crosshair(state):
    """The bridge's answer if it has one; a visual guess only if it does not.

    A frame is captured ONLY when the bridge cannot say. Vision is the
    fallback, never a second opinion on the game's own data — and the result
    carries its source so nothing downstream mistakes a colour match for a
    block id."""
    exact = mc_perception.from_bridge(state)
    if exact is not None:
        return exact
    try:
        shot = _get_observer().capture(compress=False)
        frame = shot.frame if getattr(shot, "ok", False) else None
    except Exception:
        frame = None
    return mc_perception.crosshair(None, frame)


def _find_ores(params: dict) -> str:
    """The ore the bridge's scan lists near the player, nearest first.
    Read-only; refused outside a single-player world (minecraft/ores.py)."""
    state = _get_state_source().read()
    problem = mc_ores.refusal(state)
    if problem:
        return problem
    try:
        radius = int(params.get("radius", mc_ores.DEFAULT_RADIUS))
    except (TypeError, ValueError):
        radius = mc_ores.DEFAULT_RADIUS
    ore = params.get("ore")
    hits = mc_ores.find(state, ore, radius)
    listed = [{"name": h.name, "position": list(h.position),
               "distance": h.distance, "depth": h.depth,
               "exposed": h.exposed, "fluid_near": h.fluid_near}
              for h in hits[:10]]
    return (f"{mc_ores.describe(state, hits, ore, radius)}\n"
            f"Tell the user only what this lists. Do not promise ore it "
            f"does not list.\n{ {'ores': listed} }")


def _mine_ore_plan(state, params: dict) -> str:
    """"Plan: ..." -- the ore mine_ore will go for, how far and how deep,
    and how much it will dig -- or why there is none. The choice is the
    one the task makes (minecraft/ores.py choose), from this reading."""
    problem = mc_ores.refusal(state)
    if problem:
        return problem
    ore = params.get("ore")
    under_way = mc_dig.mine_under_way(ore)
    if under_way:
        return f"Plan: carry on with {under_way}."
    try:
        radius = int(params.get("radius", mc_ores.DEFAULT_RADIUS))
    except (TypeError, ValueError):
        radius = mc_ores.DEFAULT_RADIUS
    choice, passed = mc_ores.choose(
        state, ore, radius, within_reach=mc_aiming.SHARED.within_reach)
    if choice is None:
        return (f"{mc_ores.none_chosen(state, ore, radius, passed)} "
                f"Nothing was started.")
    skipped = (f" Passed over: {'; '.join(passed[:3])}." if passed else "")
    return f"Plan: {choice.describe()}.{skipped}"


def _source_label(source) -> str:
    """Which reader answered, in the terms that matter to a person.

    Worth one line per task: almost every "why did it do that" question turns
    on whether it could see the world or only the crosshair, and the two look
    identical in a list of actions."""
    name = type(source).__name__
    if name.startswith("ModBridge"):
        try:
            notice = source.outdated_notice() if source.outdated() else ""
        except Exception:
            notice = ""
        if notice:
            return (f"the bridge mod — but an OLDER version. {notice} If "
                    f"this line is still here after that, "
                    f"py tools\\bridge_check.py says why")
        return "the bridge mod — exact, including the terrain around you"
    if name.startswith("DebugOverlay"):
        return ("the F3 overlay via OCR — position and the block under the "
                "crosshair only, NO terrain, so it cannot navigate")
    return "nothing that can read the game"


def _block_from(params: dict):
    """(x, y, z) of one block from the model's parameters, or a sentence
    saying what is missing. Never invented: aiming at a coordinate this code
    made up would turn the camera towards nothing in particular."""
    try:
        return (int(params["x"]), int(params["y"]), int(params["z"]))
    except (KeyError, TypeError, ValueError):
        return ("aim_at_block, mine_block, place_block_at and build_line need "
                "x, y and z as whole numbers — a block that look_around or "
                "read_state reported.")


def _destination_from(params: dict):
    """(x, z) from the model's parameters, or a sentence saying what is wrong.

    Coordinates are taken literally and never invented: "somewhere over
    there" is not a destination, and guessing one would send a real player's
    character walking off on a number this code made up."""
    if "x" not in params and "z" not in params:
        return None
    try:
        return (int(params["x"]), int(params["z"]))
    except (KeyError, TypeError, ValueError):
        return ("navigate_to needs both x and z as whole numbers. Call "
                "look_around to see coordinates I can actually reach.")


def _around_line(state, summary: dict) -> str:
    """One sentence about the surroundings, before the data.

    Says UNKNOWN when the scan is not there, rather than describing an empty
    world — an empty scan and a bare plain look identical in the numbers and
    are not the same thing at all."""
    if not summary.get("columns_seen"):
        return ("I cannot see the world around me. That needs the bridge mod "
                "running in Minecraft — without it I only know what is under "
                "the crosshair.")
    bits = [f"I can see {summary['columns_seen']} columns of ground within "
            f"{summary.get('scan_radius', '?')} blocks"]
    trees = summary.get("trees") or []
    if trees:
        # Trees as well as the nearest log: the log is often a branch five
        # blocks up and says nothing about how many trees there are, but its
        # exact coordinates are what aim_at_block and mine_block need.
        tree = trees[0]
        bits.append(f"{summary.get('trees_seen', len(trees))} tree(s) in "
                    f"view; nearest: the {tree['kind']} tree at "
                    f"({tree['trunk'][0]}, {tree['trunk'][1]}), "
                    f"{tree['distance']} away, {tree['logs_seen']} log(s) "
                    f"visible")
    for label in ("log", "stone", "water"):
        found = summary.get(f"nearest_{label}")
        if found:
            bits.append(f"nearest {label}: {found['name']} at "
                        f"{tuple(found['position'])}, {found['distance']} away")
    for label in ("hostile", "passive"):
        found = summary.get(f"nearest_{label}")
        if found:
            bits.append(f"nearest {label} mob: {found['name']}, "
                        f"{found['distance']} away")
    if summary.get("ores_seen"):
        bits.append(f"ores in view: {', '.join(summary['ores_seen'])}")
    bits.extend(_player_bits(state))
    return ". ".join(bits) + "."


def _player_bits(state) -> list:
    """Health, hunger, food to hand and whether it is night -- each only
    when the bridge actually reported it."""
    bits = []
    health, hunger = state.health, state.hunger
    if isinstance(health, (int, float)):
        bits.append(f"health {health:.0f}/20")
    if isinstance(hunger, (int, float)):
        food = [f"{' '.join(str(s.name).split('_'))} x{s.count}"
                for s in (state.inventory or ())
                if s.slot is not None and 0 <= s.slot <= 8
                and s.name in mc_skills.FOODS]
        text = f"hunger {hunger:.0f}/20"
        if food:
            text += f"; food on the hotbar: {', '.join(food)}"
        elif hunger < 14:
            text += "; no food on the hotbar"
        bits.append(text)
    ticks = state.time_of_day
    # Only the overworld has a night; the clock runs on elsewhere.
    if isinstance(ticks, int) and mc_danger.has_night(state):
        if 13000 <= ticks < 23000:
            bits.append("it is night — hostile mobs spawn in the dark")
        elif 12000 <= ticks < 13000:
            bits.append("it is getting dark")
    return bits


def _status_line(status: dict) -> str:
    if not status.get("minecraft_running"):
        return status.get("process_detail") or "Minecraft is not running."
    window = status.get("window") or {}
    if not window.get("found"):
        return "Minecraft is running but I cannot find its window."
    if status.get("session_active"):
        session = status.get("session") or {}
        remaining = session.get("remaining_seconds")
        # An unlimited session has no remaining time -- None, not zero -- and
        # formatting None as a number is what made "status" fail outright.
        if session.get("unlimited") or not isinstance(remaining, (int, float)):
            return "Controlling Minecraft — the session has no time limit."
        return (f"Controlling Minecraft — {remaining:.0f}s "
                f"left on the session.")
    focus = "in front" if window.get("foreground") else "not in front"
    return (f"Minecraft is running and its window is {focus}. "
            f"No control session is open.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "minecraft_control",
    "description": TOOL_DESCRIPTION,
    "parameters": TOOL_PARAMETERS,
    "handler": minecraft_control,
    "capability": _mc_capability,
    "guard": _mc_guard,
}
