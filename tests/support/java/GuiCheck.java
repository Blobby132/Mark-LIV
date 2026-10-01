package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.List;

/**
 * Drives {@link Gui} for tests/bridge/test_bridge_gui_fields.py. One command per
 * line: {@code role result craftGrid playerInventory containerSlot},
 * {@code centre origin slot scale}, {@code slot i role x y item count}
 * ({@code null} for no item), {@code view scale w h cx cy},
 * {@code measure n} (bytes of the GUI fields for n full slots) and
 * {@code count n} (how many of n slots would be reported).
 */
final class GuiCheck {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            String[] p = line.trim().split("\\s+");
            switch (p[0]) {
                case "role" -> System.out.println(Gui.role(
                        Boolean.parseBoolean(p[1]), Boolean.parseBoolean(p[2]),
                        Boolean.parseBoolean(p[3]), Integer.parseInt(p[4])));
                case "centre" -> System.out.println(Gui.centre(
                        Integer.parseInt(p[1]), Integer.parseInt(p[2]),
                        Double.parseDouble(p[3])));
                case "slot" -> System.out.println(Gui.slot(
                        Integer.parseInt(p[1]), p[2],
                        Double.parseDouble(p[3]), Double.parseDouble(p[4]),
                        p[5].equals("null") ? null : p[5],
                        Integer.parseInt(p[6])));
                case "view" -> System.out.println(Gui.view(
                        Double.parseDouble(p[1]), Integer.parseInt(p[2]),
                        Integer.parseInt(p[3]), Double.parseDouble(p[4]),
                        Double.parseDouble(p[5])));
                case "measure" -> {
                    int n = Integer.parseInt(p[1]);
                    List<String> slots = new ArrayList<>();
                    for (int i = 0; i < Math.min(n, Gui.MAX_SLOTS); i++) {
                        slots.add(Gui.slot(i, "inventory", 1234.56, 789.01,
                                "minecraft:polished_blackstone_brick_stairs",
                                64));
                    }
                    String all = "\"screen\":" + Gui.screen("crafting_table")
                            + ",\"gui\":" + Gui.view(2.5, 2560, 1440, 1279.5,
                                                     719.5)
                            + ",\"slots\":[" + String.join(",", slots) + "]"
                            + ",\"carried\":" + Gui.carried(
                                    "minecraft:polished_blackstone_brick_stairs",
                                    64)
                            + ",\"game_mode\":\"survival\"";
                    System.out.println(all.length());
                }
                case "count" -> System.out.println(
                        Math.min(Integer.parseInt(p[1]), Gui.MAX_SLOTS));
                default -> { }
            }
        }
    }
}
