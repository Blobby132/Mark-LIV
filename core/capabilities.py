"""
core/capabilities.py — what kinds of side effect exist, and who may authorise them.

WHY THIS FILE EXISTS
    Before this, "is this safe?" was answered in eighteen different places, in
    eighteen different ways, and most of them answered "yes" by not asking.
    `actions/computer_settings.py` kept a set called `_IRREVERSIBLE` with three
    entries in it; `actions/game_updater.py` powered the machine off with no
    check at all. Two files, two opinions, no shared vocabulary.

    This module is the vocabulary and the verdict, and nothing else. It holds no
    logic, performs no action, imports nothing from the rest of the app, and has
    no function that changes the table. A capability is a plain string so it can
    be written into the audit log and read back a year later.

THE TABLE IS DEVELOPER-CONTROLLED, NOT MODEL-CONTROLLED
    The model chooses *which action to call*. It does not get a say in what that
    action is allowed to do. The mapping below is a literal written in source,
    frozen at import, and exposed read-only:

        - there is no setter, no `configure()`, no environment override
        - `POLICY` is a MappingProxyType, so `POLICY[x] = y` raises
        - changing a verdict means editing this file and restarting

    That last line is the honest limit of the claim. Python has no way to stop
    code already running inside the process from rebinding a module attribute;
    anything that can do that can also call `os.unlink` directly and skip this
    file entirely. What is ruled out here is the thing that actually happens in
    practice: an action, a plugin or a model-supplied parameter quietly talking
    its way into a higher privilege at runtime. There is no API to do it with.

UNKNOWN MEANS NO
    `decision_for()` returns DENY for any capability it does not recognise. A
    typo in a capability name fails closed and loudly, rather than silently
    becoming an unguarded action — which is the failure mode this whole layer
    exists to remove.

THE THIRD VERDICT
    Most tables like this have two answers. This one has three, because the
    codebase already discovered the third and wrote it down in `core/undo.py`:
    an assistant that asks before turning the volume down is one nobody uses.
    So CONFIRM_IF_IRREVERSIBLE means "ask, unless the caller has supplied a
    working reversal" — and `core/permissions.py` only honours that when it is
    handed an actual undo callable, never when a caller merely claims it.
"""

from __future__ import annotations

from types import MappingProxyType

# ─────────────────────────────────────────────────────────────────────────────
# Verdicts
# ─────────────────────────────────────────────────────────────────────────────
#
# Deliberately strings rather than an IntEnum: they land in the audit log as
# themselves, and a log line that says "CONFIRM" needs no decoder ring.

ALLOW = "ALLOW"
"""Do it now. Read-only work, and changes cheap enough that a question costs
more than the mistake would."""

CONFIRM = "CONFIRM"
"""A human presses a button on the HUD, or it does not happen."""

CONFIRM_IF_IRREVERSIBLE = "CONFIRM_IF_IRREVERSIBLE"
"""Ask — unless the caller supplies a real reversal, in which case do it now and
register the undo. See `core/permissions.py`; a caller cannot claim this
relaxation without handing over the callable that performs the reversal."""

DENY = "DENY"
"""Never, by any route, with or without a human. Reserved for things that cannot
be made safe by asking nicely — see CODE_EXEC_IN_PROCESS."""

VERDICTS = frozenset({ALLOW, CONFIRM, CONFIRM_IF_IRREVERSIBLE, DENY})


# ─────────────────────────────────────────────────────────────────────────────
# Capabilities
# ─────────────────────────────────────────────────────────────────────────────
#
# One per *kind of side effect*, not one per tool. Tools come and go; "this
# deletes one of the user's files" does not. Two tools doing the same damage
# must name the same capability, which is what stops the next action file from
# inventing its own private idea of "safe".

# — Information only ————————————————————————————————————————————————————————
READ_ONLY = "info.read"
"""Answers a question without touching anything: system stats, a weather
lookup, listing a directory, reading back memory."""

FILE_READ = "file.read"
"""Reads the contents of a file the user pointed at. Separate from READ_ONLY so
the audit log can show *that* a file was read even though it never shows what
was in it."""

SCREEN_CAPTURE = "computer.screen_capture"
"""Screenshot or camera frame. Not free of consequence — it can capture a
password manager — but it is the assistant's primary sense, and gating it would
gate seeing."""

NETWORK_FETCH = "net.fetch"
"""Fetches a remote resource: a search, a page, an image download. The *write*
of anything fetched is gated separately under FILE_WRITE."""

# — The user's files ————————————————————————————————————————————————————————
FILE_WRITE = "file.write"
"""Creates or overwrites file contents."""

FILE_MOVE = "file.move"
"""Moves or renames. Reversible when the caller remembers where it came from,
which is why it is not a flat CONFIRM."""

FILE_DELETE = "file.delete"
"""Removes a file or folder, including to the Recycle Bin or Trash.

Flatly CONFIRM, and that is a deliberate tightening of today's behaviour, where
`delete_file` trashes on the model's say-so and relies on undo afterwards. Two
reasons. The Trash is only a real undo on Windows — `_restore_from_trash` in
`actions/file_controller.py` says so itself, and on Linux and macOS it can do no
more than tell you where the file went. And deleting someone's file is the one
operation where "sorry" has never been an adequate answer. The trash-plus-undo
path stays exactly as it is underneath; it is now the second line of defence
rather than the first."""

FILE_ARCHIVE_EXTRACT = "file.extract"
"""Unpacks an archive: many writes at once, to paths chosen by whoever built the
archive rather than by the user. See `core/safe_path.safe_extract`."""

# — Running things ——————————————————————————————————————————————————————————
COMMAND_EXEC = "code.command"
"""Runs an external program. Covers every subprocess the assistant starts on
purpose, from ffmpeg to git."""

CODE_EXEC = "code.execute"
"""Runs code that a language model wrote, in a separate interpreter, with a
timeout and a kill. The human confirmation is the security boundary here — the
subprocess is what makes the blast radius bounded and the process killable."""

CODE_EXEC_IN_PROCESS = "code.execute_in_process"
"""`exec()` of model-written Python inside the assistant's own interpreter.

DENY, permanently. `actions/desktop.py` does this today behind a dictionary it
calls a sandbox; the escape is one expression long:

    Path.__init__.__globals__["__builtins__"]

which means every rule in that generated prompt — no subprocess, no deletion, no
imports — is a polite request to a stranger. A restricted globals dict is not a
sandbox and this file will not pretend otherwise. The capability is listed
rather than omitted so that the refusal is explicit and testable, instead of
being an unknown name that happens to fail closed."""

PACKAGE_INSTALL = "code.install_package"
"""pip / npm and friends. Arbitrary code execution wearing a respectable
jacket: a package's install hooks run as the user, before anyone reads a line
of it."""

SOFTWARE_INSTALL = "app.install"
"""Installers, store apps, game downloads — anything that puts a new executable
on the machine."""

APP_LAUNCH = "app.launch"
"""Opens an ordinary application. This is most of what the assistant is for."""

APP_LAUNCH_SHELL = "app.launch_shell"
"""Opens a terminal, a shell or an installer. `actions/open_app.py` aliases
`cmd`, `powershell`, `bash` and `terminal`, so "open a terminal" is one step
from a command line the assistant did not have to justify."""

# — The browser ————————————————————————————————————————————————————————————
BROWSER_NAVIGATE = "browser.navigate"
"""Opens a URL, scrolls, reads the page, goes back. Ungated: this is browsing."""

BROWSER_SUBMIT = "browser.submit"
"""Fills or submits a form. `actions/browser_control.py` drives the user's real
profile, with their real sessions — a submitted form is a real purchase, a real
password change, a real post."""

BROWSER_ACCOUNT = "browser.account"
"""Acts on a signed-in account without necessarily submitting a form: logging
out, deleting, changing a setting, confirming a payment."""

# — Reaching other people ——————————————————————————————————————————————————
MESSAGE_SEND = "messaging.send"
"""Sends a message as the user. Unrecallable in the way that matters: the other
person has already read it."""

# — The machine ————————————————————————————————————————————————————————————
SYSTEM_SETTINGS = "computer.settings"
"""Volume, brightness, theme, WiFi. Reversible ones go straight through with an
undo — which is exactly what `actions/computer_settings.py` does today, and this
keeps that behaviour rather than replacing it with a question."""

SYSTEM_POWER = "computer.power"
"""Shutdown, restart, sleep, logout. Unsaved work dies with it."""

INPUT_SYNTHETIC = "computer.input"
"""Synthetic keystrokes and mouse events. Ungated on purpose: typing and
clicking *is* the JARVIS trick, and a confirmation in front of every keypress
would end the product. It is audited, and the audit never records what was
typed — see `core/audit.py`, which exists partly for this case."""

# — Minecraft ——————————————————————————————————————————————————————————————
#
# Declared HERE, in the one table, rather than in a private registry inside
# minecraft/. That is the whole point of the namespace: a subsystem gets
# capabilities, not its own security framework, and `core/permissions.py`
# stays the only thing that decides.
#
# Which of these the current prototype will actually attempt is a separate,
# narrower question, answered by `minecraft/capabilities.py`. That list can
# only ever subtract from this one.

MINECRAFT_OBSERVE = "minecraft.observe"
"""Capture the Minecraft window. Narrower than SCREEN_CAPTURE: the region is
the game window, so the rest of the desktop is not in the frame."""

MINECRAFT_READ_STATE = "minecraft.read_state"
"""Read structured game state — position, health, inventory. Reading."""

MINECRAFT_CONTROL_SESSION = "minecraft.control_session"
"""Open a time-boxed window in which the assistant may drive the game.

THE gate for the whole subsystem, and the reason movement below is ALLOW.
Confirming each keypress is not an option — a walk is tens of presses, and a
confirmation people learn to dismiss is worse than none. So one human decision
buys a bounded session: five minutes at most, ended by Alt-Tab, by F12, by the
game closing, or by the clock. Inside it, `minecraft.move` and
`minecraft.look` are free; outside it they are refused before any key is
touched."""

MINECRAFT_MOVE = "minecraft.move"
"""Hold a movement key for a bounded time. Only reachable inside a live
session, and only for W/A/S/D."""

MINECRAFT_LOOK = "minecraft.look"
"""Move the mouse by a bounded relative delta. Only inside a live session."""

MINECRAFT_STOP = "minecraft.stop"
"""Stop everything and release every held key.

ALLOW, and it must stay ALLOW. A safety control that policy can refuse is not
a safety control — if this could be denied, a confused planner or an expired
confirmation would leave keys held down. It is also the one capability here
that is *more* permissive than doing nothing."""

MINECRAFT_JUMP = "minecraft.jump"
"""One hop. Only inside a live session."""

MINECRAFT_HOTBAR = "minecraft.hotbar"
"""Select hotbar slot 1-9. Changes what is held, destroys nothing."""

MINECRAFT_SNEAK = "minecraft.sneak"
"""Hold crouch, optionally while walking. Only inside a live session."""

MINECRAFT_SPRINT = "minecraft.sprint"
"""Hold sprint while walking. Only inside a live session."""

MINECRAFT_ATTACK = "minecraft.attack"
"""Left-click: mine a block, hit a mob.

ALLOW, and the reasoning is worth writing down because this is the one verdict
in this namespace that changed when it went from declared to built.

It was CONFIRM while it was a placeholder for something unimplemented — a
sensible default for a capability nothing could reach. Building it made that
default actively harmful: breaking one oak log takes several bounded swings,
so CONFIRM means a dialog per swing, and a dialog per swing is how people
learn to dismiss dialogs without reading them. That trains the exact reflex
the confirmation exists to prevent, and it would apply to the confirmations
that really matter elsewhere in the app too.

So the consent moved rather than disappeared, and it got MORE specific: a
session must be opened with interaction explicitly granted, and the
confirmation for that session names mining and placing in as many words. A
session without that grant refuses attack before any button is pressed. One
informed decision, time-boxed to five minutes, revocable by Alt-Tab, F12, the
clock or the game closing — instead of twenty identical prompts.

The verdict here is not what makes this safe; `Session.allow_interaction` is.
This row only stops the broker asking a question the session already asked
better."""

MINECRAFT_USE_ITEM = "minecraft.use_item"
"""Right-click: place a block, eat, open a door. Same reasoning as
MINECRAFT_ATTACK, and gated by the same session grant."""

MINECRAFT_TASK = "minecraft.task"
"""Run a bounded multi-step task — observe, act, verify, repeat.

CONFIRM, and this one is a gate being ADDED rather than relaxed. Every
individual action a task takes is already bounded and already inside a
session. What is new is the count: one approval buying up to twenty actions
chosen by code rather than by the person watching. That is a genuinely
different decision from "walk forward", so it gets its own."""

MINECRAFT_INVENTORY = "minecraft.inventory"
"""Open the inventory and move items. Not enabled in the current phase."""

MINECRAFT_CHAT = "minecraft.chat"
"""Type in the game chat. CONFIRM even when it is eventually enabled: on a
server this reaches other people, which makes it MESSAGE_SEND wearing a
different hat. Not enabled in the current phase."""

MINECRAFT_COMMAND = "minecraft.command"
"""Slash commands — /give, /tp, /gamemode, and on a server /op.

DENY, permanently, and listed rather than omitted so the refusal is explicit
and testable instead of being an unknown name that happens to fail closed.
There is no route from this subsystem to a command line, in the game or out
of it."""

MINECRAFT_LAUNCH = "minecraft.launch"
"""Start the game. Not enabled in the current phase — the prototype requires
Minecraft to be running already, so there is no launcher path to abuse."""


APP_STATE = "app.state"
"""The assistant's own memory, monitor list and lifecycle — not the user's
files.

Saving a fact to `memory/long_term.json`, adding a topic to the background
monitor, or quitting when asked to. These write to files the app owns and that
exist only to make it work, so they are ALLOW; the capability exists as a
separate name rather than being folded into READ_ONLY because the audit log
should still be able to say that something was written."""

# — Extensions ————————————————————————————————————————————————————————————
PLUGIN_UNCLASSIFIED = "plugin.unclassified"
"""A drop-in plugin that has not said what it does.

`plugins/*.py` are written by whoever dropped them in the folder, and before
this they ran with no gate at all. Requiring a declaration would break every
plugin already installed, so an undeclared one instead lands here: it still
runs, but a human is asked first, every time. Declaring
`PLUGIN["capability"] = capabilities.READ_ONLY` in the plugin removes the
prompt. Undeclared is not assumed harmless — that assumption is what this whole
layer exists to stop making."""


# ─────────────────────────────────────────────────────────────────────────────
# The policy
# ─────────────────────────────────────────────────────────────────────────────

_POLICY: dict[str, str] = {
    # Information: free.
    READ_ONLY:              ALLOW,
    FILE_READ:              ALLOW,
    SCREEN_CAPTURE:         ALLOW,
    NETWORK_FETCH:          ALLOW,

    # Files: reversible work runs, irreversible work asks, deletion always asks.
    FILE_WRITE:             CONFIRM_IF_IRREVERSIBLE,
    FILE_MOVE:              CONFIRM_IF_IRREVERSIBLE,
    FILE_DELETE:            CONFIRM,
    FILE_ARCHIVE_EXTRACT:   CONFIRM,

    # Execution: always a human.
    COMMAND_EXEC:           CONFIRM,
    CODE_EXEC:              CONFIRM,
    CODE_EXEC_IN_PROCESS:   DENY,
    PACKAGE_INSTALL:        CONFIRM,
    SOFTWARE_INSTALL:       CONFIRM,

    # Applications: opening Spotify is not opening a shell.
    APP_LAUNCH:             ALLOW,
    APP_LAUNCH_SHELL:       CONFIRM,

    # Browser: browsing is free, acting as the signed-in user is not.
    BROWSER_NAVIGATE:       ALLOW,
    BROWSER_SUBMIT:         CONFIRM,
    BROWSER_ACCOUNT:        CONFIRM,

    # People.
    MESSAGE_SEND:           CONFIRM,

    # Machine.
    SYSTEM_SETTINGS:        CONFIRM_IF_IRREVERSIBLE,
    SYSTEM_POWER:           CONFIRM,
    INPUT_SYNTHETIC:        ALLOW,

    # Minecraft. The session is the gate; the actions inside it are not.
    MINECRAFT_OBSERVE:          ALLOW,
    MINECRAFT_READ_STATE:       ALLOW,
    MINECRAFT_CONTROL_SESSION:  CONFIRM,
    MINECRAFT_MOVE:             ALLOW,
    MINECRAFT_LOOK:             ALLOW,
    MINECRAFT_STOP:             ALLOW,
    MINECRAFT_JUMP:             ALLOW,
    MINECRAFT_HOTBAR:           ALLOW,
    MINECRAFT_SNEAK:            ALLOW,
    MINECRAFT_SPRINT:           ALLOW,
    # Destructive, and gated by an explicit per-session grant rather than by a
    # prompt per swing — see MINECRAFT_ATTACK's docstring for why that is the
    # stronger of the two.
    MINECRAFT_ATTACK:           ALLOW,
    MINECRAFT_USE_ITEM:         ALLOW,
    # A task spends one approval on up to twenty actions, which is a different
    # decision from any single one of them.
    MINECRAFT_TASK:             CONFIRM,
    # Declared but not built. CONFIRM rather than ALLOW so that if the phase
    # gate in minecraft/capabilities.py were ever removed, these would still
    # stop and ask rather than quietly becoming available.
    MINECRAFT_INVENTORY:        CONFIRM,
    MINECRAFT_CHAT:             CONFIRM,
    MINECRAFT_LAUNCH:           CONFIRM,
    MINECRAFT_COMMAND:          DENY,

    # The assistant's own state.
    APP_STATE:              ALLOW,

    # Extensions that have not declared themselves.
    PLUGIN_UNCLASSIFIED:    CONFIRM,
}

POLICY = MappingProxyType(_POLICY)
"""The table, read-only. `POLICY[FILE_DELETE] is CONFIRM` — and assignment to it
raises TypeError."""

ALL_CAPABILITIES = frozenset(_POLICY)


def decision_for(capability: str) -> str:
    """The verdict for `capability` — one of ALLOW / CONFIRM /
    CONFIRM_IF_IRREVERSIBLE / DENY.

    An unrecognised capability returns DENY. That includes None, the empty
    string, a misspelling, and anything a caller invented on the spot: this
    function is the only door, and it opens for names that were written down
    here on purpose."""
    if not isinstance(capability, str):
        return DENY
    return _POLICY.get(capability, DENY)


def is_known(capability: str) -> bool:
    """False for anything not in the table — which `decision_for` treats as DENY."""
    return isinstance(capability, str) and capability in _POLICY


def namespace(capability: str) -> str:
    """The part before the dot — 'file', 'browser', 'computer', 'minecraft'.

    Capability names are namespaced (`file.delete`, `browser.submit`) so that a
    whole subsystem can be reasoned about, listed in a UI, or audited as a
    group without anyone maintaining a second list of which names belong to
    which area. Returns '' for a name with no namespace."""
    if not isinstance(capability, str) or "." not in capability:
        return ""
    return capability.split(".", 1)[0]


def in_namespace(prefix: str) -> tuple[str, ...]:
    """Every known capability under `prefix`, sorted.

    A subsystem being added later — `minecraft.*` is the planned one — declares
    its capabilities in the table above like everything else, and reaches them
    through here rather than keeping a private copy. There is deliberately no
    way to *add* to a namespace at runtime: a new subsystem's capabilities are
    a source change to this file, reviewed like any other."""
    prefix = str(prefix or "").rstrip(".")
    if not prefix:
        return ()
    return tuple(sorted(c for c in _POLICY if namespace(c) == prefix))


def requires_confirmation(capability: str, *, reversible: bool = False) -> bool:
    """Would this capability need a human, given the caller's reversibility?

    A pure query, for callers that want to know before they build a whole
    request. `reversible` only relaxes CONFIRM_IF_IRREVERSIBLE — it has no
    effect on CONFIRM, and a DENY is not a confirmation question at all."""
    verdict = decision_for(capability)
    if verdict == CONFIRM_IF_IRREVERSIBLE:
        return not reversible
    return verdict == CONFIRM
