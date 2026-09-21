"""
tools/f3_check.py — can JARVIS actually read your Minecraft screen?

WHY THIS IS SEPARATE FROM THE MANUAL CHECK
    The manual check drives the game. This one touches nothing: it captures
    the Minecraft window, runs OCR on it, and prints what came back. No keys,
    no session, no confirmation. Safe to run at any time, including while you
    are playing.

WHAT IT IS FOR
    The pixels-to-text step is the one part of the state pipeline I could not
    verify without your machine. Everything downstream — knowing where you
    are, telling whether a block broke, "collect some wood" doing anything at
    all — is built on it.

    So this prints the RAW OCR output as well as the parsed result. If the
    parse fails, the raw text says why: a Minecraft version whose F3 layout
    differs from the ones the patterns were written against, a GUI scale too
    small for OCR, or an overlay that is not open. Those need different fixes,
    and guessing between them from "it didn't work" wastes everyone's time.

    Run it with Minecraft open and F3 showing, then send me the output.

    python tools/f3_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ocr as core_ocr                          # noqa: E402
from minecraft import debug_overlay as f3                 # noqa: E402
from minecraft import process as mc_process               # noqa: E402
from minecraft.observation import Observer                # noqa: E402
from minecraft.window import Locator                      # noqa: E402

RULE = "─" * 72


def main() -> int:
    print(f"{RULE}\n  MARK-LIV — can I read your Minecraft screen?\n{RULE}")
    # Printed up front because "installed it but the script cannot see it" is
    # nearly always two different interpreters, and that is invisible unless
    # you say which one is running.
    print(f"\n  Running on: {sys.executable}")
    print("\n  Open Minecraft and press F3 so the debug overlay is showing.")
    print("  This sends no input and starts no session — it only looks.\n")
    try:
        input("  Press Enter when F3 is showing...")
    except EOFError:
        pass

    # ── 1. the game ──────────────────────────────────────────────────────────
    info = mc_process.find()
    print(f"\n  Minecraft: {info.detail}")
    if not info.running:
        print("  Cannot continue without a running game.")
        return 1

    locator = Locator()
    window = locator.attach()
    if not window.found or window.rect is None:
        print(f"  Window: {window.detail}")
        return 1
    print(f"  Window: {window.title[:60]}  "
          f"{window.rect.width}x{window.rect.height}")

    # ── 2. OCR ───────────────────────────────────────────────────────────────
    reader = core_ocr.create_reader()
    print(f"\n  Text reader: {reader.describe()}")
    if not getattr(reader, "available", False):
        print("\n  OCR is not set up, so nothing below can work. The message")
        print("  above says what to install.")
        return 1

    # ── 3. capture ───────────────────────────────────────────────────────────
    observation = Observer(locator).capture(compress=False)
    print(f"  Capture: {observation.describe()}")
    if not observation.ok or not observation.frame:
        return 1

    out = Path(__file__).resolve().parent / "f3_frame.png"
    try:
        out.write_bytes(observation.frame)
        print(f"  Saved the frame to: {out}")
    except Exception as e:
        print(f"  (Could not save the frame: {e})")

    # ── 4. the raw text ──────────────────────────────────────────────────────
    print(f"\n{RULE}\n  RAW OCR OUTPUT — send me this part\n{RULE}")
    try:
        text = reader.read_text(observation.frame)
    except Exception as e:
        print(f"  OCR failed: {type(e).__name__}: {e}")
        return 1

    if not text.strip():
        print("  (nothing — OCR returned an empty string)")
    else:
        for line in text.splitlines():
            if line.strip():
                print(f"  | {line}")

    # ── 5. what parsed ───────────────────────────────────────────────────────
    print(f"\n{RULE}\n  WHAT I COULD PARSE FROM IT\n{RULE}")
    print(f"  Looks like an F3 overlay: {f3.looks_like_overlay(text)}")

    found = f3.parse_overlay(text)
    if not found:
        print("\n  Nothing parsed.")
        print("  If the raw text above looks like a debug screen, the layout")
        print("  differs from the versions these patterns were written")
        print("  against and I can fix the patterns from your output.")
        print("  If it looks like garbage, OCR is struggling: try a larger")
        print("  game window, or raise GUI Scale in Video Settings.")
    else:
        for key in sorted(found):
            print(f"  {key:<16} {found[key]}")

    missing = [f for f in f3.SUPPLIES if f not in found
               and f != "target_block"]
    if found and missing:
        print(f"\n  Not read: {', '.join(missing)}")

    # ── 6. the same thing through the real state source ─────────────────────
    print(f"\n{RULE}\n  AS THE REST OF JARVIS WOULD SEE IT\n{RULE}")
    state = f3.DebugOverlayStateSource(observer=Observer(locator),
                                       reader=reader).read()
    print(f"  {state.describe()}")

    print(f"\n{RULE}")
    if state.known_fields():
        print("  Working. JARVIS can read your game state.")
        print("  'Collect some wood' should now do something real.")
        return 0
    print("  Not working yet. Send me the RAW OCR section above.")
    return 1


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        code = 1
    import platform
    if platform.system() == "Windows":
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
