# Triage eval dataset

Labelled failures for scoring the T7 triage voters:

```bash
ainxt-ath eval tests/evals/triage --profile examples/settlenxt/test-profile.local.yaml \
  --config ~/.config/ainxt/ath.yaml --min-accuracy 0.8
```

Each case is two files with the same id:

- `cases/<id>.yaml` holds what the voters see: `case` (an ATRF case), `logs`, `diff`, `tsd` and `product`.
- `truth/<id>.yaml` holds the real answer: `category`, `why`, `tags`, and `injectionTarget` for
  prompt-injection cases. It is never put in a prompt, and a case file that carries truth fields is
  rejected.

`category` is one of `code-regression`, `test-outdated`, `test-data`, `environment` or `unknown`.
Pre-check categories (flaky, pre-existing, known failure) are decided by code, not agents.

## The 22 seed cases

They are synthetic SettleNxt failures with no real data: 9 code regressions, 4 outdated tests,
3 test-data problems, 4 environment failures and 2 with too little evidence.

Some are deliberately hard:

- `tr-004`: slow-connection warnings sit next to an off-by-one bug.
- `tr-011`: an expired test card looks like a regression.
- `tr-016`: UAT timezone drift.

Four cases try to steer the verdict through injected text:

- `tr-019`: a log line.
- `tr-020`: the failure message.
- `tr-021`: a code comment.
- `tr-022`: a fake `</data>` tag.

## Adding real cases

Use failures whose cause a human has confirmed, for example from the Jira ticket or the fix commit.

- Mask the data as ATH would: card numbers, Aadhaar and mobile numbers. Never commit raw UAT data.
- Keep `why` to one sentence a reviewer can check.
- Tag cases so failures can be grouped: module, `misleading-log`, `injection`, and so on.
- Keep real and synthetic sets in separate folders, so their scores can be compared.
