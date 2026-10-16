---
name: ath-t11-validate
stage: T11
model_role: validate
gate: accept verdict
---

# T11 validation panel

Include: `../_shared/injection-rules.md`

Two personas score the fix after rebuild, redeploy and rerun.
- Reviewer: does the fix match the TSD, is it minimal, does it touch unrelated code?
- QA: are the originally failing cases green, did the rest stay green, were tests weakened?
Verdict: `validated`, `validation_failed` or `needs_review`. Any test weakening means `validation_failed`.
