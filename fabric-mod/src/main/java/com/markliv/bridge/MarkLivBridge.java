package com.markliv.bridge;

import net.fabricmc.api.ClientModInitializer;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.HitResult;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * Publishes what the Minecraft client already knows, to one file, for MARK LIV
 * to read.
 *
 * <h2>Why this exists</h2>
 * Everything the assistant wants to know -- where you are, what block is under
 * the crosshair, what is in your inventory -- the client computes every tick
 * and then draws as pixels. Recovering it by reading those pixels back with
 * OCR is lossy, slow, dependent on a native install, and wrong often enough to
 * be dangerous: a misread health bar is a death. This hands over the numbers
 * instead.
 *
 * <h2>This mod is read-only, and that is the point</h2>
 * It has no command channel, no socket, no listener and no way to be told to do
 * anything. It observes and writes one file. Input still travels the long way
 * round -- through the operating system, as synthetic keystrokes, past the
 * focus guard and the session authorisation that governs every action MARK LIV
 * takes.
 *
 * <p>That separation is deliberate. A bridge that could also act would make
 * the assistant's safety story depend on this mod being correct, and would put
 * a control channel inside the game process where none of the existing stops
 * -- Alt-Tab, F12, focus loss -- could reach it. Keeping it read-only means the
 * worst a broken bridge can do is report nonsense, which the Python side
 * already treats as "unknown".
 *
 * <h2>No Fabric API dependency</h2>
 * The obvious way to run code every tick is Fabric API's tick event, and it
 * would cost the user another mod to install. It is not needed: scheduling
 * onto the client thread with {@code Minecraft.execute} is plain Minecraft, so
 * a background thread can ask for a snapshot at its own pace and this mod
 * depends on the loader alone.
 *
 * <p>The snapshot itself still runs on the client thread, because reading the
 * player and the level from any other thread races the game's own writes and
 * returns values that were never simultaneously true. Only the file write
 * happens off-thread, so disk latency never stalls a frame.
 *
 * <h2>A file, not a socket</h2>
 * No port to choose, no firewall prompt, no listening service on the machine,
 * and nothing for anything else on the network to connect to. The write is
 * atomic (temp file, then rename), so a reader never sees half a document, and
 * every payload carries the tick it was taken on so a stale file is detectable
 * rather than quietly believed.
 */
public class MarkLivBridge implements ClientModInitializer {

    /** How often to publish. Five times a second is far more often than an
     *  assistant can act on, and cheap enough not to matter. */
    private static final long PUBLISH_INTERVAL_MS = 200L;

    /** Entities further than this are not reported. */
    private static final double NEARBY_RADIUS = 16.0;

    /** Cap on reported entities, so a mob farm cannot produce a huge file. */
    private static final int MAX_ENTITIES = 24;

    /**
     * Horizontal reach of the terrain scan, in blocks.
     *
     * <p>Ten gives a 21x21 footprint -- far enough to plan a path around an
     * obstacle, short enough that the scan stays inside chunks the client has
     * certainly loaded. Beyond about sixteen the client may not have the data
     * and would report air where it simply has not looked, which is exactly
     * the lie this whole design refuses to tell.
     */
    private static final int SCAN_RADIUS = 10;

    /** How far above and below the player's feet each column is searched. */
    private static final int SCAN_UP = 4;
    private static final int SCAN_DOWN = 5;

    /**
     * Cap on individually reported blocks of interest.
     *
     * <p>A jungle fills the scan volume with logs; reporting every one would
     * be a large file describing a decision nobody needs it to make. The
     * nearest few dozen are enough to pick a target.
     */
    private static final int MAX_NOTABLE = 64;

    /** How far above a surface block to bother measuring empty space. */
    private static final int MAX_CLEARANCE = 4;

    /** Blocks of passable space a standing player needs. */
    private static final int PLAYER_HEIGHT = 2;

    /**
     * Refuse to write beyond this. A payload that grows without bound is a
     * bug somewhere above, and truncating is better than handing the reader a
     * file it must parse five times a second.
     */
    private static final int MAX_PAYLOAD_BYTES = 256 * 1024;

    private static final String SCHEMA = "markliv.minecraft.state/4";

    private Path target;
    private Path temp;
    private boolean warned = false;

    @Override
    public void onInitializeClient() {
        target = stateFile();
        temp = target.resolveSibling(target.getFileName() + ".tmp");
        try {
            Files.createDirectories(target.getParent());
        } catch (IOException ignored) {
            // Reported on the first write attempt instead; failing here would
            // take the game down over a diagnostic file.
        }

        Thread publisher = new Thread(this::publishLoop, "markliv-bridge");
        // Daemon: when Minecraft closes, this must not keep the JVM alive.
        publisher.setDaemon(true);
        publisher.start();
    }

    private void publishLoop() {
        while (true) {
            try {
                Thread.sleep(PUBLISH_INTERVAL_MS);
            } catch (InterruptedException stop) {
                Thread.currentThread().interrupt();
                return;
            }

            Minecraft client = Minecraft.getInstance();
            if (client == null) {
                continue;
            }

            // The snapshot runs on the client thread; the write does not.
            // Reading the player and level off-thread races the game's own
            // writes and yields values that were never simultaneously true.
            CompletableFuture<String> pending = new CompletableFuture<>();
            try {
                client.execute(() -> {
                    try {
                        pending.complete(snapshot(client));
                    } catch (Throwable error) {
                        pending.completeExceptionally(error);
                    }
                });
                String json = pending.get(2, TimeUnit.SECONDS);
                writeAtomically(json);
            } catch (Throwable error) {
                // Never let a diagnostic file take the game with it.
                if (!warned) {
                    warned = true;
                    System.err.println("[markliv-bridge] could not publish "
                            + "state: " + error);
                }
            }
        }
    }

    /**
     * Where the state file lives.
     *
     * <p>Deliberately NOT inside .minecraft: the Python side would then have to
     * find the game directory, which varies by launcher, and a wrong guess
     * there is indistinguishable from the mod not running. Both sides compute
     * this same path from the same environment variable instead.
     */
    static Path stateFile() {
        String os = System.getProperty("os.name", "").toLowerCase(Locale.ROOT);
        if (os.contains("win")) {
            String base = System.getenv("LOCALAPPDATA");
            if (base != null && !base.isBlank()) {
                return Paths.get(base, "MarkLIV", "minecraft_state.json");
            }
        }
        String home = System.getProperty("user.home", ".");
        return Paths.get(home, ".markliv", "minecraft_state.json");
    }

    private void writeAtomically(String json) throws IOException {
        if (json.length() > MAX_PAYLOAD_BYTES) {
            // Something above has grown without bound. Refusing is better
            // than handing the reader a file it must parse five times a
            // second, and the reader treats a missing update as stale.
            if (!warned) {
                warned = true;
                System.err.println("[markliv-bridge] refusing to write "
                        + json.length() + " bytes; cap is "
                        + MAX_PAYLOAD_BYTES);
            }
            return;
        }
        Files.writeString(temp, json, StandardCharsets.UTF_8);
        try {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING,
                    StandardCopyOption.ATOMIC_MOVE);
        } catch (AtomicMoveNotSupportedException fallback) {
            Files.move(temp, target, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    // ── the snapshot ────────────────────────────────────────────────────────

    private String snapshot(Minecraft client) {
        Json out = new Json();
        out.raw("schema", Json.quote(SCHEMA));
        out.raw("written_at_ms", Long.toString(System.currentTimeMillis()));

        var player = client.player;
        var level = client.level;
        if (player == null || level == null) {
            // In a menu. Say so plainly rather than emit a hollow snapshot
            // that looks like a player standing at the origin.
            out.raw("in_game", "false");
            return out.close();
        }
        out.raw("in_game", "true");

        // The mouse sensitivity slider, 0..1. Reported because the only other
        // way to know how far a synthetic mouse delta turns the view is to
        // guess and then correct -- and a wrong guess spends the whole step
        // budget overshooting. Minecraft's own arithmetic is
        //     degrees_per_count = 0.15 * (sensitivity * 0.6 + 0.2)^3 * 8
        // so one number here replaces the guessing entirely.
        out.raw("mouse_sensitivity",
                Json.number(client.options.sensitivity().get()));

        out.raw("position", Json.array(
                Json.number(player.getX()),
                Json.number(player.getY()),
                Json.number(player.getZ())));
        out.raw("rotation", Json.array(
                Json.number(player.getYRot()),
                Json.number(player.getXRot())));
        out.raw("health", Json.number(player.getHealth()));
        out.raw("max_health", Json.number(player.getMaxHealth()));
        out.raw("hunger", Json.number(player.getFoodData().getFoodLevel()));
        out.raw("on_ground", Boolean.toString(player.onGround()));

        Inventory inventory = player.getInventory();
        out.raw("selected_slot", Integer.toString(inventory.getSelectedSlot()));
        out.raw("held_item", itemJson(player.getMainHandItem(),
                inventory.getSelectedSlot()));
        out.raw("inventory", inventoryJson(inventory));

        out.raw("dimension", Json.quote(
                level.dimension().identifier().toString()));
        // getDayTime() is gone in 26.3; the clock lives here now.
        out.raw("time_of_day",
                Long.toString(Math.floorMod(level.getDefaultClockTime(), 24000L)));
        out.raw("weather", Json.quote(level.isThundering() ? "thunder"
                : level.isRaining() ? "rain" : "clear"));

        BlockPos feet = player.blockPosition();
        out.raw("biome", Json.quote(
                level.getBiome(feet).unwrapKey()
                        .map(key -> key.identifier().toString())
                        .orElse("unknown")));
        out.raw("light_level", Integer.toString(
                level.getMaxLocalRawBrightness(feet)));

        out.raw("target_block", targetBlockJson(client));
        out.raw("target_entity", targetEntityJson(client));
        out.raw("nearby_entities", nearbyJson(client, player));

        out.raw("scan", Json.object(
                "radius", Integer.toString(SCAN_RADIUS),
                "up", Integer.toString(SCAN_UP),
                "down", Integer.toString(SCAN_DOWN)));
        Terrain terrain = scanTerrain(level, feet);
        out.raw("surface", terrain.surface);
        out.raw("notable_blocks", terrain.notable);

        return out.close();
    }

    /** The two products of one pass over the scan volume. */
    private record Terrain(String surface, String notable) { }

    /**
     * Can a player's body occupy this block?
     *
     * <p>Judged on the COLLISION shape, not on air: grass, flowers, torches
     * and signs all occupy a block and none of them stop you walking through.
     * Treating those as solid would make a flowery meadow impassable.
     *
     * <p>Fluid is not passable. Water has no collision shape either, and
     * without this a riverbed would look like dry floor with the river as
     * headroom -- a route straight through the water, which the planner
     * cannot walk.
     */
    private static boolean passable(net.minecraft.world.level.Level level,
                                    BlockPos pos, BlockState state) {
        return state.isAir()
                || (state.getCollisionShape(level, pos).isEmpty()
                    && state.getFluidState().isEmpty());
    }

    /**
     * One pass over the volume around the player, producing two things.
     *
     * <p><b>surface</b> is the floor in each column: what a path planner
     * needs, and nothing more. A full voxel dump of this volume would be
     * several thousand entries to say what 441 already say. Each entry is
     * {@code [x, y, z, block, solid, clearance, cover]}: the floor, whether
     * it has collision, how many passable blocks are above it (capped at
     * {@link #MAX_CLEARANCE}), and the first non-air block a player standing
     * there would be inside -- grass at the feet, a berry bush, fire -- or
     * null.
     *
     * <p>The floor is chosen by {@link ColumnScan#floor}: the block with
     * room for a player above it NEAREST the player's feet. It used to be the
     * topmost block, which under a tree is the canopy. A column with no floor
     * that fits a player reports its topmost block instead, with the
     * clearance that says it does not fit -- water, lava and a trunk under a
     * low canopy stay visible as places not to go.
     *
     * <p><b>notable</b> is the blocks worth travelling to -- logs, ores,
     * water, work stations -- with their real coordinates, so "find a tree"
     * stops meaning "sweep the crosshair and hope".
     *
     * <p>A column with nothing in range is OMITTED rather than reported as
     * air. The reader then knows it does not know, instead of being told
     * there is a floor where nobody looked.
     */
    private Terrain scanTerrain(net.minecraft.world.level.Level level,
                                BlockPos feet) {
        List<String> surface = new ArrayList<>();
        List<String> notable = new ArrayList<>();

        int originX = feet.getX();
        int originY = feet.getY();
        int originZ = feet.getZ();
        BlockPos.MutableBlockPos cursor = new BlockPos.MutableBlockPos();

        // Each column is read once, top down, from MAX_CLEARANCE above the
        // scan's top -- so the headroom over the highest scanned block is
        // measured, not assumed -- to the scan's bottom.
        int top = SCAN_UP + MAX_CLEARANCE;
        int length = top + SCAN_DOWN + 1;
        int first = MAX_CLEARANCE;          // index of dy = SCAN_UP
        int last = length - 1;              // index of dy = -SCAN_DOWN
        int feetIndex = top;                // index of dy = 0
        String[] names = new String[length];
        boolean[] air = new boolean[length];
        boolean[] collides = new boolean[length];
        boolean[] open = new boolean[length];

        for (int dx = -SCAN_RADIUS; dx <= SCAN_RADIUS; dx++) {
            for (int dz = -SCAN_RADIUS; dz <= SCAN_RADIUS; dz++) {
                int x = originX + dx;
                int z = originZ + dz;

                for (int i = 0; i < length; i++) {
                    cursor.set(x, originY + top - i, z);
                    BlockState state = level.getBlockState(cursor);
                    air[i] = state.isAir();
                    collides[i] = !air[i] && !state
                            .getCollisionShape(level, cursor).isEmpty();
                    open[i] = passable(level, cursor, state);
                    names[i] = air[i] ? null : BuiltInRegistries.BLOCK
                            .getKey(state.getBlock()).toString();

                    if (i >= first && !air[i]
                            && notable.size() < MAX_NOTABLE
                            && isNotable(names[i])) {
                        notable.add(Json.array(
                                Integer.toString(x),
                                Integer.toString(originY + top - i),
                                Integer.toString(z), Json.quote(names[i])));
                    }
                }

                int floor = ColumnScan.floor(collides, open, first, last,
                        feetIndex, PLAYER_HEIGHT, MAX_CLEARANCE);
                int chosen = floor >= 0 ? floor
                        : ColumnScan.top(air, first, last);
                if (chosen < 0) {
                    continue;
                }
                int cover = floor >= 0
                        ? ColumnScan.cover(air, floor, PLAYER_HEIGHT) : -1;
                surface.add(Json.array(
                        Integer.toString(x),
                        Integer.toString(originY + top - chosen),
                        Integer.toString(z), Json.quote(names[chosen]),
                        Boolean.toString(collides[chosen]),
                        Integer.toString(ColumnScan.clearance(
                                open, chosen, MAX_CLEARANCE)),
                        cover < 0 ? "null" : Json.quote(names[cover])));
            }
        }

        return new Terrain(Json.array(surface.toArray(new String[0])),
                           Json.array(notable.toArray(new String[0])));
    }

    /**
     * Is this block worth reporting individually?
     *
     * <p>Matched on the name rather than on a tag or class, so a modded log
     * called {@code biomesoplenty:fir_log} is picked up without this mod
     * knowing anything about that mod. The cost is the occasional false
     * positive, which costs a wasted walk rather than a wrong belief.
     */
    private static boolean isNotable(String name) {
        return name.endsWith("_log") || name.endsWith("_wood")
                || name.endsWith("_ore") || name.contains("water")
                || name.contains("lava") || name.endsWith("crafting_table")
                || name.endsWith("furnace") || name.endsWith("chest");
    }

    private String targetBlockJson(Minecraft client) {
        HitResult hit = client.hitResult;
        if (!(hit instanceof BlockHitResult block)
                || hit.getType() != HitResult.Type.BLOCK) {
            // Looking at nothing is a reading, not a gap. The Python side
            // distinguishes "air" from "could not tell", and conflating them
            // would make breaking the last block in front of you
            // unconfirmable.
            return Json.object(
                    "name", Json.quote("minecraft:air"),
                    "x", "null", "y", "null", "z", "null",
                    "face", "null");
        }
        BlockPos pos = block.getBlockPos();
        BlockState state = client.level.getBlockState(pos);
        String name = BuiltInRegistries.BLOCK.getKey(state.getBlock())
                .toString();
        return Json.object(
                "name", Json.quote(name),
                "x", Integer.toString(pos.getX()),
                "y", Integer.toString(pos.getY()),
                "z", Integer.toString(pos.getZ()),
                "face", Json.quote(block.getDirection().getName()));
    }

    private String targetEntityJson(Minecraft client) {
        HitResult hit = client.hitResult;
        if (!(hit instanceof EntityHitResult entityHit)
                || hit.getType() != HitResult.Type.ENTITY) {
            return "null";
        }
        Entity entity = entityHit.getEntity();
        return Json.object(
                "name", Json.quote(entityName(entity)),
                "distance", Json.number(
                        client.player == null ? 0.0
                                : client.player.distanceTo(entity)));
    }

    private String nearbyJson(Minecraft client, Entity self) {
        List<String> items = new ArrayList<>();
        var box = self.getBoundingBox().inflate(NEARBY_RADIUS);
        for (Entity entity : client.level.getEntities(self, box)) {
            if (items.size() >= MAX_ENTITIES) {
                break;
            }
            items.add(Json.object(
                    "name", Json.quote(entityName(entity)),
                    "distance", Json.number(self.distanceTo(entity)),
                    "position", Json.array(Json.number(entity.getX()),
                                           Json.number(entity.getY()),
                                           Json.number(entity.getZ())),
                    "category", Json.quote(categoryOf(entity))));
        }
        return Json.array(items.toArray(new String[0]));
    }

    /**
     * Rough classification, from the game's own mob category.
     *
     * <p>Reported rather than derived on the Python side because the game
     * already knows, and "is a drowned hostile" is the sort of question that
     * gets answered wrongly by a name list. Anything that does not fit
     * cleanly is "other" -- deliberately not "passive", because treating an
     * unrecognised entity as safe is the mistake that matters.
     */
    private static String categoryOf(Entity entity) {
        if (entity instanceof net.minecraft.world.entity.player.Player) {
            return "player";
        }
        if (entity instanceof net.minecraft.world.entity.item.ItemEntity) {
            return "item";
        }
        try {
            var category = entity.getType().getCategory();
            String name = category.getName();
            if ("monster".equals(name)) {
                return "hostile";
            }
            if ("creature".equals(name) || "ambient".equals(name)
                    || "axolotls".equals(name) || "water_creature".equals(name)
                    || "water_ambient".equals(name)) {
                return "passive";
            }
            return name;
        } catch (Throwable ignored) {
            return "other";
        }
    }

    private String inventoryJson(Inventory inventory) {
        List<String> items = new ArrayList<>();
        for (int slot = 0; slot < inventory.getContainerSize(); slot++) {
            ItemStack stack = inventory.getItem(slot);
            if (!stack.isEmpty()) {
                items.add(itemJson(stack, slot));
            }
        }
        return Json.array(items.toArray(new String[0]));
    }

    private String itemJson(ItemStack stack, int slot) {
        if (stack == null || stack.isEmpty()) {
            return "null";
        }
        String name = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        return Json.object(
                "slot", Integer.toString(slot),
                "name", Json.quote(name),
                "count", Integer.toString(stack.getCount()));
    }

    private static String entityName(Entity entity) {
        return BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType())
                .toString();
    }

    // ── a very small JSON writer ────────────────────────────────────────────
    //
    // Hand-rolled to keep this mod dependency-free: pulling in Gson would mean
    // shading it or depending on the game's copy, and the payload is a flat
    // object of numbers and short identifiers.

    private static final class Json {
        private final StringBuilder out = new StringBuilder(1024).append('{');
        private boolean first = true;

        void raw(String key, String rawValue) {
            if (!first) {
                out.append(',');
            }
            first = false;
            out.append(quote(key)).append(':').append(rawValue);
        }

        String close() {
            return out.append('}').toString();
        }

        static String quote(String value) {
            StringBuilder sb = new StringBuilder(value.length() + 2);
            sb.append('"');
            for (int i = 0; i < value.length(); i++) {
                char c = value.charAt(i);
                switch (c) {
                    case '"' -> sb.append("\\\"");
                    case '\\' -> sb.append("\\\\");
                    case '\n' -> sb.append("\\n");
                    case '\r' -> sb.append("\\r");
                    case '\t' -> sb.append("\\t");
                    default -> {
                        if (c < 0x20) {
                            sb.append(String.format("\\u%04x", (int) c));
                        } else {
                            sb.append(c);
                        }
                    }
                }
            }
            return sb.append('"').toString();
        }

        static String number(double value) {
            if (Double.isNaN(value) || Double.isInfinite(value)) {
                return "null";
            }
            return Double.toString(Math.round(value * 1000.0) / 1000.0);
        }

        static String array(String... parts) {
            return "[" + String.join(",", parts) + "]";
        }

        static String object(String... keyThenRawValue) {
            StringBuilder sb = new StringBuilder("{");
            for (int i = 0; i + 1 < keyThenRawValue.length; i += 2) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append(quote(keyThenRawValue[i])).append(':')
                        .append(keyThenRawValue[i + 1]);
            }
            return sb.append('}').toString();
        }
    }
}
