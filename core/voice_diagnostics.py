"""
core/voice_diagnostics.py — where did the utterance go?

THE PROBLEM THIS EXISTS FOR
    "The sound bars moved and JARVIS did nothing." The bars are driven by the
    microphone callback, which runs long before anything reaches the model, so
    they prove exactly one thing: the microphone is working. Between there and
    a reply sit a bounded queue, a socket, the server's own decision about
    whether you were even talking to it, and a turn boundary — and until now a
    frame could vanish at any of them without leaving a trace.

    So this counts. Every stage of

        captured -> gated -> queued -> sent -> transcribed -> turn -> answered

    has a number, and the numbers are cheap enough to keep always-on: integers
    behind one lock, updated per 64ms audio frame.

WHY COUNTERS AND NOT LOGGING
    Logging every frame is useless at 16 frames a second and drowns the thing
    you are looking for. What answers "where did it go" is the SHAPE of the
    counters: captured climbing while sent stays flat is a send problem;
    both climbing with no transcript is the server; a transcript with no turn
    is turn detection; a turn with no reply is the model choosing silence.

    One line at the end of an utterance tells you which. That is the whole
    design.

NOT A GATE
    Nothing here decides whether audio flows. It observes. A failure inside
    this module must never cost the user a word, so every public method is
    safe to call from the audio callback and swallows its own errors.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# How long after locally-detected speech we still expect the server to have
# transcribed something. Generous: the server needs the end-of-speech silence
# window (around half a second by default) plus a round trip, and calling a
# slow network a lost utterance would make this cry wolf.
LOST_INPUT_AFTER_S = 6.0

# Sound louder than this counts as "someone said something" for the watchdog.
# Deliberately well above room tone: the watchdog's job is to notice that a
# CLEAR utterance produced nothing, not to police every rustle.
SPEECH_LEVEL = 0.06

# Consecutive loud frames before the watchdog believes it. At 64ms a frame,
# four is about a quarter of a second — longer than a keystroke or a cough.
SPEECH_FRAMES = 4

LOST_INPUT = "VOICE_PIPELINE_LOST_INPUT"
HEARD_IGNORED = "VOICE_HEARD_BUT_UNANSWERED"

# How long after a finished turn to wait for the model to say or do something.
# A turn that produced a transcript and then nothing at all is the fingerprint
# of proactive audio deciding the words were not aimed at JARVIS.
UNANSWERED_AFTER_S = 8.0


@dataclass
class _Utterance:
    """One stretch of local speech, and what became of it."""
    started_at: float
    frames: int = 0
    queued: int = 0
    dropped: int = 0
    sent_at_start: int = 0
    transcript: str = ""
    resolved: bool = False


@dataclass
class VoiceDiagnostics:
    """Counters for one run of the assistant.

    Every counter is monotonic within a process. Rates and deltas are the
    caller's business; what matters here is that nothing is ever quietly
    forgotten."""

    # Microphone thread.
    captured: int = 0             # frames the device handed us
    gated_asleep: int = 0         # dropped: wake word not spoken
    gated_speaking: int = 0       # dropped: JARVIS was talking
    gated_echo: int = 0           # dropped: our own voice in the tail
    gated_ptt: int = 0            # dropped: push-to-talk not held
    gated_muted: int = 0          # dropped: muted, or the phone has the mic
    queued: int = 0               # handed to the send queue
    dropped_queue_full: int = 0   # THE silent one, until now
    callback_errors: int = 0

    # Send task.
    sent: int = 0
    send_errors: int = 0
    queue_depth: int = 0
    queue_high_water: int = 0

    # Server.
    input_transcripts: int = 0
    turns_complete: int = 0
    responses: int = 0
    tool_calls: int = 0
    reconnects: int = 0

    # Watchdog.
    lost_utterances: int = 0
    unanswered_turns: int = 0
    last_loss: str = ""
    last_user_speech_at: float = 0.0
    last_transcript_at: float = 0.0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _loud_run: int = field(default=0, repr=False)
    _open: _Utterance | None = field(default=None, repr=False)
    _awaiting: tuple | None = field(default=None, repr=False)
    _clock: object = field(default=time.monotonic, repr=False)

    # ── microphone thread ────────────────────────────────────────────────

    def frame_captured(self, level: float = 0.0) -> None:
        """One block arrived from the device. Called before every gate."""
        try:
            with self._lock:
                self.captured += 1
                self._note_level(level)
        except Exception:
            pass

    def frame_gated(self, why: str) -> None:
        """A frame was deliberately not sent. `why` names which gate."""
        field_name = {
            "asleep": "gated_asleep", "speaking": "gated_speaking",
            "echo": "gated_echo", "ptt": "gated_ptt", "muted": "gated_muted",
        }.get(why)
        if field_name is None:
            return
        try:
            with self._lock:
                setattr(self, field_name, getattr(self, field_name) + 1)
        except Exception:
            pass

    def frame_queued(self) -> None:
        try:
            with self._lock:
                self.queued += 1
                if self._open is not None:
                    self._open.queued += 1
        except Exception:
            pass

    def frame_dropped(self) -> None:
        """The queue was full. This used to be invisible, which is why an
        utterance could vanish while the level meter kept bouncing."""
        try:
            with self._lock:
                self.dropped_queue_full += 1
                if self._open is not None:
                    self._open.dropped += 1
        except Exception:
            pass

    def callback_error(self) -> None:
        try:
            with self._lock:
                self.callback_errors += 1
        except Exception:
            pass

    def _note_level(self, level: float) -> None:
        """Track a run of loud frames as one utterance. Caller holds the lock."""
        if level >= SPEECH_LEVEL:
            self._loud_run += 1
            if self._loud_run == SPEECH_FRAMES and self._open is None:
                self.last_user_speech_at = self._clock()
                self._open = _Utterance(started_at=self.last_user_speech_at,
                                        sent_at_start=self.sent)
            if self._open is not None:
                self._open.frames += 1
        else:
            self._loud_run = 0

    # ── send task ────────────────────────────────────────────────────────

    def frame_sent(self, depth: int = 0) -> None:
        try:
            with self._lock:
                self.sent += 1
                self.queue_depth = depth
                if depth > self.queue_high_water:
                    self.queue_high_water = depth
        except Exception:
            pass

    def send_error(self) -> None:
        try:
            with self._lock:
                self.send_errors += 1
        except Exception:
            pass

    # ── server ───────────────────────────────────────────────────────────

    def input_transcript(self, text: str = "") -> None:
        try:
            with self._lock:
                self.input_transcripts += 1
                self.last_transcript_at = self._clock()
                if self._open is not None:
                    self._open.transcript = text
                    self._open.resolved = True
                    self._open = None
                self._loud_run = 0
        except Exception:
            pass

    def turn_complete(self, transcript: str = "") -> None:
        """A turn ended. If it carried words, start the clock on an answer.

        A transcript means the server DID hear you and DID decide your turn
        was over. If nothing follows, the words were understood and then set
        aside — which is a different failure from losing the audio, and the
        only one that survives turning proactive audio off."""
        try:
            with self._lock:
                self.turns_complete += 1
                if transcript:
                    self._awaiting = (self._clock(), transcript,
                                      self.responses, self.tool_calls)
                else:
                    self._awaiting = None
        except Exception:
            pass

    def response(self) -> None:
        try:
            with self._lock:
                self.responses += 1
                self._awaiting = None
        except Exception:
            pass

    def tool_call(self) -> None:
        try:
            with self._lock:
                self.tool_calls += 1
                self._awaiting = None
        except Exception:
            pass

    def reconnected(self) -> None:
        try:
            with self._lock:
                self.reconnects += 1
                # A reconnect invalidates any utterance in flight: whatever was
                # queued for the old socket is not arriving. Closing it here
                # keeps the watchdog from blaming the server for a socket that
                # went away.
                self._open = None
                self._loud_run = 0
        except Exception:
            pass

    # ── watchdog ─────────────────────────────────────────────────────────

    def check_unanswered(self) -> str:
        """Did the server transcribe something and then do nothing?

        Reports once per turn and never retries: re-sending a command the
        model may already be acting on is how one "move forward" becomes
        two."""
        try:
            with self._lock:
                pending = self._awaiting
                if pending is None:
                    return ""
                at, text, responses, tools = pending
                if self._clock() - at < UNANSWERED_AFTER_S:
                    return ""
                self._awaiting = None
                if self.responses > responses or self.tool_calls > tools:
                    return ""
                self.unanswered_turns += 1
                message = (
                    f"{HEARD_IGNORED}: Gemini transcribed \"{text[:80]}\" and "
                    f"then neither answered nor called a tool. The audio was "
                    f"fine — this is the model deciding the words were not "
                    f"addressed to it. If \"proactive_audio\" is true in "
                    f"config/api_keys.json, set it to false; that judgement is "
                    f"exactly what it enables.")
                self.last_loss = message
                return message
        except Exception:
            return ""

    def check_for_loss(self) -> str:
        """Has a clearly-spoken utterance produced nothing at all?

        Returns a one-line explanation naming the stage it died at, or "".
        Called on a timer, not per frame."""
        try:
            with self._lock:
                open_one = self._open
                if open_one is None:
                    return ""
                waited = self._clock() - open_one.started_at
                if waited < LOST_INPUT_AFTER_S:
                    return ""
                self._open = None
                self._loud_run = 0
                self.lost_utterances += 1
                message = self._explain(open_one, waited)
                self.last_loss = message
                return message
        except Exception:
            return ""

    def _explain(self, utterance: _Utterance, waited: float) -> str:
        """Name the stage. Caller holds the lock.

        The counters make this unambiguous, which is the point of keeping
        them: each stage has a different fix and they all look the same from
        the outside."""
        sent_during = self.sent - utterance.sent_at_start
        head = (f"{LOST_INPUT}: {waited:.0f}s after you spoke "
                f"({utterance.frames} frames) there is still no transcript")

        if utterance.dropped:
            return (f"{head} — {utterance.dropped} frames were DROPPED because "
                    f"the send queue was full. The audio never left this "
                    f"machine. Check the connection and whether the sender is "
                    f"keeping up (queue high-water {self.queue_high_water}).")
        if utterance.queued == 0:
            return (f"{head} — none of it was queued at all, so a gate "
                    f"swallowed it: asleep {self.gated_asleep}, "
                    f"JARVIS speaking {self.gated_speaking}, "
                    f"echo tail {self.gated_echo}, "
                    f"push-to-talk {self.gated_ptt}, "
                    f"muted {self.gated_muted}.")
        if sent_during == 0:
            return (f"{head} — {utterance.queued} frames were queued but NONE "
                    f"were sent. The send loop is stuck or the socket is gone "
                    f"({self.send_errors} send errors, {self.reconnects} "
                    f"reconnects).")
        return (f"{head} — {sent_during} frames reached Gemini and it returned "
                f"no transcription. The audio arrived; the server did not turn "
                f"it into a turn. If this repeats, proactive audio may be "
                f"judging your speech as not addressed to JARVIS — turn it off "
                f"in config/api_keys.json with \"proactive_audio\": false.")

    # ── reporting ────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "captured": self.captured,
                "queued": self.queued,
                "dropped_queue_full": self.dropped_queue_full,
                "sent": self.sent,
                "queue_depth": self.queue_depth,
                "queue_high_water": self.queue_high_water,
                "gated": {
                    "asleep": self.gated_asleep,
                    "jarvis_speaking": self.gated_speaking,
                    "echo_tail": self.gated_echo,
                    "push_to_talk": self.gated_ptt,
                    "muted": self.gated_muted,
                },
                "input_transcripts": self.input_transcripts,
                "turns_complete": self.turns_complete,
                "responses": self.responses,
                "tool_calls": self.tool_calls,
                "reconnects": self.reconnects,
                "send_errors": self.send_errors,
                "callback_errors": self.callback_errors,
                "lost_utterances": self.lost_utterances,
                "unanswered_turns": self.unanswered_turns,
                "last_loss": self.last_loss,
            }

    def describe(self) -> str:
        """One line a person can read, in pipeline order."""
        s = self.snapshot()
        gated = sum(s["gated"].values())
        return (f"mic {s['captured']} captured / {gated} gated / "
                f"{s['queued']} queued / {s['dropped_queue_full']} DROPPED / "
                f"{s['sent']} sent  ->  gemini {s['input_transcripts']} "
                f"transcripts / {s['turns_complete']} turns / "
                f"{s['responses']} responses / {s['tool_calls']} tool calls"
                + (f"  [{s['lost_utterances']} lost]"
                   if s["lost_utterances"] else "")
                + (f"  [{s['unanswered_turns']} heard-but-unanswered]"
                   if s["unanswered_turns"] else ""))


__all__ = ["VoiceDiagnostics", "LOST_INPUT", "HEARD_IGNORED",
           "LOST_INPUT_AFTER_S", "UNANSWERED_AFTER_S",
           "SPEECH_LEVEL", "SPEECH_FRAMES"]
