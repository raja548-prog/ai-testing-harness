package com.settle.clearing.model;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.math.BigDecimal;

public class FeeResponse {
    @JsonProperty("fee_amount")
    private BigDecimal feeAmount;
    private String currency = "INR";
    private static final long serialVersionUID = 1L;

    public BigDecimal getFeeAmount() { return feeAmount; }
}
