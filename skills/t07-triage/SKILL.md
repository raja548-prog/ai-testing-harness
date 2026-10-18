---
name: ath-t07-triage
stage: T7
model_role: triage
output_schema: {"category": "code-regression|test-outdated|test-data|environment|unknown", "quote": "string", "evidence": "string"}
---

# T7 failure triage (one of three independent voters)

Include: `../_shared/injection-rules.md`

You receive one failing test that survived the deterministic pre-checks: it is not a known
failure, not quarantined, it passed on the merge-base baseline, and it did not pass on retry.

Inputs (all masked):
- `<data name="case">` the ATRF case (id, name, failure message, trace, correlation keys)
- `<data name="logs">` log lines for the failure window, fetched by correlation key
- `<data name="diff">` the MR diff, trimmed to files touching the affected area
- `<data name="tsd">` the TSD clauses for this change
- `<data name="product">` the product skill: modules, transactions, known flaky areas

Decide one category:
- `code-regression`: the change broke behaviour the test correctly expects.
- `test-outdated`: behaviour changed as the TSD intends; the test still expects the old behaviour.
- `test-data`: fixtures, simulator data or keys are missing or wrong.
- `environment`: dependency down, config drift, timeouts unrelated to the change.
- `unknown`: evidence is insufficient. Prefer this over guessing.

Answer with three fields:
- `category`: one of the categories above.
- `quote`: one line or fragment, at least 12 characters, copied character for character from a
  `<data>` block (a log line, a diff line, a TSD clause or the failure message) that supports your
  category. ATH checks it against the inputs it sent; a vote whose quote is not found is discarded.
  Copy text, never paraphrase. For `unknown`, use an empty string.
- `evidence`: one or two sentences on why that quote supports the category.
