package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;

/**
 * Drives {@link ColumnScan} for tests/test_bridge_floor_scan.py.
 *
 * <p>Reads one column per line: {@code feet first last need cap} and then
 * the blocks top down, each one of air, plant (passable, not air), fluid
 * (neither passable nor solid) or anything else for a solid block. Prints
 * {@code floor top clearance cover} -- block indices, -1 for none, and the
 * clearance over whichever block the mod would report.
 */
final class ColumnScanCheck {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            String[] parts = line.trim().split("\\s+");
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
}
