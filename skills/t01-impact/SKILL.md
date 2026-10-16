---
name: ath-t01-impact
stage: T1
model_role: impact
output_schema: {"modules": ["string"], "transactions": ["string"], "risk": "low|medium|high", "extra_suites": ["string"], "confidence": "number"}
---

# T1 change-impact map

Include: `../_shared/injection-rules.md`

From the MR diff, the TSD and the product skill, list affected modules and transaction
types, and suites that should run beyond the profile's rules. You may only add suites
that exist in the profile; T2 applies profile rules first and ignores unknown suites.
If confidence is below 0.6, set risk to high so T2 widens selection to full regression.
