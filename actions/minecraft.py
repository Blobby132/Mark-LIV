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
from core import interrupts
from core import ocr as core_ocr

from minecraft import capabilities as mc_phase
from minecraft import navigation as mc_nav
from minecraft import perception as mc_perception
from minecraft import skills as mc_skills
from minecraft.controller import MinecraftController
from minecraft.debug_overlay import DebugOverlayStateSource, NEEDS_MOD_BRIDGE
from minecraft.mod_bridge import ModBridgeStateSource
from minecraft.errors import (
    CapabilityDisabled, InvalidAction, MinecraftError, TaskAlreadyRunning,
)
from minecraft.observation import Observer
from minecraft.state import VisionStateSource
from minecraft.session import (
    DEFAULT_SESSION_SECONDS, GRANT_SUMMARY, MAX_SESSION_SECONDS,
    MIN_SESSION_SECONDS, UNLIMITED,
)
from minecraft.task_runner import MAX_TASK_SECONDS, MAX_TASK_STEPS, TaskRunner
from minecraft.task_slot import TaskSlot

# One controller for the process. Minecraft is one game with one window and one
# session, and a second controller would mean a second ledger — two records of
# which keys are held, neither of them complete.
_controller: MinecraftController | None = None
_observer: Observer | None = None
_state_source = None
_state_source_pinned = False

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
    global _state_source
    if _state_source_pinned:
        return _state_source

    bridge = ModBridgeStateSource()
    if bridge.available():
        # Reuse the existing instance when it is already the bridge, so
        # nothing it has cached internally is thrown away each call.
        if not isinstance(_state_source, ModBridgeStateSource):
            _state_source = bridge
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
    _controller = controller
    _observer = observer
    _state_source = state_source
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
                      session_memory=None, response=None, speak=None) -> str:
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
                "count", "slot", "target"):
        if key in params:
            options[key] = params[key]
    if name in ("aim_at_block", "mine_block"):
        block = _block_from(params)
        if isinstance(block, str):
            return block
        options.update(x=block[0], y=block[1], z=block[2])
        options.pop("target", None)
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
    return (f"Started {name} — {job.goal}. It is running now, in the "
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


def _source_label(source) -> str:
    """Which reader answered, in the terms that matter to a person.

    Worth one line per task: almost every "why did it do that" question turns
    on whether it could see the world or only the crosshair, and the two look
    identical in a list of actions."""
    name = type(source).__name__
    if name.startswith("ModBridge"):
        try:
            outdated = bool(source.outdated())
        except Exception:
            outdated = False
        if outdated:
            return ("the bridge mod — but an OLDER version, which cannot see "
                    "the ground under trees, so it may find no way to one. "
                    "Quit Minecraft, run install_mod.bat, then start "
                    "Minecraft again; if this line is still here after "
                    "that, py tools\\bridge_check.py says why")
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
        return ("aim_at_block and mine_block need x, y and z as whole numbers "
                "— a block that look_around or read_state reported.")


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
    return ". ".join(bits) + "."


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
        "look_around, task_status. With the bridge mod, read_state and "
        "look_around read the game's own data: position, rotation, health, "
        "hunger, the inventory, the held item, the block under the crosshair "
        "(name, x, y, z, face) and the terrain around the player. Without "
        "it, read_state falls back to the F3 overlay (toggle_debug presses "
        "F3, and like every key press needs a session).\n"
        "look_around is the one to use for 'what is around me', 'is there a "
        "tree nearby', 'any mobs'. It returns the nearest log, stone, water, "
        "ores and mobs with coordinates, from the bridge mod's terrain scan. "
        "If it says it cannot see the world, say that — do not describe a "
        "world from the crosshair or from memory.\n"
        "GAMEPLAY: move (direction, duration up to 2s), move_and_jump (walks "
        "and jumps together, up to 1s — the way onto a one-block ledge), "
        "look (dx/dy in PIXELS, up to 400 each — not degrees; to point at a "
        "particular block use run_task aim_at_block instead), jump, sneak "
        "and sprint (up to 2s), attack (up to 2s), mine (one continuous hold "
        "of up to 10s that lets go when the bridge sees the block change), "
        "place, interact (up to 1s), use_item and eat (up to 2s), drop (one "
        "item), hotbar_select (slot 1-9), inventory (state=open|close), "
        "stop. Longer durations are shortened to the limit, not refused.\n"
        "TASKS: run_task does a bounded multi-step job, observing and "
        "verifying between steps: walk_forward, survey, find_block, "
        "break_block, place_block, collect_logs, fell_tree, navigate_to, "
        "aim_at_block, "
        "mine_block. A task RUNS IN THE BACKGROUND: run_task answers "
        "'started' at once, and when the task ends a message beginning "
        "[Minecraft task] reports what actually happened — relay that, and "
        "never say a task worked before it arrives. A task stops itself "
        "after 45 steps, after two minutes, or when it detects it is making "
        "no progress. Only one runs at a time, and while it runs the other "
        "gameplay actions are refused; task_status says how it is going.\n"
        "STOPPING: if the user says stop, halt or cancel while a task or "
        "action is running, it is stopped at once and every key released "
        "(the control session stays open). cancel_task does the same. stop "
        "(the action) also ENDS the control session; F12 is the hard stop "
        "and ends it too.\n"
        "aim_at_block and mine_block take x, y and z — a block look_around "
        "or read_state actually reported. They turn until the game itself "
        "confirms the crosshair is on that exact block; mine_block then "
        "breaks it and checks that the block at that coordinate is gone. "
        "Neither mines anything if the aim cannot be confirmed, and neither "
        "walks: if the block is out of reach, navigate_to first.\n"
        "navigate_to walks somewhere, routing round obstacles: give it "
        "either x and z, or target='log'|'stone'|'water'. It refuses a "
        "destination outside the scanned area instead of setting off "
        "hopefully — if it says it cannot see that far, relay that rather "
        "than retrying with a bigger number. If it says it stopped N blocks "
        "short because the task ran out of steps, call it again with the "
        "same destination: it carries on from where it is.\n"
        "collect_logs walks to the nearest tree it can reach, breaks logs "
        "until it has the count, and walks over the drops to pick them up. "
        "It finishes one tree before starting another. fell_tree takes "
        "every log it can reach from ONE tree — the one in front, else the "
        "nearest — picks them up and stops: use it for 'chop down the "
        "tree', 'mine the tree' or 'the rest of the tree', not collect_logs "
        "with a guessed count. Both break leaves in the way first — a few "
        "at most, never counted as logs. Their reports name the tree each "
        "log came from and any logs left too high to reach: answer 'which "
        "tree' or 'why that tree' from that report, and if it does not say, "
        "say you do not know rather than guess. break_block only breaks "
        "whatever the crosshair is on right now — it does not aim or walk — "
        "so for 'break a log' use collect_logs.\n"
        "REPORTING RESULTS HONESTLY — this matters most:\n"
        "  * Holding attack is not breaking a block. Never say a block broke, "
        "a tree was chopped or wood was collected unless "
        "verification.status == 'success' or the task report says so.\n"
        "  * 'unverifiable' means I could not SEE whether it worked. Say "
        "that plainly; do not treat it as success or as failure.\n"
        "  * Whether I can confirm an item was PICKED UP depends on the "
        "bridge mod. With it, the inventory is readable and 'collected' is a "
        "real claim. Without it I only see a block disappear — then say "
        "'broke', not 'collected'. The task result says which one it used; "
        "do not upgrade 'broke' to 'collected'.\n"
        "  * An unknown block is not air, and a place I have not scanned is "
        "not empty ground. If something says UNKNOWN, report UNKNOWN.\n"
        "  * ok == true only means the input reached the game.\n"
        "Input only goes to Minecraft while it is the window in front: if "
        "the user alt-tabs away, anything held is released within one tick "
        "and a running task stops."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "status | observe | read_state | look_around | "
                    "task_status | toggle_debug | "
                    "start_session | end_session | move | move_and_jump | "
                    "look | jump | "
                    "sneak | sprint | attack | mine | place | interact | "
                    "use_item | eat | drop | hotbar_select | inventory | "
                    "run_task | cancel_task | stop"),
            },
            "direction": {
                "type": "STRING",
                "description": ("For move/move_and_jump/sneak/sprint: "
                                "forward | back | left | right."),
            },
            "duration": {
                "type": "NUMBER",
                "description": ("Seconds to hold. Limits: move, sneak, "
                                "sprint, attack, use_item, eat 2.0; mine "
                                "10.0; move_and_jump and interact 1.0. "
                                "Longer requests are shortened to the "
                                "limit, not refused. place, drop, jump, "
                                "hotbar_select and inventory are taps and "
                                "take no duration."),
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
                                "collect_logs | fell_tree | navigate_to | "
                                "aim_at_block | mine_block."),
            },
            "x": {
                "type": "INTEGER",
                "description": ("For run_task navigate_to, aim_at_block and "
                                "mine_block: the block's x. Use a coordinate "
                                "look_around or read_state actually "
                                "reported; do not invent one."),
            },
            "y": {
                "type": "INTEGER",
                "description": ("For run_task aim_at_block and mine_block: "
                                "the block's y."),
            },
            "z": {
                "type": "INTEGER",
                "description": ("For run_task navigate_to, aim_at_block and "
                                "mine_block: the block's z. Needs x as "
                                "well."),
            },
            "target": {
                "type": "STRING",
                "description": ("For run_task navigate_to: walk to the "
                                "nearest one of these instead of a "
                                "coordinate — log, stone, dirt, grass, sand, "
                                "water, crafting_table, furnace, chest, or "
                                "an exact block name."),
            },
            "count": {
                "type": "INTEGER",
                "description": ("For collect_logs: how many logs to get. With "
                                "the bridge mod they are counted in the "
                                "inventory (collected); without it I can "
                                "only report blocks that disappeared "
                                "(broke)."),
            },
            "seconds": {
                "type": "NUMBER",
                "description": "For the walk_forward task: how long to walk.",
            },
            "expected": {
                "type": "STRING",
                "description": (
                    "For break_block, aim_at_block and mine_block: the block "
                    "name that must be there, e.g. 'oak_log'. If something "
                    "else is, the task stops rather than break the wrong "
                    "thing."),
            },
            "max_steps": {
                "type": "INTEGER",
                "description": ("For run_task: fewer steps than the limit of "
                                "45. It cannot be raised above 45."),
            },
        },
        "required": ["action"],
    },
    "handler": minecraft_control,
    "capability": _mc_capability,
    "guard": _mc_guard,
}
