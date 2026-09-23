"""
minecraft/task_slot.py — one Minecraft task at a time, off the voice thread.

WHY TASKS RUN IN THE BACKGROUND
    A task is up to two minutes of observe-act-verify. It used to run inside
    the tool call, and the tool call ran inside the loop that receives
    everything Gemini sends -- so for those two minutes nothing the user said
    could be acted on. Including "stop". F12 still worked; the assistant you
    were talking to did not.

    So `run_task` now starts the task here and returns at once. The task runs
    on its own thread, the conversation carries on, and when the task ends
    its result is reported back into the conversation.

WHAT DID NOT CHANGE
    Everything that makes a task safe lives below this file and is untouched:
    the runner's step and time limits, the session grant every action is
    checked against, the focus guard on every tick, F12, the deadman. This
    module decides only WHEN a task runs, never what it may do.

ONE AT A TIME
    There is one slot. Starting a task while one runs is refused with the
    name of the one running -- not queued, which would act later on a request
    the user may have forgotten, and not replaced, which would cancel
    something they asked for without saying so.

CANCELLING
    `cancel()` asks the runner to stop. The runner is tied to the controller
    for each step (see MinecraftController.cancellable), so the step already
    holding keys stops within one tick and releases them; no further step
    starts. The session is left alone: cancelling a task is "stop doing
    that", not "you may no longer play".
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from minecraft.errors import TaskAlreadyRunning


@dataclass
class Job:
    """One task, running or finished."""

    job_id: str
    name: str
    goal: str
    started_at: float
    runner: object
    result: object = None
    error: str = ""
    cancel_reason: str = ""
    finished_at: float | None = None
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def running(self) -> bool:
        return not self.done.is_set()

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None \
            else time.monotonic()
        return max(0.0, end - self.started_at)

    def as_dict(self) -> dict:
        out = {
            "job_id": self.job_id,
            "task": self.name,
            "goal": self.goal,
            "running": self.running,
            "elapsed_s": round(self.elapsed, 1),
        }
        if self.cancel_reason:
            out["cancelled"] = self.cancel_reason
        if self.error:
            out["error"] = self.error
        if self.result is not None:
            out["status"] = getattr(self.result, "status", None)
            out["reason"] = getattr(self.result, "reason", "")
            out["steps_taken"] = getattr(self.result, "steps_taken", None)
        return out


class TaskSlot:
    """Holds the one running task, if any. Safe to use from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._last: Job | None = None

    # ── queries ──────────────────────────────────────────────────────────────

    def current(self) -> Job | None:
        """The running task, or None."""
        with self._lock:
            job = self._job
        return job if job is not None and job.running else None

    def busy(self) -> bool:
        return self.current() is not None

    def last(self) -> Job | None:
        """The most recently finished task."""
        with self._lock:
            return self._last

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, runner, skill, name: str, max_steps=None,
              on_done=None) -> Job:
        """Run `skill` on `runner` in the background. Returns at once.

        Raises TaskAlreadyRunning if the slot is taken. `on_done(job)` is
        called on the task's thread once the task has ended for any reason,
        after the slot is free again -- so a report written from it may say
        "ready for the next one" truthfully."""
        goal = str(getattr(skill, "goal", name) or name)
        with self._lock:
            running = self._job
            if running is not None and running.running:
                raise TaskAlreadyRunning(
                    f"The {running.name} task is still running "
                    f"({running.elapsed:.0f}s so far). Say stop, or cancel "
                    f"it, before starting another.")
            job = Job(job_id=uuid.uuid4().hex[:8], name=str(name), goal=goal,
                      started_at=time.monotonic(), runner=runner)
            self._job = job

        thread = threading.Thread(
            target=self._work, args=(job, skill, max_steps, on_done),
            name=f"minecraft-task-{job.job_id}", daemon=True)
        job.thread = thread
        try:
            thread.start()
        except Exception:
            # Could not even start: free the slot so the next request is not
            # refused because of a task that never existed.
            with self._lock:
                if self._job is job:
                    self._job = None
            job.error = "the task thread could not be started"
            job.finished_at = time.monotonic()
            job.done.set()
            raise
        return job

    def _work(self, job: Job, skill, max_steps, on_done) -> None:
        try:
            if max_steps is None:
                job.result = job.runner.run(skill)
            else:
                job.result = job.runner.run(skill, max_steps=max_steps)
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
        finally:
            job.finished_at = time.monotonic()
            with self._lock:
                self._last = job
                if self._job is job:
                    self._job = None
            try:
                if on_done is not None:
                    on_done(job)
            except Exception:
                pass          # a failed report must not leave `done` unset
            finally:
                job.done.set()

    def cancel(self, reason: str = "cancelled") -> Job | None:
        """Ask the running task to stop. Returns it, or None if idle.

        Does not wait. The runner stops the step in progress within one tick
        and starts no other; `wait()` is there for anyone who must know it
        has."""
        job = self.current()
        if job is None:
            return None
        if not job.cancel_reason:
            job.cancel_reason = str(reason or "cancelled")[:120]
        try:
            job.runner.cancel(job.cancel_reason)
        except Exception:
            pass
        return job

    def wait(self, timeout: float | None = None) -> Job | None:
        """Block until the running task (if any) ends. For tests and tools,
        never for the voice path."""
        job = self.current()
        if job is None:
            return self.last()
        job.done.wait(timeout)
        return job


__all__ = ["TaskSlot", "Job"]
