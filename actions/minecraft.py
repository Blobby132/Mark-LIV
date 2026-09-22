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
    module. `tests/test_minecraft_boundary.py` fails the build if that changes.
"""

from __future__ import annotations

from core import capabilities
from core import ocr as core_ocr

from minecraft import capabilities as mc_phase
from minecraft import skills as mc_skills
from minecraft.controller import MinecraftController
from minecraft.debug_overlay import DebugOverlayStateSource, NEEDS_MOD_BRIDGE
from minecraft.mod_bridge import ModBridgeStateSource
from minecraft.errors import CapabilityDisabled, InvalidAction, MinecraftError
from minecraft.observation import Observer
from minecraft.state import VisionStateSource
from minecraft.session import (
    DEFAULT_SESSION_SECONDS, GRANT_SUMMARY, MAX_SESSION_SECONDS,
    MIN_SESSION_SECONDS, UNLIMITED,
)
from minecraft.task_runner import MAX_TASK_STEPS, TaskRunner

# One controller for the process. Minecraft is one game with one window and one
# session, and a second controller would mean a second ledger — two records of
# which keys are held, neither of them complete.
_controller: MinecraftController | None = None
_observer: Observer | None = None
_state_source = None


def _target_probe():
    """What the crosshair is on, for the mining loop to watch.

    Returns None when nothing can read it, which the controller treats as "no
    probe" and falls back to a plain timed hold. Deliberately cheap: it runs
    every 40ms while a block is being broken."""
    try:
        block = _get_state_source().read().target_block
    except Exception:
        return None
    if block is None:
        return None
    return (block.name, block.x, block.y, block.z)


def _get_controller() -> MinecraftController:
    global _controller, _observer
    if _controller is None:
        _controller = MinecraftController(progress_probe=_target_probe)
        _observer = Observer(_controller._locator)
    return _controller


def _get_observer() -> Observer:
    _get_controller()
    return _observer


def _get_state_source():
    """The best state source available RIGHT NOW.

    Three implementations, in order of how much they can actually tell us:

        mod bridge  — the game's own numbers. Everything, `exact`.
        F3 overlay  — OCR of the debug screen. Position and the targeted
                      block, `inferred`, and nothing about the inventory.
        vision      — an honest empty state. Reads nothing.

    Re-resolved on every call rather than cached, because availability
    genuinely changes: someone starts JARVIS, then starts Minecraft, then
    loads a world, and each step makes a better source possible. Caching the
    first answer meant the common order of events — assistant first, game
    second — permanently pinned the worst source.

    Everything above this function is written against `StateSource` and never
    learns which one it got; that seam is why the bridge could be added
    without touching the planner, the task runner or verification."""
    global _state_source
    if _state_source is not None and getattr(_state_source, "available",
                                             lambda: True)():
        return _state_source

    bridge = ModBridgeStateSource()
    if bridge.available():
        _state_source = bridge
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
    global _controller, _observer, _state_source
    _controller = controller
    _observer = observer
    _state_source = state_source


# ── Capability resolution ────────────────────────────────────────────────────

_CAPABILITY_BY_ACTION = {
    # Reading. No session needed.
    "status":        capabilities.MINECRAFT_OBSERVE,
    "observe":       capabilities.MINECRAFT_OBSERVE,
    "read_state":    capabilities.MINECRAFT_READ_STATE,
    "toggle_debug":  capabilities.MINECRAFT_READ_STATE,

    # THE confirmation, and the way out of it.
    "start_session": capabilities.MINECRAFT_CONTROL,
    "end_session":   capabilities.MINECRAFT_STOP,
    "stop":          capabilities.MINECRAFT_STOP,

    # Gameplay. All covered by the one session grant.
    "move":          capabilities.MINECRAFT_MOVEMENT,
    "jump":          capabilities.MINECRAFT_MOVEMENT,
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
    "run_task":      capabilities.MINECRAFT_TASK,

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
            f"anything outside Minecraft."
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
            player.write_log(f"[minecraft] {result.action}: ok "
                             f"({result.actual_duration_ms}ms)")
        else:
            player.write_log(f"[minecraft] {result.action} REFUSED: "
                             f"{result.stopped_reason or result.error_class}")
            if result.error:
                player.write_log(f"[minecraft] {result.error}")
    return f"{result.describe()}\n{result.as_dict()}"


# ── The handler ──────────────────────────────────────────────────────────────

def minecraft_control(parameters: dict = None, player=None,
                      session_memory=None, response=None) -> str:
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

        if action == "status":
            status = controller.status()
            return (f"{_status_line(status)}\n{status}")

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

        if action == "run_task":
            return _run_task(controller, params, player)

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


def _run_task(controller, params: dict, player=None) -> str:
    """Run one bounded skill to completion, or to its limit.

    The model chooses WHICH skill and with what parameters. It does not get to
    describe the steps: a skill is ordinary Python, so the sequence of actions
    is decided in source and the runner validates every one of them again
    against `action_spec` before it reaches the controller."""
    name = str(params.get("task", "")).strip().lower()
    if not name:
        return (f"Which task? I have: {', '.join(mc_skills.available())}. "
                f"Some things I cannot do yet: "
                f"{', '.join(sorted(mc_skills.NOT_YET_POSSIBLE))}.")

    options = {}
    for key in ("seconds", "direction", "expected", "swings", "steps",
                "count", "slot"):
        if key in params:
            options[key] = params[key]

    try:
        skill = mc_skills.create(name, **options)
    except KeyError as e:
        return str(e).strip('"')
    except TypeError as e:
        return (f"'{name}' does not take those options ({e}). "
                f"Try it without them.")

    runner = TaskRunner(controller, _get_state_source(),
                        observer=_get_observer())
    result = runner.run(skill, max_steps=params.get("max_steps",
                                                    MAX_TASK_STEPS))
    if player:
        player.write_log(f"[minecraft] task {name}: {result.status} "
                         f"({result.steps_taken} steps)")

    # The summary line is built to be un-overstatable: it says what was
    # verified, separately from what was attempted.
    return f"{result.describe()}\n{result.as_dict()}"


def _status_line(status: dict) -> str:
    if not status.get("minecraft_running"):
        return status.get("process_detail") or "Minecraft is not running."
    window = status.get("window") or {}
    if not window.get("found"):
        return "Minecraft is running but I cannot find its window."
    if status.get("session_active"):
        session = status.get("session") or {}
        return (f"Controlling Minecraft — {session.get('remaining_seconds', 0):.0f}s "
                f"left on the session.")
    focus = "in front" if window.get("foreground") else "not in front"
    return (f"Minecraft is running and its window is {focus}. "
            f"No control session is open.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "minecraft_control",
    "description": (
        "Plays Minecraft Java Edition. Use for any request about looking at "
        "or playing Minecraft.\n"
        "ONE CONFIRMATION: call start_session once, with no duration_s. The "
        "user approves a single banner and that covers ALL gameplay until "
        "they stop it — "
        "movement, looking, jumping, sprinting, sneaking, attacking, mining, "
        "placing, items, the hotbar, the inventory and interaction. Do NOT "
        "ask them to confirm individual actions, and do NOT call "
        "start_session again while one is open; if you are unsure, call "
        "status.\n"
        "READING (no session needed): status, observe, read_state, "
        "toggle_debug (presses F3, which read_state needs).\n"
        "GAMEPLAY: move (direction, duration<=2s), look (dx/dy in PIXELS, "
        "<=400 each — degrees are NOT supported), jump, sneak, sprint, "
        "attack, mine, place, interact, use_item, eat, drop, hotbar_select "
        "(slot 1-9), inventory (state=open|close), stop.\n"
        "TASKS: run_task does a bounded multi-step job, observing and "
        "verifying between steps: walk_forward, survey, find_block, "
        "break_block, place_block, collect_logs. It stops itself at 20 "
        "steps, after two minutes, or when it detects it is making no "
        "progress.\n"
        "REPORTING RESULTS HONESTLY — this matters most:\n"
        "  * One mine does NOT break a block. Breaking an oak log takes "
        "several. Never say a block broke, a tree was chopped or wood was "
        "collected unless verification.status == 'success'.\n"
        "  * 'unverifiable' means I could not SEE whether it worked. Say "
        "that plainly; do not treat it as success or as failure.\n"
        "  * I cannot read the inventory at all, so I can never confirm an "
        "item was picked up — only that a block disappeared. Say 'broke' not "
        "'collected'.\n"
        "  * ok == true only means the input reached the game.\n"
        "Movement only works while Minecraft is the window in front. If the "
        "user alt-tabs or presses F12 everything stops and the session ends."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | observe | read_state | toggle_debug | "
                    "start_session | end_session | move | look | jump | "
                    "sneak | sprint | attack | mine | place | interact | "
                    "use_item | eat | drop | hotbar_select | inventory | "
                    "run_task | stop"),
            },
            "direction": {
                "type": "STRING",
                "description": ("For move/sneak/sprint: forward | back | "
                                "left | right."),
            },
            "duration": {
                "type": "NUMBER",
                "description": ("Seconds to hold, up to 2.0 for every action "
                                "that takes one. Longer requests are "
                                "shortened to 2.0, not refused."),
            },
            "dx": {
                "type": "INTEGER",
                "description": ("For look: horizontal mouse movement in "
                                "pixels, -400 to 400. Positive turns right."),
            },
            "dy": {
                "type": "INTEGER",
                "description": ("For look: vertical mouse movement in pixels, "
                                "-400 to 400. Positive looks down."),
            },
            "slot": {
                "type": "INTEGER",
                "description": ("For hotbar_select: which slot, 1 to 9. Out "
                                "of range is refused, not adjusted."),
            },
            "duration_s": {
                "type": "NUMBER",
                "description": ("For start_session. LEAVE THIS OUT unless the "
                                "user asks for a time limit — the session "
                                "then runs until they stop it, which is what "
                                "they usually want. A number gives a timed "
                                "session, up to 300 seconds."),
            },
            "state": {
                "type": "STRING",
                "description": "For inventory: open | close.",
            },
            "task": {
                "type": "STRING",
                "description": ("For run_task: walk_forward | survey | "
                                "find_block | break_block | place_block | "
                                "collect_logs."),
            },
            "count": {
                "type": "INTEGER",
                "description": ("For collect_logs: how many logs to break. "
                                "Note I cannot read the inventory, so I "
                                "report blocks broken, not items collected."),
            },
            "seconds": {
                "type": "NUMBER",
                "description": "For the walk_forward task: how long to walk.",
            },
            "expected": {
                "type": "STRING",
                "description": (
                    "For the break_block task: the block name expected under "
                    "the crosshair, e.g. 'oak_log'. If something else is "
                    "there the task stops rather than break the wrong thing."),
            },
            "max_steps": {
                "type": "INTEGER",
                "description": ("For run_task: fewer steps than the limit of "
                                "20. It cannot be raised above 20."),
            },
        },
        "required": ["action"],
    },
    "handler": minecraft_control,
    "capability": _mc_capability,
    "guard": _mc_guard,
}
