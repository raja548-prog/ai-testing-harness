package com.settle.clearing;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;
import java.util.Map;

/** Interchange fee calculation. { braces in comments are ignored } */
public class FeeCalculator {

    private static final BigDecimal RATE = new BigDecimal("0.0125");
    private final Map<String, List<BigDecimal>> history = new java.util.HashMap<>();
    private final int[] limits = {1, 2, 3};

    public FeeCalculator() {
    }

    /** Fee for one transaction. */
    public BigDecimal interchangeFee(String pan,
                                     BigDecimal amount) {
        if (amount.signum() < 0) { throw new IllegalArgumentException("negative; {not a brace}"); }
        return amount.multiply(RATE).setScale(2, RoundingMode.HALF_UP);
    }

    @Deprecated
    protected <T extends Comparable<T>> List<T> sorted(List<T> items,
            Map<String, ? extends T> extra) {
        return items;
    }

    private void audit(String message) {
        System.out.println(message);
    }

    public static class Summary {
        public BigDecimal total() { return BigDecimal.ZERO; }
    }
}
