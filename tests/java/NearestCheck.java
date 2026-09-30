package com.markliv.bridge;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Drives {@link Nearest} and {@link Kinds} for the Python tests.
 *
 * <p>Input, one per line: {@code quotas cat=n,cat=n}, {@code first cat,cat},
 * {@code offer cat distance label}, {@code kind name} (prints the block's
 * notable kind), {@code group category} (prints an entity's quota group),
 * and {@code select} (prints the kept labels in order, one
 * per line, then {@code end}).
 */
final class NearestCheck {

    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in));
        Map<String, Integer> quotas = new LinkedHashMap<>();
        List<String> first = new ArrayList<>();
        Nearest nearest = null;
        String line;
        while ((line = in.readLine()) != null) {
            String[] parts = line.trim().split("\\s+");
            switch (parts[0]) {
                case "quotas" -> {
                    for (String pair : parts[1].split(",")) {
                        String[] kv = pair.split("=");
                        quotas.put(kv[0], Integer.parseInt(kv[1]));
                    }
                    nearest = new Nearest(quotas);
                }
                case "first" -> first = List.of(parts[1].split(","));
                case "offer" -> {
                    double d = Double.parseDouble(parts[2]);
                    if (nearest.wants(parts[1], d)) {
                        nearest.offer(parts[1], d, parts[3]);
                    }
                }
                case "kind" -> System.out.println(Kinds.notable(parts[1]));
                case "group" -> System.out.println(
                        Kinds.entityGroup(parts[1]));
                case "select" -> {
                    for (String label : nearest.select(first)) {
                        System.out.println(label);
                    }
                    System.out.println("end");
                }
                default -> { }
            }
        }
    }
}
