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

# How many notable events the timeline keeps. Events are stage transitions --
# speech detected, transcript, turn, reply, tool, reconnect -- never frames, so
# forty is minutes of history, which is what "what happened to what I just
# said" needs.
EVENT_LOG_SIZE = 40

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
    last_turn_reason: str = ""
    # A RESPONSE is one generation: counted once when its first audio or
    # words arrive, not once per audio chunk. Counting chunks made "responses"
    # a measure of how long JARVIS talked rather than whether it answered.
    responses: int = 0
    responses_completed: int = 0
    generations_complete: int = 0
    server_interruptions: int = 0
    tool_calls: int = 0
    tool_calls_finished: int = 0
    tool_call_cancellations: int = 0
    go_aways: int = 0
    receive_errors: int = 0
    errors: int = 0
    last_error: str = ""

    # Sessions. `sessions` counts every connect; `reconnects` every connect
    # after the first, whatever caused it -- a fresh one as much as a resumed
    # one, because audio spoken during either gap went nowhere.
    sessions: int = 0
    reconnects: int = 0
    session_ends: int = 0
    last_session_end_reason: str = ""
    proactive_audio: bool | None = None   # as configured on the live session

    # Watchdog.
    lost_utterances: int = 0
    unanswered_turns: int = 0
    last_loss: str = ""
    last_user_speech_at: float = 0.0
    last_transcript_at: float = 0.0

    # When each stage last happened (monotonic seconds, 0.0 = never). The
    # counters say HOW MANY; these say WHERE the flow stopped: a recent
    # "queued" next to a stale "sent" is a stuck sender, whatever the totals.
    last_captured_at: float = 0.0
    last_queued_at: float = 0.0
    last_dropped_at: float = 0.0
    last_sent_at: float = 0.0
    last_turn_at: float = 0.0
    last_response_at: float = 0.0
    last_response_done_at: float = 0.0
    last_tool_call_at: float = 0.0
    last_session_at: float = 0.0
    last_error_at: float = 0.0

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _loud_run: int = field(default=0, repr=False)
    _open: _Utterance | None = field(default=None, repr=False)
    _awaiting: tuple | None = field(default=None, repr=False)
    _clock: object = field(default=time.monotonic, repr=False)
    _in_response: bool = field(default=False, repr=False)
    _turn_answered: bool = field(default=False, repr=False)
    # Bounded by _event(); a plain list so this module imports nothing but
    # threading, time and dataclasses (see the no-retry test).
    _events: list = field(default_factory=list, repr=False)

    def _event(self, entry: tuple) -> None:
        """Append to the timeline, keeping only the newest entries. Caller
        holds the lock."""
        self._events.append(entry)
        if len(self._events) > EVENT_LOG_SIZE:
            del self._events[:len(self._events) - EVENT_LOG_SIZE]

    # ── microphone thread ────────────────────────────────────────────────

    def frame_captured(self, level: float = 0.0) -> None:
        """One block arrived from the device. Called before every gate."""
        try:
            with self._lock:
                self.captured += 1
                self.last_captured_at = self._clock()
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
                self.last_queued_at = self._clock()
                if self._open is not None:
                    self._open.queued += 1
        except Exception:
            pass

    def frame_dropped(self) -> None:
        """The queue was full. This used to be invisible, which is why an
        utterance could vanish while the level meter kept bouncing.

        A burst of drops is ONE timeline entry with a count, not one per
        frame: at sixteen frames a second a stalled sender would otherwise
        push everything else out of the timeline within seconds."""
        try:
            with self._lock:
                self.dropped_queue_full += 1
                self.last_dropped_at = self._clock()
                if self._open is not None:
                    self._open.dropped += 1
                last = self._events[-1] if self._events else None
                if last is not None and last[1] == "dropped":
                    self._events[-1] = (last[0], "dropped",
                                        str(int(last[2]) + 1))
                else:
                    self._event((self._clock(), "dropped", "1"))
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
                # Measured at the microphone, BEFORE every gate -- the same
                # place the HUD meter is drawn from. It says someone spoke,
                # never that anything was sent; the entries after it say that.
                self._event((self.last_user_speech_at,
                                     "speech_at_mic", ""))
            if self._open is not None:
                self._open.frames += 1
        else:
            self._loud_run = 0

    # ── send task ────────────────────────────────────────────────────────

    def frame_sent(self, depth: int = 0) -> None:
        try:
            with self._lock:
                self.sent += 1
                self.last_sent_at = self._clock()
                self.queue_depth = depth
                if depth > self.queue_high_water:
                    self.queue_high_water = depth
        except Exception:
            pass

    def send_error(self) -> None:
        try:
            with self._lock:
                self.send_errors += 1
                if self.send_errors == 1 or self.send_errors % 50 == 0:
                    self._event((self._clock(), "send_error",
                                         str(self.send_errors)))
        except Exception:
            pass

    # ── server ───────────────────────────────────────────────────────────

    def input_transcript(self, text: str = "") -> None:
        """Words the server heard. Streams in pieces; each piece counts, and
        the timeline shows the first piece of each utterance."""
        try:
            with self._lock:
                self.input_transcripts += 1
                self.last_transcript_at = self._clock()
                last = self._events[-1] if self._events else None
                if last is not None and last[1] == "transcript":
                    self._events[-1] = (last[0], "transcript",
                                        (last[2] + " " + str(text))[-80:])
                else:
                    self._event((self._clock(), "transcript",
                                         str(text)[:80]))
                if self._open is not None:
                    self._open.transcript = text
                    self._open.resolved = True
                    self._open = None
                self._loud_run = 0
        except Exception:
            pass

    def turn_complete(self, transcript: str = "", reason: str = "") -> None:
        """A turn ended. If it carried words, start the clock on an answer.

        A transcript means the server DID hear you and DID decide your turn
        was over. If nothing follows, the words were understood and then set
        aside — which is a different failure from losing the audio, and the
        only one that survives turning proactive audio off."""
        try:
            with self._lock:
                now = self._clock()
                self.turns_complete += 1
                self.last_turn_at = now
                # The reply to a turn normally arrives BEFORE the server marks
                # the turn complete -- audio first, turn_complete last. So a
                # turn that already carried a reply or a tool call is answered
                # now, not something to wait on; judging it by what arrives
                # afterwards reported every ordinary answer as ignored.
                answered = self._turn_answered
                if self._in_response:
                    self._finish_response(now, "turn_complete")
                detail = ("answered" if answered else
                          ("words, no reply yet" if transcript else ""))
                # The server's own account of why the turn ended, when it
                # gives one: RESPONSE_REJECTED or a safety reason is a turn
                # that was heard and deliberately not answered, which no
                # amount of microphone checking would ever find.
                reason = str(reason or "")
                if reason and not reason.endswith("UNSPECIFIED"):
                    self.last_turn_reason = reason[:60]
                    detail = f"{detail} reason={self.last_turn_reason}".strip()
                self._event((now, "turn_complete", detail))
                if transcript and not answered:
                    self._awaiting = (now, transcript,
                                      self.responses, self.tool_calls)
                else:
                    self._awaiting = None
                self._turn_answered = False
        except Exception:
            pass

    def response_started(self) -> None:
        """The first audio or words of a reply. Idempotent until the reply
        ends, so a reply of two hundred audio chunks counts once."""
        try:
            with self._lock:
                self._turn_answered = True
                self._awaiting = None
                if self._in_response:
                    return
                self._in_response = True
                self.responses += 1
                self.last_response_at = self._clock()
                self._event((self.last_response_at,
                                     "response_started", ""))
        except Exception:
            pass

    def response(self) -> None:
        """Kept for callers written before replies were told apart from
        chunks. Means exactly `response_started`."""
        self.response_started()

    def generation_complete(self) -> None:
        """The server says the model has finished generating this reply.

        Not the same as turn_complete: a turn that calls a tool generates,
        waits for the tool, and generates again, and only the last one ends
        the turn."""
        try:
            with self._lock:
                self.generations_complete += 1
                if self._in_response:
                    self._finish_response(self._clock(),
                                          "generation_complete")
        except Exception:
            pass

    def server_interrupted(self) -> None:
        """The server stopped a reply part-way because it heard the user."""
        try:
            with self._lock:
                now = self._clock()
                self.server_interruptions += 1
                self._event((now, "server_interrupted", ""))
                if self._in_response:
                    self._finish_response(now, "interrupted")
        except Exception:
            pass

    def _finish_response(self, now: float, how: str) -> None:
        """Close the reply in flight. Caller holds the lock."""
        self._in_response = False
        self.responses_completed += 1
        self.last_response_done_at = now
        self._event((now, "response_done", how))

    def tool_call(self, names=()) -> None:
        """One tool_call message from the server, carrying `names`."""
        try:
            with self._lock:
                count = max(1, len(names or ()))
                self.tool_calls += count
                self.last_tool_call_at = self._clock()
                self._turn_answered = True
                self._awaiting = None
                self._event((self.last_tool_call_at, "tool_call",
                                     ", ".join(str(n) for n in names or ())))
        except Exception:
            pass

    def tool_call_finished(self, name: str = "", seconds: float = 0.0,
                           outcome: str = "sent") -> None:
        """A tool finished. `outcome` is what became of its result:
        "sent" to the model, or "discarded" because it was cancelled or the
        session it belonged to has gone."""
        try:
            with self._lock:
                self.tool_calls_finished += 1
                self._event((self._clock(), "tool_done",
                                     f"{name} {seconds:.1f}s {outcome}"))
        except Exception:
            pass

    def tool_call_cancelled(self, ids=()) -> None:
        """The server withdrew tool calls it had issued."""
        try:
            with self._lock:
                self.tool_call_cancellations += max(1, len(ids or ()))
                self._event((self._clock(), "tool_cancelled",
                                     ", ".join(str(i) for i in ids or ())))
        except Exception:
            pass

    def go_away(self, time_left: str = "") -> None:
        """The server announced it will close this connection soon."""
        try:
            with self._lock:
                self.go_aways += 1
                self._event((self._clock(), "go_away",
                                     str(time_left or "")))
        except Exception:
            pass

    def session_started(self, resumed: bool = False,
                        proactive_audio: bool | None = None) -> None:
        """A Live session connected. Every connect after the first is a
        reconnect, whatever caused it."""
        try:
            with self._lock:
                self.sessions += 1
                self.last_session_at = self._clock()
                self.proactive_audio = proactive_audio
                self._in_response = False
                self._turn_answered = False
                self._awaiting = None
                self._event((self.last_session_at, "session_start",
                                     "resumed" if resumed else "fresh"))
            if self.sessions > 1:
                self.reconnected()
        except Exception:
            pass

    def session_ended(self, reason: str = "") -> None:
        """A Live session closed. Audio spoken from now until the next
        session_start is not going anywhere; the timeline shows the gap."""
        try:
            with self._lock:
                self.session_ends += 1
                self.last_session_end_reason = str(reason or "")[:120]
                self._in_response = False
                self._event((self._clock(), "session_end",
                                     self.last_session_end_reason))
        except Exception:
            pass

    def error(self, where: str, exc=None) -> None:
        """Anything that went wrong on the voice path, with where."""
        try:
            with self._lock:
                self.errors += 1
                if where == "receive":
                    self.receive_errors += 1
                self.last_error_at = self._clock()
                detail = f"{where}: {type(exc).__name__}: {exc}" if exc \
                    else where
                self.last_error = detail[:160]
                self._event((self.last_error_at, "error",
                                     self.last_error))
        except Exception:
            pass

    def note(self, kind: str, detail: str = "") -> None:
        """A notable event from outside this module, for the timeline only.
        Never a per-frame event."""
        try:
            with self._lock:
                self._event((self._clock(), str(kind)[:32],
                                     str(detail)[:120]))
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
                self._event((self._clock(), "UNANSWERED",
                                     text[:60]))
                message = (
                    f"{HEARD_IGNORED}: Gemini transcribed \"{text[:80]}\" and "
                    f"then neither answered nor called a tool. The audio was "
                    f"fine — it reached the model and was understood. "
                    + self._proactive_hint())
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
                self._event((self._clock(), "LOST",
                                     message.split(" — ", 1)[-1][:100]))
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
                f"it into a turn. " + self._proactive_hint())

    def _proactive_hint(self) -> str:
        """What proactive audio has to do with it, from how it is ACTUALLY
        configured on the live session -- not a standing accusation.

        Naming it when it is off sends the user to change a setting that
        cannot be the cause. Caller holds the lock."""
        if self.proactive_audio is True:
            return ("Proactive audio is ON for this session, and deciding "
                    "speech was not addressed to JARVIS is exactly what it "
                    "does; set \"proactive_audio\": false in "
                    "config/api_keys.json to rule it out.")
        if self.proactive_audio is False:
            return ("Proactive audio is off for this session "
                    "(\"proactive_audio\": false), so it is not the cause; "
                    "this is the model or the server's turn detection.")
        return ("Whether proactive audio was on is not known here; if "
                "\"proactive_audio\" is true in config/api_keys.json, that "
                "judgement is a candidate.")

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
                "last_turn_reason": self.last_turn_reason,
                "responses": self.responses,
                "responses_completed": self.responses_completed,
                "generations_complete": self.generations_complete,
                "server_interruptions": self.server_interruptions,
                "tool_calls": self.tool_calls,
                "tool_calls_finished": self.tool_calls_finished,
                "tool_call_cancellations": self.tool_call_cancellations,
                "go_aways": self.go_aways,
                "sessions": self.sessions,
                "reconnects": self.reconnects,
                "session_ends": self.session_ends,
                "last_session_end_reason": self.last_session_end_reason,
                "proactive_audio": self.proactive_audio,
                "send_errors": self.send_errors,
                "receive_errors": self.receive_errors,
                "callback_errors": self.callback_errors,
                "errors": self.errors,
                "last_error": self.last_error,
                "lost_utterances": self.lost_utterances,
                "unanswered_turns": self.unanswered_turns,
                "last_loss": self.last_loss,
            }

    def line(self) -> str:
        """The whole pipeline as one key=value line, in pipeline order.

            mic=1234 gated=10 queued=1220 dropped=14 sent=1218 transcripts=3
            turns=3 responses=2 ...

        Read left to right, the first number that stops keeping up with the
        one before it is where the audio went. `mic` is what the device
        handed over, which is also all the HUD meter shows -- it proves the
        microphone works and nothing after it."""
        s = self.snapshot()
        parts = [
            f"mic={s['captured']}",
            f"gated={sum(s['gated'].values())}",
            f"queued={s['queued']}",
            f"dropped={s['dropped_queue_full']}",
            f"sent={s['sent']}",
            f"transcripts={s['input_transcripts']}",
            f"turns={s['turns_complete']}",
            f"responses={s['responses']}",
            f"done={s['responses_completed']}",
            f"interrupted={s['server_interruptions']}",
            f"tools={s['tool_calls']}",
            f"tool_cancel={s['tool_call_cancellations']}",
            f"reconnects={s['reconnects']}",
            f"go_away={s['go_aways']}",
            f"errors={s['errors'] + s['send_errors'] + s['callback_errors']}",
        ]
        if s["lost_utterances"]:
            parts.append(f"LOST={s['lost_utterances']}")
        if s["unanswered_turns"]:
            parts.append(f"UNANSWERED={s['unanswered_turns']}")
        return " ".join(parts)

    def stage_ages(self) -> str:
        """How long ago each stage last happened.

        The counters can all look healthy while one stage has quietly stopped:
        `queued 0.1s ago, sent 41s ago` is a dead sender whatever the totals
        say."""
        now = self._clock()

        def ago(at: float) -> str:
            return "never" if not at else f"{max(0.0, now - at):.1f}s"

        with self._lock:
            stages = (("captured", self.last_captured_at),
                      ("queued", self.last_queued_at),
                      ("sent", self.last_sent_at),
                      ("transcript", self.last_transcript_at),
                      ("turn", self.last_turn_at),
                      ("reply", self.last_response_at),
                      ("tool", self.last_tool_call_at),
                      ("session", self.last_session_at))
            dropped = self.last_dropped_at
            errored = self.last_error_at
        text = ", ".join(f"{name} {ago(at)}" for name, at in stages)
        if dropped:
            text += f", DROP {ago(dropped)}"
        if errored:
            text += f", error {ago(errored)}"
        return f"last: {text}"

    def timeline(self, limit: int = 15) -> list:
        """The most recent notable events, oldest first, timed from now.

        This is what answers "where did the thing I just said go": speech at
        the mic, then either a transcript and a reply, or the point where the
        sequence stops."""
        now = self._clock()
        with self._lock:
            events = list(self._events)[-max(1, int(limit)):]
        lines = []
        for at, kind, detail in events:
            lines.append(f"-{max(0.0, now - at):5.1f}s {kind}"
                         + (f" {detail}" if detail else ""))
        return lines

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
           "SPEECH_LEVEL", "SPEECH_FRAMES", "EVENT_LOG_SIZE"]
