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
from minecraft.errors import CapabilityDisabled, InvalidAction, MinecraftError
from minecraft.observation import Observer
from minecraft.state import VisionStateSource
from minecraft.session import (
    DEFAULT_SESSION_SECONDS, MAX_SESSION_SECONDS, MIN_SESSION_SECONDS,
)
from minecraft.task_runner import MAX_TASK_STEPS, TaskRunner

# One controller for the process. Minecraft is one game with one window and one
# session, and a second controller would mean a second ledger — two records of
# which keys are held, neither of them complete.
_controller: MinecraftController | None = None
_observer: Observer | None = None
_state_source = None


def _get_controller() -> MinecraftController:
    global _controller, _observer
    if _controller is None:
        _controller = MinecraftController()
        _observer = Observer(_controller._locator)
    return _controller


def _get_observer() -> Observer:
    _get_controller()
    return _observer


def _get_state_source():
    """The best state source available.

    This is the one place that decides which source the rest of the system
    talks to, and everything above it — the task runner, verification, the
    handler — is written against the `StateSource` interface and never learns
    which it got. Adding the Fabric bridge later is a change to this function.

    The OCR reader is built HERE, from core, and injected. `minecraft/` imports
    no OCR engine: pytesseract shells out to a binary, and that package is
    forbidden from starting processes — see core/ocr.py."""
    global _state_source
    if _state_source is None:
        overlay = DebugOverlayStateSource(observer=_get_observer(),
                                          reader=core_ocr.create_reader())
        # A source that cannot read is worse than the honest empty one: it
        # would report "overlay closed" when the real problem is a missing
        # OCR install, and the user would go and press F3 for nothing.
        _state_source = overlay if overlay.available() else VisionStateSource()
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
    "status":        capabilities.MINECRAFT_OBSERVE,
    "observe":       capabilities.MINECRAFT_OBSERVE,
    "read_state":    capabilities.MINECRAFT_READ_STATE,
    "start_session": capabilities.MINECRAFT_CONTROL_SESSION,
    "end_session":   capabilities.MINECRAFT_STOP,
    "stop":          capabilities.MINECRAFT_STOP,
    "move":          capabilities.MINECRAFT_MOVE,
    "look":          capabilities.MINECRAFT_LOOK,
    "jump":          capabilities.MINECRAFT_JUMP,
    "attack":        capabilities.MINECRAFT_ATTACK,
    "mine":          capabilities.MINECRAFT_ATTACK,
    "use_item":      capabilities.MINECRAFT_USE_ITEM,
    "place":         capabilities.MINECRAFT_USE_ITEM,
    "hotbar_select": capabilities.MINECRAFT_HOTBAR,
    "sneak":         capabilities.MINECRAFT_SNEAK,
    "sprint":        capabilities.MINECRAFT_SPRINT,
    "toggle_debug":  capabilities.MINECRAFT_READ_STATE,
    "run_task":      capabilities.MINECRAFT_TASK,
    # Named so they resolve to their real capability and are refused by the
    # phase gate with an explanation, rather than falling through to the
    # unknown-action branch and getting a vaguer answer.
    "inventory":     capabilities.MINECRAFT_INVENTORY,
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
    """What the confirmation banner says. Only `start_session` shows one."""
    action = str((params or {}).get("action", "")).lower().strip()
    if action != "start_session":
        return {"summary": f"Minecraft: {action or 'unknown'}"}

    requested = (params or {}).get("duration_s", DEFAULT_SESSION_SECONDS)
    try:
        seconds = max(MIN_SESSION_SECONDS,
                      min(float(requested), MAX_SESSION_SECONDS))
    except (TypeError, ValueError):
        seconds = DEFAULT_SESSION_SECONDS

    interact = bool((params or {}).get("allow_interaction"))
    summary = (f"Let me control Minecraft for {int(seconds)} seconds"
               + (" — INCLUDING breaking and placing blocks" if interact
                  else ""))

    detail = (
        "I can walk, turn and jump inside the Minecraft window only, and "
        "only while it is the window in front. Alt-Tab or F12 stops me "
        "immediately. I cannot type in chat, run commands, or touch "
        "anything outside the game."
    )
    if interact:
        # Spelled out rather than implied: this is the grant that replaces a
        # confirmation per swing, so it has to actually inform.
        detail += (
            "\n\nThis session ALSO lets me mine blocks and use/place items. "
            "That changes your world and I cannot undo it — use a creative or "
            "throwaway world if you are not sure."
        )
    else:
        detail += ("\n\nThis session does NOT let me break or place "
                   "anything.")

    return {"summary": summary, "detail": detail}


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
            info = controller.start_session(
                duration_s=params.get("duration_s"),
                owner=str(params.get("owner", "user"))[:40],
                allow_interaction=bool(params.get("allow_interaction")),
            )
            if player:
                player.write_log("[minecraft] control session started")
            warning = ""
            if not info.get("input_available"):
                warning = ("\nNOTE: I cannot actually send input on this "
                           "machine — " + info.get("input_backend", ""))
            if info.get("reused_existing"):
                return (f"I already have control — "
                        f"{info['session']['remaining_seconds']:.0f}s left on "
                        f"the session that is already open. No need to start "
                        f"another; just tell me what to do.{warning}\n{info}")
            return (f"Minecraft control session open for "
                    f"{info['session']['granted_seconds']:.0f}s. "
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

        if action in ("attack", "mine"):
            return _result_line(controller.attack(params), player)

        if action in ("use_item", "place"):
            return _result_line(controller.use_item(params), player)

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
    for key in ("seconds", "direction", "expected", "swings", "steps"):
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
        "Observes, understands and controls Minecraft Java Edition. Use for "
        "any request about looking at or playing Minecraft.\n"
        "READING: status (is it running, is it in front), observe (capture "
        "the window), read_state (position, facing, biome, and the block "
        "under the crosshair — needs the F3 overlay open), toggle_debug "
        "(press F3 to open/close that overlay).\n"
        "CONTROL: start_session asks the user for permission for up to 300 "
        "seconds; nothing below works until they confirm. Pass "
        "allow_interaction=true ONLY if the task needs to break or place "
        "blocks — it changes their world and is asked for separately. Then: "
        "move (direction, duration<=2s), look (dx/dy in PIXELS, <=400 each — "
        "degrees are NOT supported), jump, sneak, sprint, hotbar_select "
        "(slot 1-9), attack (duration<=2s), use_item (duration<=2s), stop.\n"
        "TASKS: run_task performs a bounded multi-step job, observing and "
        "verifying between steps. task=walk_forward|survey|find_block|"
        "break_block. It stops by itself at 20 steps.\n"
        "IMPORTANT: one attack does NOT break a block — breaking an oak log "
        "takes several. Never say a block broke, a tree was chopped or "
        "anything was collected unless a result says so: check "
        "verification.status == 'success', not just ok == true. 'unverifiable' "
        "means I could not see whether it worked — say that, do not guess. "
        "Movement only works while Minecraft is the window in front; if the "
        "user switches away it stops by itself."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | observe | read_state | toggle_debug | "
                    "start_session | end_session | move | look | jump | "
                    "sneak | sprint | hotbar_select | attack | use_item | "
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
                "description": ("For start_session: how many seconds of "
                                "control to ask for, up to 300."),
            },
            "allow_interaction": {
                "type": "BOOLEAN",
                "description": (
                    "For start_session. False by default. True also asks "
                    "permission to break and place blocks, which changes the "
                    "user's world and cannot be undone. Only ask for it when "
                    "the task actually needs it."),
            },
            "task": {
                "type": "STRING",
                "description": ("For run_task: walk_forward | survey | "
                                "find_block | break_block."),
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
