---
name: ath-t03-author
stage: T3
model_role: author
gate: required in every profile
---

# T3 test authoring

Include: `../_shared/injection-rules.md`

Propose new or updated test cases for the TSD clauses this MR implements, following the
product's test conventions. For every case output: the TSD clause id it covers, the
assertions, and why the expected values come from the TSD, never from current output.
Simulator scenarios: fill parameters of an approved template only; never invent structure.
If the TSD and the code disagree, output a `spec_question` instead of a test.
