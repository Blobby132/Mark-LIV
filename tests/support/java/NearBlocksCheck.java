package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;

/**
 * Drives {@link NearBlocks} for tests/bridge/test_bridge_near_blocks.py. One
 * command per line: {@code inbox dx dy dz}, {@code origin x y z} (starts a
 * new snapshot), {@code offer x y z name solid}, {@code cell x y z name
 * solid fluid} (one grid cell; {@code air} for no block, {@code -} for no
 * fluid), {@code json} (prints the field) and {@code measure} (prints its
 * length in bytes).
 */
final class NearBlocksCheck {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        NearBlocks near = null;
        String line;
        while ((line = in.readLine()) != null) {
            String[] p = line.trim().split("\\s+");
            switch (p[0]) {
                case "inbox" -> System.out.println(NearBlocks.inBox(
                        Integer.parseInt(p[1]), Integer.parseInt(p[2]),
                        Integer.parseInt(p[3])));
                case "origin" -> near = new NearBlocks(
                        Integer.parseInt(p[1]), Integer.parseInt(p[2]),
                        Integer.parseInt(p[3]));
                case "offer" -> near.offer(Integer.parseInt(p[1]),
                        Integer.parseInt(p[2]), Integer.parseInt(p[3]), p[4],
                        Boolean.parseBoolean(p[5]));
                case "cell" -> near.cell(Integer.parseInt(p[1]),
                        Integer.parseInt(p[2]), Integer.parseInt(p[3]),
                        p[4].equals("air") ? null : p[4],
                        Boolean.parseBoolean(p[5]),
                        p[6].equals("-") ? null : p[6]);
                case "json" -> System.out.println(near.json());
                case "measure" -> System.out.println(
                        near.json().getBytes("UTF-8").length);
                default -> { }
            }
        }
    }
}
