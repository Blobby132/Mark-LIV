# MARK LIV Bridge — the companion mod

A small client-side Fabric mod that lets JARVIS see the game.

**The built mod is already in this repository: [`mods/markliv-bridge-1.0.0.jar`](../mods/markliv-bridge-1.0.0.jar).**
You do not need Java, Gradle, or any of this folder to use it.

---

## What it does

Minecraft already computes everything the assistant wants to know — where you
are, what block is under the crosshair, what is in your inventory — and then
draws it as pixels. Recovering that by reading the pixels back with OCR was
lossy, needed a native Tesseract install, could not see the inventory at all,
and was wrong often enough to matter. A misread health bar is a death.

This hands over the numbers instead:

| | Without the mod | With it |
|---|---|---|
| position, rotation | `inferred` (OCR, if installed) | **`exact`** |
| targeted block | `inferred` | **`exact`** |
| **health, hunger** | unknown | **`exact`** |
| **inventory contents** | unknown | **`exact`** |
| **held item** | unknown | **`exact`** |
| **nearby entities** | unknown | **`exact`** |
| biome, dimension, time, weather | partial | **`exact`** |

"Collect 4 logs" can only mean *four logs in the inventory* with this
installed. Without it, the best available claim is *four blocks seen to
disappear*, which is a weaker and different thing — an item that fell in lava
was broken and never collected.

## Install

1. Install **[Fabric Loader](https://fabricmc.net/use/installer/)** for
   Minecraft **26.3**.
2. Drop `markliv-bridge-1.0.0.jar` into your `mods` folder
   (`%APPDATA%\.minecraft\mods` on Windows).
3. Start Minecraft with the Fabric profile.

There is nothing else. **Fabric API is not required** — this mod depends only
on the loader.

Confirm it is working:

```powershell
py tools\bridge_check.py
```

## It is read-only, and that is the design

The mod observes and writes one file. It has no command channel, no socket, no
listener, and no way to be told to do anything.

Input still travels the long way round — as synthetic keystrokes through the
operating system, past the focus guard and the session authorisation that
governs every action JARVIS takes. That separation is deliberate: a bridge
that could also act would put a control channel *inside* the game process,
where Alt-Tab, F12 and the focus guard could not reach it. Every stop this
subsystem relies on works precisely because input arrives from outside.

The worst a broken bridge can do is report nonsense, which the Python side
already treats as unknown.

## A file, not a socket

The mod writes `%LOCALAPPDATA%\MarkLIV\minecraft_state.json` five times a
second, atomically (temp file, then rename), so a reader never sees half a
document.

No port to choose, no firewall prompt, nothing listening on your machine, and
nothing on the network can reach it. Both sides compute the same path from the
same environment variable rather than hunting for a `.minecraft` directory,
whose location varies by launcher — and where a wrong guess would be
indistinguishable from the mod not running.

Every payload carries the moment it was written. Anything older than three
seconds is discarded rather than believed: a file on disk outlives the process
that wrote it, and a closed game's last frame must not pass as current.

## Building it yourself

Only needed if you change the Java.

```bash
cd fabric-mod
./gradlew build      # needs JDK 25; the wrapper fetches Gradle 9.7
```

The jar lands in `build/libs/`.

### Notes for whoever maintains this

* **Minecraft 26.3 ships unobfuscated.** `net/minecraft/client/Minecraft.class`
  is in the jar under its real name, which is why Mojang publishes no
  `client_mappings` and Yarn has none. Loom is told
  `mappings "net.fabricmc:intermediary:0.0.0:v2"` — an identity mapping.
* **No Fabric API.** `Minecraft.execute()` gives a client-thread hook on its
  own, so the mod needs only the loader: one fewer thing to install, one fewer
  version to keep in step.
* **No sources jar.** Asking for one makes Loom remap every dependency's
  sources at configure time, which does not survive an unobfuscated Minecraft.
* Two renames caught at compile time in 26.3: `ResourceLocation` is now
  `Identifier` and the accessor is `identifier()`; `Level.getDayTime()` is
  gone, replaced by `getDefaultClockTime()`.
