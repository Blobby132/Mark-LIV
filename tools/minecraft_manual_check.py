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

from core import ocr as core_ocr                      # noqa: E402
from minecraft import process as mc_process          # noqa: E402
from minecraft import navigation as mc_nav          # noqa: E402
from minecraft import skills as mc_skills            # noqa: E402
from minecraft.controller import MinecraftController  # noqa: E402
from minecraft.debug_overlay import DebugOverlayStateSource  # noqa: E402
from minecraft.mod_bridge import ModBridgeStateSource  # noqa: E402
from minecraft.observation import Observer            # noqa: E402
from minecraft.state import VisionStateSource         # noqa: E402
from minecraft.task_runner import TaskRunner          # noqa: E402

RULE = "─" * 72
results: list = []


_step_number = 0


def heading(title: str) -> None:
    """Numbered automatically. Hand-numbered headings meant that inserting a
    check in the middle silently renumbered every later one, and the numbers
    in the summary stopped matching the numbers on screen."""
    global _step_number
    _step_number += 1
    print(f"\n{RULE}\n  STEP {_step_number}: {title}\n{RULE}")


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
  MARK-LIV — MINECRAFT PHASE 4 MANUAL CHECK
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

  ONE CONFIRMATION
    The session you approve at step 5 covers every gameplay action in this
    check — walking, looking, jumping, attacking, mining, placing, items,
    the inventory and interaction. You should not be asked again.
    If any later step asks you to confirm something, that is a bug and I
    want to know.

  What this checks, in order:
     1. Minecraft process detection      10. Report your position
     2. Minecraft window detection       11. Aim at a block
     3. Foreground focus                 12. Mine it, and VERIFY it broke
     4. Capture one Minecraft-only frame 13. Select a hotbar slot
     5. ONE session confirmation         14. Place a block, and verify
     6. Relative mouse movement          15. Open and close the inventory
     7. Hold W / Hold A                  16. Interact with a block
     8. Tap SPACE, sprint, sneak         17. Emergency stop (F12)
     9. Read the F3 debug overlay        18. Everything released

  Steps 12, 14 and 16 change the world. Use a throwaway creative world.
""")
    pause("Press Enter when Minecraft is open and you are ready.")

    controller = MinecraftController(start_watchers=False)
    observer = Observer(controller._locator)
    reader = core_ocr.create_reader()
    source = DebugOverlayStateSource(observer=observer, reader=reader)
    state = None
    aimed = None
    session_open = False

    try:
        # ── 1 ────────────────────────────────────────────────────────────────
        heading("Is Minecraft running?")
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
        heading("Can I find the window?")
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
        heading("Does Minecraft have focus?")
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
        heading("Capture one Minecraft-only frame")
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

        vision = VisionStateSource().read()
        print(f"\n  Vision-only state: {vision.describe()}")
        print("  (Expected — the F3 overlay is what reads real values; that")
        print("   is tested further down.)")

        # ── 5 ────────────────────────────────────────────────────────────────
        heading("Does relative mouse movement turn the camera?")
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

        # ── movement ─────────────────────────────────────────────────────────
        for label, params, question in [
            ("Hold W for 0.5s", {"direction": "forward", "duration": 0.5},
             "Did your character walk FORWARD?"),
            ("Hold A for 0.5s", {"direction": "left", "duration": 0.5},
             "Did your character strafe LEFT?"),
        ]:
            heading(label)
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

        heading("Tap SPACE")
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
        # ── Phase 3: state, interaction, verification ────────────────────────
        heading("Read the F3 debug overlay")
        print("  Reading your position needs Minecraft's own debug screen.")
        print(f"  Text reader: {reader.describe()}")
        if not getattr(reader, "available", False):
            record("F3 state reading", False,
                   "no OCR installed — see the message above")
            print("\n  Skipping the state-dependent steps (9-12).")
            state = None
        else:
            print("\n  I will press F3 to open the overlay.")
            if ask("Open the debug overlay?") and ensure_session(controller):
                session_open = True
                controller.toggle_debug_overlay()
                time.sleep(0.6)
            else:
                print("  Press F3 yourself, then continue.")
                pause("Press Enter when the overlay is showing.")

            state = source.read()
            print(f"\n  {state.describe()}")
            got = bool(state.known_fields())
            record("F3 state reading", got,
                   f"read {len(state.known_fields())} field(s)" if got
                   else "could not read any values — try a bigger window "
                        "or a larger GUI scale")

        # ── 10 ───────────────────────────────────────────────────────────────
        heading("Does it know where you are?")
        if state is not None and state.position:
            x, y, z = state.position
            print(f"  I make your position:  X={x:.1f}  Y={y:.1f}  Z={z:.1f}")
            if state.facing:
                print(f"  Facing: {state.facing}")
            if state.biome:
                print(f"  Biome:  {state.biome}")
            print("\n  Compare that with the XYZ line on your F3 screen.")
            right = ask("Does that match what F3 shows?")
            record("position reading", right,
                   "" if right else "the numbers did not match — OCR misread "
                                    "them; a larger window usually fixes it")
        else:
            record("position reading", False, "no position was read")

        # ── 11 ───────────────────────────────────────────────────────────────
        heading("Aim at a block")
        print("  Point your crosshair at a solid block — a tree trunk is")
        print("  ideal, since step 12 will try to break it.")
        pause("Aim at a block, click back on Minecraft, then press Enter.")

        target = source.read() if state is not None else None
        aimed = getattr(getattr(target, "target_block", None), "name", None)
        if aimed and aimed != "air":
            print(f"  I think you are looking at: {aimed}")
            right = ask("Is that the block you are aiming at?")
            record("target block identification", right,
                   "" if right else f"I read {aimed}")
        else:
            print("  I cannot see a block under your crosshair.")
            record("target block identification", False,
                   "no target block read" if state is not None
                   else "skipped — no state source")

        # ── 12 ───────────────────────────────────────────────────────────────
        heading("Attack, and verify the result")
        print("  THIS BREAKS A BLOCK. Only continue in a throwaway world.")
        print()
        print("  The point of this step is not that the block breaks. It is")
        print("  that I can tell you WHETHER it broke, rather than assuming")
        print("  it did because I held the button down.")
        print()
        if aimed and aimed != "air" and ask(f"Break the {aimed}?") \
                and ensure_session(controller):
            session_open = True
            print("  Click back on Minecraft.")
            countdown(4, "Breaking in:")
            outcome = TaskRunner(controller, source,
                                 observer=observer).run(
                                     mc_skills.BreakBlock(expected=aimed))
            print(f"\n  {outcome.describe()}")
            for entry in outcome.records:
                print(f"    swing {entry.index + 1}: "
                      f"{entry.verification['status']:<12} "
                      f"{entry.verification['reason']}")
            verified = outcome.verified_steps > 0
            record("attack + verification", verified,
                   "the break was confirmed by re-reading the target"
                   if verified else
                   "the block did not break, or I could not confirm it did")
            gone = ask(f"Is the {aimed} actually gone in the game?")
            record("verification matched reality", gone == verified,
                   "" if gone == verified else
                   f"I said {verified} and the game says {gone} — this is the "
                   f"important failure to report")
        else:
            record("attack + verification", False, "skipped")

        # ── 13 ───────────────────────────────────────────────────────────────
        heading("Select a hotbar slot")
        print("  I will select slot 3. Watch the hotbar highlight move.")
        if ask("Ready?") and ensure_session(controller):
            session_open = True
            countdown(3, "Selecting slot 3 in:")
            result = controller.hotbar_select({"slot": 3})
            print(f"  {result.describe()}")
            moved = ask("Did the hotbar selection move to slot 3?")
            record("hotbar select", moved and result.ok)
            print("\n  Note: I cannot VERIFY this myself — the selected slot")
            print("  is not on the F3 screen. It needs the mod bridge.")
        else:
            record("hotbar select", False, "skipped")

        # ── 14 ───────────────────────────────────────────────────────────────
        heading("Use an item")
        print("  Put a placeable block in slot 3 and aim at the ground.")
        print("  THIS PLACES A BLOCK.")
        if ask("Try using/placing what is in your hand?") \
                and ensure_session(controller):
            session_open = True
            pause("Aim at the ground, click back on Minecraft, press Enter.")
            countdown(3, "Using in:")
            result = controller.use_item({"duration": 0.2})
            print(f"  {result.describe()}")
            placed = ask("Did something happen (a block placed, an item used)?")
            record("use item", placed and result.ok)
        else:
            record("use item", False, "skipped")

        # ── Phase 4: sprint, sneak, place, inventory, interact ───────────────
        heading("Sprint and sneak")
        if ask("Ready? I will sprint forward, then sneak.") \
                and ensure_session(controller):
            countdown(3, "Sprinting in:")
            sprinted = controller.sprint({"duration": 0.6,
                                          "direction": "forward"})
            print(f"  {sprinted.describe()}")
            countdown(2, "Sneaking in:")
            sneaked = controller.sneak({"duration": 0.6})
            print(f"  {sneaked.describe()}")
            seen = ask("Did you sprint forward and then crouch?")
            record("sprint + sneak", seen and sprinted.ok and sneaked.ok)
        else:
            record("sprint + sneak", False, "skipped")

        heading("Place a block")
        print("  Put a placeable block in slot 3 and aim at the ground.")
        print("  THIS PLACES A BLOCK.")
        if ask("Place a block from slot 3?") and ensure_session(controller):
            pause("Aim at the ground, click back on Minecraft, press Enter.")
            countdown(3, "Placing in:")
            outcome = TaskRunner(controller, source, observer=observer).run(
                mc_skills.PlaceBlock(slot=3))
            print(f"\n  {outcome.describe()}")
            for entry in outcome.records:
                print(f"    {entry.step['action']:<14} "
                      f"{entry.verification['status']:<12} "
                      f"{entry.verification['reason']}")
            placed = ask("Did a block actually appear?")
            record("place block", placed)
            print("  Note: placement is confirmed by the targeted block")
            print("  changing, which misses a block that lands out of view.")
        else:
            record("place block", False, "skipped")

        heading("Open and close the inventory")
        if ask("Open the inventory?") and ensure_session(controller):
            countdown(2, "Opening in:")
            opened = controller.inventory({"state": "open"})
            print(f"  {opened.describe()}")
            saw = ask("Did the inventory screen open?")
            countdown(2, "Closing in:")
            closed = controller.inventory({"state": "close"})
            print(f"  {closed.describe()}")
            shut = ask("Did it close again?")
            record("inventory open/close", saw and shut and opened.ok)
        else:
            record("inventory open/close", False, "skipped")

        heading("Interact with a block")
        print("  Aim at a door, chest, crafting table or lever.")
        if ask("Try interacting with it?") and ensure_session(controller):
            pause("Aim at it, click back on Minecraft, press Enter.")
            countdown(3, "Interacting in:")
            result = controller.interact({})
            print(f"  {result.describe()}")
            worked = ask("Did it open / toggle / respond?")
            record("interact", worked and result.ok)
        else:
            record("interact", False, "skipped")

        # ── Navigation, which only means anything with the mod running ──────
        heading("Can I see the world around you?")
        print("  This needs the bridge mod. Without it every step below is")
        print("  skipped, because navigating on a map you cannot read is")
        print("  guessing, and the code refuses to do it.")
        world_source = ModBridgeStateSource()
        world = world_source.read()
        local = mc_nav.LocalMap.from_state(world)
        if local.usable:
            summary = mc_nav.summarise(world)
            print(f"  Scan: {summary['columns_seen']} columns within "
                  f"{summary.get('scan_radius')} blocks.")
            for key, value in sorted(summary.items()):
                if key.startswith("nearest_"):
                    print(f"    {key}: {value}")
            right = ask("Does that match what is actually around you?")
            record("terrain scan", right,
                   "" if right else "the scan disagrees with the game — "
                                    "check the mod version against the "
                                    "Minecraft version")
        else:
            print(f"  {world_source.unavailable_reason()}")
            record("terrain scan", False, "the bridge mod is not reporting "
                                          "terrain; navigation steps skipped")

        if local.usable:
            heading("Walk to a coordinate")
            print("  I will pick a walkable spot about 6 blocks away and")
            print("  route to it, going round anything in the way.")
            here = (local.origin[0], local.origin[2])
            options = [c for c in local.ground
                       if 5 <= abs(c[0] - here[0]) + abs(c[1] - here[1]) <= 8
                       and local.standable(*c)
                       and mc_nav.find_path(world, c).found]
            if not options:
                record("navigate to a coordinate", False,
                       "nothing 5-8 blocks away is both walkable and "
                       "reachable — try somewhere more open")
            elif ask(f"Walk to {options[0]}?") and ensure_session(controller):
                skill = mc_skills.create("navigate_to",
                                         destination=options[0])
                runner = TaskRunner(controller, world_source,
                                    observer=observer)
                countdown(3, "Walking in:")
                result = runner.run(skill)
                for entry in result.records:
                    print(f"    {entry.index + 1}. {entry.step['action']:<5} "
                          f"{entry.verification['status']:<12} "
                          f"{entry.step['note']}")
                print(f"  {result.describe()}")
                print(f"  Skill says: {skill.done_reason}")
                arrived = ask("Did your character actually walk there?")
                record("navigate to a coordinate", arrived and not skill.failed,
                       "" if arrived else "it did not arrive — compare the "
                                          "per-step notes above against what "
                                          "you saw")
            else:
                record("navigate to a coordinate", False, "skipped")

            heading("Does it turn the right way?")
            print("  The pixels-per-degree figure and the sign of the turn")
            print("  are DERIVED, not measured — they depend on your mouse")
            print("  sensitivity. The skill corrects itself after one bad")
            print("  turn, but a big correction here means the default is")
            print("  wrong for your setup and every turn costs an extra step.")
            print(f"  Current default: {mc_nav.PIXELS_PER_DEGREE} px/degree.")
            if ask("Measure it? I will turn, then read how far you turned.") \
                    and ensure_session(controller):
                before = world_source.read()
                countdown(2, "Turning in:")
                controller.look({"dx": 400, "dy": 0})
                time.sleep(0.4)
                after = world_source.read()
                try:
                    turned = abs(mc_nav.yaw_difference(before.rotation[0],
                                                       after.rotation[0]))
                except Exception:
                    turned = 0.0
                if turned > 0.5:
                    measured = 400.0 / turned
                    print(f"  400 pixels turned you {turned:.1f}° "
                          f"= {measured:.2f} px/degree.")
                    close = abs(measured - mc_nav.PIXELS_PER_DEGREE) < 2.0
                    record("turn calibration", close,
                           "" if close else
                           f"set PIXELS_PER_DEGREE in minecraft/navigation.py "
                           f"to about {measured:.1f} for this machine")
                else:
                    record("turn calibration", False,
                           "the view did not turn measurably")
            else:
                record("turn calibration", False, "skipped")

            heading("Collect a log, with the map")
            print("  With the scan running this should WALK to a tree rather")
            print("  than turn on the spot. If it spins, that is the bug.")
            tree = mc_nav.nearest_block(world, "log", reachable_only=True)
            if tree is None:
                seen = mc_nav.nearest_block(world, "log")
                record("collect one log", False,
                       "no reachable tree in the scan"
                       + (f" (nearest seen: {seen.name} at {seen.position}, "
                          f"no route)" if seen else ""))
            elif ask(f"Collect one {tree.name} at {tree.position}?") \
                    and ensure_session(controller):
                skill = mc_skills.create("collect_logs", count=1)
                runner = TaskRunner(controller, world_source,
                                    observer=observer)
                countdown(3, "Starting in:")
                result = runner.run(skill)
                for entry in result.records:
                    print(f"    {entry.index + 1}. {entry.step['action']:<5} "
                          f"{entry.verification['status']:<12} "
                          f"{entry.step['note']}")
                print(f"  {result.describe()}")
                print(f"  Skill says: {skill.done_reason}")
                walked = ask("Did it WALK to the tree (not just turn)?")
                broke = ask("Did a log actually break?")
                record("collect one log", walked and broke and not skill.failed,
                       "" if (walked and broke) else
                       "walked=%s broke=%s — the per-step notes say which "
                       "half failed" % (walked, broke))
            else:
                record("collect one log", False, "skipped")

        heading("Emergency stop")
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
        heading("Is everything released?")
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


def ensure_session(controller) -> bool:
    """Make sure an authorised session exists.

    In the app there is ONE confirmation and it covers every gameplay action,
    so there is nothing to upgrade here and no second tier to ask about. This
    only exists because the later steps involve reading prompts and looking at
    the game, which takes longer than a session lasts — so by the time you
    answer, the one opened earlier has often expired. Failing a check for that
    reason would say nothing about what the check is testing.

    Returns False if it could not, so the caller records a skip rather than
    walking into a refusal."""
    current = controller.sessions.current
    if current is not None and current.is_authorized():
        return True
    try:
        controller.start_session(duration_s=120, owner="manual check")
        return True
    except Exception as e:
        print(f"  Could not open a session: {e}")
        return False


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
        print(f"  All {len(results)} checks passed. Phase 4 works on your "
          f"machine.")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        raise SystemExit(130)
