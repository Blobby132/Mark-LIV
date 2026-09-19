"""
core/permissions.py — the one place that decides whether something may happen.

THE SHAPE
    Gemini  →  action chosen  →  BROKER  →  confirmation (if required)
                                   ↓
                              execution  →  result checked  →  audit log

    Everything with a side effect goes through `guard()`. The broker consults
    `core/capabilities.py` for the verdict, puts a banner in front of a human
    when the verdict says to, runs the work, checks that it actually worked, and
    writes the whole story to `core/audit.py`. An action file's job is to
    describe what it wants to do and hand over a callable that does it; it has
    no business deciding whether it is allowed to.

WHY THE MODEL CANNOT AUTHORISE ITSELF
    Three separate reasons, because one would not be enough:

    1. `guard()` never reads tool parameters. Its inputs are written by the
       action file — trusted Python — not by the model. There is no argument
       named `confirmed`, `approved` or `force`, so there is nothing for a
       model to fill in. `strip_forged_approval()` exists to delete such keys
       from a parameter dict before a handler ever sees them, for the case
       where a handler is written carelessly later.

    2. The verdict comes from a frozen table in another module with no setter.

    3. Approval travels from a finger on the HUD to `core/confirm.py` and
       arrives as a *call*, not as a value that could be spoofed. The old gate
       in `actions/computer_settings.py` compared a string the model wrote
       against "yes"; this one cannot be reached without the UI invoking it.

WHY THE THIRD VERDICT MATTERS
    `CONFIRM_IF_IRREVERSIBLE` lets the volume, the brightness and an ordinary
    file write go straight through, which is what keeps the assistant usable —
    `core/undo.py` already made this argument and it is right. The relaxation is
    only granted when the caller passes an `undo` callable, and the broker then
    registers that callable itself. A caller cannot claim reversibility; it can
    only demonstrate it by handing over the reversal.

WHAT "SUCCESS" MEANS HERE
    A returned result is not a successful one. `guard()` treats a raised
    exception as FAILED, and a caller can supply `verify` to have the broker
    check the world afterwards — did the file actually appear, did the process
    actually exit zero. This is deliberate: the audit's whole value is that
    "succeeded" in the log means the thing happened, not that a function
    returned without complaining.

NON-BLOCKING, AS BEFORE
    A confirmation does not block. `guard()` returns PENDING immediately with a
    sentence for the assistant to say, and the work runs later, on the UI's
    thread pool, if and only if someone presses CONFIRM. That is `confirm.py`'s
    existing design and it costs no latency. The cost is that a gated action
    cannot return its result in the same turn — it returns "I have asked".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core import audit, capabilities, confirm
from core import undo as undo_stack

# Parameter names that look like a permission slip. None of the app's tools
# declare any of these today; the list exists so that if a future handler is
# written to read one, the value it reads has already been removed.
FORGED_APPROVAL_KEYS = frozenset({
    "confirmed", "confirm", "confirmation", "approved", "approval", "approve",
    "authorized", "authorised", "authorization", "authorisation",
    "permitted", "permission", "allowed", "allow",
    "user_confirmed", "user_approved", "human_approved", "human_confirmed",
    "consent", "consented", "acknowledged",
    "force", "forced", "override", "bypass", "skip_confirm",
    "skip_confirmation", "no_confirm", "sudo", "elevated", "as_admin",
})


# ── Outcomes ─────────────────────────────────────────────────────────────────

ALLOWED = "allowed"
"""Permitted with no question. When `run` was given, see SUCCEEDED/FAILED."""

PENDING = "pending"
"""A banner is on the HUD. Nothing has happened yet, and may never."""

DENIED = "denied"
"""Refused. Policy said no, or there was no way to ask a human."""

SUCCEEDED = "succeeded"
"""Ran, and the work actually completed."""

FAILED = "failed"
"""Ran, and it did not. The reason is in `.error`."""


@dataclass(frozen=True)
class GuardResult:
    """What the broker did. Returned from every `guard()` call.

    Built so a caller cannot accidentally report success: `.ok` is True only for
    SUCCEEDED (or for a permission check that was ALLOWED and had no work to
    do), and `.message` is always a sentence that is true about what happened."""

    outcome: str
    message: str
    action: str = ""
    capability: str = ""
    decision: str = ""
    confirmation_required: bool = False
    value: Any = None
    error: str = ""
    error_class: str = ""
    duration_ms: int = 0

    @property
    def allowed(self) -> bool:
        return self.outcome in (ALLOWED, SUCCEEDED)

    @property
    def pending(self) -> bool:
        return self.outcome == PENDING

    @property
    def denied(self) -> bool:
        return self.outcome == DENIED

    @property
    def succeeded(self) -> bool:
        return self.outcome == SUCCEEDED

    @property
    def failed(self) -> bool:
        return self.outcome == FAILED

    @property
    def ok(self) -> bool:
        """True only when the caller may tell the user it is done.

        Not true for PENDING — that is the mistake this property exists to make
        hard to write."""
        return self.outcome in (SUCCEEDED, ALLOWED)


# ── Parameter hygiene ────────────────────────────────────────────────────────

def strip_forged_approval(parameters: dict | None) -> dict:
    """Return a copy of `parameters` with any self-granted permission removed.

    The model writes these. They have never meant anything and they must never
    start to: a handler that reads `params["confirmed"]` is reading something
    the model wrote about itself. Matching is case-insensitive and ignores
    dashes, so `Skip-Confirm` goes too."""
    if not isinstance(parameters, dict):
        return {}
    cleaned = {}
    for key, value in parameters.items():
        normalised = str(key).strip().lower().replace("-", "_")
        if normalised in FORGED_APPROVAL_KEYS:
            continue
        cleaned[key] = value
    return cleaned


def had_forged_approval(parameters: dict | None) -> list[str]:
    """Which approval-shaped keys were present. For the audit log — a model
    trying to authorise itself is worth a line in the record."""
    if not isinstance(parameters, dict):
        return []
    return sorted(
        str(k) for k in parameters
        if str(k).strip().lower().replace("-", "_") in FORGED_APPROVAL_KEYS
    )


# ── Pure policy query ────────────────────────────────────────────────────────

def check(capability: str, *, reversible: bool = False) -> str:
    """The *effective* verdict for `capability`, with no side effects and
    nothing logged.

    For a caller that wants to know before it builds a whole request — a tool
    description, a UI badge, a preflight. Unknown capabilities return DENY.

    `reversible=True` resolves CONFIRM_IF_IRREVERSIBLE to ALLOW, so the answer
    is what would actually happen rather than what the raw table says. Note
    that `guard()` grants that relaxation only against a real `undo` callable;
    passing True here is a question, not a claim."""
    verdict = capabilities.decision_for(capability)
    if reversible and verdict == capabilities.CONFIRM_IF_IRREVERSIBLE:
        return capabilities.ALLOW
    return verdict


def requires_confirmation(capability: str, *, reversible: bool = False) -> bool:
    """Would a human be asked? Pure."""
    return capabilities.requires_confirmation(capability, reversible=reversible)


# ── The gate ─────────────────────────────────────────────────────────────────

def guard(
    action: str,
    capability: str,
    *,
    summary: str,
    detail: str = "",
    target: str = "",
    url: str = "",
    run: Optional[Callable[[], Any]] = None,
    undo: Optional[Callable[[], Any]] = None,
    undo_provider: Optional[Callable[[], Any]] = None,
    undo_label: str = "",
    verify: Optional[Callable[[Any], bool]] = None,
    key: str = "",
) -> GuardResult:
    """Decide, ask if necessary, do the work, check it, and write it down.

    `action`      what to call this in the log, e.g. "file_controller.delete".
    `capability`  one of `core/capabilities.py`'s constants. Unknown ⇒ denied.
    `summary`     one line for the confirmation banner. Written by the action.
    `detail`      the consequence, in the user's terms. Shown under `summary`.
    `target`/`url` what is being acted on, for the audit log (both redacted).
    `run`         the callable that does the work. Omit to ask only whether the
                  capability is permitted.
    `undo`        a callable that reverses `run`. Supplying one is what makes a
                  CONFIRM_IF_IRREVERSIBLE capability run without asking, and the
                  broker registers it on `core/undo.py` after a successful run.
    `undo_provider`
                  for the common case where the reversal can only be built by
                  looking at the world first — you cannot write the undo for
                  "overwrite notes.txt" without reading notes.txt. The broker
                  calls this BEFORE deciding; whatever callable it returns is
                  used as `undo`, and returning None means "this particular
                  operation is not reversible after all", which asks.

                  This is still demonstrate-don't-claim: the provider has to
                  actually produce a working reversal. `file_controller` uses it
                  to snapshot a file it is about to overwrite, and to return
                  None when the file is too large to hold in memory — so the
                  cheap write goes straight through and the one that cannot be
                  taken back asks first, decided by the facts rather than by
                  anybody's assertion.
    `verify`      given `run`'s return value, says whether it really worked. Use
                  it wherever a function can fail by returning rather than by
                  raising — which, in this codebase, is most of them.
    `key`         de-duplication key for the banner. Defaults to `action`.

    Never raises. Every failure is a GuardResult."""

    key = key or action or capability
    verdict = capabilities.decision_for(capability)

    # Build the reversal before deciding, when the caller needs the world to
    # build it. A provider that raises is treated as "no reversal available" —
    # failing to snapshot is exactly the case where we should be asking, not
    # the case where we should be crashing.
    if undo is None and undo_provider is not None and verdict != capabilities.DENY:
        try:
            produced = undo_provider()
        except Exception as e:
            produced = None
            audit.record("permission", action=action, capability=capability,
                         note="undo provider failed", error_class=type(e).__name__)
        if callable(produced):
            undo = produced

    reversible = callable(undo)
    needs_human = capabilities.requires_confirmation(capability, reversible=reversible)

    # ── Denied outright ──────────────────────────────────────────────────────
    if verdict == capabilities.DENY:
        known = capabilities.is_known(capability)
        message = (
            f"I will not do that: {summary}. "
            + ("That is not something I am permitted to do at all, with or "
               "without your approval."
               if known else
               "That request did not map to anything I am allowed to do.")
        )
        audit.decision(action, capability, capabilities.DENY,
                       required=False, target=target, url=url,
                       note="unknown capability" if not known else "denied by policy")
        return GuardResult(
            outcome=DENIED, message=message, action=action,
            capability=capability, decision=capabilities.DENY,
            confirmation_required=False,
            error="denied by policy" if known else "unknown capability",
        )

    # ── Needs a human ────────────────────────────────────────────────────────
    if needs_human:
        if run is None:
            # A permission *check* for something that needs a human is not a
            # question anyone can answer — there is no work attached to
            # approve. Fail closed and say why, rather than returning a
            # permissive-looking ALLOWED.
            audit.decision(action, capability, verdict, required=True,
                           target=target, url=url,
                           note="check only; nothing to confirm")
            return GuardResult(
                outcome=DENIED,
                message=f"'{summary}' needs your approval, so I cannot do it silently.",
                action=action, capability=capability, decision=verdict,
                confirmation_required=True, error="confirmation required",
            )

        if not confirm.is_available():
            # Headless, or before the UI is wired up. There is no way to ask, so
            # the answer is no. This is the fail-closed case and it must never
            # quietly become a yes.
            audit.decision(action, capability, verdict, required=True,
                           target=target, url=url, note="no confirmation surface")
            return GuardResult(
                outcome=DENIED,
                message=(f"I cannot do '{summary}' right now: it needs your "
                         f"confirmation and there is no way for me to ask you. "
                         f"Nothing has been done."),
                action=action, capability=capability, decision=verdict,
                confirmation_required=True, error="no confirmation surface",
            )

        waiting = confirm.pending_title()
        if waiting:
            return GuardResult(
                outcome=DENIED,
                message=(f"There is already a confirmation on screen for "
                         f"'{waiting}'. Ask the user to answer that one first, "
                         f"then ask me again."),
                action=action, capability=capability, decision=verdict,
                confirmation_required=True, error="another confirmation pending",
            )

        sentence = confirm.request(
            key=key, title=summary, detail=detail,
            run=_approved_runner(action, capability, summary, target, url,
                                 run, undo, undo_label, verify),
        )
        audit.record("permission", action=action, capability=capability,
                     decision=verdict, outcome=audit.PENDING,
                     confirmation_required=True, target=target, url=url)
        return GuardResult(
            outcome=PENDING, message=sentence, action=action,
            capability=capability, decision=verdict,
            confirmation_required=True,
        )

    # ── Allowed ──────────────────────────────────────────────────────────────
    audit.decision(action, capability, verdict, required=False,
                   target=target, url=url,
                   note="reversible" if (reversible and verdict ==
                                         capabilities.CONFIRM_IF_IRREVERSIBLE) else "")

    if run is None:
        return GuardResult(
            outcome=ALLOWED, message="", action=action, capability=capability,
            decision=verdict, confirmation_required=False,
        )

    return _execute(action, capability, verdict, summary, target, url,
                    run, undo, undo_label, verify, approved=None)


def _approved_runner(action, capability, summary, target, url,
                     run, undo, undo_label, verify) -> Callable[[], str]:
    """Wraps the caller's work for `confirm.resolve()` to run after CONFIRM.

    Runs on `confirm.py`'s worker thread. Returns a short string, which that
    module writes to the HUD log — so the user sees the outcome of the thing
    they just approved, not merely that it was approved."""

    def _runner() -> str:
        result = _execute(action, capability, capabilities.CONFIRM, summary,
                          target, url, run, undo, undo_label, verify,
                          approved=True)
        if result.succeeded:
            return result.message or "Done."
        return result.message or "It did not work."

    return _runner


def _execute(action, capability, verdict, summary, target, url,
             run, undo, undo_label, verify, approved) -> GuardResult:
    """Run the work, decide honestly whether it worked, record it."""
    started = time.monotonic()
    try:
        value = run()
    except Exception as e:
        duration = int((time.monotonic() - started) * 1000)
        audit.result(action, capability, ok=False, target=target, url=url,
                     error_class=type(e).__name__, error=str(e),
                     duration_ms=duration, approved=approved)
        return GuardResult(
            outcome=FAILED,
            message=f"{summary} failed: {e}",
            action=action, capability=capability, decision=verdict,
            confirmation_required=bool(approved), value=None,
            error=str(e), error_class=type(e).__name__, duration_ms=duration,
        )

    duration = int((time.monotonic() - started) * 1000)

    # A function that returned is not a function that worked. Where the caller
    # has given us a way to check, check.
    if verify is not None:
        try:
            worked = bool(verify(value))
        except Exception as e:
            worked = False
            audit.result(action, capability, ok=False, target=target, url=url,
                         error_class=type(e).__name__,
                         error=f"verification raised: {e}",
                         duration_ms=duration, approved=approved)
            return GuardResult(
                outcome=FAILED,
                message=f"{summary} could not be verified: {e}",
                action=action, capability=capability, decision=verdict,
                confirmation_required=bool(approved), value=value,
                error=str(e), error_class=type(e).__name__,
                duration_ms=duration,
            )
        if not worked:
            audit.result(action, capability, ok=False, target=target, url=url,
                         error_class="VerificationFailed",
                         error="the operation reported no error but did not take effect",
                         duration_ms=duration, approved=approved)
            return GuardResult(
                outcome=FAILED,
                message=(f"{summary} did not take effect. I am not going to "
                         f"claim it did."),
                action=action, capability=capability, decision=verdict,
                confirmation_required=bool(approved), value=value,
                error="verification failed", error_class="VerificationFailed",
                duration_ms=duration,
            )

    # Only now, with the work done and checked, is the undo worth registering.
    if callable(undo):
        undo_stack.push_undo(undo_label or summary, undo)

    audit.result(action, capability, ok=True, target=target, url=url,
                 duration_ms=duration, approved=approved)
    return GuardResult(
        outcome=SUCCEEDED,
        message=str(value) if isinstance(value, str) and value else summary,
        action=action, capability=capability, decision=verdict,
        confirmation_required=bool(approved), value=value,
        duration_ms=duration,
    )


# ── Confirmation outcomes reach the audit log ────────────────────────────────

def _on_confirmation(key: str, title: str, outcome: str) -> None:
    """Called by `core/confirm.py` when a banner is answered, ignored or
    replaced. Approval and execution are recorded by `_execute`; this covers
    the endings where nothing runs and there would otherwise be no trace."""
    mapping = {
        "approved":   audit.APPROVED,
        "cancelled":  audit.CANCELLED,
        "expired":    audit.EXPIRED,
        "superseded": "superseded",
    }
    audit.confirmation(key or "confirm", "", mapping.get(outcome, outcome),
                       note=title)


def install() -> None:
    """Wire the broker to the confirmation gate. Idempotent; called from
    main.py at startup and from tests."""
    confirm.set_observer(_on_confirmation)


# Installed at import. The observer is inert until something actually asks for a
# confirmation, and doing it here means no call site can forget.
install()


__all__ = [
    "GuardResult", "guard", "check", "requires_confirmation",
    "strip_forged_approval", "had_forged_approval", "FORGED_APPROVAL_KEYS",
    "install",
    "ALLOWED", "PENDING", "DENIED", "SUCCEEDED", "FAILED",
]
