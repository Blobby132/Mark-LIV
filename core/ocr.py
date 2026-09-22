"""
core/ocr.py — optional text extraction from an image.

WHY THIS IS IN core/ AND NOT IN minecraft/

    `minecraft/` is forbidden from starting processes. That is not a style
    rule: it is asserted by `tests/test_minecraft_boundary.py`, which parses
    every module in the package and fails the build on an import of
    `subprocess` — so that "JARVIS can play Minecraft" can never quietly come
    to mean "JARVIS can run programs".

    pytesseract runs the Tesseract binary in a subprocess. Importing it from
    inside `minecraft/` would put a process spawn back into the one package
    built to have none, and the honest way to allow that would be to weaken
    the boundary test — which is the opposite of the point.

    So the OCR bridge lives here instead. `minecraft/debug_overlay.py` declares
    a `TextReader` interface, imports no OCR library at all, and is handed a
    reader by the adapter. The Minecraft package keeps its bright line; the
    subprocess stays in core, where the rest of the app's process rules apply.

WHAT THIS DOES AND DOES NOT ALLOW
    The argv is fixed by pytesseract: the Tesseract program, a temporary image
    file it writes itself, and its own flags. No part of it is reachable from
    a model, a prompt, or a Minecraft frame — the only influence anything
    upstream has is over the PIXELS in the image, which become a bitmap on
    disk and never a command.

    That is a narrower thing than "core may run processes", and it is worth
    being precise about, because the reason the boundary exists is that
    "probably safe" compounds badly across a codebase.

OPTIONAL, ALWAYS
    Tesseract is a separate program, not a pip package. Requiring it would
    make every MARK LIV install carry a native dependency for one feature in
    one subsystem. So it is optional: when it is missing, `create_reader()`
    returns a reader that is honestly unavailable and says what to install,
    and nothing else in the app changes.

A BETTER READER, LATER
    General-purpose OCR is the wrong tool for this job and is used only because
    it is the one available without a fresh dependency. Minecraft renders the
    F3 overlay in a FIXED bitmap font at a known scale, so matching glyphs
    against a sampled atlas would be more accurate than Tesseract, need no
    install, and spawn no process — PIL and numpy are already dependencies.
    Building that needs glyph samples from a real capture, which is why it is
    not here yet.
"""

from __future__ import annotations

import io
import os
import sys

# Where the Windows installer actually puts tesseract.exe.
#
# PATH is the documented way to find it and the least reliable one: the
# UB-Mannheim installer does not always offer to set it, setting it needs the
# terminal reopened, and a user who did everything right still gets "not on
# PATH" with no clue which step failed. Every one of those ends up as "OCR is
# broken" in a bug report.
#
# So PATH is tried first, and these are checked when it fails. Fixed absolute
# paths, not a search: nothing here takes a user-supplied string, and no
# directory is scanned.
_WINDOWS_INSTALL_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def _local_install_paths() -> tuple:
    """Per-user install locations, which depend on the account."""
    out = []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("USERPROFILE")):
        if not base:
            continue
        out.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
        out.append(os.path.join(base, "Programs", "Tesseract-OCR",
                                "tesseract.exe"))
    return tuple(out)


TESSERACT_ENV_VARS = ("TESSERACT_CMD", "TESSERACT_EXE", "TESSERACT_PATH")
"""Point one of these at tesseract.exe to skip the search entirely.

The last resort that always works. Install layouts vary more than any fixed
list can anticipate -- winget, Chocolatey, a portable unzip and a custom
install folder all land somewhere different -- and telling a user "it is not
installed" when their package manager says it is means the list was wrong,
not the user."""


def _env_override() -> str:
    for name in TESSERACT_ENV_VARS:
        value = (os.environ.get(name) or "").strip().strip('"')
        if value and os.path.isfile(value):
            return value
    return ""


def _search_locations() -> tuple:
    """Everywhere that is checked, in order. Exposed so the failure message
    can LIST them rather than assert a conclusion it cannot support."""
    return _WINDOWS_INSTALL_PATHS + _local_install_paths()


def _find_tesseract_exe() -> str:
    """An installed tesseract.exe that PATH did not expose, or ''."""
    override = _env_override()
    if override:
        return override
    for candidate in _search_locations():
        try:
            if candidate and os.path.isfile(candidate):
                return candidate
        except Exception:
            continue
    return ""

# Threshold above which a pixel is treated as text rather than world. The F3
# overlay is drawn in near-white; everything dimmer is the game behind it.
TEXT_THRESHOLD = 180

# Upscaling before thresholding. F3 text is small, and Tesseract is markedly
# better on larger glyphs.
DEFAULT_SCALE = 2


class UnavailableReader:
    """No OCR engine. Returns nothing, and says what would fix it."""

    name = "unavailable"
    available = False

    def __init__(self, reason: str):
        self._reason = reason

    def read_text(self, frame: bytes) -> str:
        return ""

    def describe(self) -> str:
        return self._reason


class TesseractReader:                                 # pragma: no cover
    """Text from image bytes, via pytesseract.

    The preprocessing matters more than the engine. Hard-thresholding turns
    white-on-anything into black-on-white, which is the case Tesseract is
    actually good at; without it, accuracy on a bright Minecraft scene is
    poor enough to be useless."""

    name = "tesseract"
    available = True
    version = "unknown"

    def __init__(self, engine, image_module, scale: int = DEFAULT_SCALE):
        self._engine = engine
        self._Image = image_module
        self._scale = max(1, int(scale))

    def read_text(self, frame: bytes) -> str:
        if not frame:
            return ""
        image = self._Image.open(io.BytesIO(frame)).convert("L")
        if self._scale > 1:
            image = image.resize(
                (image.width * self._scale, image.height * self._scale),
                self._Image.LANCZOS)
        image = image.point(lambda p: 255 if p > TEXT_THRESHOLD else 0)
        return self._engine.image_to_string(image) or ""

    def describe(self) -> str:
        return f"OCR via Tesseract {self.version}."


_TESSERACT_DOWNLOAD = "https://github.com/UB-Mannheim/tesseract/wiki"

_NOT_WORKING_TAIL = (
    "Without it I can still see the screen and control the game; I just "
    "cannot read the numbers off it."
)


def _missing_package_help(name: str, detail: str) -> str:
    """Name the ONE thing that is missing, and the interpreter it is missing
    from.

    Listing both install steps whenever anything was absent was the same
    unhelpfulness this codebase keeps fixing elsewhere: it cannot tell "you
    have not installed the package" from "you installed it into a different
    Python", and those have completely different fixes. Printing
    sys.executable makes the second one visible instead of invisible, which
    matters because `pip install X` and `py script.py` routinely resolve to
    different interpreters on Windows."""
    return (
        f"Reading the F3 overlay needs the '{name}' package, which this "
        f"Python cannot import ({detail}).\n"
        f"  Interpreter: {sys.executable}\n"
        f"  Install it into THAT interpreter with:\n"
        f'    "{sys.executable}" -m pip install {name}\n'
        f"  (Using plain `pip install` can install into a different Python, "
        f"which looks exactly like not installing it at all.)\n"
        f"  You will also need the Tesseract PROGRAM, separately, from "
        f"{_TESSERACT_DOWNLOAD}\n"
        f"{_NOT_WORKING_TAIL}"
    )


def create_reader(scale: int = DEFAULT_SCALE):
    """The best available text reader, or one that explains its absence.

    Imports are inside the function on purpose: at module level they would
    make importing `core` probe for an OCR engine, and would put pytesseract
    into the import graph of everything that touches core."""
    # Imported separately so the failure names the package that is actually
    # missing. Together, a missing Pillow reported as "install pytesseract".
    try:
        import pytesseract
    except Exception as exc:
        return UnavailableReader(
            _missing_package_help("pytesseract",
                                  f"{type(exc).__name__}: {exc}"))

    try:
        from PIL import Image
    except Exception as exc:
        return UnavailableReader(
            _missing_package_help("pillow", f"{type(exc).__name__}: {exc}"))

    try:
        version = pytesseract.get_tesseract_version()
    except Exception:
        # Not on PATH. Look where the installer puts it before giving up —
        # "installed correctly but PATH was never set" is the single most
        # common way this fails, and it is entirely fixable from here.
        found = _find_tesseract_exe()
        if found:
            try:
                pytesseract.pytesseract.tesseract_cmd = found
                version = pytesseract.get_tesseract_version()
            except Exception as exc:
                return UnavailableReader(
                    f"Found Tesseract at {found} but could not run it "
                    f"({type(exc).__name__}: {exc}).\n{_NOT_WORKING_TAIL}")
        else:
            return UnavailableReader(_tesseract_missing_help())

    reader = TesseractReader(pytesseract, Image, scale=scale)
    reader.version = str(version)
    return reader


def _tesseract_missing_help() -> str:
    """Say what was actually checked, not what is concluded.

    The previous wording announced that Tesseract "is not installed" on the
    strength of a fixed list of paths. When a user's package manager says it
    IS installed, that sentence is simply wrong, and it sends them to
    reinstall something they already have. All that is really known is where
    this looked."""
    looked = "\n".join(f"      {path}" for path in _search_locations())
    return (
        "The 'pytesseract' package is installed, but I could not find the "
        "Tesseract PROGRAM it drives.\n"
        "  Not on PATH, and not at any of these:\n"
        f"{looked}\n"
        "  If it IS installed somewhere else, point me straight at it — no "
        "reinstall, no PATH changes:\n"
        '    setx TESSERACT_CMD "C:\\path\\to\\tesseract.exe"\n'
        "    (then close and reopen the terminal)\n"
        "  To find it:\n"
        "    Get-ChildItem C:\\ -Recurse -Filter tesseract.exe "
        "-ErrorAction SilentlyContinue | "
        "Select-Object -First 1 -ExpandProperty FullName\n"
        "  If it is genuinely not installed:\n"
        "    winget install --id UB-Mannheim.TesseractOCR\n"
        f"    or the 64-bit installer from {_TESSERACT_DOWNLOAD}\n"
        f"{_NOT_WORKING_TAIL}"
    )


def is_available() -> bool:
    return bool(getattr(create_reader(), "available", False))


__all__ = ["create_reader", "is_available", "TesseractReader",
           "UnavailableReader", "TEXT_THRESHOLD", "DEFAULT_SCALE",
           "TESSERACT_ENV_VARS"]
