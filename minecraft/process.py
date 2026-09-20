"""
minecraft/process.py — is Minecraft running, and which process is it?

DOES NOT LAUNCH ANYTHING
    There is no start function here and there will not be one this phase. The
    prototype attaches to a game the user already opened, which removes a whole
    class of question — no launcher path, no account handling, no "did it
    install" — and means the user is at the keyboard when control begins.

HOW MINECRAFT IS RECOGNISED
    Minecraft Java is a JVM process, so the process name is `javaw.exe`,
    `java.exe` or `java`, which is also true of every other Java program on the
    machine. Name alone is not enough, so the command line is checked for the
    markers a Minecraft launch always carries — `net.minecraft.client.main.Main`,
    `--assetIndex`, `--gameDir`, a `versions/` path.

    That is a heuristic. It is written to prefer a false negative ("I cannot
    find Minecraft") over a false positive ("I will send W to your IDE"), and
    `describe()` says which marker matched so a wrong answer is diagnosable
    rather than mysterious.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    import psutil
    _PSUTIL = True
except Exception:                                     # pragma: no cover
    _PSUTIL = False

# Process names worth looking at. The JVM ones need a command-line check;
# the native launchers and Bedrock-ish names are conclusive on their own.
_JVM_NAMES = frozenset({"javaw.exe", "java.exe", "java", "javaw"})
_DIRECT_NAMES = frozenset({"minecraft.exe", "minecraftlauncher.exe"})

# Markers that a JVM command line is a Minecraft client. Checked case-folded.
_CLIENT_MARKERS = (
    "net.minecraft.client.main.main",
    "--assetindex",
    "--gamedir",
    "--versiontype",
    "net.fabricmc.loader.impl.launch.knot.knotclient",
    "cpw.mods.bootstraplauncher",
)
# A weaker marker: only trusted alongside one of the above, or on its own when
# nothing stronger is present, because plenty of Java programs mention a path.
_PATH_MARKERS = ("/.minecraft/", "\\.minecraft\\", "/versions/", "\\versions\\")


@dataclass(frozen=True)
class ProcessInfo:
    """What was found. `running` False with a populated `detail` is the useful
    failure: it says what was looked for and what was seen."""

    running: bool
    pid: int | None = None
    name: str = ""
    matched_on: str = ""
    detail: str = ""
    psutil_available: bool = True

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "name": self.name,
            "matched_on": self.matched_on,
            "detail": self.detail,
        }


def _cmdline_of(proc) -> str:
    try:
        parts = proc.cmdline() or []
    except Exception:
        return ""
    try:
        return " ".join(str(p) for p in parts).lower()
    except Exception:
        return ""


def _classify(name: str, cmdline: str) -> str:
    """Which marker, if any, makes this process Minecraft. '' for none."""
    lowered = name.lower()
    if lowered in _DIRECT_NAMES:
        return f"process name '{name}'"
    if lowered not in _JVM_NAMES:
        return ""
    for marker in _CLIENT_MARKERS:
        if marker in cmdline:
            return f"command line contains '{marker}'"
    for marker in _PATH_MARKERS:
        if marker in cmdline:
            return f"command line contains '{marker.strip()}'"
    return ""


def find() -> ProcessInfo:
    """Look for a running Minecraft client. Never raises.

    When several match — a launcher and a game, or two instances — the one with
    the strongest marker wins, and the lowest pid breaks a tie so repeated calls
    give the same answer rather than flapping between two windows."""
    if not _PSUTIL:
        return ProcessInfo(
            running=False, psutil_available=False,
            detail="psutil is not installed, so I cannot see the process list. "
                   "Run: pip install psutil",
        )

    candidates: list[tuple[int, int, str, str]] = []   # (rank, pid, name, marker)
    java_seen = 0
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").strip()
                if not name:
                    continue
                lowered = name.lower()
                if lowered in _JVM_NAMES:
                    java_seen += 1
                if lowered not in _JVM_NAMES and lowered not in _DIRECT_NAMES:
                    continue
                marker = _classify(name, _cmdline_of(proc))
                if not marker:
                    continue
                # Prefer a client marker over a mere path match, and a real
                # game process over the launcher.
                rank = 0 if "main" in marker or "assetindex" in marker else 1
                if "launcher" in lowered:
                    rank += 2
                candidates.append((rank, int(proc.info["pid"]), name, marker))
            except Exception:
                continue
    except Exception as e:
        return ProcessInfo(running=False,
                           detail=f"Could not read the process list ({type(e).__name__}).")

    if not candidates:
        if java_seen:
            return ProcessInfo(
                running=False,
                detail=f"Found {java_seen} Java process(es) but none looked like "
                       f"a Minecraft client. Is the game actually open, rather "
                       f"than just the launcher?",
            )
        return ProcessInfo(running=False,
                           detail="No Minecraft process is running.")

    candidates.sort()
    _rank, pid, name, marker = candidates[0]
    return ProcessInfo(running=True, pid=pid, name=name, matched_on=marker,
                       detail=f"Minecraft looks like pid {pid} ({name}).")


def is_alive(pid: int | None) -> bool:
    """Is this exact pid still running? Cheap enough to call on every tick.

    Used by the controller's guard, so it has to be fast and it has to be
    certain: a stale True here is the difference between releasing W and leaving
    it held."""
    if pid is None:
        return False
    if not _PSUTIL:
        # Without psutil, fall back to the OS. signal 0 asks "does this pid
        # exist and may I signal it" without sending anything.
        try:
            os.kill(int(pid), 0)
            return True
        except (ProcessLookupError, ValueError, TypeError):
            return False
        except PermissionError:
            return True                  # exists, owned by someone else
        except Exception:
            return False
    try:
        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def describe() -> str:
    """One human sentence, for `status` and the manual check."""
    info = find()
    if info.running:
        return f"{info.detail} Matched on {info.matched_on}."
    return info.detail


__all__ = ["ProcessInfo", "find", "is_alive", "describe"]
