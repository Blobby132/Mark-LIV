package com.markliv.bridge;

import net.fabricmc.api.ClientModInitializer;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.LevelChunkSection;
import net.minecraft.world.level.material.Fluid;
import net.minecraft.world.level.material.FluidState;
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
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
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

    /**
     * How many nearby entities to report, per kind, nearest first; hostile
     * mobs are listed before everything else.
     *
     * <p>Per kind because the first version kept the first 24 in the level's
     * own order, and after felling a tree -- logs, saplings, sticks and
     * apples on the ground -- a zombie could be the 25th. The danger check
     * reads this list; a mob missing from it is a mob that is not there.
     */
    private static final Map<String, Integer> ENTITY_QUOTAS = Map.of(
            "hostile", 16, "player", 8, "passive", 12, "item", 12,
            "other", 8);

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
     * How high logs are reported. The rows up to SCAN_UP + MAX_CLEARANCE are
     * read anyway, for headroom; logs among them are reported, and a trunk
     * that reaches the top of them is followed up its own column to here.
     * Four was below the top of most oaks and every spruce, and "fell the
     * tree" could not see the tree it was felling.
     */
    private static final int TREE_UP = 12;

    /**
     * How many blocks of interest to report, per kind, nearest first.
     *
     * <p>Per kind so that a lake cannot crowd out the logs and a forest
     * cannot crowd out the lava. The first version kept the first 64 found
     * in scan order, west to east -- in a forest, all of them six or more
     * blocks west, and not the tree the player was standing at. At about 45
     * bytes an entry, the full quota is some 12KB of a 256KB payload.
     */
    private static final Map<String, Integer> NOTABLE_QUOTAS = Map.of(
            "log", 160, "ore", 48, "lava", 24, "water", 24, "station", 16);

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

    /**
     * What this build reports, by name, sent as "features" so the reader can
     * tell an older jar from this one. The schema number cannot: the screen
     * fields (gui), near_blocks, the names of dropped items, the taller tree
     * scan and the mob categories were all added under /4, so an older /4 jar
     * looked current and crafting failed without a word about why. Add a name
     * here whenever a field is added; minecraft/mod_bridge.py FEATURES lists
     * the ones Jarvis needs.
     */
    private static final String[] FEATURES = {
            "gui", "near_blocks", "item_names", "tree_up", "mob_categories",
            "singleplayer", "ores", "near_grid"};

    /**
     * The ore finder (see OreScan). Runs only in a single-player world --
     * on a server, finding ore inside rock is x-ray -- and starts again
     * when the player changes world or dimension.
     */
    private OreScan oreScan = new OreScan();
    private Object oreScanLevel = null;

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
        out.raw("features", featuresJson());
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
        writeScreen(out, client);

        out.raw("scan", Json.object(
                "radius", Integer.toString(SCAN_RADIUS),
                "up", Integer.toString(SCAN_UP),
                "down", Integer.toString(SCAN_DOWN),
                "tree_up", Integer.toString(TREE_UP)));
        Terrain terrain = scanTerrain(level, feet);
        out.raw("surface", terrain.surface);
        out.raw("notable_blocks", terrain.notable);
        out.raw("near_blocks", terrain.near);

        // The client runs its own integrated server: a single-player world
        // (or one opened to LAN from it). Anything else is a server.
        boolean single = client.hasSingleplayerServer();
        out.raw("singleplayer", Boolean.toString(single));
        String ores = null;
        if (single) {
            if (oreScanLevel != level) {
                oreScan = new OreScan();
                oreScanLevel = level;
            }
            oreScan.step(feet.getX(), feet.getY(), feet.getZ(),
                         System.currentTimeMillis(), new LevelOres(level));
            ores = oreScan.json();
        }
        out.raw("ores", ores == null ? "null" : ores);

        return out.close();
    }

    /** FEATURES as a JSON array of strings. Sent in a menu too. */
    private static String featuresJson() {
        String[] quoted = new String[FEATURES.length];
        for (int i = 0; i < FEATURES.length; i++) {
            quoted[i] = Json.quote(FEATURES[i]);
        }
        return Json.array(quoted);
    }

    /** Registry names of fluids, by fluid type: water, flowing_water... */
    private final IdentityHashMap<Fluid, String> fluidNames =
            new IdentityHashMap<>();

    private String fluidName(FluidState fluid) {
        if (fluid.isEmpty()) {
            return null;
        }
        return fluidNames.computeIfAbsent(fluid.getType(),
                type -> BuiltInRegistries.FLUID.getKey(type).toString());
    }

    private boolean isOre(BlockState state) {
        return !state.isAir() && "ore".equals(infoOf(state)[1]);
    }

    /**
     * OreScan's view of the level: chunk sections for the scan (so a
     * section's palette can rule it out, and its cells are read without
     * looking the chunk up again for each), the level for the few
     * neighbour checks. Read only, on the game's thread.
     */
    private final class LevelOres implements OreScan.World {
        private final net.minecraft.world.level.Level level;
        private final BlockPos.MutableBlockPos cursor =
                new BlockPos.MutableBlockPos();
        private long cachedKey = Long.MIN_VALUE;
        private LevelChunkSection cached = null;

        LevelOres(net.minecraft.world.level.Level level) {
            this.level = level;
        }

        @Override
        public boolean loaded(int chunkX, int chunkZ) {
            return level.hasChunk(chunkX, chunkZ);
        }

        @Override
        public boolean mayHaveOre(int chunkX, int sectionY, int chunkZ) {
            LevelChunkSection section = section(chunkX, sectionY, chunkZ);
            return section != null && !section.hasOnlyAir()
                    && section.maybeHas(MarkLivBridge.this::isOre);
        }

        @Override
        public String oreAt(int x, int y, int z) {
            LevelChunkSection section = section(x >> 4, y >> 4, z >> 4);
            if (section == null) {
                return null;
            }
            BlockState state = section.getBlockState(x & 15, y & 15, z & 15);
            return isOre(state) ? infoOf(state)[0] : null;
        }

        @Override
        public boolean open(int x, int y, int z) {
            BlockState state = level.getBlockState(cursor.set(x, y, z));
            return state.isAir() || !state.getFluidState().isEmpty();
        }

        @Override
        public boolean fluid(int x, int y, int z) {
            return !level.getBlockState(cursor.set(x, y, z)).getFluidState()
                    .isEmpty();
        }

        private LevelChunkSection section(int chunkX, int sectionY,
                                          int chunkZ) {
            long key = ((long) chunkX & 0x3FFFFFL) << 42
                    | ((long) chunkZ & 0x3FFFFFL) << 20
                    | ((long) sectionY & 0xFFFFFL);
            if (key == cachedKey) {
                return cached;
            }
            cachedKey = key;
            int index = level.getSectionIndexFromSectionY(sectionY);
            if (index < 0 || index >= level.getSectionsCount()
                    || !level.hasChunk(chunkX, chunkZ)) {
                cached = null;
            } else {
                cached = level.getChunk(chunkX, chunkZ).getSection(index);
            }
            return cached;
        }
    }

    /** The three products of one pass over the scan volume. */
    private record Terrain(String surface, String notable, String near) { }

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
    /**
     * Can a player's body occupy this block? Air, or no collision and no
     * fluid. Takes the collision already computed: the scan used to
     * compute every block's collision shape twice.
     */
    private static boolean passable(boolean air, boolean noCollision,
                                    BlockState state) {
        return air || (noCollision && state.getFluidState().isEmpty());
    }

    /**
     * Per block TYPE, its registry name and notable kind: {name, kind}.
     *
     * <p>The scan reads some 6,000 blocks five times a second and used to
     * build each one's name as a fresh string, then match it against the
     * notable kinds -- for the few dozen types a scan actually sees. Only
     * ever touched on the client thread, from the scan.
     */
    private final IdentityHashMap<Block, String[]> blockInfo =
            new IdentityHashMap<>();

    private String[] infoOf(BlockState state) {
        return blockInfo.computeIfAbsent(state.getBlock(), block -> {
            String name = BuiltInRegistries.BLOCK.getKey(block).toString();
            return new String[] {name, Kinds.notable(name)};
        });
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
        Nearest notable = new Nearest(NOTABLE_QUOTAS);

        int originX = feet.getX();
        int originY = feet.getY();
        int originZ = feet.getZ();
        // Every non-air block close by, from the rows this pass reads
        // anyway: no extra block lookups.
        NearBlocks near = new NearBlocks(originX, originY, originZ);
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
        String[] fluids = new String[length];
        String[] kinds = new String[length];
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
                    fluids[i] = air[i] ? null
                            : fluidName(state.getFluidState());
                    boolean noCollision = air[i] || state
                            .getCollisionShape(level, cursor).isEmpty();
                    collides[i] = !noCollision;
                    open[i] = passable(air[i], noCollision, state);
                    String[] info = air[i] ? null : infoOf(state);
                    names[i] = info == null ? null : info[0];
                    kinds[i] = info == null ? null : info[1];

                    String kind = kinds[i];
                    if (i < first && !ColumnScan.reportedAbove(kind)) {
                        kind = null;            // headroom rows: logs only
                    }
                    if (kind != null) {
                        int dy = top - i;
                        double distance = dx * dx + dy * dy + dz * dz;
                        if (notable.wants(kind, distance)) {
                            notable.offer(kind, distance, Json.array(
                                    Integer.toString(x),
                                    Integer.toString(originY + dy),
                                    Integer.toString(z),
                                    Json.quote(names[i])));
                        }
                    }
                }

                // A trunk that reaches the top of the rows read: follow it.
                String[] trunk = new String[TREE_UP + 1];
                int[] above = ColumnScan.logsAbove(kinds[0], up -> {
                    cursor.set(x, originY + up, z);
                    BlockState state = level.getBlockState(cursor);
                    if (state.isAir()) {
                        return null;
                    }
                    String[] info = infoOf(state);
                    trunk[up] = info[0];
                    return info[1];
                }, top + 1, TREE_UP);
                for (int dy : above) {
                    double distance = dx * dx + dy * dy + dz * dz;
                    if (notable.wants("log", distance)) {
                        notable.offer("log", distance, Json.array(
                                Integer.toString(x),
                                Integer.toString(originY + dy),
                                Integer.toString(z),
                                Json.quote(trunk[dy])));
                    }
                }

                if (Math.abs(dx) <= NearBlocks.RADIUS
                        && Math.abs(dz) <= NearBlocks.RADIUS) {
                    // An unloaded chunk reads as air; its cells are left
                    // unknown in the grid rather than reported as air.
                    boolean loaded = level.hasChunk(x >> 4, z >> 4);
                    for (int dy = -NearBlocks.BELOW; dy <= NearBlocks.ABOVE;
                            dy++) {
                        int i = top - dy;
                        if (i < 0 || i >= length) {
                            continue;
                        }
                        if (!air[i]) {
                            near.offer(x, originY + dy, z, names[i],
                                       collides[i]);
                        }
                        if (loaded) {
                            near.cell(x, originY + dy, z,
                                      air[i] ? null : names[i], collides[i],
                                      fluids[i]);
                        }
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
                           Json.array(notable.select(List.of())
                                   .toArray(new String[0])),
                           near.json());
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
        // category and position are additive (schema /4): what the fight
        // task checks before every swing -- never a player, never a cow.
        return Json.object(
                "name", Json.quote(entityName(entity)),
                "distance", Json.number(
                        client.player == null ? 0.0
                                : client.player.distanceTo(entity)),
                "category", Json.quote(categoryOf(entity)),
                "position", Json.array(Json.number(entity.getX()),
                                       Json.number(entity.getY()),
                                       Json.number(entity.getZ())));
    }

    private String nearbyJson(Minecraft client, Entity self) {
        Nearest found = new Nearest(ENTITY_QUOTAS);
        var box = self.getBoundingBox().inflate(NEARBY_RADIUS);
        for (Entity entity : client.level.getEntities(self, box)) {
            String category = categoryOf(entity);
            String group = Kinds.entityGroup(category);
            double distance = self.distanceTo(entity);
            if (!found.wants(group, distance)) {
                continue;
            }
            String name = Json.quote(entityName(entity));
            String position = Json.array(Json.number(entity.getX()),
                                         Json.number(entity.getY()),
                                         Json.number(entity.getZ()));
            // What a dropped item IS, for item entities only. Additive:
            // older readers ignore the key, and without it (an older jar)
            // the reader says it does not know.
            String stack = entity instanceof
                    net.minecraft.world.entity.item.ItemEntity dropped
                    ? stackJson(dropped.getItem()) : null;
            found.offer(group, distance, stack == null
                    ? Json.object("name", name,
                                  "distance", Json.number(distance),
                                  "position", position,
                                  "category", Json.quote(category))
                    : Json.object("name", name,
                                  "distance", Json.number(distance),
                                  "position", position,
                                  "category", Json.quote(category),
                                  "item", stack));
        }
        return Json.array(found.select(List.of("hostile"))
                .toArray(new String[0]));
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

    /**
     * The open screen, and for an inventory-like screen its slots and the
     * pointer -- what a click inside it would land on. Additive fields, sent
     * only while a screen is open; see docs/minecraft-gui.md. The game mode
     * is sent always, because "never click in creative" needs it.
     */
    private void writeScreen(Json out, Minecraft client) {
        if (client.gameMode != null) {
            var mode = client.gameMode.getPlayerMode();
            if (mode != null) {
                out.raw("game_mode", Json.quote(mode.getName()));
            }
        }
        var screen = client.gui.screen();
        if (screen == null) {
            return;
        }
        out.raw("screen", Gui.screen(screenKind(screen)));
        if (!(screen instanceof net.minecraft.client.gui.screens.inventory
                .AbstractContainerScreen<?> container)) {
            return;
        }
        var window = client.getWindow();
        double scale = window.getGuiScaledWidth() > 0
                ? (double) window.getScreenWidth() / window.getGuiScaledWidth()
                : window.getGuiScale();
        out.raw("gui", Gui.view(scale, window.getScreenWidth(),
                window.getScreenHeight(), client.mouseHandler.xpos(),
                client.mouseHandler.ypos()));
        List<String> slots = new ArrayList<>();
        for (net.minecraft.world.inventory.Slot slot
                : container.getMenu().slots) {
            if (slots.size() >= Gui.MAX_SLOTS) {
                break;
            }
            ItemStack stack = slot.getItem();
            String item = stack == null || stack.isEmpty() ? null
                    : BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            boolean mine = slot.container instanceof Inventory;
            slots.add(Gui.slot(slot.index,
                    Gui.role(slot instanceof net.minecraft.world.inventory
                                    .ResultSlot,
                             slot.container instanceof net.minecraft.world
                                    .inventory.CraftingContainer,
                             mine, mine ? slot.getContainerSlot() : -1),
                    Gui.centre(container.leftPos, slot.x, scale),
                    Gui.centre(container.topPos, slot.y, scale),
                    item, item == null ? 0 : stack.getCount()));
        }
        out.raw("slots", Json.array(slots.toArray(new String[0])));
        ItemStack carried = container.getMenu().getCarried();
        out.raw("carried", carried == null || carried.isEmpty() ? "null"
                : Gui.carried(BuiltInRegistries.ITEM
                        .getKey(carried.getItem()).toString(),
                        carried.getCount()));
    }

    /** Which kind of screen, by class: only two of these are ever clicked. */
    private static String screenKind(Object screen) {
        if (screen instanceof net.minecraft.client.gui.screens.inventory
                .CreativeModeInventoryScreen) {
            return "other";
        }
        if (screen instanceof net.minecraft.client.gui.screens.inventory
                .InventoryScreen) {
            return "inventory";
        }
        if (screen instanceof net.minecraft.client.gui.screens.inventory
                .CraftingScreen) {
            return "crafting_table";
        }
        if (screen instanceof net.minecraft.client.gui.screens.inventory
                .AbstractFurnaceScreen<?>) {
            return "furnace";
        }
        if (screen instanceof net.minecraft.client.gui.screens.inventory
                .ContainerScreen) {
            return "chest";
        }
        if (screen instanceof net.minecraft.client.gui.screens.PauseScreen) {
            return "pause";
        }
        return "other";
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

    private static String stackJson(ItemStack stack) {
        if (stack == null || stack.isEmpty()) {
            return null;
        }
        return Kinds.stackJson(
                BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(),
                stack.getCount());
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
