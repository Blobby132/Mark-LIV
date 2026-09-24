"""
tools/bridge_check.py — is the companion mod talking to JARVIS?

Read-only and safe to run at any time, including mid-game: it opens no
session, sends no input and needs no confirmation. It reads the file the mod
writes and prints what arrived.

    python tools/bridge_check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minecraft import navigation
from minecraft.mod_bridge import (                                      # noqa: E402
    SCHEMA, ModBridgeStateSource, state_file_path,
)
from minecraft.state import EXACT                                       # noqa: E402

RULE = "─" * 72


def inspect_running_game() -> dict:
    """What the running Minecraft actually is, read from its command line.

    This is the one source of truth that settles "but I installed it". A
    launcher can have a dozen instances and the jar can be sitting in eleven
    of the wrong ones; the process itself says which directory it was started
    with and whether Fabric is on its classpath. Everything else is inference.

    Windows lets you read your own processes' command lines, so no elevation
    is needed. Anything unreadable comes back as None rather than a guess."""
    info = {"found": False, "pid": None, "game_dir": None,
            "fabric": None, "version": None, "cmdline_readable": False}
    try:
        import psutil
    except Exception:
        return info

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("javaw.exe", "java.exe", "java", "javaw"):
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(cmdline)
            if "minecraft" not in joined.lower():
                continue

            info["found"] = True
            info["pid"] = proc.info.get("pid")
            info["cmdline_readable"] = bool(cmdline)

            # --gameDir is the instance's own folder; mods/ hangs off it.
            for index, part in enumerate(cmdline):
                if part == "--gameDir" and index + 1 < len(cmdline):
                    info["game_dir"] = cmdline[index + 1]
                elif part.startswith("--gameDir="):
                    info["game_dir"] = part.split("=", 1)[1]
                elif part == "--version" and index + 1 < len(cmdline):
                    info["version"] = cmdline[index + 1]

            lowered = joined.lower()
            info["fabric"] = ("fabric" in lowered or "knot" in lowered)
            return info
        except Exception:
            continue
    return info


def bundled_jar():
    jars = sorted((Path(__file__).resolve().parent.parent / "mods")
                  .glob("markliv-bridge-*.jar"))
    return jars[-1] if jars else None


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def explain_outdated(source) -> None:
    """An older mod is running. Say which of the two usual reasons it is.

    Either the new jar never reached the folder the game loads from --
    installed while Minecraft was open (Windows keeps a running game's jar
    locked), or into a different instance -- or it did, and the game has not
    been restarted since, so the old one is still what is loaded."""
    print(f"\n{RULE}\n  WHY IS THE OLD MOD STILL RUNNING?\n{RULE}")
    print(f"  Running mod reports : {source.schema()}")
    print(f"  This JARVIS expects : {SCHEMA}")
    game = inspect_running_game()
    bundled = bundled_jar()
    if not game["game_dir"] or bundled is None:
        print("\n  Quit Minecraft completely, run install_mod.bat, then start")
        print("  Minecraft again. Installing while the game is open can fail:")
        print("  Windows locks the jar the game has loaded.")
        return
    mods = Path(game["game_dir"]) / "mods"
    installed = sorted(mods.glob("markliv-bridge-*.jar"))
    print(f"  The game loads mods from: {mods}")
    current = [j for j in installed if _same_file(j, bundled)]
    if current:
        print(f"\n  The NEW jar is in that folder ({current[0].name}), but")
        print("  Minecraft has not been restarted since it was copied, so the")
        print("  old one is still loaded. Quit Minecraft completely and start")
        print("  it again.")
        return
    if installed:
        print(f"\n  The jar in that folder ({installed[0].name}) is the OLD one:")
        print("  the copy did not happen. Quit Minecraft first -- Windows")
        print("  locks it while the game runs -- then run:")
    else:
        print("\n  There is no copy of the mod in that folder at all, so the")
        print("  one running is loaded from somewhere else. Quit Minecraft,")
        print("  then run:")
    print(f"    py tools\\install_mod.py --into \"{game['game_dir']}\"")


def explain_why_not() -> None:
    """Say which of the three possible mistakes was actually made."""
    game = inspect_running_game()

    print(f"\n{RULE}\n  WHY NOT? Reading the running game\n{RULE}")

    if not game["found"]:
        print("\n  Minecraft does not appear to be running at all.")
        print("  Start it first, load a world, then run this again.")
        return

    print(f"\n  Minecraft is running (pid {game['pid']}).")

    if not game["cmdline_readable"]:
        print("  I could not read how it was started, so I cannot tell which")
        print("  folder or loader it is using.")
        return

    if game["version"]:
        print(f"  Launched profile : {game['version']}")

    if game["fabric"] is False:
        print("\n  >>> This is NOT a Fabric launch. <<<")
        print("  The mod can only load under Fabric, so it is being ignored.")
        print("  In the Minecraft Launcher, change the profile dropdown next")
        print("  to PLAY to the 'fabric-loader-26.3' entry and start again.")
        return

    print("  Fabric           : yes")

    game_dir = game["game_dir"]
    if not game_dir:
        print("\n  It did not say which game directory it uses, so I cannot")
        print("  check whether the mod landed in the right place.")
        return

    mods = Path(game_dir) / "mods"
    print(f"  Game directory   : {game_dir}")
    print(f"  Its mods folder  : {mods}")

    if not mods.is_dir():
        print("\n  >>> That folder does not exist. <<<")
        print("  This instance has no mods folder, so nothing is being loaded.")
        return

    ours = sorted(mods.glob("markliv-bridge-*.jar"))
    others = len(list(mods.glob("*.jar")))
    if ours:
        print(f"\n  The mod IS here: {ours[0].name}")
        print(f"  ({others} jar(s) in that folder in total.)")
        print("\n  So it is installed in the right place but did not start.")
        print("  Check the game's log for 'markliv-bridge' — most likely the")
        print("  Fabric loader rejected it for a version mismatch.")
    else:
        print(f"\n  >>> The mod is NOT in this instance. <<<")
        print(f"  That folder has {others} other jar(s), but not ours.")
        print("\n  This is the instance you are actually playing, so this is")
        print("  where it needs to go. Copy it there with:")
        print(f"    py tools\\install_mod.py --into \"{game_dir}\"")


def main() -> int:
    print(f"{RULE}\n  MARK LIV — is the Minecraft bridge working?\n{RULE}")
    path = state_file_path()
    print(f"\n  Looking for: {path}")

    source = ModBridgeStateSource()
    if not source.available():
        print(f"\n  NOT WORKING\n")
        for line in source.unavailable_reason().splitlines():
            print(f"  {line}")
        explain_why_not()
        return 1

    state = source.read()
    print(f"  {state.notes}\n")
    print(f"{RULE}\n  WHAT JARVIS CAN SEE\n{RULE}")

    for name in state._FIELDS:
        value = getattr(state, name)
        if value is None:
            continue
        if name == "inventory":
            print(f"  {name:<16} {len(value)} stack(s)")
            for stack in value[:8]:
                print(f"  {'':<16}   slot {stack.slot}: "
                      f"{stack.count} x {stack.name}")
            continue
        if name == "nearby_entities":
            print(f"  {name:<16} {len(value)} nearby")
            for entity in value[:6]:
                print(f"  {'':<16}   {entity.name} at "
                      f"{entity.distance:.1f} blocks")
            continue
        if name in ("surface", "notable_blocks"):
            # Several hundred of these. Printing them raw buries everything
            # else on the screen and tells you nothing you can act on.
            print(f"  {name:<16} {len(value)} block(s)")
            for block in value[:5]:
                print(f"  {'':<16}   {block.name} at "
                      f"({block.x}, {block.y}, {block.z})")
            if len(value) > 5:
                print(f"  {'':<16}   ... and {len(value) - 5} more")
            continue
        if hasattr(value, "as_dict"):
            value = value.as_dict()
        print(f"  {name:<16} {value}")

    print(f"\n{RULE}\n  IS THE MOD UP TO DATE?\n{RULE}")
    has_clearance = any(b.clearance is not None for b in (state.surface or ()))
    has_sensitivity = state.mouse_sensitivity is not None
    sees_floors = not source.outdated()
    print(f"  Head clearance reported:   {'yes' if has_clearance else 'NO'}")
    print(f"  Ground under trees:        {'yes' if sees_floors else 'NO'}")
    print(f"  Mouse sensitivity:         "
          f"{state.mouse_sensitivity if has_sensitivity else 'NOT REPORTED'}")
    print(f"  Standing on the ground:    {state.on_ground}")
    if not (has_clearance and has_sensitivity and sees_floors):
        print("\n  An OLDER jar is loaded. It still works, but without these")
        print("  JARVIS cannot tell a low branch from open ground, cannot see")
        print("  the ground under a tree (so it may find no way to one), and")
        print("  has to guess how far your mouse turns. Reinstall and restart:")
        print("    install_mod.bat")
        if not sees_floors:
            explain_outdated(source)

    print(f"\n{RULE}\n  CAN IT NAVIGATE?\n{RULE}")
    local = navigation.LocalMap.from_state(state)
    if not local.usable:
        print("\n  NO — the bridge is running but it is not reporting")
        print("  terrain. That is the OLD version of the mod.")
        print("\n  Reinstall it and restart Minecraft:")
        print("    install_mod.bat")
        print("\n  Everything else above still works; JARVIS just cannot")
        print("  walk anywhere on purpose until the terrain scan arrives.")
        return 1

    summary = navigation.summarise(state)
    print(f"\n  YES — {summary['columns_seen']} columns of ground within "
          f"{summary.get('scan_radius')} blocks.")
    for label in ("log", "stone", "water"):
        found = summary.get(f"nearest_{label}")
        if found:
            print(f"    nearest {label:<6} {found['name']} at "
                  f"{tuple(found['position'])}, {found['distance']} away")
    for label in ("hostile", "passive"):
        found = summary.get(f"nearest_{label}")
        if found:
            print(f"    nearest {label:<6} {found['name']}, "
                  f"{found['distance']} away")
    if summary.get("ores_seen"):
        print(f"    ores in view  {', '.join(summary['ores_seen'])}")
    if not summary.get("nearest_log"):
        print("    no logs in range — stand near some trees and run this")
        print("    again if you want to test 'collect some wood'.")

    exact = state.fields_at_least(EXACT)
    print(f"\n{RULE}")
    print(f"  {len(exact)} field(s) known exactly. The bridge is working.")
    print("  Try: 'JARVIS, what's around me?' then 'walk to the nearest tree'.")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    import platform
    if platform.system() == "Windows":
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
