#!/usr/bin/env python3
"""
tools/minecraft_manual_check.py — the one test that needs a real Minecraft.

Everything in tests/ runs against fakes, which is right for the logic and
useless for the question that actually decides whether any of this works:

    does an injected relative mouse delta move the Minecraft camera?

Minecraft's "Raw Input" setting is on by default, and whether it accepts
synthetic deltas depends on the driver and the build. I have not verified it
against yours, and this script does not assume — step 5 tests it first, before
anything else touches the keyboard, and tells you plainly if it does not work.

HOW IT BEHAVES
    Interactive, never autonomous. It says what it is about to do, waits for
    you to press Enter, does one bounded thing, and asks what you saw. You can
    skip any step or quit at any point, and it releases every key on the way
    out however it exits.

    Run it with Minecraft ALREADY OPEN, in a throwaway creative or peaceful
    world. The movement steps will walk your character a short distance.

    python tools/minecraft_manual_check.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import process as mc_process          # noqa: E402
from minecraft.controller import MinecraftController  # noqa: E402
from minecraft.observation import Observer            # noqa: E402
from minecraft.state import VisionStateSource         # noqa: E402

RULE = "─" * 72
results: list = []


def heading(number: int, title: str) -> None:
    print(f"\n{RULE}\n  STEP {number}: {title}\n{RULE}")


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    mark = "PASS" if ok else "FAIL"
    print(f"\n  [{mark}] {name}" + (f" — {note}" if note else ""))


def ask(prompt: str) -> bool:
    """Yes/no. Anything that is not a yes is a no — a hesitant 'maybe' about
    whether the camera moved is a no."""
    try:
        answer = input(f"  {prompt} [y/N/q] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n  Stopped.")
    if answer == "q":
        raise SystemExit("\n  Stopped at your request.")
    return answer in ("y", "yes")


def pause(message: str) -> None:
    try:
        input(f"  {message} ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\n  Stopped.")


def countdown(seconds: int, why: str) -> None:
    print(f"\n  {why}")
    for remaining in range(seconds, 0, -1):
        print(f"    {remaining}...", end="\r", flush=True)
        time.sleep(1)
    print("    go!      ")


def main() -> int:
    print(f"""
{RULE}
  MARK-LIV — MINECRAFT PHASE 2 MANUAL CHECK
{RULE}

  Before you start:
    * Minecraft Java Edition should ALREADY BE OPEN.
    * Use a throwaway creative or peaceful world. This will move your
      character a couple of blocks.
    * Run the game WINDOWED or BORDERLESS, not exclusive fullscreen —
      window tracking and region capture are unreliable in exclusive
      fullscreen.
    * Nothing here is autonomous. Every step asks first, and you can
      answer 'q' at any prompt to stop.

  What this checks, in order:
    1. Minecraft process detection        6. Hold W for 0.5s
    2. Minecraft window detection         7. Hold A for 0.5s
    3. Foreground focus                   8. Tap SPACE
    4. Capture one Minecraft-only frame   9. Emergency stop (F12)
    5. Relative mouse movement           10. Everything released
""")
    pause("Press Enter when Minecraft is open and you are ready.")

    controller = MinecraftController(start_watchers=False)
    observer = Observer(controller._locator)
    session_open = False

    try:
        # ── 1 ────────────────────────────────────────────────────────────────
        heading(1, "Is Minecraft running?")
        info = mc_process.find()
        print(f"  {info.detail}")
        if info.running:
            print(f"  Matched on: {info.matched_on}")
            record("process detection", True, f"pid {info.pid}")
        else:
            record("process detection", False, info.detail)
            print("\n  Cannot continue without a running game.")
            return summarise()

        # ── 2 ────────────────────────────────────────────────────────────────
        heading(2, "Can I find the window?")
        window = controller._locator.attach()
        print(f"  {window.detail}")
        if window.found and window.rect:
            rect = window.rect
            print(f"  Title : {window.title[:60]}")
            print(f"  Size  : {rect.width} x {rect.height}")
            print(f"  At    : ({rect.left}, {rect.top})")
            record("window detection", True, f"{rect.width}x{rect.height}")
        else:
            record("window detection", False, window.detail)
            return summarise()

        # ── 3 ────────────────────────────────────────────────────────────────
        heading(3, "Does Minecraft have focus?")
        if not window.focus_known:
            print(f"  I cannot read the foreground window on this platform.")
            print(f"  {window.detail}")
            record("focus detection", False,
                   "not supported on this platform — input will be refused")
            print("\n  Observation will still be tested; input cannot be.")
        else:
            print("  Click on the Minecraft window to give it focus.")
            countdown(5, "Checking focus in:")
            window = controller._locator.probe()
            print(f"  Foreground: {window.foreground}")
            record("focus detection", bool(window.foreground),
                   "Minecraft is in front" if window.foreground
                   else "another window has focus")

        # ── 4 ────────────────────────────────────────────────────────────────
        heading(4, "Capture one Minecraft-only frame")
        print("  This captures the game window region ONLY — not your whole")
        print("  desktop. Nothing is sent anywhere; it is saved next to this")
        print("  script so you can look at it.")
        pause("Press Enter to capture.")

        observation = observer.capture()
        print(f"  {observation.describe()}")
        if observation.ok and observation.frame:
            suffix = ".jpg" if "jpeg" in observation.mime else ".png"
            out = Path(__file__).resolve().parent / f"minecraft_frame{suffix}"
            out.write_bytes(observation.frame)
            print(f"  Saved: {out}")
            print(f"  Open it and check it shows ONLY Minecraft.")
            only_game = ask("Does the image show only the game window?")
            record("window-only capture", only_game,
                   "" if only_game else "the frame included other windows")
        else:
            record("window-only capture", False, observation.error)

        state = VisionStateSource().read()
        print(f"\n  State source says: {state.describe()}")
        print("  (That is expected in this phase — nothing reads game state yet.)")

        # ── 5 ────────────────────────────────────────────────────────────────
        heading(5, "Does relative mouse movement turn the camera?")
        print("  THIS IS THE IMPORTANT ONE.")
        print()
        print("  Minecraft reads mouse DELTAS while it has the cursor captured,")
        print("  not cursor position, and its 'Raw Input' setting may or may")
        print("  not accept injected movement. If this step fails, looking")
        print("  around will not work and nothing else will fix it.")
        print()
        print(f"  Backend: {controller.backend.describe()}")
        if not getattr(controller.backend, "available", False):
            record("relative mouse", False,
                   "no input backend on this platform")
            print("\n  Skipping every input step — there is no way to send it.")
            return summarise()

        print("\n  I will open a control session first. You will see a")
        print("  confirmation on the JARVIS HUD if it is running; if JARVIS is")
        print("  not running, the session opens directly for this test.")
        if not ask("Open a 120-second control session?"):
            record("relative mouse", False, "skipped")
            return summarise()

        controller.start_session(duration_s=120, owner="manual check")
        session_open = True
        print(f"  {controller.emergency.describe()}")

        print("\n  Click on Minecraft, then watch the camera.")
        countdown(5, "Turning right by 150px in:")
        result = controller.look({"dx": 150, "dy": 0})
        print(f"  {result.describe()}")
        turned = ask("Did the camera turn to the RIGHT?")
        if turned:
            countdown(3, "Turning back left in:")
            controller.look({"dx": -150, "dy": 0})
        record("relative mouse", turned,
               "" if turned else
               "injected deltas did not move the camera — try turning OFF "
               "'Raw Input' in Minecraft's mouse settings and re-running")

        # ── 6, 7, 8 ──────────────────────────────────────────────────────────
        for number, (label, params, question) in enumerate([
            ("Hold W for 0.5s", {"direction": "forward", "duration": 0.5},
             "Did your character walk FORWARD?"),
            ("Hold A for 0.5s", {"direction": "left", "duration": 0.5},
             "Did your character strafe LEFT?"),
        ], start=6):
            heading(number, label)
            print("  Click on Minecraft and watch your character.")
            if not ask(f"Ready to {label.lower()}?"):
                record(label, False, "skipped")
                continue
            countdown(3, "Moving in:")
            result = controller.move(params)
            print(f"  {result.describe()}")
            print(f"  ok={result.ok} "
                  f"actual={result.actual_duration_ms}ms "
                  f"stopped_reason={result.stopped_reason} "
                  f"focused_throughout={result.window_focused_throughout}")
            moved = ask(question)
            record(label, moved and result.ok,
                   "" if moved else "no movement observed")

        heading(8, "Tap SPACE")
        if ask("Ready to jump?"):
            countdown(3, "Jumping in:")
            result = controller.jump({})
            print(f"  {result.describe()}")
            jumped = ask("Did your character JUMP?")
            record("jump", jumped and result.ok,
                   "" if jumped else "no jump observed")
        else:
            record("jump", False, "skipped")

        # ── 9 ────────────────────────────────────────────────────────────────
        heading(9, "Emergency stop")
        print(f"  {controller.emergency.describe()}")
        print()
        if controller.emergency.scope == "global":
            print("  I will hold W for 2 seconds. Press F12 partway through.")
            print("  The character should stop the instant you press it.")
            if ask("Ready?"):
                controller.emergency.start()
                countdown(3, "Holding W in:")
                result = controller.move({"direction": "forward",
                                          "duration": 2.0})
                print(f"  {result.describe()}")
                print(f"  stopped_reason={result.stopped_reason} "
                      f"actual={result.actual_duration_ms}ms")
                stopped = ask("Did pressing F12 stop the movement?")
                record("F12 emergency stop", stopped,
                       "" if stopped else "F12 did not interrupt the movement")
            else:
                record("F12 emergency stop", False, "skipped")
        else:
            print("  No global hotkey on this platform. Testing Alt-Tab")
            print("  instead — that works everywhere and is the primary stop.")
            if ask("Ready? I will hold W for 2s; Alt-Tab away partway through."):
                countdown(3, "Holding W in:")
                result = controller.move({"direction": "forward",
                                          "duration": 2.0})
                print(f"  {result.describe()}")
                stopped = result.stopped_reason in ("focus_lost", "focus_unknown")
                record("focus-loss stop", stopped,
                       f"stopped_reason={result.stopped_reason}")
            else:
                record("focus-loss stop", False, "skipped")

        # ── 10 ───────────────────────────────────────────────────────────────
        heading(10, "Is everything released?")
        outcome = controller.stop("manual check finished")
        held = controller.ledger.held()
        print(f"  Ledger says held: {sorted(held) or 'nothing'}")
        print(f"  Released on stop: {outcome['released'] or 'nothing'}")
        if outcome.get("failed_to_release"):
            print(f"  COULD NOT RELEASE: {outcome['failed_to_release']}")
        clean = not held and outcome.get("clean", False)
        record("all input released", clean,
               "" if clean else "something was left held — press W, A and "
                                "SPACE yourself to be sure")
        session_open = False

        print("\n  Check the game: your character should be standing still and")
        print("  no key should be stuck down.")
        still = ask("Is the character completely still, with no stuck keys?")
        record("no stuck keys in game", still)

        return summarise()

    finally:
        # However this exits — a crash, Ctrl-C, a 'q' at any prompt — the keys
        # come up. This is the same release path the controller uses.
        try:
            controller.stop("manual check exiting")
            controller._stop_watchers()
        except Exception as e:
            print(f"\n  WARNING: cleanup failed ({e}). If a key feels stuck, "
                  f"press W, A, S, D, SPACE and SHIFT yourself.")
        if session_open:
            print("  (Control session closed.)")


def summarise() -> int:
    print(f"\n{RULE}\n  RESULT\n{RULE}")
    if not results:
        print("  Nothing was tested.")
        return 1
    width = max(len(name) for name, _ok, _note in results)
    failed = 0
    for name, ok, note in results:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name.ljust(width)}" + (f"  {note}" if note else ""))
        if not ok:
            failed += 1
    print()
    if failed:
        print(f"  {failed} of {len(results)} checks did not pass.")
        print("  Send me this output and I will work out why — particularly if")
        print("  the relative-mouse step failed, since that one decides whether")
        print("  looking around can work at all.")
    else:
        print(f"  All {len(results)} checks passed. Phase 2 works on your machine.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        raise SystemExit(130)
