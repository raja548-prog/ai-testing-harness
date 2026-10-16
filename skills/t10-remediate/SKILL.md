---
name: ath-t10-remediate
stage: T10
model_role: remediate
gate: approve redeploy
---

# T10 remediation

Include: `../_shared/injection-rules.md`

Fix only failures triaged as `code-regression`. Make the smallest change that satisfies
the TSD. Never edit, skip, weaken or delete tests; a fix that does is rejected by T11.
Commit only to the ADLC branch of this MR.
