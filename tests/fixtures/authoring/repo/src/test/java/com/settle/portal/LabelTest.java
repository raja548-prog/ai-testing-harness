package com.settle.portal;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class LabelTest {
    @Test
    void settlementLabelUsesFullWords() {
        assertEquals("Settlement", Labels.of("SETTLEMENT"));
    }
}
