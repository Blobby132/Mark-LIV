"""
The receive loop: tool calls off it, interrupts that cannot swallow a reply,
and the Live API events it now answers.

WHY main.py IS LOADED THIS WAY
    Same reason as tests/test_voice_main_paths.py: main.py imports the audio
    stack, Qt and the Gemini SDK at module level and cannot be imported here.
    The methods under test are lifted out of the real source by name and
    bound to a small stand-in, so what runs is the shipped code, not a copy.

    What this cannot cover: a real socket, the real server's timing, real
    audio. Those need the app running against Gemini.

What these pin down:

  * a tool call no longer holds up the messages behind it -- a transcript
    arriving while a long tool runs is processed at once;
  * the result of a withdrawn call is never sent, and a call that belongs to
    Minecraft releases its keys when withdrawn;
  * a result is never sent to a session other than the one that asked;
  * an interrupt after a reply has fully arrived does not discard the next
    reply (the bug), while one during a reply still discards the rest of it;
  * server `interrupted`, `generation_complete`, `turn_complete_reason`,
    `waiting_for_input` and `go_away` do what the API says they mean;
  * a spoken "stop" reaches a running Minecraft task directly, once.
"""

from __future__ import annotations

import ast
import asyncio
import re
import sys
import threading
import time
import traceback
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import interrupts                                         # noqa: E402
from core.voice_diagnostics import VoiceDiagnostics                 # noqa: E402

MAIN = ROOT / "main.py"

METHODS = [
    "_handle_server_message", "_log_finished_turn", "_maybe_voice_stop",
    "_stop_running_actions", "_start_tool_calls", "_run_tool_calls",
    "_on_tool_call_cancellation", "_on_server_interrupted", "_on_go_away",
    "_reconnect_when_idle", "interrupt", "_interrupt_now", "speak_when_idle",
]
MODULE_FUNCTIONS = ["_is_repeat_chunk", "_clean_transcript", "_seconds_of"]
MODULE_CONSTANTS = ["_REPEAT_MIN", "_CTRL_RE", "_GO_AWAY_DEFAULT_S",
                    "_SPEAK_IDLE_WAIT_S"]


class FakeTypes:
    """Stands in for google.genai.types.FunctionResponse."""

    class FunctionResponse:
        def __init__(self, id=None, name=None, response=None, **extra):
            self.id, self.name, self.response = id, name, response
            self.extra = extra


def _lift():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    wanted, found = set(METHODS), {}
    module_nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in MODULE_FUNCTIONS:
            module_nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in MODULE_CONSTANTS
                for t in node.targets):
            module_nodes.append(node)
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and item.name in wanted:
                    found[item.name] = item
    missing = wanted - set(found)
    if missing:
        raise AssertionError(f"main.py no longer defines {sorted(missing)}")
    namespace = {"asyncio": asyncio, "time": time, "re": re,
                 "datetime": datetime, "traceback": traceback,
                 "threading": threading, "types": FakeTypes,
                 "_interrupts": interrupts}
    module = ast.Module(body=module_nodes + list(found.values()),
                        type_ignores=[])
    exec(compile(module, str(MAIN), "exec"), namespace)
    return namespace


NS = _lift()


class FakeUI:
    def __init__(self):
        self.lines = []
        self.muted = False
        self.states = []

    def write_log(self, line):
        self.lines.append(line)

    def set_state(self, state):
        self.states.append(state)

    def stop_camera_stream(self):
        pass


class FakeVisemes:
    def feed_text(self, _text):
        pass

    def reset(self):
        pass


class FakeSession:
    def __init__(self):
        self.tool_responses = []
        self.client_content = []

    async def send_tool_response(self, function_responses):
        self.tool_responses.append(list(function_responses))

    async def send_client_content(self, turns, turn_complete=True):
        self.client_content.append(turns)


def assistant():
    a = types.SimpleNamespace()
    a._voice = VoiceDiagnostics()
    a.ui = FakeUI()
    a._visemes = FakeVisemes()
    a.session = FakeSession()
    a._loop = None
    a._resume_handle = None
    a._interrupted = False
    a._gen_active = False
    a._discard_turn_log = False
    a._tool_tasks = {}
    a._cancelled_calls = set()
    a._go_away_task = None
    a._in_buf, a._out_buf = [], []
    a._stop_scan_from = 0
    a._last_out_logged = ""
    a._session_log = []
    a._dashboard = None
    a._asst_name = "JARVIS"
    a._voice_debug = False
    a._vision_close_pending = False
    a._vision_busy = False
    a._last_user_speech = 0.0
    a._speaking_lock = threading.Lock()
    a._is_speaking = False
    a._tail_until = 0.0
    a._play_cursor = 0.0
    a._turn_done_event = None
    a.audio_in_queue = None
    a.reconnects = []
    a.executed = []
    a.speaking_calls = []

    a._tail_active = lambda: time.monotonic() < a._tail_until

    def set_speaking(value):
        a.speaking_calls.append(value)
        a._is_speaking = value
    a.set_speaking = set_speaking

    def request_reconnect(keep_context=True, reason=""):
        a.reconnects.append((keep_context, reason))
    a.request_reconnect = request_reconnect

    async def _flush_pending_vision():
        return False
    a._flush_pending_vision = _flush_pending_vision

    async def _execute_tool(fc):
        a.executed.append(fc.name)
        gate = getattr(fc, "gate", None)
        if gate is not None:
            await gate.wait()
        return FakeTypes.FunctionResponse(id=fc.id, name=fc.name,
                                          response={"result": "ok"})
    a._execute_tool = _execute_tool

    for name in METHODS:
        setattr(a, name, types.MethodType(NS[name], a))
    return a


# ── messages, shaped like google.genai.types.LiveServerMessage ──────────────

def msg(data=None, server_content=None, tool_call=None, go_away=None,
        tool_call_cancellation=None):
    return types.SimpleNamespace(
        data=data, server_content=server_content, tool_call=tool_call,
        go_away=go_away, tool_call_cancellation=tool_call_cancellation,
        session_resumption_update=None)


def content(**fields):
    base = dict(output_transcription=None, input_transcription=None,
                turn_complete=False, generation_complete=False,
                interrupted=False, waiting_for_input=False,
                turn_complete_reason=None)
    base.update(fields)
    return types.SimpleNamespace(**base)


def said(text):
    return content(input_transcription=types.SimpleNamespace(text=text))


def spoke(text):
    return content(output_transcription=types.SimpleNamespace(text=text))


def call(name, call_id, gate=None):
    return types.SimpleNamespace(name=name, id=call_id, args={}, gate=gate)


def tool_call(*calls):
    return types.SimpleNamespace(function_calls=list(calls))


AUDIO = b"\x01\x00" * 2400


def in_loop(coroutine_fn):
    """Run a test body inside an event loop -- the receive loop's home."""
    def wrapper(self):
        return asyncio.run(coroutine_fn(self))
    wrapper.__name__ = coroutine_fn.__name__
    wrapper.__doc__ = coroutine_fn.__doc__
    return wrapper


class ToolCallsDoNotBlockTests(unittest.TestCase):

    @in_loop
    async def test_a_transcript_behind_a_long_tool_is_handled_at_once(self):
        a = assistant()
        gate = asyncio.Event()
        a._handle_server_message(msg(tool_call=tool_call(
            call("minecraft_control", "c1", gate))))
        await asyncio.sleep(0)            # let the tool task start
        self.assertEqual(a.executed, ["minecraft_control"])

        # The tool is still running. The next message must not wait for it.
        a._handle_server_message(msg(server_content=said("jarvis stop")))
        self.assertEqual(a._in_buf, ["jarvis stop"],
                         "the transcript waited behind the tool call")
        self.assertEqual(a.session.tool_responses, [])

        gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(len(a.session.tool_responses), 1)
        self.assertEqual(a.session.tool_responses[0][0].id, "c1")
        self.assertEqual(a._tool_tasks, {}, "finished calls must be forgotten")

    @in_loop
    async def test_a_withdrawn_call_is_never_answered(self):
        a = assistant()
        gate = asyncio.Event()
        a._handle_server_message(msg(tool_call=tool_call(
            call("web_search", "c2", gate))))
        await asyncio.sleep(0)
        a._handle_server_message(msg(tool_call_cancellation=types.SimpleNamespace(
            ids=["c2"])))
        gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(a.session.tool_responses, [])
        self.assertEqual(a._voice.snapshot()["tool_call_cancellations"], 1)
        self.assertIn("withdrew the web_search", "\n".join(a.ui.lines))

    @in_loop
    async def test_a_withdrawn_minecraft_call_releases_its_keys(self):
        a = assistant()
        cancelled = []
        interrupts.register("probe_mc", lambda: True, cancelled.append,
                            tools=("minecraft_control",))
        try:
            gate = asyncio.Event()
            a._handle_server_message(msg(tool_call=tool_call(
                call("minecraft_control", "c3", gate))))
            await asyncio.sleep(0)
            a._handle_server_message(msg(
                tool_call_cancellation=types.SimpleNamespace(ids=["c3"])))
            self.assertEqual(len(cancelled), 1)
            self.assertIn("released every key", "\n".join(a.ui.lines))
            gate.set()
            await asyncio.sleep(0.05)
            self.assertEqual(a.session.tool_responses, [])
        finally:
            interrupts.unregister("probe_mc")

    @in_loop
    async def test_a_result_is_not_sent_to_a_different_session(self):
        a = assistant()
        old = a.session
        gate = asyncio.Event()
        a._handle_server_message(msg(tool_call=tool_call(
            call("web_search", "c4", gate))))
        await asyncio.sleep(0)
        a.session = FakeSession()          # reconnected meanwhile
        gate.set()
        await asyncio.sleep(0.05)
        self.assertEqual(old.tool_responses, [])
        self.assertEqual(a.session.tool_responses, [])


class InterruptRaceTests(unittest.TestCase):
    """The bug: an interrupt after the reply had fully arrived left the discard
    flag up, and the NEXT reply vanished -- audio and transcript both."""

    def setUp(self):
        self.a = assistant()
        self.a.audio_in_queue = asyncio.Queue()
        self.a._turn_done_event = asyncio.Event()

    def reply(self, text, finish=True):
        self.a._handle_server_message(msg(data=AUDIO))
        self.a._handle_server_message(msg(server_content=spoke(text)))
        if finish:
            self.a._handle_server_message(msg(server_content=content(
                generation_complete=True)))
            self.a._handle_server_message(msg(server_content=content(
                turn_complete=True)))

    @in_loop
    async def test_interrupt_after_the_reply_arrived_does_not_eat_the_next(self):
        self.reply("here is the first answer in full")
        self.a._interrupt_now()          # still playing locally
        self.assertFalse(self.a._interrupted,
                         "nothing is left to arrive, so nothing to discard")

        self.reply("and this is the second answer")
        self.assertGreater(self.a.audio_in_queue.qsize(), 0,
                           "the next reply's audio was discarded")
        self.assertIn("JARVIS: and this is the second answer",
                      self.a.ui.lines)

    @in_loop
    async def test_interrupt_during_a_reply_discards_only_its_remainder(self):
        self.a._handle_server_message(msg(data=AUDIO))
        self.a._interrupt_now()
        self.assertTrue(self.a._interrupted)
        drained = self.a.audio_in_queue.qsize()
        self.assertEqual(drained, 0)

        # The rest of the interrupted reply arrives and is dropped...
        self.a._handle_server_message(msg(data=AUDIO))
        self.assertEqual(self.a.audio_in_queue.qsize(), 0)
        self.a._handle_server_message(msg(server_content=content(
            turn_complete=True)))
        self.assertNotIn("JARVIS:", "\n".join(self.a.ui.lines))

        # ...and the reply after it is heard.
        self.reply("a fresh answer that must be heard")
        self.assertGreater(self.a.audio_in_queue.qsize(), 0)
        self.assertIn("JARVIS: a fresh answer that must be heard",
                      self.a.ui.lines)

    @in_loop
    async def test_interrupt_from_another_thread_is_handed_to_the_loop(self):
        loop = asyncio.get_running_loop()
        self.a._loop = loop
        self.a._handle_server_message(msg(data=AUDIO))
        worker = threading.Thread(target=self.a.interrupt)
        worker.start()
        worker.join(2)
        self.assertFalse(self.a._interrupted,
                         "it ran on the UI thread instead of the loop")
        await asyncio.sleep(0.01)
        self.assertTrue(self.a._interrupted)


class ServerEventTests(unittest.TestCase):

    def setUp(self):
        self.a = assistant()
        self.a.audio_in_queue = asyncio.Queue()
        self.a._turn_done_event = asyncio.Event()

    @in_loop
    async def test_server_interrupted_empties_playback_and_discards_nothing_later(self):
        self.a._handle_server_message(msg(data=AUDIO))
        self.a._handle_server_message(msg(server_content=content(
            interrupted=True)))
        self.assertEqual(self.a.audio_in_queue.qsize(), 0)
        self.assertFalse(self.a._interrupted)
        self.assertFalse(self.a._gen_active)
        self.assertIn(False, self.a.speaking_calls)
        self.assertEqual(self.a._voice.snapshot()["server_interruptions"], 1)
        # The API sends turn_complete next; the reply after that is heard.
        self.a._handle_server_message(msg(server_content=content(
            turn_complete=True)))
        self.a._handle_server_message(msg(data=AUDIO))
        self.assertGreater(self.a.audio_in_queue.qsize(), 0)

    @in_loop
    async def test_generation_complete_ends_the_reply(self):
        self.a._handle_server_message(msg(data=AUDIO))
        self.a._handle_server_message(msg(data=AUDIO))
        self.assertEqual(self.a._voice.snapshot()["responses"], 1,
                         "a reply was counted once per audio chunk")
        self.a._handle_server_message(msg(server_content=content(
            generation_complete=True)))
        self.assertFalse(self.a._gen_active)
        self.assertEqual(self.a._voice.snapshot()["responses_completed"], 1)
        # When the microphone reopens is unchanged: still turn_complete, so
        # the echo guard's timing does not move.
        self.assertFalse(self.a._turn_done_event.is_set())
        self.a._handle_server_message(msg(server_content=content(
            turn_complete=True)))
        self.assertTrue(self.a._turn_done_event.is_set())

    @in_loop
    async def test_a_tool_call_ends_the_generation_that_asked_for_it(self):
        self.a._handle_server_message(msg(data=AUDIO))
        self.a._handle_server_message(msg(tool_call=tool_call(
            call("status_tool", "c9"))))
        self.assertFalse(self.a._gen_active)
        self.assertTrue(self.a._turn_done_event.is_set())
        await asyncio.sleep(0.02)

    @in_loop
    async def test_turn_complete_reason_is_recorded(self):
        self.a._handle_server_message(msg(server_content=said("hello there")))
        reason = types.SimpleNamespace(name="RESPONSE_REJECTED")
        self.a._handle_server_message(msg(server_content=content(
            turn_complete=True, turn_complete_reason=reason)))
        self.assertEqual(self.a._voice.snapshot()["last_turn_reason"],
                         "RESPONSE_REJECTED")
        self.assertTrue(any("RESPONSE_REJECTED" in line
                            for line in self.a._voice.timeline()))

    @in_loop
    async def test_waiting_for_input_is_on_the_timeline(self):
        self.a._handle_server_message(msg(server_content=content(
            waiting_for_input=True)))
        self.assertTrue(any("waiting_for_input" in line
                            for line in self.a._voice.timeline()))

    @in_loop
    async def test_go_away_reconnects_at_the_next_pause_keeping_context(self):
        self.a._handle_server_message(msg(go_away=types.SimpleNamespace(
            time_left="3s")))
        await asyncio.wait_for(self.a._go_away_task, 3)
        self.assertEqual(len(self.a.reconnects), 1)
        keep, _reason = self.a.reconnects[0]
        self.assertTrue(keep, "the conversation must be kept")
        self.assertEqual(self.a._voice.snapshot()["go_aways"], 1)

    @in_loop
    async def test_go_away_waits_for_a_reply_to_finish(self):
        with self.a._speaking_lock:
            self.a._is_speaking = True
        self.a._handle_server_message(msg(go_away=types.SimpleNamespace(
            time_left="5s")))
        await asyncio.sleep(0.3)
        self.assertEqual(self.a.reconnects, [], "it cut JARVIS off mid-reply")
        with self.a._speaking_lock:
            self.a._is_speaking = False
        await asyncio.wait_for(self.a._go_away_task, 3)
        self.assertEqual(len(self.a.reconnects), 1)

    @in_loop
    async def test_go_away_does_nothing_if_the_session_already_went(self):
        self.a._handle_server_message(msg(go_away=types.SimpleNamespace(
            time_left="2s")))
        self.a.session = FakeSession()
        await asyncio.wait_for(self.a._go_away_task, 3)
        self.assertEqual(self.a.reconnects, [])

    def test_durations_are_read_in_every_shape_the_api_uses(self):
        from datetime import timedelta
        seconds_of = NS["_seconds_of"]
        self.assertEqual(seconds_of("10s"), 10.0)
        self.assertEqual(seconds_of("9.5s"), 9.5)
        self.assertEqual(seconds_of(4), 4.0)
        self.assertEqual(seconds_of(timedelta(seconds=7)), 7.0)
        self.assertIsNone(seconds_of(None))
        self.assertIsNone(seconds_of("soon"))


class VoiceStopTests(unittest.TestCase):

    def setUp(self):
        self.a = assistant()
        self.cancelled = []
        self.active = True
        interrupts.register("probe_stop", lambda: self.active,
                            self.cancelled.append)

    def tearDown(self):
        interrupts.unregister("probe_stop")

    def hear(self, text):
        self.a._handle_server_message(msg(server_content=said(text)))

    def test_a_spoken_stop_cancels_the_running_task_once(self):
        self.hear("jarvis")
        self.hear("stop")
        self.hear("right now")
        self.assertEqual(len(self.cancelled), 1)
        self.assertIn("you said stop", self.cancelled[0])
        self.assertIn("Heard stop", "\n".join(self.a.ui.lines))

    def test_a_second_stop_in_the_same_turn_fires_again(self):
        self.hear("stop")
        self.hear("I said stop")
        self.assertEqual(len(self.cancelled), 2)

    def test_stop_in_ordinary_conversation_reaches_nothing(self):
        self.active = False
        self.hear("stop talking about the weather")
        self.assertEqual(self.cancelled, [])

    def test_words_that_merely_contain_stop_do_not_count(self):
        self.hear("that was unstoppable")
        self.assertEqual(self.cancelled, [])


class SpeakWhenIdleTests(unittest.TestCase):

    @in_loop
    async def test_a_background_report_waits_for_jarvis_to_finish(self):
        a = assistant()
        a._loop = asyncio.get_running_loop()
        with a._speaking_lock:
            a._is_speaking = True
        await asyncio.to_thread(a.speak_when_idle, "[Minecraft task] done")
        await asyncio.sleep(0.3)
        self.assertEqual(a.session.client_content, [],
                         "it talked over JARVIS")
        with a._speaking_lock:
            a._is_speaking = False
        await asyncio.sleep(0.3)
        self.assertEqual(len(a.session.client_content), 1)
        self.assertIn("[Minecraft task] done",
                      a.session.client_content[0]["parts"][0]["text"])


class QueueAccountingTests(unittest.TestCase):
    """Every frame handed to the enqueue path is accounted for exactly once:
    queued, and -- when the queue was full -- one older frame counted as
    dropped. The level meter plays no part in any of it."""

    def test_the_counts_reconcile_with_what_is_in_the_queue(self):
        from tests.test_voice_main_paths import METHODS as MAIN_PATHS
        a = types.SimpleNamespace(_voice=VoiceDiagnostics(),
                                  out_queue=asyncio.Queue(maxsize=5))
        a._enqueue_audio = types.MethodType(MAIN_PATHS["_enqueue_audio"], a)
        for i in range(12):
            a._enqueue_audio({"data": bytes([i])})
        s = a._voice.snapshot()
        self.assertEqual(s["queued"], 12)
        self.assertEqual(s["dropped_queue_full"], 12 - 5)
        self.assertEqual(a.out_queue.qsize(), 5)
        self.assertIn("dropped=7", a._voice.line())
        self.assertIn("queued=12", a._voice.line())


if __name__ == "__main__":
    unittest.main(verbosity=2)
