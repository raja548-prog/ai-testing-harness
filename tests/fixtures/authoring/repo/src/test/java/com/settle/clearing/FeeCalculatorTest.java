package com.settle.clearing;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.math.BigDecimal;
import org.junit.jupiter.api.Test;

class FeeCalculatorTest {

    private static final String CARD = "4111 1111 1111 1111";

    @Test
    void feeIsRoundedToTwoDecimals() {
        FeeCalculator calc = new FeeCalculator();
        BigDecimal fee = calc.interchangeFee(CARD, new BigDecimal("1000.00"));
        assertEquals(new BigDecimal("12.50"), fee);
    }
}
