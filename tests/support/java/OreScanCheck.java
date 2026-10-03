package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Random;
import java.util.Set;

/**
 * Drives {@link OreScan} for tests/bridge/test_bridge_ore_scan.py over a
 * made-up world: a grid of cells, stone by default below {@code ground},
 * air above. One command per line:
 * <ul>
 *   <li>{@code world minX minY minZ maxX maxY maxZ ground} -- a new world</li>
 *   <li>{@code set x y z name} -- one cell (air, stone, water, lava, or
 *       any {@code minecraft:..._ore})</li>
 *   <li>{@code scatter seed count name} -- ore at random cells</li>
 *   <li>{@code unload cx cz} -- a chunk that is not loaded</li>
 *   <li>{@code palette all|real} -- whether every section claims it may
 *       hold ore (the worst case) or only those that do</li>
 *   <li>{@code step x y z nowMs} -- prints lookups, sections skipped,
 *       whether a pass is running</li>
 *   <li>{@code run x y z} -- steps 200 ms apart until a pass is
 *       published; prints the steps it took and the most cells one step
 *       read</li>
 *   <li>{@code json} -- the published field</li>
 *   <li>{@code single server published} -- {@link OreScan#singleplayer}
 *       for 0/1 each: prints true or false</li>
 *   <li>{@code time x y z steps} -- runs that many steps 200 ms apart and
 *       prints the average and largest step in milliseconds, the cells
 *       read per pass, and the passes finished</li>
 * </ul>
 */
final class OreScanCheck {

    static final class Grid implements OreScan.World {
        int minX, minY, minZ, maxX, maxY, maxZ;
        byte[] cells;
        final List<String> palette = new ArrayList<>(List.of(
                "air", "minecraft:stone", "minecraft:water", "minecraft:lava"));
        final Set<Long> unloaded = new HashSet<>();
        boolean allMayHaveOre = false;
        // Stands in for the section palette: worked out once per section,
        // so the timing is the scan's work, not this harness's.
        final HashMap<String, Boolean> sectionHasOre = new HashMap<>();

        Grid(int minX, int minY, int minZ, int maxX, int maxY, int maxZ,
             int ground) {
            this.minX = minX; this.minY = minY; this.minZ = minZ;
            this.maxX = maxX; this.maxY = maxY; this.maxZ = maxZ;
            cells = new byte[(maxX - minX + 1) * (maxY - minY + 1)
                    * (maxZ - minZ + 1)];
            for (int y = minY; y <= maxY; y++) {
                for (int z = minZ; z <= maxZ; z++) {
                    for (int x = minX; x <= maxX; x++) {
                        cells[index(x, y, z)] = (byte) (y < ground ? 1 : 0);
                    }
                }
            }
        }

        boolean inside(int x, int y, int z) {
            return x >= minX && x <= maxX && y >= minY && y <= maxY
                    && z >= minZ && z <= maxZ;
        }

        int index(int x, int y, int z) {
            return ((y - minY) * (maxZ - minZ + 1) + (z - minZ))
                    * (maxX - minX + 1) + (x - minX);
        }

        String name(int x, int y, int z) {
            if (!inside(x, y, z)
                    || unloaded.contains(key(x >> 4, z >> 4))) {
                return "air";
            }
            return palette.get(cells[index(x, y, z)]);
        }

        void set(int x, int y, int z, String name) {
            sectionHasOre.clear();
            int i = palette.indexOf(name);
            if (i < 0) {
                palette.add(name);
                i = palette.size() - 1;
            }
            cells[index(x, y, z)] = (byte) i;
        }

        static long key(int cx, int cz) {
            return ((long) cx << 32) ^ (cz & 0xFFFFFFFFL);
        }

        @Override
        public boolean loaded(int chunkX, int chunkZ) {
            return !unloaded.contains(key(chunkX, chunkZ));
        }

        @Override
        public boolean mayHaveOre(int chunkX, int sectionY, int chunkZ) {
            if (sectionY * 16 > maxY || sectionY * 16 + 15 < minY) {
                return false;                   // outside the world
            }
            if (allMayHaveOre) {
                return true;
            }
            return sectionHasOre.computeIfAbsent(
                    chunkX + "," + sectionY + "," + chunkZ,
                    k -> scanSection(chunkX, sectionY, chunkZ));
        }

        private boolean scanSection(int chunkX, int sectionY, int chunkZ) {
            for (int y = sectionY * 16; y < sectionY * 16 + 16; y++) {
                for (int z = chunkZ * 16; z < chunkZ * 16 + 16; z++) {
                    for (int x = chunkX * 16; x < chunkX * 16 + 16; x++) {
                        if (name(x, y, z).endsWith("_ore")) {
                            return true;
                        }
                    }
                }
            }
            return false;
        }

        @Override
        public String oreAt(int x, int y, int z) {
            String name = name(x, y, z);
            return name.endsWith("_ore") ? name : null;
        }

        @Override
        public boolean open(int x, int y, int z) {
            String name = name(x, y, z);
            return name.equals("air") || fluid(x, y, z);
        }

        @Override
        public boolean fluid(int x, int y, int z) {
            String name = name(x, y, z);
            return name.endsWith("water") || name.endsWith("lava");
        }
    }

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        Grid world = null;
        OreScan scan = new OreScan();
        String line;
        while ((line = in.readLine()) != null) {
            String[] p = line.trim().split("\\s+");
            switch (p[0]) {
                case "world" -> {
                    world = new Grid(i(p[1]), i(p[2]), i(p[3]), i(p[4]),
                                     i(p[5]), i(p[6]), i(p[7]));
                    scan = new OreScan();
                }
                case "set" -> world.set(i(p[1]), i(p[2]), i(p[3]), p[4]);
                case "scatter" -> {
                    Random random = new Random(Long.parseLong(p[1]));
                    for (int n = 0; n < i(p[2]); n++) {
                        int x = world.minX + random.nextInt(
                                world.maxX - world.minX + 1);
                        int y = world.minY + random.nextInt(
                                world.maxY - world.minY + 1);
                        int z = world.minZ + random.nextInt(
                                world.maxZ - world.minZ + 1);
                        if (!world.name(x, y, z).equals("air")) {
                            world.set(x, y, z, p[3]);
                        }
                    }
                }
                case "unload" -> world.unloaded.add(
                        Grid.key(i(p[1]), i(p[2])));
                case "palette" -> world.allMayHaveOre = p[1].equals("all");
                case "step" -> {
                    scan.step(i(p[1]), i(p[2]), i(p[3]),
                              Long.parseLong(p[4]), world);
                    System.out.println(scan.lastLookups() + " "
                            + scan.lastSkipped() + " " + scan.running());
                }
                case "run" -> {
                    int before = scan.passes();
                    long now = 1_000_000L;
                    int steps = 0;
                    int most = 0;
                    while (scan.passes() == before && steps < 10_000) {
                        scan.step(i(p[1]), i(p[2]), i(p[3]), now, world);
                        most = Math.max(most, scan.lastLookups());
                        now += 200;
                        steps++;
                    }
                    System.out.println(steps + " " + most);
                }
                case "json" -> System.out.println(scan.json());
                case "single" -> System.out.println(OreScan.singleplayer(
                        p[1].equals("1"), p[2].equals("1")));
                case "time" -> {
                    int steps = i(p[4]);
                    long now = 5_000_000L;
                    long total = 0;
                    long worst = 0;
                    long cells = 0;
                    int before = scan.passes();
                    // Warm up the JIT the way a running game would have.
                    for (int n = 0; n < 50; n++) {
                        scan.step(i(p[1]), i(p[2]), i(p[3]), now, world);
                        now += 200;
                    }
                    before = scan.passes();
                    for (int n = 0; n < steps; n++) {
                        long t0 = System.nanoTime();
                        scan.step(i(p[1]), i(p[2]), i(p[3]), now, world);
                        long dt = System.nanoTime() - t0;
                        total += dt;
                        worst = Math.max(worst, dt);
                        cells += scan.lastLookups();
                        now += 200;
                    }
                    int passes = scan.passes() - before;
                    System.out.println(String.format(Locale.ROOT,
                            "%.4f %.4f %d %d", total / 1e6 / steps,
                            worst / 1e6, passes == 0 ? 0 : cells / passes,
                            passes));
                }
                default -> { }
            }
        }
    }

    private static int i(String s) {
        return Integer.parseInt(s);
    }
}
