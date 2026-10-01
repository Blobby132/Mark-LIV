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

## 🚀 Capabilities

### Core Features
| Feature | Description |
|---|---|
| 🧑‍🎤 Holographic Avatar | An animated human head in the HUD — real facial geometry, lit and drawn in software, no GPU or extra packages |
| 👄 Real Lip-Sync | ~50 mouth shapes a second from the audio's formants **and** the transcript — closures, spreads and rounds, not a volume meter |
| 🌍 Language-Free Mouth | Articulation is derived by Unicode reduction, so Latin, Cyrillic and Greek scripts all work from one rule set — and scripts that hide pronunciation fall back cleanly |
| 🙂 Facial Acting | Brows track the phrase, gaze saccades between fixations, natural blinking, a small nod on stressed syllables |
| 😐 Face as Status | Looks away while thinking, meets your eyes while listening, lids fall while asleep, glances down at new content |
| 🎚️ Push-to-Talk | Hold **Ctrl+Space** and the mic opens — closed the rest of the time. Truly global on Windows, window-scoped elsewhere |
| 🔇 Self-Echo Guard | Never answers its own last sentence: the tail of its own voice is recognised and dropped without muting you |
| 🪪 Runtime Self-Knowledge | Name, OS, abilities **and limits** are generated from the live system each session — rename it or add a plugin and it knows |
| 🎙️ Wake Word | Local **"Hey Jarvis"** detection — sleeps until called, auto-sleeps after 2 min of silence, and never streams audio while asleep |
| ⚡ Instant Acknowledgment | Speaks a short, context-aware reply in **your language** the instant a longer task starts — no more silent waiting |
| 🚀 Faster Live Engine | Runs on **Gemini 3.1 Flash Live** — roughly 2× faster time-to-first-word than the previous model |
| 🧩 Self-Describing Skills | Actions and plugins share one shape (`TOOL` / `PLUGIN` dict + `run()`), auto-discovered at launch — adding a skill is a single file |
| 🧠 Recallable Memory | No size limit and nothing silently forgotten — the prompt carries what fits, the rest is looked up on demand from a local search |
| 👁️ Memory Panel | See every fact JARVIS has stored about you, when it learned it, and delete any of it in one click |
| ↩️ Undo | Take back what the assistant did — files it moved, renamed, created or wrote, and settings it changed |
| ⚠️ Real Confirmation | Shutdown, restart and WiFi wait for a button **you** press — the model cannot confirm its own irreversible actions |
| 🎧 Audio Device Picker | Choose the microphone and speakers by name, filtered to the short list your OS shows — and measured, so every entry actually works |
| 🔗 Session Continuity | A dropped connection, a voice change or a device change no longer wipes the conversation |
| 🧩 Plugin System | Drop a single `.py` file into `plugins/` — JARVIS learns a new skill on next launch |
| 🎙️ Real-time Voice | Ultra-low latency conversation in any language via Gemini Live API |
| 🎨 Live Theming | Recolour the entire HUD from a hue wheel or hex — the avatar retints with it |
| 〰️ Reactive HUD | Waveform pulses to real audio — your mic while listening, JARVIS while speaking |
| 🎙️ Voice Picker | Choose from 5 native Gemini voices and switch live from the UI — no restart |
| ♾️ Unlimited Sessions | Sliding-window context compression — one conversation can last for hours |
| 🖥️ System Control | Launch apps, adjust volume/brightness, WiFi, shortcuts, power — all by voice |
| 🧩 Autonomous Tasks | High-level planning for complex multi-step goals via agent mode |
| 👁️ Visual Awareness | Screen capture and webcam vision piped into your main Gemini session, labelled by source |
| 🧠 Persistent Memory | Deeply remembers projects, preferences, and personal context across sessions |
| ⌨️ Hybrid Input | Seamlessly switch between keyboard typing and voice commands |
| 🌅 Morning Briefing | On first boot: greets you, reads the time, recaps yesterday, and fetches live news |
| 🔔 Proactive 2.0 | Time-aware, context-aware check-ins — knows the time of day, your projects, and what you've been discussing |
| 🗓️ Session Memory | Summarises each conversation and mentions it naturally next morning — consumed after use, never repeats |
| 👁️‍🗨️ Background Monitoring | User-configured topic watching — checks for new headlines once a day and alerts naturally |
| 📊 Hardware Monitoring | Continuous CPU, RAM, GPU and temperature telemetry with localized voice alerts |
| 🌤️ Weather Report | Live weather data for your city, personalized from memory |
| 🗺️ Dynamic Content Panel | Scrollable display layer beneath the HUD that renders web results, news, and search data |
| 🔍 Multi-Mode Web Search | `news` / `research` / `price` / `compare` / `search` — Gemini Grounded first, DDG fallback |
| ⏰ Smart Reminders | OS-native scheduled notifications (Windows Task Scheduler / macOS LaunchAgent / Linux systemd) |
| ✈️ Flight Finder | Live flight price and availability lookup |
| 🎮 Game Updater | Checks and triggers game updates on Steam and Epic Games on demand |
| 📂 File Processor | Read, summarize, and answer questions about local files |
| 💻 Code Helper | Inline code review, debugging, and generation |
| 🌐 Browser Control | Open URLs, navigate tabs, and interact with the browser by voice |
| 📨 Send Message | Compose and send messages through WhatsApp, Telegram, and more |
| 🎬 YouTube Control | Search, play, and control YouTube playback by voice |
| 🖱️ Desktop Control | Taskbar, window management, and desktop-level operations |
| 🧑‍💻 Silent Language Memory | Detects spoken language on first use — all future sessions adapt automatically |
| 📱 Remote Dashboard | Control the assistant from your phone via QR code pairing |
| ⚡ Auto-Start on Boot | Registers with the OS startup system (registry / LaunchAgent / .desktop) |
| 📋 Clipboard Intelligence | Copy any text → floating panel with Translate / Summarise / Explain / Fix |
| 🪪 Assistant Customization | Change the assistant name, your name, voice, and colour from the UI — takes effect immediately |

---

## 🆕 What's New in Mark LIV

No hardcoded language, no GPU requirement, no new dependencies — identical on Windows, macOS and Linux.

### The face

#### 🧑‍🎤 A real head, rendered in software
The centre of the HUD is now an animated human head. Its face is **real measured human geometry** — MediaPipe's canonical face model, with actual eyelids, nostrils, lips and cheekbones. The skull, neck, jaw rig and lip rig are generated around it at startup, and the whole thing is lit and drawn with **QPainter**, which means:

* **no new dependencies** — it runs on the PyQt6 and numpy the app already needed;
* **no OpenGL, no shaders, no GPU driver** to disagree with you — a VM, a remote desktop session and an old integrated laptop all render the same picture;
* **one 25 KB asset**, and everything else is a formula.

It breathes, sways, blinks, and retints instantly when you change the HUD colour.

#### 👄 Lip-sync you can actually read
The old mouth opened to the volume meter, five times a second. The new one produces **~50 mouth shapes per second** from two sources at once:

* **the audio** — a formant read of each 20 ms slice gives openness (how far the jaw drops) and width (spread for *i* and *e*, rounded for *u* and *o*). This is physics, so it is language-independent by construction.
* **the transcript** — because no spectrum can tell you the lips are *closed*. /m/, /b/ and /p/ look identical to a filter bank and completely different on a face. The words supply the shape; the audio supplies the timing and the force.

#### 🌍 One rule set, every alphabet
There is no per-language table. Every character is reduced to a bare Latin letter — Unicode decomposition strips accents (é, ü, ş, ğ, ế, ñ, å…), and Cyrillic and Greek transliterate — then articulation is looked up on the sound.

**Turkish, English, German, French, Spanish, Polish, Vietnamese, Czech, Russian, Ukrainian and Greek all work from the same twenty-odd rules.** Scripts whose spelling doesn't reveal pronunciation (Arabic, Chinese, Japanese, Hindi, Korean, Hebrew, Thai) are detected automatically and the mouth runs on the audio-only shape — less detail, never wrong. Adding a language costs nothing, because there is nothing to add.

#### 🙂 It acts while it talks
Brows ride the *phrase*, not the syllable, with a slow asymmetry between them. The eyes make real saccades between fixation points, more often while speaking. It blinks. Loud syllables tip the head. Everything relaxes to neutral in silence — and the mouth **only** moves for the assistant's own voice, never for yours.

#### ◉ Two HUDs, one toggle
Not everyone wants a face looking back at them. ⚙ → **HUD** swaps the centrepiece between the animated head and a **reactor core** — a gauge ring, three arcs that turn at a rate the state sets, a spectrum ring driven by the real audio level, and a core that brightens with the voice. Both render in the same software painter and cost the same; the choice is taste, and it survives a restart.

Nothing on the core moves for decoration. The rings speed up when JARVIS is thinking, the spikes are the actual waveform, and the colour is the state — the same language the face speaks, without the face.

#### 🗣️ It answers before it works
Some replies used to open with three or four seconds of silence: not because a tool was slow, but because JARVIS was still *writing the tool call* — a set of quiz questions, the findings from a contract. The tool was instant; the composing was not, and from the user's side those are the same thing.

The rule is now about the silence rather than the tool: if a gap would form, say one sentence naming what you are starting, then do it. It applies to anything that takes a moment to run **or** a moment to write, without a list of which tools those are.

#### 😐 The face is a status light
You read a gaze faster than you read a word, so the head tells you what the assistant is doing before it says anything. It **looks away and holds it while thinking** — brows drawn down, blinking suppressed, the way concentration actually looks — **meets your eyes while listening**, and **lets its lids fall while asleep**. When something appears in the content panel below, it **glances down at it**: a wordless "that landed".

### Talking to it

#### 🎚️ Push-to-talk
Wake word is hands-free, but in a meeting or a noisy room a key is faster and never mishears. Turn on **⚙ → PUSH-TO-TALK** and the microphone stays **closed** until you hold **Ctrl+Space** — nothing leaves the machine while you are not holding it. Holding the chord also wakes the assistant, so it doubles as a silent alternative to saying the wake word.

On **Windows** the chord is genuinely global: it works while any other application has focus, implemented by polling two virtual-key codes thirty times a second, with **no new dependency** and no message loop. On **macOS and Linux** there is no dependency-free way to read global key state, so the chord is bound inside the window instead — and the app **says so in the log** rather than pretending otherwise.

#### 🔇 It no longer talks itself into replying
Writing audio to a device returns when the buffer *accepts* the sound, not when the speaker has finished with it — so for a moment after a reply "ends", it is still in the room. Streaming the microphone during that gap is how an assistant hears its own last sentence, decides it was addressed, and answers itself.

Mark LIV holds a guard open across that gap, sized from the **device's own reported latency** rather than a guessed constant, so a machine with a large audio buffer gets a longer guard and one with a small buffer is not penalised. The microphone is **not muted** during it: both streams are reduced to band energies and as much of what was just played is subtracted from the microphone as fits, so only your assistant's own voice is dropped — replying the instant it stops still works.

> Interrupting it mid-sentence by voice is built on the same machinery and is deliberately **switched off** in this release. It depends too much on the listener's room to ship without testing on real hardware.

### How it understands itself

#### 🪪 It knows what it is, and what it isn't
Who it is, what machine it runs on, what it can do and **what it cannot do** are assembled from the live system at session start — the configured name, the real OS, the tools actually discovered. Install a plugin and it knows it gained an ability; remove one and it stops claiming it.

The **limits** half is the important one: it knows its sight is a single frame on demand rather than a live feed, that it acts on this machine only, and that anything outside its tool list should be stated plainly instead of improvised.

All prompt wording lives in `core/prompt.txt` with `{tokens}` the app fills in — so you can rewrite the personality without touching Python, and a stray brace in your own wording can't break startup.

### 🩹 Fixes
* Answers were sometimes **logged and spoken twice** — the Live API re-sends the tail of a transcript across the several turn-completes a tool call produces. Now de-duplicated at both the chunk and the flush level.
* Asking JARVIS to look at the screen produced **two different answers** — the flow made it speak once *before* the image arrived, so it improvised, and again after. The frame is now attached to the same exchange as its tool result: one turn, one answer, one fewer round trip.
* Screen captures were **unlabelled**, so a screenshot of this app — which has a face in the middle of it — could be read as a photo of the user. Images now carry their source.
* On a non-UTF-8 console (cp1254, cp1251, cp932…) the emoji in the status lines **crashed the session on startup**. Streams are reconfigured at launch, so it starts the same way in every locale.
* The HUD kept rendering the avatar **while the window was hidden or minimised**. It now stops, and resumes mid-motion rather than snapping.
* Activity-log lines were fixed amber and ignored the theme; they now follow the accent colour.
* Dependencies had no upper bounds, so the next major release of any of them would break every fresh clone. The load-bearing ones are now capped.
* The mouth **ran ahead of the words, then behind them**. Three separate faults compounded: each 200 ms of audio only produced 160 ms of mouth shapes, every new batch overwrote the previous one instead of continuing it, and the schedule was anchored to the moment audio was *handed to the device* rather than the moment it becomes *audible*. The mouth now follows one continuous playback clock — measured at **15 ms** of timing error whether the sound card buffers 100 ms or 500 ms.
* The jaw was driven by the **waveform meter**, which deliberately holds peaks so the bars don't flicker. That hold spanned exactly the consonants the mouth needed to close on. It now reads the audio's own 20 ms levels, so the gaps between words are real gaps.
* Mouth timing was measured in **frames rather than seconds**, so the same constants meant three different mouths at 60, 30 and 20 fps, and a closure shorter than one frame could vanish entirely. Timing is now in seconds and the mouth is stepped once per 20 ms of audio, not once per repaint.
* The **brows barely moved** — 6 px of travel on a 250 px head, because the rig weights halved an already small constant. Derived from the anatomy instead: 19 px.
* The activity log opened with **a dozen lines of plumbing** — one per plugin loaded, plus wake-word and briefing status. The console still carries the full boot transcript; the log now shows your conversation, state changes and anything you have to act on, and nothing else.

> Built on the Mark LI–LIII foundation: the **🧩 Plugin System**, **♾️ Unlimited Sessions**, **🎨 Live Theming**, **🎙️ Wake Word** and **🧩 Self-Describing Skills** are all still here.

---

## 🔄 The Foundation Update — in every Mark from LII

These four landed across **Mark LII, LIII, LIV and LV at the same time**, after each of those releases had already shipped. They are not what any one of those versions originally introduced; they are the floor all of them now stand on, so moving up a Mark never costs you something the one below it had.

No new dependencies. No bundled asset files. No hardcoded language, and nothing that assumes one operating system.

### 🧠 A memory that actually remembers

The store was capped at **2,200 characters — the whole memory, not per entry** — because all of it was pasted into the system prompt on every connect, so growing the memory grew every request. When it filled, the oldest entries were deleted and one line was printed to a console nobody reads. An assistant advertised as remembering "projects, preferences and personal context" was in practice a two-page notepad that quietly forgot your sister's name after a few weeks.

Storage and prompt budget are now separate problems:

* **Nothing is deleted.** The cap is a runaway guard normal use never approaches, and if it is ever hit it says so in the activity log instead of on stdout.
* **The prompt carries a core, not a dump.** Identity in full, then the most recently updated facts, budgeted — measured at **971 characters on a memory holding 62 stored facts.** That is *smaller* than the old whole-store cap, so sessions now connect with fewer tokens than before.
* **The rest is fetched on demand.** A `recall_memory` tool searches the full store locally — no network, no second model, well under a millisecond.

The part that is easy to get wrong: **a model cannot look something up if it doesn't know the thing exists.** So the prompt also carries an **index of the keys** it had no room for. Without it, "who is Ayşe?" gets "I don't know" while `ayse_sister` sits on disk unread. That index interleaves categories rather than sorting by recency — sorted like the core, a memory with forty preferences pushed the one entry the index existed for off the end.

⚙ → **🧠 MEMORY** shows every stored fact, when it was learned, and a ✕ to forget it. Everything stays in `memory/long_term.json` on your machine.

### ↩️ Undo — it can take back what it did

JARVIS moves files, renames them, writes to them and changes your settings. None of that had a way back; if it misheard you, the only remedy was to fix it by hand.

Say **"undo"** — in any language — and it reverses its own last action:

| | |
|---|---|
| **Files** | move · rename · create · copy · write · delete · organize desktop |
| **Settings** | volume · brightness · dark mode |

Three things it deliberately does *not* do:

* **It does not guess.** Settings undo reads the current value *before* changing it. Where a platform won't report that value, nothing is registered — an undo that restores a guess is worse than no undo.
* **It does not hoard.** Undoing a write means keeping the old contents in memory, so files over 1 MB are excluded and it says so rather than holding a 200 MB log for the session.
* **It does not delete your files to undo a copy.** The reverse of a copy is removing the copy; the reverse of "create a folder" is removing it *only while it's still empty*.

`organize_desktop` gets special treatment — one command that moves dozens of files, which made it the least reversible thing the assistant could do. It journals every move and puts all of them back in one go, cleaning up the folders it created if they're still empty.

**Undo costs nothing at runtime.** It appends a closure to a list; nothing in it runs unless you ask.

### ⚠️ A confirmation the model can't forge

The old gate read like this:

```python
if action in _DANGEROUS_ACTIONS:            # {"restart", "shutdown"}
    confirmed = str(params.get("confirmed", "")).lower()
```

`confirmed` is a **tool parameter, which means the model fills it in.** Nothing stopped it sending `confirmed=yes` on the first call and nothing checked that a human was ever involved. It was a convention, not a gate. And its coverage was two actions — so `toggle_wifi`, which cuts the assistant's own connection to the Live API and therefore *cannot be asked to undo itself*, went through with no gate at all.

The token is now issued by the interface. Shutdown, restart and WiFi put a banner on the HUD and **return immediately**; the action runs only if you press CONFIRM. Nothing blocks — JARVIS keeps talking while the banner is up — so this is **cheaper than the old gate**, which burned two tool round trips on every power command.

> The split between the two mechanisms is about reversibility, not about how alarming a word sounds. Anything undoable is done at once; only the genuinely irreversible asks. An assistant that checks with you before turning the volume down is one you stop talking to.

### 🎧 It finally asks which microphone

Both audio streams opened with no device argument at all, so they always took whatever the OS called "default" — and on Windows that *moves on its own* the moment you plug a headset in. "JARVIS can't hear me" almost always meant "JARVIS is listening to the webcam".

⚙ → **🎧 AUDIO DEVICES** lets you pick the microphone and the speakers by name. Two things matter more than the dropdown:

**The list is short.** `query_devices()` returns one entry per *device × host API*, not per device — measured on an ordinary Windows machine, **41 entries for what the sound settings show as 4 microphones and 4 speakers.** The same microphone appears four times, under MME, DirectSound, WASAPI and WDM-KS, with nothing to say which is which. That is not a choice, it's a quiz. The picker takes one host API per direction, drops the "Sound Mapper" and "Primary Sound Driver" pseudo-devices that just mean "default", and deduplicates. **41 → 8.**

**Every entry has been measured, not assumed.** The obvious approach is to pick the host API with the nicest names — WASAPI on Windows, which in shared mode **doesn't resample**, so with 16 kHz in and 24 kHz out against 48 kHz hardware every open failed. Adding a rate check and moving to DirectSound passes that test on both sides, and PortAudio's DirectSound **output is a silent sink**: the stream opens, every write returns success in ~0 ms, and not one sample reaches the speakers.

| | write(2.0 s) took | |
|---|---|---|
| MME | **2.02 s** | consumed in real time |
| DirectSound | **0.00 s** | swallowed instantly |

No capability flag reports that. So the app measures it — once per host API per direction, on a background thread at startup, using silence. Two consequences worth stating plainly:

* **Each direction picks its own host API.** On Windows this lands on DirectSound for the microphone and MME for the speakers — a split no amount of reasoning would have produced.
* **The probe runs in the mode the app actually ships.** DirectSound input passes a callback stream and fails a blocking read; probing the wrong mode rejected a microphone that works perfectly.

Your choice is stored **by name, not by index** — indices shift whenever something is plugged in. If the saved device is gone, it falls back to the system default and says so in the log rather than failing to start.

### 🔗 It stops forgetting the conversation when the connection drops

`session_resumption` was switched on in the config and the handle the server sent back was **never read** — so every reconnect started an empty session. A dropped packet, or simply changing the voice, wiped the conversation. "Unlimited sessions" leaked through exactly this hole.

The handle is captured and replayed now. A network blip, or switching your microphone, keeps the conversation intact.

It is held in memory only, deliberately: writing it to disk would make a fresh launch continue yesterday's chat, which sounds appealing but breaks the session-summary flow — a conversation that never ends never produces a summary, and the "yesterday we talked about…" line in the morning briefing silently disappears. Changing the **voice** also starts clean on purpose, since resuming restores the server's session state and would likely bring the old voice back with it.

### 🩹 Fixes that came with it

* **The assistant could die on a log line.** Status lines carry emoji and arrows (`📤 file_controller → Moved: a.txt → Documents/`). On a non-UTF-8 console — cp1254 on a Turkish Windows, cp1251 on a Russian one, cp932 on a Japanese one — printing one raises `UnicodeEncodeError`, and because that print sits *after* the tool's own `try/except`, it escaped into the receive loop and took the session down.
* **Every computer command paid for two model round trips.** `computer_settings` made an *entire second Gemini call, inside the tool*, purely to translate the request into one of its own action names — because the declaration only said "The action to perform", so the model rarely filled it in. When that second call failed, the fallback was `description.lower().replace(" ", "_")`, which turns the Turkish for "turn it down" into `sesi_kis` and straight into "Unknown action". The declaration now names all 56 actions and the rest is spelling tolerance handled locally by `difflib` in microseconds. When nothing matches it suggests real action names instead of dead-ending.
* An unresolvable saved audio device, or one the driver refuses to open, falls back to the system default and says so — on both the microphone and the speakers.
* A rejected session-resumption handle is dropped after one attempt, so an expired handle can never be replayed on every retry and prevent the reconnect it exists to protect.



---

## 🎮 Minecraft (experimental)

JARVIS can observe and play Minecraft Java Edition, inside a bounded, revocable session.

**How much it can see depends on one optional mod.** `mods/markliv-bridge-1.0.0.jar` is a small **read-only** Fabric mod: it publishes client state to one JSON file and accepts nothing back. It is not a command channel, and it cannot be turned into one — it has no input path at all. Install it with `install_mod.bat`.

| | Without the mod | With the mod |
|---|---|---|
| Position, facing | F3 overlay via OCR, if Tesseract is installed | exact, every tick |
| Block you are aiming at | F3 overlay via OCR | exact |
| Inventory, health, hunger | not readable | exact |
| **The terrain around you** | **not readable** | **surface heights within 10 blocks** |
| **Nearby blocks and mobs** | **not readable** | **with coordinates and categories** |
| Finding a tree | sweep the crosshair and hope | look it up and walk there |

**What it can do now.** Walk, turn, jump, sneak, sprint, select a hotbar slot, mine, place, interact, eat, drop, open the inventory. Placing is a right-click, and a right-click does what the held item does — so `place` checks the hand first and refuses a bucket, flint and steel, TNT, a spawn egg, an ender pearl, a potion, a bow or a tool, and refuses when it cannot see the hand at all. Run bounded multi-step tasks that observe and **verify** between every step: `walk_forward`, `survey`, `find_block`, `break_block`, `place_block`, `collect_logs`, `fell_tree`, `collect_blocks`, `craft_item`, `place_block_at`, `build_line`, `build_blueprint`, `navigate_to`, `flee`, `fight`, `aim_at_block`, `mine_block`, `eat_food`.

**Gathering other blocks.** `collect_blocks` uses the same machinery as `collect_logs` for stone (cobblestone), dirt, sand, gravel, deepslate and coal, iron and copper ore. It only takes blocks it can reach that are already exposed — it never digs, and says a buried block is not reachable without digging — never the block under your feet, and never one touching water or lava. It aims at the face that is actually open (a block set in the ground can only be hit from above), takes up the right tool, and counts success only from the drop arriving in your inventory.

**Crafting.** `craft_item` crafts by clicking in the grid — the inventory's 2×2, or a crafting table's 3×3 within reach — exactly as a player would: bring the pointer onto a stack, pick it up, put one into each cell, put the rest back, shift-click the output. It knows planks, sticks, the crafting table, wooden and stone tools, the furnace, torches and the chest, and checks the ingredients before it starts. A 3×3 recipe with no table in reach and one in the inventory puts the table down beside you first, by `place_block_at` (below), and then uses it. Every click passes a gate that reads the game's own report of where the pointer is and which screen is open (see `docs/minecraft-gui.md`): it never clicks between slots, never in a chest, a furnace or the creative inventory, and stops — closing the screen — if a hostile comes within eight blocks or you are hurt. The result is proved from the inventory.

**Navigation.** With the mod running, `navigate_to` reads the terrain scan, runs A\* over it, and walks the route. The player is modelled as a body, not a point: 0.6 blocks wide, two blocks tall, stepping up only 0.6 of a block (a slab) without jumping. The mod reports the headroom above every column, so a route under a low branch or a one-block ledge is refused rather than walked into. Where the route steps up a full block it walks **and** jumps in one action (`move_and_jump`) — the only way onto a ledge in Minecraft. Straight stretches become single long strides; near obstacles the strides are short. For each column the mod reports the floor nearest your feet with room to stand — so the ground under a tree's canopy is walkable and a grass tuft is not mistaken for a missing floor — plus anything you would be standing in, so a berry bush or fire is walked round. `look_around` answers "what is near me" in a sentence: the trees in view, the nearest blocks and mobs with coordinates, and your health, hunger, the food on your hotbar and whether it is night.

**When it gets stuck, it says why.** Eight kinds, each with its own recovery: a one-block step (hop), a wall or low ceiling (re-route), a mob in the way (go round it), unscanned ground (look again), a route over changed ground (re-plan), no route at all, input not landing (open ground and no movement — usually focus), and aiming that will not converge.

**It measures your mouse.** The mod reports Minecraft's sensitivity slider and `minecraft/aiming.py` computes the exact pixels-per-degree from it. Only the *direction* of each axis is still observed, because nothing reports it. Aiming is closed-loop: correct, observe the real rotation, correct again.

**Mining.** It holds attack only when the mod confirms the crosshair is on the exact block it means — nothing is ever swung at by accident. Leaves between it and a log are broken on purpose, a few at most, at the exact coordinate the crosshair reports, and never counted as logs. Before swinging it takes up the best tool in the hotbar for that block — an axe for a log, a stone pickaxe or better for iron — and puts your slot back when the task ends. When the right tool — or one that saves at least a second and a half a block — is only in the main inventory, it opens the inventory, puts the pointer on it, presses the hotbar number key to swap it into the hotbar (an empty slot, else one holding blocks rather than a tool or food), closes the inventory and says where it went. A block nothing it can hold will harvest (stone with no pickaxe anywhere, iron with a wooden pickaxe) is refused before any swing, with the reason. The hold length comes from Minecraft's own break-time formula (block hardness, tool, whether you are on the ground), and it lets go the moment the block goes. Success means that exact coordinate changed with the camera held still — or, when the inventory is readable, that the item arrived.

**Trees, one at a time.** Logs are grouped into trees. `collect_logs` finishes the tree it started before moving to the next; `fell_tree` takes every log of one tree it can reach, picks them all up and says how many are left too high. Whatever it breaks, it picks up before it stops — including when it gives up — and the report names the tree every log came from, so "why that tree?" has a true answer.

**Placing at a coordinate.** `place_block_at` puts one plain building block (dirt, cobblestone, stone, planks and the like — nothing that falls, nothing that does anything when right-clicked) into one exact cell. It places only into space the scan saw empty, or into grass the game replaces. Below the ground, above the headroom the scan measures, or outside the scan is unknown, and it refuses. It places against a solid block next to the cell that it knows of, never a chest, door or crafting table, and stands within reach, on the outside of that face and out of the cell. It presses nothing until the game reports the crosshair on that block *and* that face; the controller checks both again at the instant it presses. Success needs two proofs: the crosshair (aimed through the cell) now shows that block there, and the held stack is one smaller. In creative the stack does not go down, and it says the crosshair is the only proof. It never presses twice: a press it cannot prove is reported, not repeated.

`build_line` lays up to 16 blocks in a straight line (north, south, east, west or up), one `place_block_at` after another. Each block can go against the one before it. A cell that fails is reported and the next one tried, until three fail in a row or the blocks run out. The report lists every cell placed and every cell that failed with the reason, and checks the total against the inventory. A column built from the ground stops at two blocks: the third goes on a top face above a standing player's eyes, and placing while jumping is not built yet.

`build_blueprint` builds a named plan bottom-up from those same proven placements:
- a `platform` (2 to 5 across);
- a `wall` (2 to 8 long, two high);
- a hollow 3×3×3 `shelter`: walls two high with a doorway, a roof, and a three-block step along one side wall to stand on for the roof, 26 blocks.

Before pressing anything it checks every limit:
- at most 64 blocks;
- every cell within 6 blocks of where it starts;
- every cell empty or grass;
- flat ground under the bottom layer;
- plain building blocks only, and enough of them for the whole plan.

It refuses if any fails. The model says the plan in one sentence first, and the task's "started" answer repeats it. Within a layer it places the furthest cell it can reach, so no block hides the face of the next one. While it builds it never stands inside the structure's footprint or routes over its walls, except on that step. The roof is placed from the step. It runs the length of a side so that a hop onto it cannot carry past it. A task's 45 steps place ten to fifteen blocks, so a shelter took three or four tasks in the simulation. Progress is kept in memory, and asking again carries on. Every report lists the cells placed and the cells failed, and what is still to place, counted against the inventory. When the plan is done it is checked block by block against the mod's `near_blocks`: every non-air block within 4 of the player, from one below the feet to four above. A cell that does not hold the block goes back on the list, and the report names it. With that list, a cell under a wall's top is known; the terrain scan, with one floor per column, could not say.

**Getting away.** `flee` runs to the ground it can reach that is furthest from every hostile mob it can see, keeping two blocks clear of each on the way. It sprints where the way is straight and clear, and chooses again every few steps as they move. It stops when the nearest is more than 12 blocks off, or when its time runs out, and says where the mob is. Unlike every other task it is never stopped by a mob being close or by taking damage — that is what it is for.

**Fighting (opt-in).** `fight` runs only when you ask for it. It takes the nearest hostile mob within 16 blocks, or the kind you name, walks into reach, aims at the middle of its body, and hits in short taps every other step, so each swing is at full strength after the cooldown. Every swing carries a precondition the controller checks as the button goes down: the game must report a *hostile* mob under the crosshair. A player or an animal that steps in front is never hit. It will not walk up to a creeper or a warden. Below 8 health it retreats with `flee`, and it stops after 20 seconds. A hit is judged by the knockback the game gives a hit mob; the bridge does not report a mob's health, so "gone" is reported as most likely killed, not as proven.

**Eating.** `eat_food` eats from the hotbar: food that will not make you ill, the most filling that does not overshoot what is missing, never a golden apple. With a chest, door or campfire under the crosshair it looks up first — right-clicking would use that instead — and it puts your held slot back afterwards. With no food on the hotbar it fetches food from the main inventory the same way the mining tasks fetch a tool. While the inventory is open, a hostile within 8 blocks or any lost health closes it and stops the task. The one exception is the damage starving does on its own: that gets one more try. Without a mod that reports screens, or in creative, it names the food and asks you to move it.

**It looks up from the job.** Between every step, a task checks your health and the mobs around you. Losing a heart, or a hostile mob within five blocks at your level, stops the task and says which — before it starts, too. Walking is the exception, because walking is how you get away: `navigate_to` keeps going and ends with a note of anything close, and a single move, sprint or sneak is never stopped by a mob or by damage — not even with a zombie at arm's length. Working holds (mining, eating, using or placing a block) also let go mid-hold for a hostile within three blocks or a heart lost. It does not fight or flee on its own; that is your call.

**Perception.** The mod is authoritative. When it cannot say, a colour/texture classifier gives a *labelled guess* — `visual_high_confidence` or `visual_low_confidence` — which can steer the camera but can never authorise breaking anything.

**Verification is the point.** "I held the attack button" and "the block broke" are different answers, and the system reports them separately. A swing that lands on an unbroken log is recorded as delivered-but-failed, not as success. When it cannot see the target at all, it says `unverifiable` rather than guessing either way. Evidence is ranked: the inventory beats a block vanishing from the scan, which beats the crosshair changing — and the result says which one it used.

**What it deliberately cannot do.** Type in chat. Run slash commands — `minecraft.command` is `DENY` permanently. Launch the game. Touch anything outside the Minecraft window. Click in any screen but your inventory and a crafting table — never a chest, a furnace or the creative inventory — or anywhere the game does not report the pointer over the slot it means; a click between slots would drop what it carries, so it cannot happen. Dig through or bridge over an obstacle. Build higher than it can reach from the ground or a step — it does not jump and place. Walk anywhere it cannot currently see.

**The safety boundary.** The subsystem cannot start a process, reach the shell, open a browser, send a message, or write a file — `tests/test_minecraft_boundary.py` parses every module and fails the build if that changes. `minecraft/navigation.py` imports nothing but `heapq`, `math` and `dataclasses`, and that is asserted from the import graph rather than from its docstring. Input is limited to a fixed table of keys and two mouse buttons; there is no function anywhere that takes a keycode. Everything held is recorded before it is pressed and released by a single `release_all()` reachable from five independent stops: focus loss (checked every 40ms), F12, a deadman timer, session expiry, and the game closing.

**Consent: one confirmation, for the whole session.** You approve once, and that covers every gameplay action until you stop it — there is no per-swing dialog, on purpose, because a prompt per swing is how people learn to dismiss prompts unread. The grant is set membership, not a name prefix, so a capability added to `minecraft.*` later is *not* covered by an old approval. Chat, slash commands and launching the game are outside it permanently, and nothing the model can call grants itself the session.

**Requires Windows** for input (Linux and macOS can observe but not control) and Minecraft in windowed or borderless mode. OCR is only needed if you are *not* running the bridge mod.

Test it against a real game with a throwaway creative world:

```bat
py tools\minecraft_manual_check.py
```

It is interactive and never autonomous: it says what it is about to do, waits, does one bounded thing, and asks what you saw. That one covers the raw input plumbing. For gameplay — aiming, mining with coordinate and inventory proof, hopping a step, going round a wall, safety stops and guided voice checks — run:

```bat
gameplay_check.bat
gameplay_check.bat C      &:: just the mining section
```

**Voice diagnostics.** Type `voice check` in the HUD text box to see where your speech is going: frames captured, held back by a gate (and which one), queued, **dropped**, sent, transcribed, answered. If an utterance vanishes, JARVIS logs `VOICE_PIPELINE_LOST_INPUT` naming the stage it died at, or `VOICE_HEARD_BUT_UNANSWERED` when Gemini transcribed it and did nothing. Set `"voice_debug": true` in `config/api_keys.json` for a one-line summary after every turn. Proactive audio is now **off by default**; `"proactive_audio": true` restores it.

---

## 🗺️ Mark Roadmap

| Mark | Focus |
|---|---|
| **XLIX** | Auto-start · clipboard intelligence · assistant customization |
| **L** | Session memory · background monitoring · proactive 2.0 · instant vision |
| **LI** | Plugin system · affective dialog · proactive audio · unlimited sessions |
| **LII** | Voice picker · live theming · reactive HUD · recallable memory · undo · real confirmation · audio device picker · session continuity |
| **LIII** | Wake word · Gemini 3.1 Flash Live · instant acknowledgment · self-describing action/plugin architecture |
| **LIV** | Holographic avatar · viseme lip-sync · facial acting · face-as-status · push-to-talk · self-echo guard · runtime self-knowledge & limits |
| *shared* | The last five above also shipped to LIII, LIV and LV at the same time — moving up a Mark never loses them |
| **LV+** | Interrupt by voice · conversation history · plugin files: email · quiz mode · calendar · home assistant · 3D-printer |

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
> `tests/test_python_compatibility.py` keeps this honest: it re-checks syntax,
> removed and deprecated standard-library modules, and the asyncio patterns
> that change between versions, against whichever interpreter is running the
> tests. It cannot check that third-party wheels exist for a *future* Python —
> that stays a manual step before declaring support for one.

---

## 🗂️ Project Structure

```
Mark LIV/
├── main.py                   # Core loop — Gemini Live session, audio I/O, viseme extraction, tool dispatch
├── ui.py                     # PyQt6 HUD — avatar canvas, waveform, log panel, settings drawer, camera feed
├── setup.py                  # OS-aware installer (skips wrong-OS dependencies, checks your Python)
├── .gitignore                # Keeps your API key, TLS key and memories out of the repository
├── plugins/
│   ├── quiz.py               # Interactive quiz — JARVIS writes the questions, you answer on screen
│   ├── document_review.py    # Contracts and policies in plain language, ordered by what matters
│   ├── _google_core.py       # Shared OAuth for the Gmail/Calendar plugins (not a plugin itself)
│   ├── _printer_core.py      # Shared printer connectivity (not a plugin itself)
│   ├── _template.py          # Copy this to write a new plugin — one file, drop in, done
│   └── ...                   # Drop-in skills (each self-describes via a PLUGIN dict + run())
├── actions/                  # Bundled skills — each self-describes via a TOOL dict + handler
│   ├── web_search.py         # Gemini + DDG parallel search (news, research, price, compare)
│   ├── screen_processor.py   # Screen & webcam capture for vision
│   ├── background_monitor.py # User-configured topic watching — daily DDG check
│   ├── proactive.py          # Proactive 2.0 — time/context/rotation-aware check-ins
│   ├── reminder.py           # OS-native scheduled notifications
│   ├── system_monitor.py     # CPU / RAM / GPU / temperature telemetry
│   ├── computer_settings.py  # Volume, brightness, WiFi, power (per-OS)
│   ├── computer_control.py   # Keyboard shortcuts, mouse, window management
│   ├── open_app.py           # Application launcher (per-OS name map)
│   ├── browser_control.py    # Web browser control
│   ├── file_controller.py    # File system operations
│   ├── file_processor.py     # Document reading and summarization
│   ├── send_message.py       # Messaging integration
│   ├── weather_report.py     # Live weather data
│   ├── flight_finder.py      # Flight search
│   ├── youtube_video.py      # YouTube playback control
│   ├── game_updater.py       # Game update management (Steam / Epic)
│   ├── code_helper.py        # Code review and generation
│   ├── dev_agent.py          # Developer task agent
│   └── desktop.py            # Desktop and taskbar control
├── memory/
│   ├── memory_manager.py     # Load/save long_term.json — sessions, monitors, identity
│   ├── config_manager.py     # api_keys.json access — key, OS, name, voice, colour, toggles
│   └── long_term.json        # Persistent store — created on first run
├── core/
│   ├── prompt.txt            # All prompt wording — {tokens} are filled from the live system at startup
│   ├── avatar.py             # Avatar renderer — lighting, pose, expression, mouth (QPainter)
│   ├── avatar_mesh.py        # Head geometry — loads the face, generates skull/neck/rigs
│   ├── face_model.obj        # The face itself (MediaPipe canonical model, Apache-2.0, 25 KB)
│   ├── viseme.py             # Transcript → mouth shapes, fused with the audio's timing
│   ├── echo.py               # Tells your voice from the assistant's own echo; self-calibrating
│   ├── hotkey.py             # Push-to-talk chord — global on Windows, windowed fallback elsewhere
│   ├── undo.py               # One shared undo stack — actions register how to reverse themselves
│   ├── confirm.py            # Irreversible-action gate — the token is issued by the UI, not the model
│   ├── audio_devices.py      # Microphone / speaker list — filtered, measured, resolved by name
│   ├── plugin_loader.py      # Plugin engine — discovery, validation, crash isolation
│   ├── action_loader.py      # Bundled-action engine — the built-in twin of plugin_loader
│   └── wake_word.py          # Local "Hey Jarvis" detector — own thread, offline, opt-in
└── config/
    ├── api_keys.json         # API key, name, voice, colour, toggles — created on first launch (git-ignored)
    └── certs/                # Self-signed TLS pair for the phone dashboard — generated locally (git-ignored)
```

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
