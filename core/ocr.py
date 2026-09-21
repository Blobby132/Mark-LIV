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
import sys

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
    except Exception as exc:
        return UnavailableReader(
            f"The 'pytesseract' package is installed, but the Tesseract "
            f"PROGRAM it drives is not on PATH ({type(exc).__name__}).\n"
            f"  Install it from {_TESSERACT_DOWNLOAD} — the 64-bit .exe, "
            f"ticking 'Add to PATH' — then close and REOPEN your terminal so "
            f"PATH is picked up.\n"
            f"  If it is already installed, its folder (usually "
            f"C:\\Program Files\\Tesseract-OCR) is missing from PATH.\n"
            f"{_NOT_WORKING_TAIL}"
        )

    reader = TesseractReader(pytesseract, Image, scale=scale)
    reader.version = str(version)
    return reader


def is_available() -> bool:
    return bool(getattr(create_reader(), "available", False))


__all__ = ["create_reader", "is_available", "TesseractReader",
           "UnavailableReader", "TEXT_THRESHOLD", "DEFAULT_SCALE"]
