package com.markliv.bridge;

import java.util.Arrays;
import java.util.function.IntFunction;

/**
 * Which block in one column of the terrain scan is the floor.
 *
 * <p>Pure arithmetic over arrays, with no Minecraft types, so the rules can
 * be checked without a game running -- the Python test suite compiles this
 * file and drives it with columns built by hand.
 *
 * <p>A column is read TOP DOWN: index 0 is the highest block read and each
 * index after it is one block lower, so the block above index {@code i} is
 * {@code i - 1}. The column is read a few blocks higher than the scan
 * reaches, so the headroom over the highest scanned block is known rather
 * than assumed.
 */
final class ColumnScan {

    private ColumnScan() { }

    /**
     * The floor nearest the feet, or -1 if nothing in range fits a player.
     *
     * <p>A floor is a block with collision and at least {@code need} passable
     * blocks above it. Of those, the one whose standing position is closest
     * to the player's feet wins, and a tie goes to the lower one -- a drop is
     * walkable where a climb of the same size is not.
     *
     * <p>NOT the topmost block. Topmost was the first design, and under a
     * tree it reported the canopy: every column round a trunk looked like a
     * wall of leaves four blocks up, and every tree was unreachable.
     *
     * @param collides whether each block has a collision shape
     * @param passable whether a player's body can occupy each block
     * @param first    highest index that may be a floor (the scan's top)
     * @param last     lowest index that may be a floor (the scan's bottom)
     * @param feet     index of the block the player's feet are in
     * @param need     passable blocks a standing player needs above a floor
     * @param cap      furthest to count headroom, as in {@link #clearance}
     */
    static int floor(boolean[] collides, boolean[] passable, int first,
                     int last, int feet, int need, int cap) {
        int best = -1;
        int bestDistance = Integer.MAX_VALUE;
        for (int i = Math.max(first, 0);
                i <= last && i < collides.length; i++) {
            if (!collides[i] || clearance(passable, i, cap) < need) {
                continue;
            }
            int distance = Math.abs((i - 1) - feet);
            if (distance <= bestDistance) {
                best = i;
                bestDistance = distance;
            }
        }
        return best;
    }

    /**
     * Passable blocks directly above {@code index}, counting at most
     * {@code cap}: nothing downstream cares whether the sky is four blocks up
     * or four hundred. Stops at the top of what was read -- unknown is not
     * room.
     */
    static int clearance(boolean[] passable, int index, int cap) {
        int clear = 0;
        for (int i = index - 1; i >= 0 && clear < cap; i--) {
            if (!passable[i]) {
                break;
            }
            clear++;
        }
        return clear;
    }

    /** The highest non-air block in the scan's range, or -1. */
    static int top(boolean[] air, int first, int last) {
        for (int i = Math.max(first, 0); i <= last && i < air.length; i++) {
            if (!air[i]) {
                return i;
            }
        }
        return -1;
    }

    /**
     * The first non-air block in the space a player standing on
     * {@code floor} occupies -- grass at the feet, a vine at the head -- or
     * -1. Passable, since the floor was chosen for its room; reported because
     * passable is not harmless, and a berry bush or fire is walked THROUGH.
     */
    static int cover(boolean[] air, int floor, int need) {
        for (int i = floor - 1; i >= 0 && i >= floor - need; i--) {
            if (!air[i]) {
                return i;
            }
        }
        return -1;
    }

    /**
     * Whether a notable block of this kind is reported from the rows read
     * ABOVE the scan's top -- the headroom rows, read anyway to measure
     * clearance. Only logs: a trunk rising past the scan's top is the tree
     * the player is felling, and its upper logs were invisible to it.
     */
    static boolean reportedAbove(String kind) {
        return "log".equals(kind);
    }

    /**
     * The tall-tree pass: where the highest block read in a column is a
     * log, follow the trunk up that one column, from {@code from} to
     * {@code to} (dy above the feet), and return the dy of each log --
     * stopping at the first block that is not one.
     *
     * <p>Cheap by construction: a column not topped by a log costs nothing,
     * and one that is costs a read per log of trunk plus one.
     *
     * @param topKind the notable kind of the highest block already read
     * @param kindAt  the notable kind of the block at a dy, or null
     */
    static int[] logsAbove(String topKind, IntFunction<String> kindAt,
                           int from, int to) {
        if (!"log".equals(topKind) || to < from) {
            return new int[0];
        }
        int[] found = new int[to - from + 1];
        int n = 0;
        for (int dy = from; dy <= to; dy++) {
            if (!"log".equals(kindAt.apply(dy))) {
                break;
            }
            found[n++] = dy;
        }
        return Arrays.copyOf(found, n);
    }
}
