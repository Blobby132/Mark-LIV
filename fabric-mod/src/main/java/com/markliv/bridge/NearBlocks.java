package com.markliv.bridge;

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
 * under the feet to {@link #ABOVE} over them -- 486 cells at most. Kept
 * nearest first with quotas, solid blocks and passable ones (grass, a
 * torch) apart, so a field of grass cannot push the blocks of a wall off
 * the list. When anything is left out, {@code complete_within} says how
 * far the list is complete: a cell nearer than that and not listed is
 * air; beyond it, the reader does not know.
 *
 * <p>No Minecraft types, so the Python tests compile this file and check
 * it directly. The mod only reads: this builds a string.
 */
final class NearBlocks {

    static final int RADIUS = 4;
    static final int BELOW = 1;
    static final int ABOVE = 4;

    static final int SOLID_QUOTA = 192;
    static final int OTHER_QUOTA = 32;

    private final Nearest kept;
    private final int originX;
    private final int originY;
    private final int originZ;
    private double nearestLeftOut = Double.POSITIVE_INFINITY;  // squared

    NearBlocks(int originX, int originY, int originZ) {
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
