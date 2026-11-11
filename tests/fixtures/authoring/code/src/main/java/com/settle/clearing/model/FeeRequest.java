package com.settle.clearing.model;

import java.math.BigDecimal;

public record FeeRequest(String pan, BigDecimal amount, Channel channel) {
    public FeeRequest {
        if (amount == null) throw new IllegalArgumentException("amount");
    }

    public enum Channel { POS, ECOM("e"), ATM; Channel() {} Channel(String c) {} }
}
