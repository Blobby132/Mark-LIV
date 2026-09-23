"""
core/interrupts.py — "stop" for things that are running, without the model.

WHY THIS EXISTS
    Saying "stop" to JARVIS used to depend on Gemini hearing it, deciding it
    was addressed to it, and choosing to call the right tool -- the same chain
    of judgements that is sometimes the reason JARVIS hears you and does
    nothing. For an assistant that is holding keys in a game, that is the
    wrong thing for "stop" to depend on.

    So anything long-running registers here, and the voice path cancels it
    directly when the words "stop", "halt", "freeze", "cancel" or "abort" come
    back from the server's transcription of what you said. The model is still
    told, and still gets to answer; it just is not the thing standing between
    you and the keys coming up.

WHAT IT CAN DO
    Only cancel. Nothing registered here can be started, resumed or granted
    anything through this module, so a misheard word can at worst stop
    something -- the harmless direction. A transcript containing "don't stop"
    will stop it; that is accepted, and deliberate.

    It is also not the emergency stop. F12 does not pass through here, does
    not depend on the microphone, and remains the hard stop.

WHAT IT IMPORTS
    Nothing that acts. Entries are callables the owner hands in; this module
    only decides whether to call them.
"""

from __future__ import annotations

import re
import threading

STOP_WORDS = ("stop", "halt", "freeze", "cancel", "abort")
"""Whole words, in the transcript Gemini returns. English only: the server
transcribes in the language spoken, and guessing other languages' words for
"stop" here would be a list that is wrong more often than it helps."""

_STOP_RE = re.compile(r"\b(" + "|".join(STOP_WORDS) + r")\b", re.IGNORECASE)

_lock = threading.Lock()
_entries: dict = {}


def register(key: str, is_active, cancel, tools=()) -> None:
    """Make something cancellable by voice.

    `is_active()` says whether there is anything to stop right now -- "stop"
    in ordinary conversation must not reach anything. `cancel(reason)` stops
    it and must not block. `tools` names the tool calls that belong to it, so
    a tool call the server withdraws can be cancelled by name."""
    with _lock:
        _entries[str(key)] = (is_active, cancel, tuple(tools or ()))


def unregister(key: str) -> None:
    with _lock:
        _entries.pop(str(key), None)


def is_stop_request(text: str) -> bool:
    """Does this transcript contain a stop word, as a whole word?"""
    return bool(_STOP_RE.search(str(text or "")))


def _snapshot() -> list:
    with _lock:
        return list(_entries.items())


def _active(is_active) -> bool:
    try:
        return bool(is_active())
    except Exception:
        return False


def any_active() -> bool:
    return any(_active(is_active) for _key, (is_active, _c, _t)
               in _snapshot())


def active_keys() -> list:
    return [key for key, (is_active, _c, _t) in _snapshot()
            if _active(is_active)]


def cancel_active(reason: str) -> list:
    """Cancel everything that is running. Returns the keys cancelled."""
    cancelled = []
    for key, (is_active, cancel, _tools) in _snapshot():
        if not _active(is_active):
            continue
        try:
            cancel(reason)
            cancelled.append(key)
        except Exception:
            pass
    return cancelled


def cancel_for_tool(tool_name: str, reason: str) -> list:
    """Cancel whatever belongs to `tool_name`, active or not -- the server
    withdrawing a call is reason enough to make sure nothing is held."""
    cancelled = []
    for key, (_is_active, cancel, tools) in _snapshot():
        if tool_name not in tools:
            continue
        try:
            cancel(reason)
            cancelled.append(key)
        except Exception:
            pass
    return cancelled


__all__ = ["STOP_WORDS", "register", "unregister", "is_stop_request",
           "any_active", "active_keys", "cancel_active", "cancel_for_tool"]
