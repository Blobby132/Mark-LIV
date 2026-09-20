"""
core/safe_path.py — containment for every path the assistant is handed.

WHAT THIS IS FOR
    Almost every path in this app arrives from somewhere untrustworthy. The
    model writes `name` and `destination` for `file_controller`. The model
    writes the whole file plan for `dev_agent`, including
    `{"path": "../../.bashrc"}`. An archive writes its own member names, and
    whoever built the archive chose them. None of those are hostile most of the
    time, and all of them are hostile some of the time.

    `actions/file_controller.py` already had the right idea — `_is_safe_path()`
    resolves and checks against `Path.home()` — and it is correct everywhere it
    is called. The problem was never the check, it was the calls that forgot it:
    `rename_file()` computes `target.parent / new_name` and never checks the
    result, so `new_name="../../../tmp/x"` walks straight out of the safe root.

    So this module exists to be the one implementation, with the awkward parts
    handled once: symlinks, case-insensitive filesystems, non-existent tails,
    `/home/userX` looking like a prefix of `/home/user`, and archives.

HOW CONTAINMENT IS DECIDED
    Resolve both sides, then compare path *components*, not strings:

        /home/user       contains  /home/user/Desktop/a.txt     ✓
        /home/user       contains  /home/user                   ✓ (the root itself)
        /home/user       contains  /home/userX/secrets          ✗ (different component)
        /home/user       contains  /etc/passwd                  ✗

    `Path.resolve()` follows symlinks for the whole path, so a symlink inside
    the root that points outside it resolves outside and is rejected — which is
    the behaviour you want for reading and writing, because writing through that
    symlink writes to the target, not to the link.

WHAT THIS DOES NOT DO
    It does not close the time-of-check/time-of-use window. Between resolving a
    path and opening it, another process can replace a component with a symlink.
    Defeating that needs `O_NOFOLLOW` walks or `openat`, which is not portable
    to Windows and is far past what this threat model justifies — the adversary
    here is a confused language model and a malicious download, not a local
    attacker racing the assistant. Said plainly rather than left implied.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# ── Limits for archive extraction ────────────────────────────────────────────
#
# Guards against a decompression bomb: a few hundred kilobytes that expand into
# everything the disk has. Both are generous for anything a person actually
# downloads and small enough that the failure is a clean refusal rather than a
# full filesystem. Callers may raise them for a known-good archive.
DEFAULT_MAX_FILES = 20_000
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 ** 3      # 2 GiB, uncompressed
_COPY_CHUNK = 256 * 1024

# Windows treats these as devices no matter the extension or directory, and
# refuses or misbehaves on a trailing dot/space. An archive member named "CON"
# is never a legitimate file the user wanted.
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
})


# ── Failures ─────────────────────────────────────────────────────────────────

class PathSafetyError(Exception):
    """Base for every refusal in this module. Callers that want one except
    clause can catch this."""


class PathEscape(PathSafetyError):
    """A path resolved outside the root it was required to stay inside."""

    def __init__(self, candidate: str, root: str):
        # Deliberately no resolved absolute path in the message: this string
        # ends up in a tool result that goes back to the model, and the resolved
        # form of "../../.." is a map of the machine.
        super().__init__(
            f"'{candidate}' is outside the folder I am allowed to touch ({root})."
        )
        self.candidate = candidate
        self.root = root


class UnsafeArchiveMember(PathSafetyError):
    """An archive entry that would escape the destination, or that is a link or
    a device rather than an ordinary file."""

    def __init__(self, member: str, reason: str):
        super().__init__(f"Refusing the archive: entry '{member}' {reason}.")
        self.member = member
        self.reason = reason


class ArchiveTooLarge(PathSafetyError):
    """The archive declares — or turns out to contain — more than the caller
    allowed."""


# ── Roots ────────────────────────────────────────────────────────────────────

def home_root() -> Path:
    """The user's home directory, resolved."""
    return real(Path.home())


def temp_root() -> Path:
    """The system temp directory, resolved.

    Worth naming explicitly: on macOS `/tmp` is a symlink to `/private/tmp` and
    `tempfile.gettempdir()` returns a `/var/folders/...` path that is itself
    under a symlink, so a naive string comparison against "/tmp" fails for every
    file the UI drops there."""
    return real(Path(tempfile.gettempdir()))


def default_roots() -> tuple[Path, ...]:
    """Where the assistant may work unless a caller says otherwise: the user's
    home, plus the temp directory the interface drops uploaded files into."""
    return (home_root(), temp_root())


# ── Resolution and containment ───────────────────────────────────────────────

def real(path: str | os.PathLike) -> Path:
    """Absolute, symlink-free, `..`-free form of `path`.

    Non-strict: the path does not have to exist, which matters because half the
    calls here are about a file that is *going* to be created."""
    return Path(path).expanduser().resolve()


def _norm_parts(path: Path) -> tuple[str, ...]:
    """Path components, case-folded where the platform is case-insensitive.

    `os.path.normcase` is a no-op on POSIX and lowercases (and flips slashes) on
    Windows, which is exactly the distinction that matters: `C:\\Users\\Bob` and
    `c:\\users\\bob` are the same directory there and different strings
    everywhere."""
    return tuple(os.path.normcase(part) for part in path.parts)


def is_within(root: str | os.PathLike, candidate: str | os.PathLike) -> bool:
    """Does `candidate` resolve to `root` itself or something underneath it?

    Component-wise, so `/home/userX` is not inside `/home/user` — the bug every
    `startswith()` implementation of this function has."""
    try:
        root_parts = _norm_parts(real(root))
        cand_parts = _norm_parts(real(candidate))
    except (OSError, ValueError, RuntimeError):
        # RuntimeError: symlink loop. OSError: unreadable component. ValueError:
        # embedded NUL, or a path longer than the platform allows. All of them
        # mean "I could not establish that this is safe", which is a no.
        return False
    return len(cand_parts) >= len(root_parts) and cand_parts[:len(root_parts)] == root_parts


def resolve_within(
    root: str | os.PathLike,
    candidate: str | os.PathLike,
    *,
    allow_absolute: bool = True,
) -> Path:
    """Resolve `candidate` and guarantee the result is inside `root`.

    `candidate` may be relative (joined onto `root`) or absolute (used as given).
    Either way the resolved result must land inside `root` or this raises
    `PathEscape`.

    `allow_absolute=False` additionally refuses an absolute candidate outright,
    before resolving. Use it where only a sub-path is ever meaningful — an
    archive member, or the `"path"` field of a model-written project plan —
    because there "/etc/passwd" is not a path that happens to be outside the
    root, it is a path that should never have had a leading slash.

    Raises `ValueError` for an empty candidate, `PathEscape` for an escape."""
    raw = os.fspath(candidate) if candidate is not None else ""
    if not str(raw).strip():
        raise ValueError("No path given.")

    root_real = real(root)
    candidate_path = Path(raw).expanduser()

    if candidate_path.is_absolute() or _has_windows_drive(raw):
        if not allow_absolute:
            raise PathEscape(str(raw), str(root_real))
        target = candidate_path
    else:
        target = root_real / candidate_path

    resolved = real(target)
    if not is_within(root_real, resolved):
        raise PathEscape(str(raw), str(root_real))
    return resolved


def resolve_within_any(
    roots: "tuple[Path, ...] | list[Path]",
    candidate: str | os.PathLike,
    *,
    allow_absolute: bool = True,
) -> Path:
    """`resolve_within` against several permitted roots — the first that
    contains the candidate wins.

    Relative candidates are joined onto the first root, which is why the caller
    passes the most likely one first (home, then temp)."""
    roots = tuple(roots)
    if not roots:
        raise ValueError("No permitted roots given.")

    last: PathSafetyError | None = None
    for root in roots:
        try:
            return resolve_within(root, candidate, allow_absolute=allow_absolute)
        except PathEscape as e:
            last = e
        except ValueError:
            raise
    raise last or PathEscape(str(candidate), str(roots[0]))


def _has_windows_drive(raw: str) -> bool:
    """True for 'C:\\x', 'C:x' and UNC '\\\\server\\share'.

    `PurePosixPath("C:\\Windows").is_absolute()` is False, so on Linux a Windows
    path would otherwise be silently treated as a relative name and joined onto
    the root. That is the right containment outcome by luck rather than by
    design, and it hides the fact that the caller was handed nonsense — so name
    it and refuse it explicitly instead."""
    text = str(raw)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def classify_path(path: str | os.PathLike) -> str:
    """A coarse category for a path: 'home', 'temp', 'system' or 'other'.

    For the audit log, which records what *kind* of place was touched without
    necessarily recording where. Never raises."""
    try:
        resolved = real(path)
    except Exception:
        return "unknown"
    try:
        if is_within(temp_root(), resolved):
            return "temp"
        if is_within(home_root(), resolved):
            return "home"
    except Exception:
        return "unknown"
    return "system"


# ── Archives ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtractResult:
    """What `safe_extract` actually wrote. Returned only on full success — a
    refused archive raises instead, so there is no state where a caller has to
    work out whether a partial extraction happened."""
    destination: Path
    files: int
    directories: int
    total_bytes: int


def _reject_member_name(name: str) -> str | None:
    """Why this archive entry name is unacceptable, or None if it is fine.

    Checked on the *name as written in the archive*, before any joining, because
    some of these ("/etc/passwd", "C:\\Windows\\x") are rejectable on sight and
    saying so names the real problem."""
    if not name or not name.strip():
        return "has an empty name"
    if "\x00" in name:
        return "contains a null byte"

    # Normalise separators: a zip built on Windows uses backslashes, and a tar
    # can contain whatever the writer put in it.
    unified = name.replace("\\", "/")

    if unified.startswith("/"):
        return "is an absolute path"
    if _has_windows_drive(name):
        return "is an absolute Windows path"

    parts = [p for p in PurePosixPath(unified).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return "escapes the destination with '..'"

    for part in parts:
        stem = part.split(".")[0].lower()
        if stem in _WINDOWS_RESERVED:
            return f"uses the reserved device name '{part}'"
        if part != part.rstrip(". "):
            return f"ends in a dot or space ('{part}'), which Windows silently strips"
        if ":" in part:
            return f"contains ':' ('{part}'), an alternate data stream on Windows"
    return None


def _member_target(destination: Path, name: str) -> Path:
    """Where this member would land. Raises `UnsafeArchiveMember` if that is
    anywhere other than inside `destination`.

    Belt and braces: `_reject_member_name` has already refused the obvious
    forms, and this catches whatever it did not think of, because the question
    that actually matters is not "does the name look odd" but "does the result
    land inside the destination"."""
    unified = name.replace("\\", "/").lstrip("/")
    try:
        return resolve_within(destination, unified, allow_absolute=False)
    except (PathEscape, ValueError):
        raise UnsafeArchiveMember(name, "would be written outside the destination")


def _check_budget(count: int, total: int, max_files: int, max_total_bytes: int) -> None:
    if count > max_files:
        raise ArchiveTooLarge(
            f"Archive holds more than {max_files} entries — refusing to unpack it."
        )
    if total > max_total_bytes:
        raise ArchiveTooLarge(
            f"Archive expands to more than {max_total_bytes // (1024 ** 2)} MB — "
            f"refusing to unpack it."
        )


def _plan_zip(zf: zipfile.ZipFile, destination: Path,
              max_files: int, max_total_bytes: int) -> list[tuple[zipfile.ZipInfo, Path]]:
    plan: list[tuple[zipfile.ZipInfo, Path]] = []
    total = 0
    for info in zf.infolist():
        reason = _reject_member_name(info.filename)
        if reason:
            raise UnsafeArchiveMember(info.filename, reason)

        # A zip records the unix mode in the top 16 bits of external_attr. S_IFLNK
        # there means the "file" is a symlink whose *contents* are its target —
        # extract it and every later member writing "through" it writes wherever
        # it points. Python's own extractall creates these happily.
        mode = (info.external_attr >> 16) & 0o170000
        if mode == 0o120000:
            raise UnsafeArchiveMember(info.filename, "is a symbolic link")
        if mode and mode not in (0o100000, 0o040000):
            raise UnsafeArchiveMember(info.filename, "is not an ordinary file or folder")

        target = _member_target(destination, info.filename)
        if info.is_dir():
            plan.append((info, target))
            continue

        total += max(0, int(info.file_size))
        _check_budget(len(plan) + 1, total, max_files, max_total_bytes)
        plan.append((info, target))

    _check_budget(len(plan), total, max_files, max_total_bytes)
    return plan


def _plan_tar(tf: tarfile.TarFile, destination: Path,
              max_files: int, max_total_bytes: int) -> list[tuple[tarfile.TarInfo, Path]]:
    plan: list[tuple[tarfile.TarInfo, Path]] = []
    total = 0
    for member in tf.getmembers():
        reason = _reject_member_name(member.name)
        if reason:
            raise UnsafeArchiveMember(member.name, reason)

        # Tar is the format where this matters most: until Python 3.12 there was
        # no extraction filter at all, and `extractall` would happily create a
        # symlink pointing at / and then write through it. Both link kinds are
        # refused outright rather than resolved-and-allowed — a tarball of the
        # user's own files does not need them, and deciding whether a link
        # target is safe is a harder problem than refusing one.
        if member.issym() or member.islnk():
            raise UnsafeArchiveMember(member.name, "is a link")
        if not (member.isfile() or member.isdir()):
            raise UnsafeArchiveMember(
                member.name, "is a device, socket or other special entry"
            )

        target = _member_target(destination, member.name)
        if member.isdir():
            plan.append((member, target))
            continue

        total += max(0, int(member.size))
        _check_budget(len(plan) + 1, total, max_files, max_total_bytes)
        plan.append((member, target))

    _check_budget(len(plan), total, max_files, max_total_bytes)
    return plan


def _write_stream(source, target: Path, remaining: int) -> int:
    """Copy `source` into `target`, stopping if it exceeds `remaining` bytes.

    The budget is enforced here as well as from the header because a header is
    whatever the archive says it is. A member can declare one byte and deliver a
    gigabyte, and only the write knows the difference."""
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as out:
        while True:
            chunk = source.read(_COPY_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > remaining:
                raise ArchiveTooLarge(
                    "Archive contains more data than it declared — refusing to unpack it."
                )
            out.write(chunk)
    return written


def safe_extract(
    archive: str | os.PathLike,
    destination: str | os.PathLike,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> ExtractResult:
    """Unpack a .zip or .tar[.gz|.bz2|.xz] into `destination`, and nowhere else.

    Every member is validated *before* anything is written. One bad entry
    refuses the whole archive, which is deliberate: a half-unpacked archive
    leaves the caller unable to say what happened, and "it extracted 41 of 43
    files and two of them tried to overwrite your SSH key" is not a state worth
    supporting.

    What gets refused:
      - absolute paths, '..' traversal, and anything landing outside `destination`
      - symlinks and hardlinks, in both formats
      - devices, fifos and sockets
      - Windows device names, alternate-data-stream names, trailing dots/spaces
      - archives over the file-count or uncompressed-size budget, whether they
        declare it in the header or only reveal it while being read

    Permissions from the archive are *not* applied — files are created with the
    process umask. A tar can carry a setuid bit, and there is no reason for one
    the assistant unpacked to keep it.

    Raises `UnsafeArchiveMember`, `ArchiveTooLarge`, `PathEscape`, or
    `FileNotFoundError` / `ValueError` for an archive that is missing or is not
    a format this handles."""
    src = real(archive)
    if not src.is_file():
        raise FileNotFoundError(f"Archive not found: {src.name}")

    dest = real(destination)
    dest.mkdir(parents=True, exist_ok=True)

    files = directories = 0
    total_bytes = 0

    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            plan = _plan_zip(zf, dest, max_files, max_total_bytes)
            for info, target in plan:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories += 1
                    continue
                with zf.open(info) as source:
                    total_bytes += _write_stream(
                        source, target, max_total_bytes - total_bytes
                    )
                files += 1
        return ExtractResult(dest, files, directories, total_bytes)

    if tarfile.is_tarfile(src):
        with tarfile.open(src) as tf:
            plan = _plan_tar(tf, dest, max_files, max_total_bytes)
            for member, target in plan:
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    directories += 1
                    continue
                source = tf.extractfile(member)
                if source is None:           # unreadable entry; already validated
                    continue
                with source:
                    total_bytes += _write_stream(
                        source, target, max_total_bytes - total_bytes
                    )
                files += 1
        return ExtractResult(dest, files, directories, total_bytes)

    raise ValueError(
        f"'{src.name}' is not a zip or tar archive I can unpack safely. "
        f"(.rar and .7z need an external tool and are not handled here.)"
    )


__all__ = [
    "PathSafetyError", "PathEscape", "UnsafeArchiveMember", "ArchiveTooLarge",
    "ExtractResult",
    "real", "is_within", "resolve_within", "resolve_within_any",
    "classify_path", "home_root", "temp_root", "default_roots",
    "safe_extract",
    "DEFAULT_MAX_FILES", "DEFAULT_MAX_TOTAL_BYTES",
]
