"""
tools/install_mod.py — put the bridge mod where Minecraft will find it.

Finding the mods folder is the kind of step that reads as trivial in
instructions and is not: it is hidden behind %APPDATA%, Explorer does not show
it by default, and every launcher puts its instances somewhere different. So
this looks, copies, and says what it did.

    python tools/install_mod.py              install where it fits
    python tools/install_mod.py --into DIR   install into one game directory
    python tools/install_mod.py --list       look, change nothing
    python tools/install_mod.py --all        every folder found (rarely right)
    python tools/install_mod.py --uninstall  remove it everywhere

ONE INSTANCE, NOT ALL OF THEM
    The first version of this copied into every mods folder it found. On a
    machine with a dozen CurseForge packs that is twelve installs, most of
    them wrong, and "wrong" here is not harmless: a Fabric jar built for 26.3
    dropped into a Forge pack or a 1.20 instance can make that instance refuse
    to start. Breaking modpacks somebody already had working is a far worse
    outcome than not finding the folder.

    So the version and loader of each instance are read first, and the mod
    goes only where it actually fits. Everything else is listed as skipped,
    with the reason, so a wrong guess is visible rather than silent.

Safe to re-run. It only ever touches its own jar.
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


TARGET_VERSION = "26.3"


def describe_instance(mods: Path) -> tuple[str, str]:
    """(minecraft version, loader) for the instance owning this mods folder.

    Best effort, and it says so: unknown values are returned as "?" and an
    unknown instance is skipped rather than guessed at, because the cost of
    guessing wrong is a modpack that will not launch."""
    instance = mods.parent
    version = loader = "?"

    # CurseForge
    meta = instance / "minecraftinstance.json"
    if meta.is_file():
        try:
            import json
            data = json.loads(meta.read_text(encoding="utf-8",
                                             errors="replace"))
            base = data.get("baseModLoader") or {}
            version = str(base.get("minecraftVersion") or "?")
            name = str(base.get("name") or "").lower()
            for known in ("fabric", "forge", "neoforge", "quilt"):
                if known in name:
                    loader = known
                    break
        except Exception:
            pass
        return version, loader

    # Prism / MultiMC
    pack = instance / "mmc-pack.json"
    if not pack.is_file():
        pack = instance.parent / "mmc-pack.json"
    if pack.is_file():
        try:
            import json
            data = json.loads(pack.read_text(encoding="utf-8",
                                             errors="replace"))
            for component in data.get("components", []):
                uid = str(component.get("uid", ""))
                if uid == "net.minecraft":
                    version = str(component.get("version") or "?")
                elif "fabric" in uid:
                    loader = "fabric"
                elif "forge" in uid:
                    loader = "forge"
                elif "quilt" in uid:
                    loader = "quilt"
        except Exception:
            pass
        return version, loader

    # The vanilla launcher's own folder. Profiles live alongside it and a
    # single directory can hold several, so the version is genuinely unknown
    # -- but this is where the Fabric installer puts things by default, which
    # makes it the one sensible default target.
    if instance.name == ".minecraft" or instance.name == "minecraft":
        return "default", "fabric?"

    return version, loader


def fits(version: str, loader: str) -> tuple[bool, str]:
    """Should the mod go here? And if not, why not."""
    if version == "default":
        return True, "the launcher's default folder"
    if version != TARGET_VERSION:
        return False, f"Minecraft {version}, not {TARGET_VERSION}"
    if loader in ("forge", "neoforge"):
        return False, f"{loader}, not Fabric"
    if loader == "?":
        return False, "could not tell which loader it uses"
    return True, f"Minecraft {version} on {loader}"


def uninstall_from(mods: Path) -> int:
    removed = 0
    for jar in mods.glob("markliv-bridge-*.jar"):
        try:
            jar.unlink()
            print(f"  Removed: {jar}")
            removed += 1
        except Exception as exc:
            print(f"  Could NOT remove {jar}: {exc}")
    return removed


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
    raw_argv = sys.argv[1:]
    argv = [a.lower() for a in raw_argv]
    listing = "--list" in argv
    every = "--all" in argv
    removing = "--uninstall" in argv

    # --into takes the guesswork out entirely: bridge_check reads the running
    # game's --gameDir and can hand back the one directory that matters.
    explicit = None
    if "--into" in argv:
        index = argv.index("--into")
        if index + 1 < len(raw_argv):
            explicit = Path(raw_argv[index + 1].strip('"'))

    print(f"{RULE}\n  MARK LIV — the Minecraft bridge mod\n{RULE}")

    jar = bundled_jar()
    if jar is None and not removing:
        print("\n  Could not find the mod jar in this download.")
        print(f"  Expected it in: {REPO / 'mods'}")
        return 1

    if explicit is not None and not removing:
        mods = explicit if explicit.name == "mods" else explicit / "mods"
        print(f"\n  Installing into the folder you named:\n    {mods}")
        ok = install_into(mods, jar)
        print(f"\n{RULE}")
        if ok:
            print("  Done. Restart Minecraft, load a world, then run:")
            print("    py tools\\bridge_check.py")
            return 0
        return 1

    found = find_mods_dirs()
    if not found:
        if removing:
            print("\n  No mods folders found; nothing to remove.")
            return 0
        print("\n  No existing mods folder found — creating the default one.")
        found = [default_mods_dir()]

    # ── uninstall ────────────────────────────────────────────────────────────
    if removing:
        print(f"\n{RULE}\n  REMOVING\n{RULE}")
        removed = sum(uninstall_from(mods) for mods in found)
        print(f"\n{RULE}")
        print(f"  Removed {removed} cop{'y' if removed == 1 else 'ies'}.")
        if removed:
            print("  Any modpack that would not start because of this should")
            print("  now launch normally again.")
        return 0

    # ── survey ───────────────────────────────────────────────────────────────
    print(f"\n  Mod to install: {jar.name} "
          f"({jar.stat().st_size // 1024} KB), for Minecraft {TARGET_VERSION}")
    print(f"\n  Found {len(found)} mods folder(s):\n")

    targets, skipped = [], []
    for mods in found:
        version, loader = describe_instance(mods)
        suitable, why = fits(version, loader)
        if suitable or every:
            targets.append(mods)
            print(f"    [use ] {mods}")
            print(f"           {why}")
        else:
            skipped.append((mods, why))
            print(f"    [skip] {mods}")
            print(f"           {why}")

    if listing:
        print(f"\n{RULE}\n  Nothing changed (--list).")
        return 0

    if not targets:
        print(f"\n{RULE}")
        print("  Nothing here matches Minecraft "
              f"{TARGET_VERSION} on Fabric.")
        print("  Install Fabric for that version, then run this again — or")
        print(f"  copy this file in by hand:\n    {jar}")
        return 1

    # ── install ──────────────────────────────────────────────────────────────
    print(f"\n{RULE}\n  INSTALLING\n{RULE}")
    installed = sum(1 for mods in targets if install_into(mods, jar))

    print(f"\n{RULE}")
    print(f"  Installed into {installed} folder(s); skipped {len(skipped)}.")
    if skipped:
        print("  The skipped ones run a different Minecraft or loader, where")
        print("  this jar could stop them launching.")
    print("\n  Next:")
    print("    1. Start Minecraft using the FABRIC profile for "
          f"{TARGET_VERSION}.")
    print("    2. Load a world.")
    print("    3. Back here, run:  py tools\\bridge_check.py")
    print("\n  Put it somewhere it should not have gone?")
    print("    py tools\\install_mod.py --uninstall")
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
