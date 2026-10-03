package com.markliv.bridge;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Every non-air block in a small box around the player: for checking a
 * finished build block by block, and for knowing what is under a wall's
 * top, which the surface scan (one floor per column) cannot say.
 *
 * <p>The box: {@link #RADIUS} blocks each way across, from {@link #BELOW}
 * under the feet to {@link #ABOVE} over them -- 729 cells. Five below,
 * because digging down a staircase needs to see the floor two below the
 * next step and what is beside and under it.
 *
 * <p>Two views of it. {@code blocks}: the non-air blocks, nearest first
 * with quotas, solid blocks and passable ones (grass, a torch) apart; when
 * anything is left out, {@code complete_within} says how far that list is
 * complete. {@code grid}: EVERY cell, air included, as a palette of
 * {@code [name, solid, fluid]} and run-length runs of palette indices in
 * y, then z, then x order from the box's low corner -- so a cell is never
 * guessed. {@code fluid} names water or lava (source or flowing, or a
 * waterlogged block) by its registry name. A cell in a chunk that is not
 * loaded is index -1, and {@code complete} is true only when there are
 * none: every cell of the box is known.
 *
 * <p>No Minecraft types, so the Python tests compile this file and check
 * it directly. The mod only reads: this builds a string.
 */
final class NearBlocks {

    static final int RADIUS = 4;
    static final int BELOW = 5;
    static final int ABOVE = 3;
    static final int SIDE = 2 * RADIUS + 1;
    static final int HEIGHT = BELOW + ABOVE + 1;
    static final String AIR = "minecraft:air";

    static final int SOLID_QUOTA = 192;
    static final int OTHER_QUOTA = 32;

    private final Nearest kept;
    private final int originX;
    private final int originY;
    private final int originZ;
    private double nearestLeftOut = Double.POSITIVE_INFINITY;  // squared
    // The grid: a palette index per cell, -1 until the cell is reported.
    private final int[] grid = new int[SIDE * SIDE * HEIGHT];
    private final List<String> palette = new ArrayList<>();
    private final Map<String, Integer> paletteIndex = new HashMap<>();

    NearBlocks(int originX, int originY, int originZ) {
        Arrays.fill(grid, -1);
        Map<String, Integer> quotas = new LinkedHashMap<>();
        quotas.put("solid", SOLID_QUOTA);
        quotas.put("other", OTHER_QUOTA);
        this.kept = new Nearest(quotas);
        this.originX = originX;
        this.originY = originY;
        this.originZ = originZ;
    }

    /** Is the offset (from the feet block) inside the box? */
    static boolean inBox(int dx, int dy, int dz) {
        return Math.abs(dx) <= RADIUS && Math.abs(dz) <= RADIUS
                && dy >= -BELOW && dy <= ABOVE;
    }

    /** One non-air block at (x, y, z). Outside the box is ignored. */
    void offer(int x, int y, int z, String name, boolean solid) {
        int dx = x - originX;
        int dy = y - originY;
        int dz = z - originZ;
        if (!inBox(dx, dy, dz)) {
            return;
        }
        double distance = (double) dx * dx + (double) dy * dy
                + (double) dz * dz;
        String category = solid ? "solid" : "other";
        if (!kept.wants(category, distance)) {
            nearestLeftOut = Math.min(nearestLeftOut, distance);
            return;
        }
        double left = kept.offer(category, distance,
                entry(x, y, z, name, solid));
        if (!Double.isNaN(left)) {
            nearestLeftOut = Math.min(nearestLeftOut, left);
        }
    }

    /**
     * What is at one cell of the box, air included: its name (null for
     * air), whether it has collision, and its fluid's name (null for
     * none). Outside the box is ignored. A cell never reported stays
     * unknown.
     */
    void cell(int x, int y, int z, String name, boolean solid, String fluid) {
        int dx = x - originX;
        int dy = y - originY;
        int dz = z - originZ;
        if (!inBox(dx, dy, dz)) {
            return;
        }
        String key = paletteEntry(name == null ? AIR : name, solid, fluid);
        Integer index = paletteIndex.get(key);
        if (index == null) {
            index = palette.size();
            palette.add(key);
            paletteIndex.put(key, index);
        }
        grid[((dy + BELOW) * SIDE + (dz + RADIUS)) * SIDE + (dx + RADIUS)] =
                index;
    }

    private static String paletteEntry(String name, boolean solid,
                                       String fluid) {
        boolean known = Gui.registryName(name);
        boolean fluidKnown = fluid != null && Gui.registryName(fluid);
        return "[" + (known ? "\"" + name + "\"" : "null") + "," + solid
                + "," + (fluidKnown ? "\"" + fluid + "\"" : "null") + "]";
    }

    /** Every cell reported? */
    boolean complete() {
        for (int index : grid) {
            if (index < 0) {
                return false;
            }
        }
        return true;
    }

    private String gridJson() {
        StringBuilder out = new StringBuilder(256 + palette.size() * 48);
        out.append("{\"order\":\"yzx\",\"palette\":[");
        for (int i = 0; i < palette.size(); i++) {
            if (i > 0) {
                out.append(',');
            }
            out.append(palette.get(i));
        }
        out.append("],\"runs\":[");
        int i = 0;
        boolean first = true;
        while (i < grid.length) {
            int j = i;
            while (j < grid.length && grid[j] == grid[i]) {
                j++;
            }
            if (!first) {
                out.append(',');
            }
            first = false;
            out.append(grid[i]).append(',').append(j - i);
            i = j;
        }
        return out.append("]}").toString();
    }

    static String entry(int x, int y, int z, String name, boolean solid) {
        boolean known = name != null && Gui.registryName(name);
        return "[" + x + "," + y + "," + z + ","
                + (known ? "\"" + name + "\"" : "null") + "," + solid + "]";
    }

    /** The whole field's value. */
    String json() {
        List<String> blocks = kept.select(List.of("solid"));
        StringBuilder out = new StringBuilder(64 + blocks.size() * 40);
        out.append("{\"origin\":[").append(originX).append(',')
                .append(originY).append(',').append(originZ).append(']')
                .append(",\"radius\":").append(RADIUS)
                .append(",\"below\":").append(BELOW)
                .append(",\"above\":").append(ABOVE)
                .append(",\"complete\":").append(complete())
                .append(",\"grid\":").append(gridJson())
                .append(",\"complete_within\":")
                .append(Double.isInfinite(nearestLeftOut) ? "null"
                        : String.format(Locale.ROOT, "%.3f",
                                        Math.sqrt(nearestLeftOut)))
                .append(",\"blocks\":[");
        for (int i = 0; i < blocks.size(); i++) {
            if (i > 0) {
                out.append(',');
            }
            out.append(blocks.get(i));
        }
        return out.append("]}").toString();
    }
}
