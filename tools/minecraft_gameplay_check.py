#!/usr/bin/env python3
"""
tools/minecraft_gameplay_check.py — the gameplay tests that need a real game.

WHAT THIS IS FOR
    Everything in tests/ runs against a simulated world. The simulation now
    enforces the rules that bit in real play — 0.6-block step height, a
    0.6-wide body, a crosshair that hits the first block along the view,
    mining that breaks nothing if the button comes up too soon — but it is
    still a simulation. This script is where the real game gets a vote.

    It covers what the automated tests cannot:

      A  Bridge and perception    is the mod new enough; what is under the
                                  crosshair, from the game and from pixels
      B  Aiming                   your real sensitivity; look left, right,
                                  up and down; aim at a chosen block
      C  Mining                   break one block, verify THAT coordinate
                                  changed, verify the inventory went up
      D  Movement                 hop a one-block step unprompted; go round
                                  a wall
      E  Safety                   F12, Alt-Tab, and closing the game mid-task
      F  Voice                    guided checks you run in JARVIS itself

    The older tools/minecraft_manual_check.py still covers the raw input
    basics (does a synthetic mouse delta move the camera at all). Run that
    first if nothing moves.

HOW IT BEHAVES
    Interactive, never autonomous. Each step says what it will do, waits for
    you, does ONE bounded thing, and asks what you saw. 'q' stops at any
    prompt, and every key is released on the way out however it exits.

    Needs the bridge mod running, Minecraft windowed or borderless, and a
    throwaway world — section C breaks a block and D walks your character.

    py tools\\minecraft_gameplay_check.py
    py tools\\minecraft_gameplay_check.py C      (just one section)
"""

from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import aiming                                  # noqa: E402
from minecraft import mining                                  # noqa: E402
from minecraft import navigation as nav                       # noqa: E402
from minecraft import perception                              # noqa: E402
from minecraft import skills                                  # noqa: E402
from minecraft import verification as verify                  # noqa: E402
from minecraft.controller import MinecraftController          # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource         # noqa: E402
from minecraft.observation import Observer                    # noqa: E402
from minecraft.task_runner import TaskRunner                  # noqa: E402

from tools.minecraft_manual_check import (                    # noqa: E402
    RULE, ask, countdown, ensure_session, pause,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list = []
_number = 0


# ── Reporting ────────────────────────────────────────────────────────────────

def heading(section: str, title: str) -> None:
    global _number
    _number += 1
    print(f"\n{RULE}\n  {section}{_number}: {title}\n{RULE}")


def record(name: str, outcome: str, note: str = "") -> None:
    """Three outcomes, not two. A step you chose to skip is not a failure,
    and counting it as one made the old summary read as broken when nothing
    had been tried."""
    results.append((name, outcome, note))
    print(f"\n  [{outcome}] {name}" + (f" — {note}" if note else ""))


def summarise() -> int:
    print(f"\n{RULE}\n  RESULT\n{RULE}")
    if not results:
        print("  Nothing was tested.")
        return 1
    width = max(len(n) for n, _o, _x in results)
    for name, outcome, note in results:
        print(f"  [{outcome}] {name.ljust(width)}" + (f"  {note}" if note else ""))
    failed = sum(1 for _n, o, _x in results if o == FAIL)
    passed = sum(1 for _n, o, _x in results if o == PASS)
    skipped = sum(1 for _n, o, _x in results if o == SKIP)
    print(f"\n  {passed} passed, {failed} failed, {skipped} skipped.")
    print("\n  Paste everything from 'RESULT' down back to me. The notes on the")
    print("  failures are what tell me which half of a step went wrong.")
    return 1 if failed else 0


# ── Shared ───────────────────────────────────────────────────────────────────

class Rig:
    """The controller, the bridge, and the camera, set up once."""

    def __init__(self):
        self.bridge = ModBridgeStateSource()
        self.controller = MinecraftController(start_watchers=False,
                                              progress_probe=self._probe)
        self.observer = Observer(self.controller._locator)

    def _probe(self):
        try:
            block = self.bridge.read().target_block
        except Exception:
            return None
        if block is None:
            return None
        return (block.name, block.x, block.y, block.z)

    def state(self, settle: float = 0.35):
        """A reading taken AFTER the last action had time to show.

        The mod publishes five times a second; reading straight after an
        action returns the snapshot from before it about half the time."""
        if settle:
            time.sleep(settle)
        return self.bridge.read()

    def runner(self):
        return TaskRunner(self.controller, self.bridge, observer=self.observer)

    def close(self):
        try:
            self.controller.stop("gameplay check exiting")
            self.controller._stop_watchers()
        except Exception as e:
            print(f"\n  WARNING: cleanup failed ({e}). If a key feels stuck, "
                  f"press W, A, S, D, SPACE and SHIFT yourself.")


def print_steps(result) -> None:
    for entry in result.records:
        print(f"    {entry.index + 1:>2}. {entry.step['action']:<13} "
              f"{entry.verification.get('status', '?'):<12} "
              f"{entry.step.get('note', '')[:70]}")
    print(f"  {result.describe()}")


def rotation(state):
    try:
        return float(state.rotation[0]), float(state.rotation[1])
    except (TypeError, IndexError, AttributeError):
        return None


# ── A. Bridge and perception ─────────────────────────────────────────────────

def section_a(rig: Rig) -> bool:
    heading("A", "Is the bridge mod running, and new enough?")
    if not rig.bridge.available():
        print(f"  {rig.bridge.unavailable_reason()}")
        record("bridge running", FAIL, "the mod is not reporting — run "
               "install_mod.bat, restart Minecraft, load a world")
        return False
    state = rig.state(settle=0)
    local = nav.LocalMap.from_state(state)
    has_clearance = any(b.clearance is not None for b in (state.surface or ()))
    print(f"  Terrain columns:        {local.known_columns}")
    print(f"  Head clearance:         {'reported' if has_clearance else 'MISSING'}")
    print(f"  Mouse sensitivity:      {state.mouse_sensitivity}")
    print(f"  On the ground:          {state.on_ground}")
    ok = local.usable and has_clearance and state.mouse_sensitivity is not None
    record("bridge is the current version", PASS if ok else FAIL,
           "" if ok else "an older jar is loaded — install_mod.bat, then "
                         "restart Minecraft")

    heading("A", "What is under the crosshair?")
    print("  Look at any block — a log, stone, grass — within reach.")
    pause("Point at something, click back on Minecraft, then press Enter here.")
    state = rig.state()
    exact = perception.from_bridge(state)
    print(f"  From the game:  {exact.describe() if exact else 'nothing reported'}")
    shot = rig.observer.capture(compress=False)
    guess = perception.classify_frame(shot.frame if shot.ok else b"")
    print(f"  From pixels:    {guess.describe()}")
    right = ask("Is the game's answer right, block AND coordinates?")
    record("identify block from the bridge", PASS if right else FAIL,
           "" if right else f"bridge said {exact.name if exact else None}")
    looks = ask("Is the pixel guess the right MATERIAL (wood / stone / "
                "dirt / grass / sand / water / leaves)?")
    record("identify material from pixels", PASS if looks else FAIL,
           f"guessed {guess.name} at {guess.confidence:.0%}"
           + ("" if looks else " — note your texture pack and time of day"))
    return True


# ── B. Aiming ────────────────────────────────────────────────────────────────

def section_b(rig: Rig) -> None:
    heading("B", "Your mouse sensitivity, from the game")
    state = rig.state(settle=0)
    resolved = aiming.SHARED.use_sensitivity(state.mouse_sensitivity)
    print(f"  {aiming.SHARED.describe()}")
    if resolved is None:
        record("sensitivity from the game", FAIL, "the bridge did not report it")
        return
    record("sensitivity from the game", PASS, f"{resolved:.2f} px/degree")

    heading("B", "Look left, right, up and down")
    print("  Four turns of 20 degrees each. After each one I read the real")
    print("  rotation back from the game and compare.")
    if not (ask("Ready? Click back on Minecraft after answering.")
            and ensure_session(rig.controller)):
        record("four-direction look", SKIP)
        return
    countdown(3, "Turning in:")
    worst, report = 0.0, []
    for label, dyaw, dpitch in (("right", 20.0, 0.0), ("left", -20.0, 0.0),
                                ("down", 0.0, 20.0), ("up", 0.0, -20.0)):
        before = rotation(rig.state())
        if before is None:
            record("four-direction look", FAIL, "rotation unreadable")
            return
        dx, dy, _ = aiming.SHARED.delta_for(before, before[0] + dyaw,
                                            before[1] + dpitch)
        rig.controller.look({"dx": dx or 1, "dy": dy})
        after = rotation(rig.state())
        turned_yaw = aiming.yaw_difference(before[0], after[0])
        turned_pitch = after[1] - before[1]
        aiming.SHARED.observe(dx, dy, before, after)
        wanted = dyaw or dpitch
        got = turned_yaw if dyaw else turned_pitch
        error = abs(got - wanted * aiming.DAMPING)
        worst = max(worst, error)
        report.append(f"{label} {got:+.1f}°")
        print(f"    {label:<5}  asked {wanted * aiming.DAMPING:+.1f}°, "
              f"turned {got:+.1f}°")
    ok = worst <= 3.0
    record("four-direction look", PASS if ok else FAIL,
           ", ".join(report) + ("" if ok else
                                f" — off by up to {worst:.1f}°. "
                                f"{aiming.SHARED.describe()}"))

    heading("B", "Aim at a block, closed loop")
    tree = nav.nearest_block(rig.state(settle=0), "log")
    if tree is None:
        print("  No log in the terrain scan. Stand within ten blocks of a tree.")
        record("aim at a chosen block", SKIP, "no log nearby")
        return
    print(f"  Target: {tree.name} at {tree.position}.")
    if not ask("Aim at it?") or not ensure_session(rig.controller):
        record("aim at a chosen block", SKIP)
        return
    countdown(2, "Aiming in:")
    for attempt in range(1, 7):
        state = rig.state()
        if mining.crosshair_on(state, tree.position, tree.name):
            record("aim at a chosen block", PASS,
                   f"on {tree.position} after {attempt - 1} correction(s)")
            return
        dx, dy, error = aiming.SHARED.aim_at(state.position, state.rotation,
                                             tree.position)
        print(f"    correction {attempt}: {error:.1f}° off")
        before = rotation(state)
        rig.controller.look({"dx": dx or 1, "dy": dy})
        aiming.SHARED.observe(dx, dy, before, rotation(rig.state()))
    seen = perception.from_bridge(rig.state())
    record("aim at a chosen block", FAIL,
           f"after 6 corrections the crosshair is on "
           f"{seen.name if seen else 'nothing'} at "
           f"{seen.position if seen else '?'} — something may be in the way")


# ── C. Mining ────────────────────────────────────────────────────────────────

def section_c(rig: Rig) -> None:
    heading("C", "Mine the block under the crosshair, and prove it broke")
    print("  Point the crosshair at a LOG within reach. I will estimate how")
    print("  long it takes with what you are holding, hold attack that long,")
    print("  and then check that exact coordinate and your inventory.")
    pause("Point at a log, click back on Minecraft, then press Enter here.")
    before = rig.state()
    exact = perception.from_bridge(before)
    if exact is None or not exact.may_destroy:
        record("mine one block", SKIP, "the crosshair is not on a block the "
               "bridge can name")
        return
    estimate = mining.estimate_break_duration(exact.name, state=before)
    print(f"  Target: {exact.describe()}")
    print(f"  Estimate: {estimate.describe()}")
    if not ask("Mine it?") or not ensure_session(rig.controller):
        record("mine one block", SKIP)
        return
    countdown(2, "Mining in:")
    held = estimate.hold_seconds(10.0) or 4.0
    result = rig.controller.mine({"duration": held})
    after = rig.state()
    print(f"  Held {result.actual_duration_ms} ms of a planned "
          f"{held * 1000:.0f} ms"
          + (f" — let go early: {result.requested.get('stopped_early')}"
             if result.requested.get("stopped_early") else ""))

    coordinate = verify.broke_block_at(exact.position, exact.name).check(
        before, after)
    print(f"  Coordinate check: {coordinate.status} — {coordinate.reason}")
    record("that exact coordinate changed",
           PASS if coordinate.status == verify.SUCCESS else FAIL,
           coordinate.reason)

    if after.inventory is None:
        record("inventory went up", SKIP, "the inventory is not readable")
        return
    time.sleep(1.0)                      # the drop has to be walked into
    later = rig.state(settle=0)
    counted = verify.collected(exact.name).check(before, later)
    print(f"  Inventory check: {counted.status} — {counted.reason}")
    if counted.status != verify.SUCCESS and not estimate.drops:
        record("inventory went up", PASS,
               f"nothing dropped, as predicted: {estimate.describe()}")
        return
    record("inventory went up",
           PASS if counted.status == verify.SUCCESS else FAIL,
           counted.reason + ("" if counted.status == verify.SUCCESS
                             else " — the item may still be on the ground"))


# ── D. Movement ──────────────────────────────────────────────────────────────

def section_d(rig: Rig) -> None:
    heading("D", "Hop a one-block step without being told")
    print("  Build this, about four blocks in front of you:")
    print("      a single row of blocks ONE block high, nothing above it.")
    print("  Then tell me where to walk: a spot on the far side of it.")
    state = rig.state(settle=0)
    here = state.position
    print(f"  You are at ({here[0]:.0f}, {here[1]:.0f}, {here[2]:.0f}).")
    target = _ask_column("Walk to which x z, on the far side?")
    if target is None or not ensure_session(rig.controller):
        record("auto-hop a one-block step", SKIP)
    else:
        countdown(3, "Walking in:")
        skill = skills.create("navigate_to", destination=target)
        result = rig.runner().run(skill)
        print_steps(result)
        hopped = any(r.step["action"] == "move_and_jump" for r in result.records)
        said_jump = ask("Did it get over the step WITHOUT anyone saying jump?")
        record("auto-hop a one-block step",
               PASS if (hopped and said_jump and not skill.failed) else FAIL,
               ("hopped" if hopped else "never tried to hop")
               + (f"; {skill.done_reason}" if skill.failed else ""))

    heading("D", "Go round a wall it cannot hop")
    print("  Build a wall TWO or more blocks high across your path, with a")
    print("  gap at one end. Give me a spot on the far side.")
    target = _ask_column("Walk to which x z, on the far side?")
    if target is None or not ensure_session(rig.controller):
        record("route round a wall", SKIP)
        return
    countdown(3, "Walking in:")
    skill = skills.create("navigate_to", destination=target)
    result = rig.runner().run(skill)
    print_steps(result)
    hops = sum(1 for r in result.records if r.step["action"] == "move_and_jump")
    went_round = ask("Did it go round through the gap, without walking into "
                     "the wall repeatedly?")
    record("route round a wall",
           PASS if (went_round and not skill.failed) else FAIL,
           f"{hops} hop attempt(s)"
           + (f"; {skill.done_reason}" if skill.failed else ""))


def _ask_column(prompt: str):
    try:
        raw = input(f"  {prompt} (blank to skip) ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n  Stopped.")
    if raw.lower() == "q":
        raise SystemExit("\n  Stopped at your request.")
    if not raw:
        return None
    try:
        x, z = (int(float(v)) for v in raw.replace(",", " ").split()[:2])
        return (x, z)
    except ValueError:
        print("  Two numbers, like: 12 -40")
        return _ask_column(prompt)


# ── E. Safety ────────────────────────────────────────────────────────────────

def section_e(rig: Rig) -> None:
    heading("E", "F12 stops everything, immediately")
    print(f"  {rig.controller.emergency.describe()}")
    if ask("I will hold W for 2 seconds. Press F12 partway through. Ready?") \
            and ensure_session(rig.controller):
        rig.controller.emergency.start()
        countdown(3, "Holding W in:")
        result = rig.controller.move({"direction": "forward", "duration": 2.0})
        print(f"  stopped_reason={result.stopped_reason} "
              f"after {result.actual_duration_ms} ms")
        stopped = ask("Did the movement stop the moment you pressed F12?")
        record("F12 stops movement", PASS if stopped else FAIL,
               f"stopped_reason={result.stopped_reason}")
    else:
        record("F12 stops movement", SKIP)

    heading("E", "Alt-Tab stops input")
    if ask("I will hold W for 2 seconds. Alt-Tab away partway through. Ready?") \
            and ensure_session(rig.controller):
        countdown(3, "Holding W in:")
        result = rig.controller.move({"direction": "forward", "duration": 2.0})
        ok = result.stopped_reason in ("focus_lost", "focus_unknown")
        record("focus loss stops movement", PASS if ok else FAIL,
               f"stopped_reason={result.stopped_reason}")
    else:
        record("focus loss stops movement", SKIP)

    heading("E", "Closing Minecraft mid-task stops input")
    print("  I will start a walk. Close Minecraft (or kill it in Task Manager)")
    print("  while it is walking. This is the last Minecraft step, so do it")
    print("  only if you are finished with the other sections.")
    target = None
    if ask("Do this one?") and ensure_session(rig.controller):
        state = rig.state(settle=0)
        target = (int(state.position[0]) + 8, int(state.position[2]))
        countdown(3, "Walking in:")
        result = rig.runner().run(skills.create("navigate_to",
                                                destination=target))
        print_steps(result)
        held = rig.controller.ledger.held()
        ok = result.status in ("stopped", "incomplete") and not held
        record("closing the game stops input", PASS if ok else FAIL,
               f"task {result.status}: {result.reason}; "
               f"still held: {sorted(held) or 'nothing'}")
    else:
        record("closing the game stops input", SKIP)


# ── F. Voice ─────────────────────────────────────────────────────────────────

def section_f(_rig) -> None:
    """Voice runs inside JARVIS, not in this script, so these are guided."""
    print(f"""
{RULE}
  F: VOICE — these run in JARVIS itself
{RULE}
  Start JARVIS normally (run_jarvis.bat) and keep this window beside it.
  In the JARVIS text box, "voice check" prints the pipeline counters:

      mic N captured / G gated / Q queued / D DROPPED / S sent
        -> gemini T transcripts / U turns / R responses / C tool calls

  What each shape means:
      captured climbing, sent flat          the send loop is stuck
      DROPPED above zero                    the queue overflowed
      sent climbing, transcripts flat       Gemini got audio, heard nothing
      transcripts up, responses+tools flat  Gemini chose not to answer
      gated / echo_tail climbing            the echo guard ate your words
""")
    for title, instruction in (
            ("Short command, five times",
             "Type 'voice check' and note the numbers. Then say "
             "'Jarvis, what time is it?' five times, waiting for each "
             "answer. Type 'voice check' again."),
            ("Talking over the end of a reply",
             "Ask something with a long answer. Say 'Jarvis, stop' while it "
             "is still speaking."),
            ("Talking straight after a reply",
             "Ask 'Jarvis, what time is it?' and the INSTANT it finishes, "
             "say 'Jarvis, what day is it?'. The first syllable is the one "
             "the echo guard could eat.")):
        heading("F", title)
        print(f"  {instruction}")
        pause("Press Enter when done.")
        answered = ask("Did every command get an answer (or an action)?")
        note = ""
        if not answered:
            try:
                note = input("  Paste the 'voice check' lines, or describe "
                             "what happened: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("\n  Stopped.")
        record(f"voice: {title.lower()}", PASS if answered else FAIL, note)


# ── Main ─────────────────────────────────────────────────────────────────────

SECTIONS = {"A": section_a, "B": section_b, "C": section_c,
            "D": section_d, "E": section_e, "F": section_f}


def main(argv) -> int:
    wanted = [a.upper() for a in argv if a.upper() in SECTIONS] or list(SECTIONS)
    print(f"""
{RULE}
  MARK-LIV — MINECRAFT GAMEPLAY CHECK
{RULE}
  Sections: {' '.join(wanted)}
  A bridge & perception   B aiming   C mining   D movement
  E safety                F voice (guided, runs in JARVIS)

  Minecraft open with the bridge mod, windowed or borderless, in a world
  you do not mind changing. 'q' at any prompt stops everything.
""")
    pause("Press Enter when ready.")
    rig = Rig()
    try:
        if "A" in wanted and not section_a(rig):
            print("\n  The bridge is not working, and every later Minecraft")
            print("  section depends on it. Fix that first.")
            wanted = [s for s in wanted if s == "F"]
        for key in wanted:
            if key != "A":
                SECTIONS[key](rig)
        return summarise()
    finally:
        rig.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        raise SystemExit(130)
