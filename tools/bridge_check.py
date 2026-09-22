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

from minecraft.mod_bridge import ModBridgeStateSource, state_file_path  # noqa: E402
from minecraft.state import EXACT                                       # noqa: E402

RULE = "─" * 72


def main() -> int:
    print(f"{RULE}\n  MARK LIV — is the Minecraft bridge working?\n{RULE}")
    path = state_file_path()
    print(f"\n  Looking for: {path}")

    source = ModBridgeStateSource()
    if not source.available():
        print(f"\n  NOT WORKING\n")
        for line in source.unavailable_reason().splitlines():
            print(f"  {line}")
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
        if hasattr(value, "as_dict"):
            value = value.as_dict()
        print(f"  {name:<16} {value}")

    exact = state.fields_at_least(EXACT)
    print(f"\n{RULE}")
    print(f"  {len(exact)} field(s) known exactly. The bridge is working.")
    print("  'JARVIS, collect some wood' can now count what it collected,")
    print("  rather than guessing from blocks that disappeared.")
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
