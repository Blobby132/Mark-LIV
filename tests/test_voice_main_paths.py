"""
Tests for the voice-path methods added to main.py.

WHY THEY LOAD main.py THIS WAY
    main.py pulls in sounddevice, the Gemini SDK, the Qt UI and numpy at
    import, and nothing in the test environment can provide an audio device.
    So these tests do not import it. They parse main.py, lift out the exact
    methods under test by name, and bind them to a small stand-in object.

    What is tested is therefore the real source text of those methods — not a
    copy — and a change to them is a change to what these tests exercise.
    What is NOT tested is anything about the live socket, the real audio
    device or Gemini: those need the app running.
"""

from __future__ import annotations

import ast
import asyncio
import sys
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.voice_diagnostics import VoiceDiagnostics                  # noqa: E402

MAIN = ROOT / "main.py"


def _lift(method_names):
    """Compile the named methods of main.py's classes into plain functions."""
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in method_names:
                    found[item.name] = item
    missing = set(method_names) - set(found)
    if missing:
        raise AssertionError(f"main.py no longer defines {sorted(missing)}")
    module = ast.Module(body=list(found.values()), type_ignores=[])
    namespace = {"asyncio": asyncio, "time": time,
                 "get_proactive_audio_enabled": lambda: False}
    exec(compile(module, str(MAIN), "exec"), namespace)
    return {name: namespace[name] for name in method_names}


METHODS = _lift(["_enqueue_audio", "_report_voice_state", "_on_text_command"])


class FakeUI:
    def __init__(self):
        self.lines = []
        self.muted = False

    def write_log(self, line):
        self.lines.append(line)


def assistant(queue_size=3):
    fake = types.SimpleNamespace()
    fake._voice = VoiceDiagnostics()
    fake.out_queue = asyncio.Queue(maxsize=queue_size)
    fake.ui = FakeUI()
    fake._speaking_lock = threading.Lock()
    fake._is_speaking = False
    fake._tail_until = 0.0
    fake._wake_enabled = False
    fake._awake = True
    fake._ptt_enabled = False
    fake._ptt_held = False
    fake._phone_active = False
    fake._enhanced_live = True
    fake._loop = None
    fake.session = None
    fake._tail_active = lambda: time.monotonic() < fake._tail_until
    for name, function in METHODS.items():
        setattr(fake, name, types.MethodType(function, fake))
    return fake


class EnqueueTests(unittest.TestCase):
    """The silent loss: a full queue used to drop the NEWEST frame, with the
    exception raised inside a loop callback where nobody saw it."""

    def test_a_frame_that_fits_is_queued_and_counted(self):
        a = assistant()
        a._enqueue_audio({"data": b"1"})
        self.assertEqual(a.out_queue.qsize(), 1)
        self.assertEqual(a._voice.snapshot()["queued"], 1)

    def test_a_full_queue_drops_the_oldest_and_says_so(self):
        a = assistant(queue_size=3)
        for i in range(5):
            a._enqueue_audio({"data": bytes([i])})
        kept = [a.out_queue.get_nowait()["data"] for _ in range(3)]
        self.assertEqual(kept, [b"\x02", b"\x03", b"\x04"],
                         "it kept the backlog and threw away what was just said")
        self.assertEqual(a._voice.snapshot()["dropped_queue_full"], 2)

    def test_no_queue_is_a_counted_drop_not_a_crash(self):
        a = assistant()
        a.out_queue = None
        a._enqueue_audio({"data": b"1"})
        self.assertEqual(a._voice.snapshot()["dropped_queue_full"], 1)


class VoiceCheckTests(unittest.TestCase):

    def test_typing_voice_check_answers_locally(self):
        """It must not be sent to Gemini: the question is about the pipe the
        model sits at the far end of."""
        a = assistant()
        sent = []
        a._loop = object()
        a.session = types.SimpleNamespace(send_client_content=sent.append)
        a._on_text_command("voice check")
        self.assertEqual(sent, [])
        text = "\n".join(a.ui.lines)
        self.assertIn("captured", text)
        self.assertIn("proactive audio", text)

    def test_it_names_the_gates_that_are_closed(self):
        a = assistant()
        a._is_speaking = True
        a._tail_until = time.monotonic() + 5
        a.ui.muted = True
        a._report_voice_state()
        text = "\n".join(a.ui.lines)
        self.assertIn("JARVIS_SPEAKING", text)
        self.assertIn("TAIL_ACTIVE", text)
        self.assertIn("MUTED", text)

    def test_it_reports_the_last_problem(self):
        a = assistant()
        a._voice.last_loss = "VOICE_PIPELINE_LOST_INPUT: example"
        a._report_voice_state()
        self.assertIn("last problem", "\n".join(a.ui.lines))


class SourceGuardTests(unittest.TestCase):
    """The play and send loops are too entangled with the audio device and
    the socket to run here. These check the guards exist in the source."""

    def test_the_play_loop_can_clear_a_stuck_speaking_flag(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn("_SPEAKING_STUCK_S", source)
        self.assertIn("reopening the microphone", source)

    def test_the_sender_survives_a_failed_frame(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        sender = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "_send_realtime")
        handlers = [n for n in ast.walk(sender)
                    if isinstance(n, ast.ExceptHandler)]
        self.assertTrue(handlers, "one bad frame would take the sender down")

    def test_nothing_enqueues_audio_except_through_the_counted_path(self):
        """`call_soon_threadsafe(out_queue.put_nowait, ...)` for the PC mic,
        and a `put_nowait` wrapped in `except QueueFull: pass` for the phone,
        were both silent losses. Only _enqueue_audio may touch the queue."""
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        offenders = []
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if fn.name == "_enqueue_audio":
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Attribute)
                            and node.attr == "put_nowait"
                            and isinstance(node.value, ast.Attribute)
                            and node.value.attr == "out_queue"):
                        offenders.append(f"{fn.name}:{node.lineno}")
        self.assertEqual(offenders, [], "audio enqueued outside the counted path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
