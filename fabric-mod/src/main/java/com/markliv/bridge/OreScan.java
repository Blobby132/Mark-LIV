package com.markliv.bridge;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * The ore blocks nearest the player, buried ones included: for finding
 * ore to dig to, in a single-player world only (the caller does not run
 * this on a server -- finding ore inside rock there is x-ray).
 *
 * <p>The volume is {@link #RADIUS} blocks each way across, {@link #DOWN}
 * below the feet and {@link #UP} above: some 117,000 cells. Reading them all
 * five times a second would cost the game frames, so:
 * <ul>
 *   <li>the volume is walked a chunk section (16x16x16) at a time, and a
 *       section whose block palette cannot contain an ore is skipped
 *       without reading a cell (the caller's {@code mayHaveOre}, which is
 *       {@code LevelChunkSection.maybeHas});</li>
 *   <li>each call to {@link #step} reads at most {@link #SLICE_CELLS}
 *       cells, so a pass is spread over several snapshots;</li>
 *   <li>a new pass starts at most once every {@link #MIN_PASS_MS}.</li>
 * </ul>
 * What is published is always the last FINISHED pass, never a half-read
 * one.
 *
 * <p>Kept: the nearest {@link #PER_TYPE} of each ore, then the nearest
 * {@link #MAX_ORES} of those, each with {@code exposed} (one of its six
 * neighbours is air or fluid) and {@code fluid_near} (water or lava within
 * {@link #FLUID_REACH} cells, a cube). {@code complete} is true only when
 * every section of the volume was in a loaded chunk and no ore was left out
 * by a cap; {@code complete_within} is then null, else the distance of the
 * nearest ore left out.
 *
 * <p>No Minecraft types, so the Python tests compile this file, drive it
 * with a made-up world and time it. The mod only reads: this builds a
 * string.
 */
final class OreScan {

    static final int RADIUS = 24;
    static final int DOWN = 32;
    static final int UP = 16;
    static final int MAX_ORES = 64;
    static final int PER_TYPE = 16;
    static final int MAX_JSON_BYTES = 16 * 1024;
    static final int FLUID_REACH = 2;
    static final long MIN_PASS_MS = 1000;
    /** Cells read per step, scan and finishing alike. */
    static final int SLICE_CELLS = 16_384;

    /**
     * Is this a single-player world, where the scan may run? Only when the
     * client runs its own integrated server and has not opened it to LAN:
     * a LAN world has other players, so it counts as multiplayer, and
     * finding ore inside rock there is x-ray.
     */
    static boolean singleplayer(boolean integratedServer, boolean published) {
        return integratedServer && !published;
    }

    /** What the scan needs to know about the world. */
    interface World {
        /** Is the chunk at chunk coordinates loaded? */
        boolean loaded(int chunkX, int chunkZ);

        /** Could the section at section coordinates hold an ore? False for
         *  a section outside the world. */
        boolean mayHaveOre(int chunkX, int sectionY, int chunkZ);

        /** The ore's registry name at (x, y, z), or null. */
        String oreAt(int x, int y, int z);

        /** Is (x, y, z) air, or any fluid? */
        boolean open(int x, int y, int z);

        /** Is there water or lava at (x, y, z), source or flowing? */
        boolean fluid(int x, int y, int z);
    }

    private record Found(String name, int x, int y, int z, double distance,
                         long order) { }

    private static final Comparator<Found> NEAREST_FIRST =
            Comparator.comparingDouble(Found::distance)
                    .thenComparingLong(Found::order);

    // The pass in progress, if any.
    private boolean running = false;
    private int originX;
    private int originY;
    private int originZ;
    private long startedAt = Long.MIN_VALUE / 2;
    private final List<int[]> sections = new ArrayList<>();
    private int nextSection = 0;
    private boolean unloaded = false;
    private final Map<String, PriorityQueue<Found>> byType = new HashMap<>();
    private double leftOut = Double.POSITIVE_INFINITY;   // squared
    private long found = 0;
    // Finishing: the kept ores, checked one at a time.
    private List<Found> finishing = null;
    private final List<String> finished = new ArrayList<>();
    private int nextFinish = 0;

    private String published = null;

    // For the harness: what the last step cost.
    private int lastLookups = 0;
    private int lastSkipped = 0;
    private int passes = 0;

    /** The last finished pass's JSON, or null before the first one. */
    String json() {
        return published;
    }

    int lastLookups() {
        return lastLookups;
    }

    int lastSkipped() {
        return lastSkipped;
    }

    int passes() {
        return passes;
    }

    boolean running() {
        return running;
    }

    /**
     * One slice of work, on the game's thread. Starts a pass from the feet
     * at (x, y, z) if none is running and the last one started at least
     * {@link #MIN_PASS_MS} ago; otherwise carries on with the one running.
     */
    void step(int x, int y, int z, long nowMs, World world) {
        lastLookups = 0;
        lastSkipped = 0;
        if (!running) {
            if (nowMs - startedAt < MIN_PASS_MS) {
                return;
            }
            begin(x, y, z, nowMs, world);
        }
        int budget = SLICE_CELLS;
        if (finishing == null) {
            budget = scan(budget, world);
            if (nextSection < sections.size()) {
                return;
            }
            choose();
        }
        finish(budget, world);
    }

    private void begin(int x, int y, int z, long nowMs, World world) {
        running = true;
        startedAt = nowMs;
        originX = x;
        originY = y;
        originZ = z;
        sections.clear();
        nextSection = 0;
        unloaded = false;
        byType.clear();
        leftOut = Double.POSITIVE_INFINITY;
        found = 0;
        finishing = null;
        finished.clear();
        nextFinish = 0;
        for (int cx = Math.floorDiv(x - RADIUS, 16);
                cx <= Math.floorDiv(x + RADIUS, 16); cx++) {
            for (int cz = Math.floorDiv(z - RADIUS, 16);
                    cz <= Math.floorDiv(z + RADIUS, 16); cz++) {
                for (int sy = Math.floorDiv(y - DOWN, 16);
                        sy <= Math.floorDiv(y + UP, 16); sy++) {
                    sections.add(new int[] {cx, sy, cz});
                }
            }
        }
        // Nearest sections first: an interrupted or capped pass still has
        // the ore closest to the player.
        sections.sort(Comparator.comparingDouble(s -> sectionDistance(s)));
    }

    private double sectionDistance(int[] s) {
        double cx = s[0] * 16 + 8 - originX;
        double cy = s[1] * 16 + 8 - originY;
        double cz = s[2] * 16 + 8 - originZ;
        return cx * cx + cy * cy + cz * cz;
    }

    /** Reads sections until `budget` cells are spent; returns what is left. */
    private int scan(int budget, World world) {
        while (nextSection < sections.size() && budget > 0) {
            int[] s = sections.get(nextSection);
            if (!world.loaded(s[0], s[2])) {
                unloaded = true;
                nextSection++;
                lastSkipped++;
                continue;
            }
            if (!world.mayHaveOre(s[0], s[1], s[2])) {
                nextSection++;
                lastSkipped++;
                continue;
            }
            int x0 = Math.max(s[0] * 16, originX - RADIUS);
            int x1 = Math.min(s[0] * 16 + 15, originX + RADIUS);
            int y0 = Math.max(s[1] * 16, originY - DOWN);
            int y1 = Math.min(s[1] * 16 + 15, originY + UP);
            int z0 = Math.max(s[2] * 16, originZ - RADIUS);
            int z1 = Math.min(s[2] * 16 + 15, originZ + RADIUS);
            for (int y = y0; y <= y1; y++) {
                for (int z = z0; z <= z1; z++) {
                    for (int x = x0; x <= x1; x++) {
                        String ore = world.oreAt(x, y, z);
                        if (ore != null) {
                            keep(ore, x, y, z);
                        }
                    }
                }
            }
            int cells = (x1 - x0 + 1) * (y1 - y0 + 1) * (z1 - z0 + 1);
            budget -= cells;
            lastLookups += cells;
            nextSection++;
        }
        return budget;
    }

    private void keep(String name, int x, int y, int z) {
        double dx = x - originX;
        double dy = y - originY;
        double dz = z - originZ;
        Found f = new Found(name, x, y, z, dx * dx + dy * dy + dz * dz,
                            found++);
        PriorityQueue<Found> heap = byType.computeIfAbsent(
                name, k -> new PriorityQueue<>(NEAREST_FIRST.reversed()));
        heap.add(f);
        if (heap.size() > PER_TYPE) {
            leftOut = Math.min(leftOut, heap.poll().distance());
        }
    }

    /** The nearest MAX_ORES of what each type kept. */
    private void choose() {
        List<Found> all = new ArrayList<>();
        for (PriorityQueue<Found> heap : byType.values()) {
            all.addAll(heap);
        }
        all.sort(NEAREST_FIRST);
        for (int i = MAX_ORES; i < all.size(); i++) {
            leftOut = Math.min(leftOut, all.get(i).distance());
        }
        finishing = new ArrayList<>(all.subList(0,
                Math.min(MAX_ORES, all.size())));
    }

    /** Checks the kept ores' neighbours within `budget`; publishes when
     *  all are done. */
    private void finish(int budget, World world) {
        int perOre = 6 + (2 * FLUID_REACH + 1) * (2 * FLUID_REACH + 1)
                * (2 * FLUID_REACH + 1);
        while (nextFinish < finishing.size()
                && (budget >= perOre || lastLookups == 0)) {
            Found f = finishing.get(nextFinish++);
            boolean exposed = world.open(f.x() + 1, f.y(), f.z())
                    || world.open(f.x() - 1, f.y(), f.z())
                    || world.open(f.x(), f.y() + 1, f.z())
                    || world.open(f.x(), f.y() - 1, f.z())
                    || world.open(f.x(), f.y(), f.z() + 1)
                    || world.open(f.x(), f.y(), f.z() - 1);
            boolean fluidNear = false;
            for (int dy = -FLUID_REACH; dy <= FLUID_REACH && !fluidNear; dy++) {
                for (int dz = -FLUID_REACH; dz <= FLUID_REACH && !fluidNear;
                        dz++) {
                    for (int dx = -FLUID_REACH; dx <= FLUID_REACH; dx++) {
                        if (world.fluid(f.x() + dx, f.y() + dy, f.z() + dz)) {
                            fluidNear = true;
                            break;
                        }
                    }
                }
            }
            finished.add(entry(f, exposed, fluidNear));
            budget -= perOre;
            lastLookups += perOre;
        }
        if (nextFinish >= finishing.size()) {
            publish();
        }
    }

    static String entry(String name, int x, int y, int z, boolean exposed,
                        boolean fluidNear) {
        return "{\"name\":\"" + name + "\",\"x\":" + x + ",\"y\":" + y
                + ",\"z\":" + z + ",\"exposed\":" + exposed
                + ",\"fluid_near\":" + fluidNear + "}";
    }

    private static String entry(Found f, boolean exposed, boolean fluidNear) {
        return entry(f.name(), f.x(), f.y(), f.z(), exposed, fluidNear);
    }

    private void publish() {
        // Over the byte cap, the farthest go first: entries are nearest
        // first, so dropping from the end keeps the nearest.
        List<String> kept = new ArrayList<>(finished);
        while (!kept.isEmpty() && body(kept).length() > MAX_JSON_BYTES) {
            Found dropped = finishing.get(kept.size() - 1);
            leftOut = Math.min(leftOut, dropped.distance());
            kept.remove(kept.size() - 1);
        }
        boolean complete = !unloaded && Double.isInfinite(leftOut);
        published = "{\"origin\":[" + originX + "," + originY + "," + originZ
                + "],\"radius\":" + RADIUS + ",\"down\":" + DOWN
                + ",\"up\":" + UP + ",\"complete\":" + complete
                + ",\"complete_within\":"
                + (Double.isInfinite(leftOut) ? "null"
                        : String.format(Locale.ROOT, "%.3f",
                                        Math.sqrt(leftOut)))
                + ",\"ores\":" + body(kept) + "}";
        running = false;
        finishing = null;
        passes++;
    }

    private static String body(List<String> entries) {
        return "[" + String.join(",", entries) + "]";
    }
}
