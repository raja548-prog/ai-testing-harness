# AiNxt Test Harness (ATH)

A product-agnostic, agentic testing harness for the AiNxt platform. One shared core runs
an 11-stage pipeline (T1–T11); each product plugs in through a declarative **Test Profile**,
a **product skill** and reusable **adapters**. Design: [`docs/design.md`](docs/design.md).

```
ainxt-ath/
├── ath_core/        library: schemas (Profile, ATRF, Run), adapters, masking, triage, report, stages
├── ath_server/      orchestrator (checkpoints, gates, resume), state store, REST API
├── ath_cli/         `ainxt-ath` CLI (ships as the `ainxt ath …` plugin)
├── skills/          LLM stage skills + mandatory injection rules
├── templates/       product package generated at onboarding
├── examples/        SettleNxt profiles (production-shaped and fake-wired)
├── tests/           unit + end-to-end tests with fake adapters and recorded fixtures
└── docs/            design document and diagrams
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[server,dev]"
pytest                                   # 29 tests, no network needed

ainxt-ath doctor examples/settlenxt/test-profile.local.yaml
ainxt-ath run --profile examples/settlenxt/test-profile.local.yaml \
  --commit mr001 --merge-base base000 --env uat1 \
  --changed src/main/java/clearing/Dispute.java
```

The demo runs the full report-only pipeline against fake Jenkins, fake suites and a fake
log source. The fixtures exercise every pre-check: one failure is pre-existing on the
merge-base, one is flaky (passes on retry), one is an accepted known failure, and one
needs triage. The failure message contains a test card number, which is masked in ATRF
and in the report.

API: `ATH_PROFILES=examples/settlenxt uvicorn ath_server.api.app:app --reload`, then
`POST /runs` with header `X-ATH-User` (stands in for SSO).

## What works today (milestone 1 to 3 foundations)

| Area | Status |
| --- | --- |
| Test Profile schema + cross-checks, `validate`, `doctor` | Done |
| ATRF schema, JUnit XML parser with correlation keys | Done |
| Masking (PAN with Luhn, track 2, CVV, PIN block, Aadhaar, mobile) | Done |
| Orchestrator: stage plan per mode, checkpoints, resume, gates, env hold, structured events | Done (in-process) |
| T2 rule-based selection, T4 deploy, T5 run + merge-base baseline cache, T6 normalize | Done |
| T7 pre-checks (known failure, quarantine, pre-existing, flaky retry), voting + 2-of-3 quorum | Done; agents pluggable |
| T9 Markdown report | Done; posting to MR/Jira is a TODO |
| Log sources: log-api (HTTP), ssh (server-only, skeleton), fake | Partly |
| Jenkins / GitLab adapters | Skeletons |
| T1 impact, T3 authoring, T10 remediation, T11 validation (LLM) | Stubs + skills drafted |

## Next steps

1. **Wire real adapters** for the first pilot: Jenkins/GitLab trigger and poll, artifact download,
   the department env-booking hold (`dept-env-booking`), and the UAT `log-api`.
2. **Triage agents**: implement `TriageAgent.classify` against the AiNxt model gateway using
   `skills/t07-triage`, then build `tests/evals/` from real past failures and measure precision
   before enabling it.
3. **Service hardening**: Postgres store, SSO JWT validation and run-scoped tokens, credential
   broker for Git/Jira, and durable workflows (e.g. Temporal) wrapping the stage functions.
4. **More parsers**: allure-json, testng, cucumber-json, and the simulator CSV format.
5. **LLM stages**: T1 → T3 (behind its gate) → T10/T11 in `assist` mode.

## Adding an adapter

Subclass the contract in `ath_core/adapters/base.py`, register it, and import the module in
`ath_core/adapters/loader.py`:

```python
@registry.register("executor", "test-server-api")
class TestServerExecutor(ExecutorAdapter):
    def start(self, suite, ctx, commit): ...
    def poll(self, handle): ...
    def collect(self, handle): ...
```

Rules: idempotent on run id, timeouts from the profile, credentials by vault reference only,
and mask anything that can contain test data before returning it.
