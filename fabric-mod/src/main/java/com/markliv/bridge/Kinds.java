package com.markliv.bridge;

/**
 * Naming what a block or entity is, from its registry name alone.
 *
 * <p>No Minecraft types, so the Python tests compile this and check it.
 */
final class Kinds {

    private Kinds() { }

    /**
     * Which kind of block worth reporting individually this is, or null.
     * Each kind has its own quota in {@code MarkLivBridge.NOTABLE_QUOTAS}.
     *
     * <p>Matched on the name rather than on a tag or class, so a modded log
     * called {@code biomesoplenty:fir_log} is picked up without this mod
     * knowing anything about that mod. The cost is the occasional false
     * positive, which costs a wasted walk rather than a wrong belief.
     */
    static String notable(String name) {
        if (name.endsWith("_log") || name.endsWith("_wood")) {
            return "log";
        }
        if (name.endsWith("_ore")) {
            return "ore";
        }
        if (name.contains("lava")) {
            return "lava";
        }
        if (name.contains("water")) {
            return "water";
        }
        if (name.endsWith("crafting_table") || name.endsWith("furnace")
                || name.endsWith("chest")) {
            return "station";
        }
        return null;
    }

    /**
     * Which quota an entity category counts against: its own for the kinds
     * that matter to a decision, "other" for the rest (xp orbs, arrows,
     * paintings, boats...), so an unforeseen category is still reported
     * rather than dropped.
     */
    static String entityGroup(String category) {
        return switch (category == null ? "" : category) {
            case "hostile", "player", "passive", "item" -> category;
            default -> "other";
        };
    }
}
