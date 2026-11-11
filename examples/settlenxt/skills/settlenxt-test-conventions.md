# SettleNxt test conventions (example)

- JUnit 5. One test class per production class, named `<Class>Test`, in the same package under `src/test/java`.
- Test method names describe the behaviour: `feeIsRoundedHalfUp`, not `test1`.
- Use `assertEquals(expected, actual)` with `BigDecimal` for money; never compare doubles.
- Card, account, mobile and Aadhaar values come from the test-data catalogue (`{{testdata:...}}`).
- Fee test data lives in `testdata/clearing/*.csv`, one row per case, keyed by `test_id` (`FEE-nnn`).
- Tests do not call the network, start processes or read environment variables.
