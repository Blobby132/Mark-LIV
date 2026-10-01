# ⚙️ MARK LIV (54)
### The Ultimate Cross-Platform Personal AI Assistant — By FatihMakes

> 📺 **[Watch the full setup video on YouTube](https://www.youtube.com/@FatihMakes)**

A real-time voice AI that can hear, see, speak, and control your computer — on any OS. Supports Windows, macOS, and Linux. Built on the Gemini Live API for native audio streaming, delivering zero subscriptions and total digital autonomy.

---

## ✨ Overview

**MARK LIV is the release where JARVIS gets a face.** A holographic head sits at the centre of the HUD and **speaks your assistant's words with real lip-sync** — not a jaw flapping to the volume meter, but actual mouth shapes: lips closing on *m*, *b*, *p*, spreading on *i*, rounding on *u*. Brows ride the sentence, the eyes flick between fixation points, and it blinks. Turn the sound down and you can follow roughly what it just said.

It ships as **zero extra dependencies and one 25 KB asset**. The face is real measured human geometry; everything else — the skull, the rig, the lighting — is generated at startup and drawn in software, so it looks identical on a gaming rig and a 2013 laptop, with no GPU driver in the loop.

The face is also the fastest status indicator in the app: it looks away while thinking, meets your eyes while listening, and lets its lids fall while asleep.

Underneath, Mark LIV rebuilt how the assistant knows itself — what it is, what machine it runs on, what it can do and, new, **what it cannot do** — all assembled from the live system at session start rather than written into a prompt that goes stale.

It's not just an assistant — it's an extension of your digital life.

---

## ⚡ Quick Start

```bash
git clone https://github.com/FatihMakes/Mark-LIV.git
cd Mark-LIV
python setup.py        # installs deps for YOUR OS + the browser automation engine
python main.py
```

`setup.py` only ever installs what your operating system needs — the Windows-only libraries are skipped automatically on macOS and Linux, and vice-versa. It also checks your Python version up front, so a wrong interpreter fails with a sentence instead of a wall of pip output. Prefer to do it by hand? `pip install -r requirements.txt` works too.

> ⚠️ **Installation Note:** If you hit a `ModuleNotFoundError` for an OS-specific package, install it with `pip install <module_name>`. The optional **wake word** engine is *not* installed here — grab it in one click from **⚙ → WAKE WORD** inside the app.

### On Windows: use `run_jarvis.bat`

Double-click **`run_jarvis.bat`** instead of `main.py`.

Double-clicking a `.py` file opens a console, runs it, and closes that console the instant the process exits. If Python exits because of an uncaught exception, the traceback is printed into a window that has already closed — all you see is a flash. That is the single most common "it doesn't work" report, and it is a reporting problem, not a bug in the app.

`run_jarvis.bat` fixes it properly. It finds a supported interpreter, sets the working directory, installs the dependencies if they are missing, and — if the app exits with an error — runs the doctor and holds the window open so the reason can actually be read.

### When it closes instantly anyway

Run the doctor:

```bat
py tools\doctor.py
```

It imports what `main.py` imports, in the same order, and reports the first thing that fails with the real error and the command that fixes it. It distinguishes a package that is *absent* (install it) from one that is *present but will not load* (a broken install or a missing Visual C++ runtime — re-running pip will not help). It also verifies the checkout is complete, that `config/` is writable, and that you are launching from the right directory.

The doctor never reads or prints your API key — only whether one is saved.

---

## 📋 Requirements

| Requirement | Details |
| --- | --- |
| **OS** | Windows 10/11, macOS, or Linux |
| **Python** | 3.11, 3.12, 3.13 or 3.14 |
| **Microphone** | Required for voice interaction (and for the "Hey Jarvis" wake word) |
| **Speakers** | Required for voice replies |
| **API Key** | Free Gemini API key (entered on first launch → `config/api_keys.json`) |
| **GPU** | **Not required.** The avatar is rendered in software |
| **Wake word** *(optional)* | One-click download from ⚙ → WAKE WORD (`openwakeword`, a few MB, fully local) |

> **Python 3.14** is supported. The test suite runs green on 3.11, 3.12, 3.13
> and 3.14, and every dependency in `requirements.txt` — including PyQt6,
> numpy, OpenCV, cryptography, Playwright and `google-genai` — resolves to a
> real 3.14 wheel. On Windows, `pywin32` ships `cp314` builds and the other
> Windows-only extras are pure Python.
>
> `tests/core/test_python_compatibility.py` keeps this honest: it re-checks syntax,
> removed and deprecated standard-library modules, and the asyncio patterns
> that change between versions, against whichever interpreter is running the
> tests. It cannot check that third-party wheels exist for a *future* Python —
> that stays a manual step before declaring support for one.

---

## 🛡️ Safety at a glance

* **Nothing irreversible on the model's say-so.** Shutdown, restart and WiFi
  put a banner on the HUD and run only if *you* press CONFIRM — the token
  comes from the interface, never from the model. See
  [the Foundation Update](docs/foundation-update.md).
* **Undo.** Say "undo" and it reverses its own last file or settings change.
* **One permission table.** Every tool call is checked against
  `core/capabilities.py` before it runs.
* **Minecraft is boxed in.** One confirmation starts a bounded session; the
  subsystem cannot start a process, write a file, reach the network or press
  a key outside a fixed list, and F12 stops everything at once. The bridge
  mod only reads. See [Minecraft](docs/minecraft/README.md) and
  [its architecture](docs/ARCHITECTURE.md).
* **Your data stays here** — see **Your Data** below.

---

## 📚 Documentation

Everything else lives in [`docs/`](docs/README.md):

| | |
|---|---|
| [Capabilities](docs/capabilities.md) | Everything it can do, feature by feature |
| [What's new in Mark LIV](docs/whats-new.md) | The face, talking to it, how it understands itself |
| [The Foundation Update](docs/foundation-update.md) | Memory, undo, confirmation, audio devices, reconnection |
| [Minecraft](docs/minecraft/README.md) | Playing Minecraft Java inside a bounded session |
| [Architecture](docs/ARCHITECTURE.md) | The Minecraft subsystem's modules and safety layers |
| [Project structure](docs/project-structure.md) | What each file and folder is for |
| [Roadmap](docs/roadmap.md) | The Marks so far and what comes next |

---

## 🙏 Third-Party Assets

| Asset | Source | Licence |
| --- | --- | --- |
| `core/face_model.obj` | [MediaPipe](https://github.com/google-ai-edge/mediapipe) canonical face model — 468 vertices of measured human face geometry | Apache License 2.0 |

---

## 🔒 Your Data

Everything stays on your machine. There is no MARK server, no telemetry and no account.

| What | Where | Notes |
|---|---|---|
| Gemini API key, plugin credentials | `config/api_keys.json` | **Plaintext.** Anyone with your user account can read it. Treat it like a password file. |
| Dashboard TLS certificate + private key | `config/certs/` | Generated locally, self-signed, never leaves the machine. |
| What the assistant remembers about you | `memory/long_term.json` | Delete the file to make it forget everything. |

All three are listed in `.gitignore`, so a fork or a pull request cannot leak them by accident. **If you have already committed `config/api_keys.json` anywhere public, revoke that key** at [aistudio.google.com](https://aistudio.google.com/app/apikey) and generate a new one — removing the file in a later commit does not remove it from the history.

Your voice is streamed to Google's Gemini Live API while a session is open; that is the one thing that leaves your computer, and it stops when you mute or close the app.

---

## ⚠️ License

Personal and non-commercial use only.
Licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)**.

---

## 👤 Connect with the Creator

Engineered by a developer building a real-world JARVIS-style assistant.
⭐ **Star the repository to support the journey to Mark 100.**

| Platform | Link |
| --- | --- |
| YouTube | [@FatihMakes](https://www.youtube.com/@FatihMakes) |
| Instagram | [@fatihmakes](https://www.instagram.com/fatihmakes) |
