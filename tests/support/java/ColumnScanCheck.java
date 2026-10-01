package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;

/**
 * Drives {@link ColumnScan} for tests/bridge/test_bridge_floor_scan.py.
 *
 * <p>Reads one column per line: {@code feet first last need cap} and then
 * the blocks top down, each one of air, plant (passable, not air), fluid
 * (neither passable nor solid) or anything else for a solid block. Prints
 * {@code floor top clearance cover} -- block indices, -1 for none, and the
 * clearance over whichever block the mod would report. Lines starting
 * {@code above} or {@code reported} drive the tall-tree pass instead.
 */
final class ColumnScanCheck {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            String[] parts = line.trim().split("\\s+");
            if (parts[0].equals("reported")) {
                System.out.println(ColumnScan.reportedAbove(kind(parts[1])));
                continue;
            }
            if (parts[0].equals("above")) {
                above(parts);
                continue;
            }
            int feet = Integer.parseInt(parts[0]);
            int first = Integer.parseInt(parts[1]);
            int last = Integer.parseInt(parts[2]);
            int need = Integer.parseInt(parts[3]);
            int cap = Integer.parseInt(parts[4]);
            int n = parts.length - 5;
            boolean[] air = new boolean[n];
            boolean[] collides = new boolean[n];
            boolean[] open = new boolean[n];
            for (int i = 0; i < n; i++) {
                String kind = parts[5 + i];
                air[i] = kind.equals("air");
                collides[i] = !air[i] && !kind.equals("plant")
                        && !kind.equals("fluid");
                open[i] = air[i] || kind.equals("plant");
            }
            int floor = ColumnScan.floor(collides, open, first, last, feet,
                    need, cap);
            int top = ColumnScan.top(air, first, last);
            int chosen = floor >= 0 ? floor : top;
            int clearance = chosen >= 0
                    ? ColumnScan.clearance(open, chosen, cap) : -1;
            int cover = floor >= 0 ? ColumnScan.cover(air, floor, need) : -1;
            System.out.println(floor + " " + top + " " + clearance + " "
                    + cover);
        }
    }

    private static String kind(String word) {
        return word.equals("null") ? null : word;
    }

    /** {@code above topKind from to kind...}: the kinds from dy=from up.
     *  Prints the number of blocks read, then the dy of each log found. */
    private static void above(String[] parts) {
        int from = Integer.parseInt(parts[2]);
        int to = Integer.parseInt(parts[3]);
        int[] reads = {0};
        int[] logs = ColumnScan.logsAbove(kind(parts[1]), dy -> {
            reads[0]++;
            int at = 4 + dy - from;
            return at < parts.length ? kind(parts[at]) : null;
        }, from, to);
        StringBuilder out = new StringBuilder();
        for (int dy : logs) {
            out.append(' ').append(dy);
        }
        System.out.println(reads[0] + out.toString());
    }
}
