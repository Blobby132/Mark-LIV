"""
tools/install_mod.py — put the bridge mod where Minecraft will find it.

Finding the mods folder is the kind of step that reads as trivial in
instructions and is not: it is hidden behind %APPDATA%, Explorer does not show
it by default, and every launcher puts its instances somewhere different. So
this looks, copies, and says what it did.

    python tools/install_mod.py

Safe to re-run. It overwrites its own jar and touches nothing else.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
RULE = "─" * 72


def bundled_jar() -> Path | None:
    jars = sorted((REPO / "mods").glob("markliv-bridge-*.jar"))
    return jars[-1] if jars else None


def candidate_game_dirs() -> list[Path]:
    """Every place a Minecraft installation might live.

    Ordered with the vanilla launcher first, because that is what most people
    have and what the Fabric installer targets by default. Third-party
    launchers keep per-instance mods folders, so those are found by looking
    for the instances directory and letting the caller pick."""
    out: list[Path] = []

    def add(path: Path | None) -> None:
        if path and path not in out:
            out.append(path)

    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            add(Path(appdata) / ".minecraft")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # Microsoft Store Java edition, and Prism/MultiMC defaults.
            add(Path(local) / "Packages" / "Microsoft.4297127D64EC6"
                / "LocalCache" / "Local" / "runtime")
            add(Path(local) / "Programs" / "PrismLauncher")
    elif platform.system() == "Darwin":
        add(Path.home() / "Library" / "Application Support" / "minecraft")
    else:
        add(Path.home() / ".minecraft")

    add(Path.home() / ".minecraft")
    add(Path.home() / "curseforge" / "minecraft" / "Instances")
    add(Path.home() / "Documents" / "Curse" / "Minecraft" / "Instances")
    return out


def find_mods_dirs() -> list[Path]:
    """Existing mods folders, plus the default one whether it exists or not."""
    found: list[Path] = []
    for game_dir in candidate_game_dirs():
        if not game_dir.exists():
            continue
        mods = game_dir / "mods"
        if mods.is_dir():
            found.append(mods)
        # Launchers with per-instance folders.
        for instance in sorted(game_dir.glob("*/mods"))[:12]:
            if instance.is_dir() and instance not in found:
                found.append(instance)
        for instance in sorted(game_dir.glob("*/.minecraft/mods"))[:12]:
            if instance.is_dir() and instance not in found:
                found.append(instance)
    return found


def default_mods_dir() -> Path:
    for game_dir in candidate_game_dirs():
        if game_dir.exists():
            return game_dir / "mods"
    return candidate_game_dirs()[0] / "mods"


def install_into(mods: Path, jar: Path) -> bool:
    try:
        mods.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"  Could not create {mods}: {exc}")
        return False

    # Remove older copies so two versions cannot both load.
    for stale in mods.glob("markliv-bridge-*.jar"):
        if stale.name != jar.name:
            try:
                stale.unlink()
                print(f"  Removed older copy: {stale.name}")
            except Exception:
                pass

    target = mods / jar.name
    try:
        shutil.copy2(jar, target)
    except Exception as exc:
        print(f"  Could not copy into {mods}: {exc}")
        return False

    ok = target.is_file() and target.stat().st_size == jar.stat().st_size
    print(f"  {'Installed' if ok else 'FAILED'}: {target}")
    return ok


def main() -> int:
    print(f"{RULE}\n  MARK LIV — install the Minecraft bridge mod\n{RULE}")

    jar = bundled_jar()
    if jar is None:
        print("\n  Could not find the mod jar in this download.")
        print(f"  Expected it in: {REPO / 'mods'}")
        return 1
    print(f"\n  Mod to install: {jar.name} ({jar.stat().st_size // 1024} KB)")

    found = find_mods_dirs()
    if found:
        print(f"\n  Found {len(found)} mods folder(s):")
        for mods in found:
            count = len(list(mods.glob('*.jar')))
            print(f"    {mods}   ({count} mod(s) already there)")
    else:
        print("\n  No existing mods folder found — creating the default one.")
        found = [default_mods_dir()]

    print(f"\n{RULE}\n  INSTALLING\n{RULE}")
    installed = sum(1 for mods in found if install_into(mods, jar))

    print(f"\n{RULE}")
    if not installed:
        print("  Nothing was installed. The paths above are where I looked.")
        print("  If your Minecraft lives somewhere else, copy this file:")
        print(f"    {jar}")
        print("  into that installation's 'mods' folder yourself.")
        return 1

    print(f"  Installed into {installed} folder(s).")
    print("\n  Next:")
    print("    1. Start Minecraft using the FABRIC profile (not vanilla).")
    print("    2. Load a world.")
    print("    3. Back here, run:  py tools\\bridge_check.py")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 1
    if platform.system() == "Windows":
        try:
            input("\nPress Enter to close...")
        except EOFError:
            pass
    sys.exit(code)
