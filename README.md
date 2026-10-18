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
pytest                                   # 99 tests, no network needed

ainxt-ath doctor examples/settlenxt/test-profile.local.yaml
ainxt-ath run --profile examples/settlenxt/test-profile.local.yaml \
  --commit mr001 --merge-base base000 --env uat1 \
  --changed src/main/java/clearing/Dispute.java
```

To install on a machine without the source checkout, build a wheel. Skills, the model policy and the
onboarding templates are included and install to `<prefix>/share/ainxt-ath/`:

```bash
pip wheel --no-deps . -w dist/ && pip install "dist/ainxt_ath-0.1.0-py3-none-any.whl[server]"
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
| ATRF schema, JUnit XML and Jenkins test-report parsers with correlation keys | Done |
| Masking (PAN with Luhn, track 2, CVV, PIN block, Aadhaar, mobile) | Done |
| Orchestrator: stage plan per mode, checkpoints, resume, gates, env hold, structured events | Done (in-process) |
| T2 rule-based selection, T4 deploy, T5 run + merge-base baseline cache, T6 normalize | Done |
| T7 pre-checks (known failure, quarantine, pre-existing, flaky retry), voting + 2-of-3 quorum | Done |
| T7 triage agents through a headless agent CLI (AiNxt CLI) | Done; tested with a fake CLI |
| Evidence-checked votes, model-family diversity, model policy (data classes) | Done |
| T9 Markdown report | Done; posting to MR/Jira is a TODO |
| Log sources: log-api (HTTP), ssh (server-only, skeleton), fake | Partly |
| Jenkins adapter (webhook or API trigger, test-report or artifact results) | Done; tested against a mock Jenkins |
| GitLab adapter | Skeleton |
| T1 impact, T3 authoring, T10 remediation, T11 validation (LLM) | Stubs + skills drafted |

## Next steps

1. **Wire real adapters** for the first pilot: GitLab trigger and poll, the department env-booking
   hold (`dept-env-booking`), and the UAT `log-api`.
2. **Triage quality**: confirm the AiNxt CLI's headless flags in `examples/ath-config.yaml`, attach the
   MR diff, TSD excerpt and product skill to the triage inputs, then build `tests/evals/` from real past
   failures and measure precision before enabling it.
3. **Service hardening**: Postgres store, SSO JWT validation and run-scoped tokens, credential
   broker for Git/Jira, and durable workflows (e.g. Temporal) wrapping the stage functions.
4. **More parsers**: allure-json, testng, cucumber-json, and the simulator CSV format.
5. **LLM stages**: T1 → T3 (behind its gate) → T10/T11 in `assist` mode.

## Using the AiNxt CLI for LLM stages

ATH runs each LLM stage call as one short-lived, headless session of an agent CLI: the AiNxt CLI on
a desktop or server, or any grok-cli style tool. Configure it once per machine in a harness config
(see [`examples/ath-config.yaml`](examples/ath-config.yaml)), found in this order: `--config`,
`$ATH_CONFIG`, `.ath/config.yaml`, `~/.config/ainxt/ath.yaml`.

```yaml
agentRunner:
  command: [ainxt, --prompt-file, "{prompt_file}", -m, "{model}", --cwd, "{workdir}",
            --output-format, json, --sandbox, strict, --max-turns, "2",
            --disallowed-tools, "run_terminal_cmd,Agent", --deny, "Edit(**)"]
  promptVia: file
  voters: 3
  passEnv: [AINXT_API_KEY, AINXT_GATEWAY_URL]
  setEnv: { AINXT_MAX_RETRIES: "2" }
```

ATH refuses a command containing `--yolo` or `bypassPermissions`: without them, headless `ainxt`
cancels any tool call that would need approval.

```bash
ainxt-ath doctor examples/settlenxt/test-profile.yaml --config ~/.config/ainxt/ath.yaml
ainxt-ath run --profile ... --commit ... --merge-base ... --env uat1     # T7 now votes through the CLI
```

Each session gets the stage skill plus masked `<data>` blocks, runs in an empty temporary directory
with only `PATH`, `HOME`, locale, the `passEnv` variables and `setEnv` values, and must answer with
one JSON object matching the skill's `output_schema` (a CLI JSON envelope such as
`ainxt --output-format json` is unwrapped first). A crash, timeout or unusable answer counts as an `unknown` vote.

Each vote must also quote, character for character, a line from the data ATH sent (a log line, diff
line, TSD clause or the failure message). ATH checks the quote; a vote with no quote or an invented one
is discarded as `unknown`, with a note in the ATRF. Verified quotes appear as evidence in the report.
Set `minFamilies` with `modelFamilies` to require voters from different model families. Today
this drives T7 triage; T1, T3, T10 and T11 will use the same runner.

## Model policy

[`config/model-policy.yaml`](config/model-policy.yaml) says which models may see which data. Each LLM
stage sees a data class (masked test results and logs are still `restricted`), and each class lists the
model-name globs allowed to receive it. By default only `local/*` and `onprem-*` models qualify.

ATH refuses a run whose triage voters are not allowed, and `doctor` also checks every per-stage model in
the product profile. The file is platform-owned: products cannot opt their own data into cloud models;
approved exceptions go under `productOverrides`. Point a harness config at another file with
`modelPolicy:`; a missing or invalid policy stops LLM stages (there is no permissive default).

## Connecting your Jenkins

Each product points ATH at its own Jenkins with `adapter: jenkins-job` (see
[`examples/settlenxt/test-profile.yaml`](examples/settlenxt/test-profile.yaml) and the docstring in
[`ath_core/adapters/pipeline/jenkins.py`](ath_core/adapters/pipeline/jenkins.py)).

On the Jenkins side, for the CD job and each suite job:

1. **Trigger.** Install the Generic Webhook Trigger plugin and give the job a token, the same way Git
   pushes trigger it. Map the variables ATH sends in the JSON body with JSONPath, e.g.
   `COMMIT_ID` ← `$.COMMIT_ID`, `ATH_RUN_ID` ← `$.ATH_RUN_ID`, `ATH_KEY` ← `$.ATH_KEY`.
   Alternatively set `trigger: api` to use `buildWithParameters` instead.
2. **Parameters.** Declare `ATH_RUN_ID` and `ATH_KEY` as string parameters, so a resumed ATH run can
   find the build it already started instead of starting a second one.
3. **Results.** Suite jobs publish JUnit results with a `junit` step (read through the test-report API,
   `collect: test-report`, `results.format: jenkins-test-report`). If your tests carry ids, tags or
   correlation keys in JUnit `<properties>`, archive the XML instead and use `collect: artifacts`
   with `results.format: junit-xml`.
4. **Read access.** Create a Jenkins user with read access to these jobs and an API token. A webhook
   token can start a job but not read it, so ATH needs this in both trigger modes.

Then export the tokens named in the profile and check:

```bash
export SNX_JENKINS_WEBHOOK_TOKEN=...   # the job's webhook token
export SNX_JENKINS_API_TOKEN=...       # API token of the read user
ainxt-ath doctor examples/settlenxt/test-profile.yaml
```

Profiles hold `env:NAME` references only, never the token; `vault:` references are reserved for the
platform credential broker.

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
