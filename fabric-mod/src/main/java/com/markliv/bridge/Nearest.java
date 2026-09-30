package com.markliv.bridge;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;

/**
 * Keeps, for each category, the candidates nearest the player -- no more
 * than that category's quota -- whatever order they were offered in.
 *
 * <p>Both capped lists in the payload used to keep the FIRST n things found.
 * The terrain scan runs west to east, so in a forest the 64 notable blocks
 * were all 6 to 10 blocks west and the tree the player stood at was never
 * reported; and the entity list was in the level's own order, so a pile of
 * dropped items could push a zombie off it. Quotas per category mean water
 * cannot crowd out logs, and items cannot crowd out hostile mobs.
 *
 * <p>No Minecraft types, so the Python tests compile this file and check it
 * directly. Each category keeps a bounded max-heap: offering thousands of
 * water blocks costs a heap of size quota, not a list of thousands.
 */
final class Nearest {

    private record Candidate(double distance, long order, String json) { }

    private static final Comparator<Candidate> FARTHEST_FIRST =
            Comparator.comparingDouble(Candidate::distance)
                    .thenComparingLong(Candidate::order).reversed();

    private final Map<String, Integer> quotas;
    private final Map<String, PriorityQueue<Candidate>> kept =
            new LinkedHashMap<>();
    private long offered = 0;

    /** @param quotas category name to how many of it to keep, in order */
    Nearest(Map<String, Integer> quotas) {
        this.quotas = new LinkedHashMap<>(quotas);
        for (String category : this.quotas.keySet()) {
            kept.put(category, new PriorityQueue<>(FARTHEST_FIRST));
        }
    }

    /**
     * Would a candidate this far away be kept? Lets a caller skip building
     * the JSON for the thousands of blocks in a lake that would not be.
     */
    boolean wants(String category, double distance) {
        PriorityQueue<Candidate> heap = category == null ? null
                : kept.get(category);
        if (heap == null || quotas.get(category) <= 0) {
            return false;
        }
        return heap.size() < quotas.get(category)
                || distance < heap.peek().distance();
    }

    /** Offer one candidate. A category with no quota is dropped. */
    void offer(String category, double distance, String json) {
        PriorityQueue<Candidate> heap = category == null ? null
                : kept.get(category);
        if (heap == null) {
            return;
        }
        int quota = quotas.get(category);
        if (quota <= 0) {
            return;
        }
        Candidate candidate = new Candidate(distance, offered++, json);
        if (heap.size() < quota) {
            heap.add(candidate);
        } else if (FARTHEST_FIRST.compare(candidate, heap.peek()) > 0) {
            // Nearer than the farthest kept (or as near and offered earlier).
            heap.poll();
            heap.add(candidate);
        }
    }

    /**
     * What was kept, nearest first -- except that the categories named in
     * {@code first} come before all the rest, each nearest first. Ties go to
     * the one offered earlier, so the output is deterministic.
     */
    List<String> select(List<String> first) {
        List<Candidate> leading = new ArrayList<>();
        List<Candidate> rest = new ArrayList<>();
        for (Map.Entry<String, PriorityQueue<Candidate>> entry
                : kept.entrySet()) {
            (first.contains(entry.getKey()) ? leading : rest)
                    .addAll(entry.getValue());
        }
        Comparator<Candidate> nearestFirst =
                Comparator.comparingDouble(Candidate::distance)
                        .thenComparingLong(Candidate::order);
        leading.sort(nearestFirst);
        rest.sort(nearestFirst);
        List<String> out = new ArrayList<>(leading.size() + rest.size());
        for (Candidate c : leading) {
            out.add(c.json());
        }
        for (Candidate c : rest) {
            out.add(c.json());
        }
        return out;
    }
}
