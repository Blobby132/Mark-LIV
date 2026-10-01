package com.markliv.bridge;

import java.util.Locale;

/**
 * The open screen, its slots and the pointer, as JSON -- the arithmetic and
 * the shape, with no Minecraft types, so the Python tests compile this and
 * check it. MarkLivBridge reads the game; this decides what it says.
 *
 * <p>Positions are WINDOW pixels, the coordinates the pointer is reported
 * in, so "is the pointer over this slot" is one comparison on the reader's
 * side. A slot is 16 GUI units square; its centre is origin + slot + 8, and
 * {@code scale} is window pixels per GUI unit.
 */
final class Gui {

    private Gui() { }

    /** At most this many slots are reported. A double chest is 90. */
    static final int MAX_SLOTS = 64;

    /**
     * What a slot is for. {@code containerSlot} is the slot's index inside
     * the player's own inventory when {@code playerInventory}: 0-8 the
     * hotbar, 9-35 the main inventory, 36-39 armour, 40 the off hand.
     */
    static String role(boolean result, boolean craftGrid,
                       boolean playerInventory, int containerSlot) {
        if (result) {
            return "craft_out";
        }
        if (craftGrid) {
            return "craft_in";
        }
        if (playerInventory) {
            if (containerSlot >= 0 && containerSlot <= 8) {
                return "hotbar";
            }
            if (containerSlot >= 9 && containerSlot <= 35) {
                return "inventory";
            }
            if (containerSlot >= 36 && containerSlot <= 39) {
                return "armor";
            }
            if (containerSlot == 40) {
                return "offhand";
            }
        }
        return "other";
    }

    /** A slot's centre on one axis, in window pixels. */
    static double centre(int origin, int slotPosition, double scale) {
        return (origin + slotPosition + 8) * scale;
    }

    static String slot(int i, String role, double x, double y, String item,
                       int count) {
        boolean known = item != null && registryName(item);
        return "{\"i\":" + i + ",\"role\":\"" + role + "\",\"x\":" + number(x)
                + ",\"y\":" + number(y) + ",\"item\":"
                + (known ? "\"" + item + "\"" : "null") + ",\"count\":"
                + (known ? count : 0) + "}";
    }

    static String view(double scale, int width, int height, double cursorX,
                       double cursorY) {
        return "{\"scale\":" + number(scale) + ",\"window_px\":[" + width + ","
                + height + "],\"cursor_px\":[" + number(cursorX) + ","
                + number(cursorY) + "]}";
    }

    /** The stack on the pointer, or "null" for nothing. */
    static String carried(String item, int count) {
        if (item == null || count <= 0 || !registryName(item)) {
            return "null";
        }
        return "{\"name\":\"" + item + "\",\"count\":" + count + "}";
    }

    static String screen(String kind) {
        return "{\"kind\":\"" + kind + "\"}";
    }

    /** Lower-case letters, digits and _-./: -- nothing that needs escaping. */
    static boolean registryName(String name) {
        if (name.isEmpty()) {
            return false;
        }
        for (int i = 0; i < name.length(); i++) {
            char c = name.charAt(i);
            boolean plain = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
                    || c == '_' || c == '-' || c == '.' || c == '/'
                    || c == ':';
            if (!plain) {
                return false;
            }
        }
        return true;
    }

    private static String number(double value) {
        if (Double.isNaN(value) || Double.isInfinite(value)) {
            return "null";
        }
        return String.format(Locale.ROOT, "%.2f", value)
                .replaceAll("0$", "").replaceAll("\\.$", ".0");
    }
}
