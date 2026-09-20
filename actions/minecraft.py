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

from minecraft import capabilities as mc_phase
from minecraft.controller import MinecraftController
from minecraft.errors import CapabilityDisabled, InvalidAction, MinecraftError
from minecraft.observation import Observer
from minecraft.state import VisionStateSource
from minecraft.session import (
    DEFAULT_SESSION_SECONDS, MAX_SESSION_SECONDS, MIN_SESSION_SECONDS,
)

# One controller for the process. Minecraft is one game with one window and one
# session, and a second controller would mean a second ledger — two records of
# which keys are held, neither of them complete.
_controller: MinecraftController | None = None
_observer: Observer | None = None
_state_source = VisionStateSource()


def _get_controller() -> MinecraftController:
    global _controller, _observer
    if _controller is None:
        _controller = MinecraftController()
        _observer = Observer(_controller._locator)
    return _controller


def _get_observer() -> Observer:
    _get_controller()
    return _observer


def _reset_for_tests(controller=None, observer=None) -> None:
    """Swap in fakes. Only the tests call this; it exists so they can exercise
    the real adapter rather than a copy of its logic."""
    global _controller, _observer
    _controller = controller
    _observer = observer


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
    "jump":          capabilities.MINECRAFT_MOVE,
    # Named so they resolve to their real capability and are refused by the
    # phase gate with an explanation, rather than falling through to the
    # unknown-action branch and getting a vaguer answer.
    "attack":        capabilities.MINECRAFT_ATTACK,
    "mine":          capabilities.MINECRAFT_ATTACK,
    "use_item":      capabilities.MINECRAFT_USE_ITEM,
    "place":         capabilities.MINECRAFT_USE_ITEM,
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

    return {
        "summary": f"Let me control Minecraft for {int(seconds)} seconds",
        "detail": (
            "I can walk, turn and jump inside the Minecraft window only, and "
            "only while it is the window in front. Alt-Tab or F12 stops me "
            "immediately. I cannot type in chat, run commands, or touch "
            "anything outside the game."
        ),
    }


# ── Result shaping ───────────────────────────────────────────────────────────

def _result_line(result) -> str:
    """One sentence for the model, plus the structured detail it needs.

    The sentence never claims more than happened: a move cut short by focus
    loss reads as stopped, not done."""
    payload = result.as_dict()
    line = result.describe()
    if result.ok:
        return f"{line}\n{payload}"
    return f"{line}\n{payload}"


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
            state = _state_source.read()
            return f"{state.describe()}\n{state.as_dict()}"

        if action == "start_session":
            info = controller.start_session(
                duration_s=params.get("duration_s"),
                owner=str(params.get("owner", "user"))[:40],
            )
            if player:
                player.write_log("[minecraft] control session started")
            warning = ""
            if not info.get("input_available"):
                warning = ("\nNOTE: I cannot actually send input on this "
                           "machine — " + info.get("input_backend", ""))
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
            return _result_line(controller.move(params))

        if action == "jump":
            return _result_line(controller.jump(params))

        if action == "look":
            return _result_line(controller.look(params))

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
        # Anything unexpected still releases held input before reporting.
        try:
            _get_controller().stop(f"unexpected error: {type(e).__name__}")
        except Exception:
            pass
        return (f"Minecraft control failed ({type(e).__name__}: {e}). "
                f"I released any keys I was holding.")


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
        "Observes and controls Minecraft Java Edition. Use for any request "
        "about looking at or playing Minecraft. Actions: status (is it "
        "running, is the window in front), observe (capture the Minecraft "
        "window), read_state (what is known about the game — currently very "
        "little), start_session (asks the user for permission to control the "
        "game for up to 300 seconds; movement is refused until they confirm), "
        "move (direction=forward|back|left|right, duration up to 2 seconds), "
        "look (dx, dy in PIXELS of mouse movement, up to 400 each — degrees "
        "are NOT supported), jump, stop (release everything immediately). "
        "Movement only works while Minecraft is the window in front; if the "
        "user switches away it stops by itself. Never claim an action "
        "succeeded unless the result says ok=true — a move can be cut short "
        "and will report how long it actually lasted."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("status | observe | read_state | start_session "
                                "| end_session | move | look | jump | stop"),
            },
            "direction": {
                "type": "STRING",
                "description": "For move: forward | back | left | right",
            },
            "duration": {
                "type": "NUMBER",
                "description": ("For move: seconds to hold, up to 2.0. Longer "
                                "requests are shortened to 2.0, not refused."),
            },
            "dx": {
                "type": "INTEGER",
                "description": ("For look: horizontal mouse movement in pixels, "
                                "-400 to 400. Positive turns right."),
            },
            "dy": {
                "type": "INTEGER",
                "description": ("For look: vertical mouse movement in pixels, "
                                "-400 to 400. Positive looks down."),
            },
            "duration_s": {
                "type": "NUMBER",
                "description": ("For start_session: how many seconds of control "
                                "to ask for, up to 300."),
            },
        },
        "required": ["action"],
    },
    "handler": minecraft_control,
    "capability": _mc_capability,
    "guard": _mc_guard,
}
