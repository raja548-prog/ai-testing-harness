---
name: settlenxt-product-skill
product: settlenxt
version: 0.3.0
approved_by: settlement-dept product owner
---

# SettleNxt product skill

Example product skill. Used by T1, T3, T7, T10, T11.

## What the product does
SettleNxt clears card transactions, computes interchange fees and settles merchant accounts daily.

## Modules and their responsibilities
| Module | Path | Responsibility |
| --- | --- | --- |
| Clearing | src/main/java/com/settle/clearing/ | Interchange fees, clearing files, refunds |
| Portal | src/main/java/com/settle/portal/ | Settlement portal labels and screens |

## Transactions and specs
| Transaction | Spec reference | Notes |
| --- | --- | --- |
| Purchase | SNX-TSD-100 | Fee charged at clearing |
| Refund | SNX-TSD-140 | Reverses the original fee |

## Reading the logs
- Correlation keys: rrn, stan, batchId
- `FEE-ROUND` warnings mean a fee was rounded; check the rounding mode

## Known flaky areas
- Batch cut-over at midnight: timing-dependent tests
