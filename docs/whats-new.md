# 🆕 What's New in Mark LIV

No hardcoded language, no GPU requirement, no new dependencies — identical on Windows, macOS and Linux.

## The face

### 🧑‍🎤 A real head, rendered in software
The centre of the HUD is now an animated human head. Its face is **real measured human geometry** — MediaPipe's canonical face model, with actual eyelids, nostrils, lips and cheekbones. The skull, neck, jaw rig and lip rig are generated around it at startup, and the whole thing is lit and drawn with **QPainter**, which means:

* **no new dependencies** — it runs on the PyQt6 and numpy the app already needed;
* **no OpenGL, no shaders, no GPU driver** to disagree with you — a VM, a remote desktop session and an old integrated laptop all render the same picture;
* **one 25 KB asset**, and everything else is a formula.

It breathes, sways, blinks, and retints instantly when you change the HUD colour.

### 👄 Lip-sync you can actually read
The old mouth opened to the volume meter, five times a second. The new one produces **~50 mouth shapes per second** from two sources at once:

* **the audio** — a formant read of each 20 ms slice gives openness (how far the jaw drops) and width (spread for *i* and *e*, rounded for *u* and *o*). This is physics, so it is language-independent by construction.
* **the transcript** — because no spectrum can tell you the lips are *closed*. /m/, /b/ and /p/ look identical to a filter bank and completely different on a face. The words supply the shape; the audio supplies the timing and the force.

### 🌍 One rule set, every alphabet
There is no per-language table. Every character is reduced to a bare Latin letter — Unicode decomposition strips accents (é, ü, ş, ğ, ế, ñ, å…), and Cyrillic and Greek transliterate — then articulation is looked up on the sound.

**Turkish, English, German, French, Spanish, Polish, Vietnamese, Czech, Russian, Ukrainian and Greek all work from the same twenty-odd rules.** Scripts whose spelling doesn't reveal pronunciation (Arabic, Chinese, Japanese, Hindi, Korean, Hebrew, Thai) are detected automatically and the mouth runs on the audio-only shape — less detail, never wrong. Adding a language costs nothing, because there is nothing to add.

### 🙂 It acts while it talks
Brows ride the *phrase*, not the syllable, with a slow asymmetry between them. The eyes make real saccades between fixation points, more often while speaking. It blinks. Loud syllables tip the head. Everything relaxes to neutral in silence — and the mouth **only** moves for the assistant's own voice, never for yours.

### ◉ Two HUDs, one toggle
Not everyone wants a face looking back at them. ⚙ → **HUD** swaps the centrepiece between the animated head and a **reactor core** — a gauge ring, three arcs that turn at a rate the state sets, a spectrum ring driven by the real audio level, and a core that brightens with the voice. Both render in the same software painter and cost the same; the choice is taste, and it survives a restart.

Nothing on the core moves for decoration. The rings speed up when JARVIS is thinking, the spikes are the actual waveform, and the colour is the state — the same language the face speaks, without the face.

### 🗣️ It answers before it works
Some replies used to open with three or four seconds of silence: not because a tool was slow, but because JARVIS was still *writing the tool call* — a set of quiz questions, the findings from a contract. The tool was instant; the composing was not, and from the user's side those are the same thing.

The rule is now about the silence rather than the tool: if a gap would form, say one sentence naming what you are starting, then do it. It applies to anything that takes a moment to run **or** a moment to write, without a list of which tools those are.

### 😐 The face is a status light
You read a gaze faster than you read a word, so the head tells you what the assistant is doing before it says anything. It **looks away and holds it while thinking** — brows drawn down, blinking suppressed, the way concentration actually looks — **meets your eyes while listening**, and **lets its lids fall while asleep**. When something appears in the content panel below, it **glances down at it**: a wordless "that landed".

## Talking to it

### 🎚️ Push-to-talk
Wake word is hands-free, but in a meeting or a noisy room a key is faster and never mishears. Turn on **⚙ → PUSH-TO-TALK** and the microphone stays **closed** until you hold **Ctrl+Space** — nothing leaves the machine while you are not holding it. Holding the chord also wakes the assistant, so it doubles as a silent alternative to saying the wake word.

On **Windows** the chord is genuinely global: it works while any other application has focus, implemented by polling two virtual-key codes thirty times a second, with **no new dependency** and no message loop. On **macOS and Linux** there is no dependency-free way to read global key state, so the chord is bound inside the window instead — and the app **says so in the log** rather than pretending otherwise.

### 🔇 It no longer talks itself into replying
Writing audio to a device returns when the buffer *accepts* the sound, not when the speaker has finished with it — so for a moment after a reply "ends", it is still in the room. Streaming the microphone during that gap is how an assistant hears its own last sentence, decides it was addressed, and answers itself.

Mark LIV holds a guard open across that gap, sized from the **device's own reported latency** rather than a guessed constant, so a machine with a large audio buffer gets a longer guard and one with a small buffer is not penalised. The microphone is **not muted** during it: both streams are reduced to band energies and as much of what was just played is subtracted from the microphone as fits, so only your assistant's own voice is dropped — replying the instant it stops still works.

> Interrupting it mid-sentence by voice is built on the same machinery and is deliberately **switched off** in this release. It depends too much on the listener's room to ship without testing on real hardware.

## How it understands itself

### 🪪 It knows what it is, and what it isn't
Who it is, what machine it runs on, what it can do and **what it cannot do** are assembled from the live system at session start — the configured name, the real OS, the tools actually discovered. Install a plugin and it knows it gained an ability; remove one and it stops claiming it.

The **limits** half is the important one: it knows its sight is a single frame on demand rather than a live feed, that it acts on this machine only, and that anything outside its tool list should be stated plainly instead of improvised.

All prompt wording lives in `core/prompt.txt` with `{tokens}` the app fills in — so you can rewrite the personality without touching Python, and a stray brace in your own wording can't break startup.

## 🩹 Fixes
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

