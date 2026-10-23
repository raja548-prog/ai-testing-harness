# AiNxt Test Harness (ATH): build, configure and use

This guide is for two groups:

- **Platform engineers**, who install and run ATH and configure the machine it runs on.
- **Product teams**, who onboard a product.

For the reasoning behind the design, see [design.md](design.md).

## Contents

1. [What ATH does today](#1-what-ath-does-today)
2. [Build and install](#2-build-and-install)
3. [The three configuration files](#3-the-three-configuration-files)
4. [Try it in five minutes](#4-try-it-in-five-minutes)
5. [Onboard a product](#5-onboard-a-product)
6. [Configure the machine: the agent CLI](#6-configure-the-machine-the-agent-cli)
7. [Use ATH](#7-use-ath)
8. [Measure triage quality](#8-measure-triage-quality)
9. [Troubleshooting](#9-troubleshooting)
10. [Reference](#10-reference)

---

## 1. What ATH does today

ATH runs an 11-stage pipeline (T1–T11) around a merge request. It does the following:

1. Deploys the MR's commit.
2. Runs the selected test suites on that commit and on the merge-base.
3. Turns the results into one common format and masks payment data.
4. Filters out failures that are not the change's fault: pre-existing, flaky, known or quarantined.
5. Has three LLM voters classify the remaining failures, each backed by a quote from the evidence.
6. Reports the results, and stops at human approval gates before anything changes code or environments.

Not everything is built yet. Plan your onboarding around this table:

| Area | Status |
| --- | --- |
| Profile validation, `doctor` | Ready |
| Jenkins: trigger by webhook or API, follow the build, collect results | Ready (tested against a mock Jenkins; verify on yours) |
| Result formats: JUnit XML, Jenkins test report | Ready |
| Masking: card numbers, track 2, CVV, PIN block, Aadhaar, mobile | Ready |
| Merge-base baseline, flaky retries, known failures, quarantine | Ready |
| Triage by 3 voters through the AiNxt CLI, quote-checked, with diff and TSD context | Ready |
| Model policy (which models may see which data) | Ready |
| Approval gates with separation of duties, CLI and API | Ready (groups come from flags or headers until SSO is wired) |
| Triage evals (`ainxt-ath eval`) | Ready (synthetic cases; real cases still needed) |
| Log source `log-api` | Ready (needs your log service) |
| Log source `ssh` | Skeleton |
| GitLab CI adapter | Skeleton |
| Department env-booking adapter | Not built: only the demo `fake-lock` exists |
| Posting the report to the MR and Jira | Not built: the report goes to a file and the API |
| T1 impact, T3 test authoring, T10 fix, T11 validation | Gates work; the LLM work behind them is not built |
| Profile fields `build`, `healthCheck`, `quarantineList`, `testAuthoring`, product skill | Validated but not used by runs yet |

---

## 2. Build and install

You need Python 3.11 or newer.

### For development, from a checkout

```bash
git clone <ath-repo-url> ai-testing-harness && cd ai-testing-harness
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[server,dev]"
pytest                    # about 140 tests, about 15 s, no network needed
```

### For installing on another machine: a wheel

```bash
pip wheel --no-deps . -w dist/
pip install "dist/ainxt_ath-0.1.0-py3-none-any.whl[server]"     # drop [server] on a desktop
```

The wheel includes:

- the stage skills;
- the model policy;
- the onboarding templates.

They install under `<prefix>/share/ainxt-ath/`, and ATH finds them there automatically.

If you add a skill folder, add one line for it under `[tool.setuptools.data-files]` in `pyproject.toml`. `tests/test_packaging.py` fails if you forget.

### Run the API server

```bash
export ATH_PROFILES=/etc/ath/profiles        # folder of product profiles
export ATH_DB=/var/lib/ath/api.db            # state store (SQLite for now)
export ATH_CONFIG=/etc/ath/ath.yaml          # harness config (section 6)
uvicorn ath_server.api.app:app --host 0.0.0.0 --port 8000
```

**Warning:** don't expose the server beyond a trusted network yet.

- **Identity.** It trusts the `X-ATH-User` and `X-ATH-Groups` headers instead of SSO.
- **Durability.** Runs execute inside the server process, so a restart interrupts them. Resume them with `ainxt-ath resume`.

---

## 3. The three configuration files

Each file has a different owner, so nobody can loosen another group's rules.

| File | Owned by | What it says | Where it lives |
| --- | --- | --- | --- |
| **Test profile** (`test-profile.yaml`) | The product's department; approved by its product owner | How to deploy and test this product: Jenkins jobs, suites, environments, log sources, masking, per-stage models | Product package; on the server, under `$ATH_PROFILES` |
| **Harness config** (`ath.yaml`) | Whoever runs the machine: a developer on a desktop, the platform team on a server | How to reach local tools, mainly the agent CLI that runs the LLM stages | Found in this order: `--config`, then `$ATH_CONFIG`, then `.ath/config.yaml`, then `~/.config/ainxt/ath.yaml` |
| **Model policy** (`model-policy.yaml`) | The platform team, after security review | Which models may receive which data | Shipped as `config/model-policy.yaml`; can be overridden with `modelPolicy:` in the harness config |

None of these files ever holds a secret. Tokens are referenced as `env:NAME`, meaning "read the environment variable NAME", and are set in the environment of the process running ATH.

---

## 4. Try it in five minutes

The SettleNxt demo profile wires everything to fake adapters, so it runs on a laptop with no Jenkins or UAT environment.

```bash
ainxt-ath doctor examples/settlenxt/test-profile.local.yaml

ainxt-ath run --profile examples/settlenxt/test-profile.local.yaml \
  --commit mr001 --merge-base base000 --env uat1 \
  --changed src/main/java/clearing/Dispute.java
```

You get a report with **Verdict: BLOCKED** and four failures:

- one also fails on the merge-base, so it's pre-existing;
- one passes on retry, so it's flaky;
- one is an accepted known failure;
- one needs triage.

The card number in its message appears masked as `[PAN****1111]`.

With no agent CLI configured, the failure that needs triage is marked `unknown` and sent for human review. Section 6 shows how to switch on LLM triage.

---

## 5. Onboard a product

### 5.1 The product package

Copy [`templates/product-package/`](../templates/product-package/). It contains three files:

| File | Purpose |
| --- | --- |
| `test-profile.yaml` | The onboarding contract. Section 5.2 describes every field. |
| `product-skill.md` | What an LLM needs to know about the product: modules, transactions, how to read its logs, known flaky areas. Fill it in now; runs don't use it yet. |
| `AGENTS.md` | Rules for agents working on the product. Keep the defaults. |

The template mentions some adapters that aren't built yet: `test-server-api`, `simulator`, `allure-json`, `cucumber-json` and `dept-env-booking`. Section 10.3 lists what exists today.

### 5.2 The test profile, field by field

Every profile starts with these fields:

```yaml
apiVersion: ainxt.io/test-profile/v1   # required; the only supported version
product: settlenxt                      # required; product id
owners: [settlement-dept]               # required; groups whose members may approve gates
repos:
  code: git@gitlab.internal:settle/settlenxt.git     # required
  tests: git@gitlab.internal:settle/settlenxt-regression.git   # optional
```

`owners` matters beyond documentation. A gate can only be approved by a member of one of these groups, and never by the person who triggered the run (section 7.4).

#### `build` and `deploy`

```yaml
build:                          # validated; ATH does not trigger CI yet
  adapter: gitlab-pipeline
  project: settle/settlenxt

deploy:                         # T4 triggers this job with the MR commit and waits for it
  adapter: jenkins-job
  baseUrl: https://jenkins.settle.internal
  job: settlenxt-cd-uat         # folders as folder/sub/job
  trigger: webhook              # webhook (Generic Webhook Trigger) | api (buildWithParameters)
  webhookTokenRef: env:SNX_JENKINS_WEBHOOK_TOKEN    # webhook mode only
  user: ath-reader              # Jenkins user with read access
  tokenRef: env:SNX_JENKINS_API_TOKEN
  params: { COMMIT_ID: "${commit}", ENV: uat1 }
  healthCheck: { url: "https://uat1.internal/settlenxt/health", expect: 200, timeoutSec: 600 }   # validated, not called yet
```

`params` values can use these placeholders: `${commit}`, `${env}`, `${runId}`, `${suite}` and `${product}`.

ATH also sends two parameters of its own:

- `ATH_RUN_ID`, the run's id;
- `ATH_KEY`, which identifies one build attempt. ATH uses it to find a build it already started, so resuming a run never starts a second deploy.

A deploy that ends `UNSTABLE`, `FAILURE` or `ABORTED` counts as failed, and the environment is marked blocked.

#### `suites`

```yaml
suites:
  - id: smoke
    executor:
      adapter: jenkins-job
      baseUrl: https://jenkins.settle.internal
      job: settlenxt-smoke
      webhookTokenRef: env:SNX_JENKINS_WEBHOOK_TOKEN
      user: ath-reader
      tokenRef: env:SNX_JENKINS_API_TOKEN
      params: { COMMIT_ID: "${commit}", ENV: "${env}" }
      collect: test-report      # read what the job's `junit` step published
    results: { format: jenkins-test-report }
    maxDurationMin: 15          # hard timeout for one suite run (default 60)
    # pollSec: 30               # optional, on the executor: longest gap between status checks (1-600 s)

  - id: regression
    executor:
      adapter: jenkins-job
      # ...same connection fields...
      job: settle/settlenxt-regression
      collect: artifacts        # download archived JUnit XML instead
      artifacts: "target/failsafe-reports/*.xml"
    results: { format: junit-xml }
    maxDurationMin: 180
```

There are two ways to collect results:

| `collect` | `results.format` | Choose it when |
| --- | --- | --- |
| `test-report` (default) | `jenkins-test-report` | The job has a `junit` step. Nothing extra to archive. Test ids become `ClassName.method`, and correlation keys are read from `key=value` lines in test output. |
| `artifacts` | `junit-xml` | Your JUnit XML carries test ids, tags or correlation keys in `<properties>`, which Jenkins' test report drops. |

A suite job that ends `UNSTABLE` (tests failed) still counts as a finished run, and its results are collected.

ATH checks a running job's status 2 seconds after starting it, then waits 1.5 times longer between each check, up to `pollSec` (default 30 seconds). Set `pollSec` on a deploy or executor block to change that limit. For example, a 3-hour suite costs about 370 status checks at the default.

Each suite runs twice: on the MR commit, and on the merge-base. The merge-base results are cached per commit, so later MRs on the same base reuse them.

#### `selection`: which suites T2 runs

```yaml
selection:
  default: [smoke]                       # always run
  rules:
    - when: { pathsChanged: ["clearing/*", "*/clearing/*"] }
      run: [smoke, regression]           # added when any changed path matches
```

Rules add suites; they never remove them.

Patterns are shell-style, and `*` also matches `/`. To match a folder at any depth, list two patterns:

| Pattern | Matches | Doesn't match |
| --- | --- | --- |
| `clearing/*` | `clearing/Fee.java`, at the top level | `src/main/java/clearing/Fee.java` |
| `*/clearing/*` | `src/main/java/clearing/Fee.java`, at any depth below the top | `clearing/Fee.java` |

Avoid two other forms:

- `*clearing/*` also matches unrelated folders such as `preclearing/`.
- `**/clearing/**` behaves like `*/clearing/*`, so it misses a top-level `clearing/` folder.

Changed paths come from `--changed`.

#### `logs`: where triage gets log lines

```yaml
logs:
  sources:                               # tried in order; the first that answers is used
    - { adapter: log-api, endpoint: "https://uat-logs.internal/logs", keys: [rrn, stan, batchId] }
    - { adapter: ssh, jumpHost: uat-jump.internal, user: ath-logread, paths: ["/opt/settlenxt/logs/*.log"] }
  masking: [pan, cvv, track2, aadhaar, mobile]    # the default; PIN blocks are always masked
```

**The log API.** `log-api` calls `GET {endpoint}?from=&to=&maxLines=&key=rrn:629…` and expects `{"lines": [...], "truncated": false}` back. Logs are fetched by the failing test's correlation keys.

**Correlation keys.** `keys` declares the product's correlation keys, and ATH uses them in two ways:

- **Filtering.** Only these keys are sent to the log API.
- **Masking.** A number written directly as one of these keys' values (`rrn=…`, `"rrn": "…"`) is not masked as Aadhaar. RRNs are 12 digits too, and the logs are fetched by RRN.

Card numbers are always masked, even when they appear as a key's value.

**SSH.** The `ssh` source is a skeleton, and is meant for servers only after a security review.

#### `policy`

```yaml
policy:
  environments: [uat1]          # required; ATH refuses any other --env
  flakyRetries: 2               # 0-5; reruns before a failure counts as real
  maxFixIterations: 3           # 0-10; for the T10/T11 fix loop
  envLock: { adapter: dept-env-booking, env: uat1, waitMaxMin: 120 }   # optional; env must be in environments
  knownFailures:                # accepted failures: reported, never blocking, until they expire
    - { caseId: SNX-004, ticket: SNX-1790, expires: "2026-12-31" }
  quarantineList: tests/quarantine.txt   # validated, not read yet; pass --quarantine to `run`
```

`envLock` holds the department's test environment for the run. Leave it out until your department's booking adapter exists, because only the demo `fake-lock` is built.

A `knownFailures` entry stops applying after its `expires` date.

#### `models`: which model each LLM stage uses

```yaml
models:
  default: onprem-coder         # required when the block is present
  triage: onprem-reasoner       # per stage: impact, author, triage, remediate, validate
  remediate: onprem-coder
```

Use model names exactly as `ainxt models` lists them.

Every model must be allowed by the model policy for the data that stage sees (section 6.3). `doctor` checks each stage's model.

#### Checks run on every profile

ATH also rejects a profile that has:

- duplicate suite ids;
- `selection` naming a suite that doesn't exist;
- an `envLock.env` that isn't in `environments`;
- an unsupported `apiVersion`.

### 5.3 Prepare your Jenkins

Do this for the deploy job and for each suite job:

1. **Trigger.**
   - **Webhook mode:** install the Generic Webhook Trigger plugin and give the job a token, the way Git pushes trigger it. Map the JSON body to variables with JSONPath, e.g. `COMMIT_ID` ← `$.COMMIT_ID`, `ATH_RUN_ID` ← `$.ATH_RUN_ID`, `ATH_KEY` ← `$.ATH_KEY`.
   - **API mode:** use `trigger: api` instead, which calls `buildWithParameters`.
2. **Parameters.** Declare `ATH_RUN_ID` and `ATH_KEY` as string parameters. Without them, a resumed run can't find its earlier build.
3. **Results.** Suite jobs need a `junit` step for `collect: test-report`, or archived XML for `collect: artifacts`.
4. **Read access.** Create a Jenkins user with read access to these jobs, and give it an API token. A webhook token can start a job but can't read it, so ATH needs this user in both modes.

### 5.4 Secrets

Export the variables your profile names wherever ATH runs, for example:

```bash
export SNX_JENKINS_WEBHOOK_TOKEN=...      # the job's webhook token
export SNX_JENKINS_API_TOKEN=...          # the read user's API token
```

`env:NAME` is the only supported form today. `vault:` references are reserved for the platform's credential broker.

### 5.5 Validate, then start in report-only mode

```bash
ainxt-ath validate test-profile.yaml      # schema and cross-checks
ainxt-ath doctor test-profile.yaml        # plus: adapters exist, settings and secrets present, model policy
```

`doctor` prints one line per adapter. `[MISSING]` or `[CONFIG]` lines say exactly what to fix. For example, `environment variable SNX_JENKINS_API_TOKEN is not set`.

Run your first MRs in `report-only` mode, the default. Compare ATH's reports with what your testers concluded before moving the product to `assist` mode.

---

## 6. Configure the machine: the agent CLI

The LLM stages run as short-lived, headless sessions of the AiNxt CLI. Without this configuration, triage is off, and every unexplained failure goes to a human as `unknown`.

### 6.1 Set up

```bash
# install the AiNxt CLI (see the ainxt-cli README), then log in once:
ainxt login
ainxt models                                   # note the model names

mkdir -p ~/.config/ainxt
cp examples/ath-config.yaml ~/.config/ainxt/ath.yaml
# edit: model names, and the CLI path if `ainxt` is not on PATH
ainxt-ath doctor <profile>                     # expect "[ok] agent runner" and "[ok] model policy"
```

On a desktop, your existing `ainxt login` is reused. On a server, set `AINXT_API_KEY` or `AINXT_TOKEN` for the service account, and list it in `passEnv`.

### 6.2 Harness config reference

```yaml
agentRunner:
  command:                          # one headless AiNxt CLI session per stage call
    - ainxt                         # or a path; ~ and $VARS are expanded
    - --prompt-file
    - "{prompt_file}"
    - -m
    - "{model}"
    - --cwd
    - "{workdir}"
    - --output-format
    - json
    - --sandbox
    - strict
    - --max-turns
    - "2"
    - --disallowed-tools
    - run_terminal_cmd,Agent
    - --deny
    - Edit(**)
  promptVia: file                   # arg | stdin | file (ainxt does not read stdin)
  timeoutSec: 300                   # per attempt
  idleTimeoutSec: 0                 # 0 = off; only useful with --output-format streaming-json
  sessionArgs: [--session-id, "{session_id}"]
  resumeArgs: [-r, "{session_id}"]
  timeoutResumes: 1                 # resume a timed-out session this many times
  voters: 3                         # triage voters; 2 must agree
  models: []                        # optional, one per voter; default: the profile's triage model
  modelFamilies: {}                 # e.g. {"onprem-llama*": meta, "onprem-qwen*": qwen}
  minFamilies: 1                    # >1: voters must span this many model families
  passEnv: [AINXT_API_KEY, AINXT_TOKEN, AINXT_API_BASE_URL, AINXT_GATEWAY_URL, AINXT_HOME]
  setEnv: { AINXT_MAX_RETRIES: "2", AINXT_DISABLE_AUTOUPDATER: "1" }
# modelPolicy: /etc/ath/model-policy.yaml
# skillsDir: ../skills
```

These placeholders are filled in for each session:

- `{prompt}` or `{prompt_file}`: the prompt itself, or the path of a file holding it.
- `{model}`: the model for this voter.
- `{workdir}`: an empty temporary folder the session runs in.
- `{session_id}`: a session id that ATH chooses.

Safety rules ATH enforces:

- **No auto-approve.** ATH refuses a command containing `--yolo`, `--always-approve` or `bypassPermissions`. Without them, headless `ainxt` cancels any tool call that would need approval.
- **A minimal environment.** The session sees only `PATH`, `HOME`, locale settings, the `passEnv` variables and the `setEnv` values. Jenkins tokens and other secrets are withheld.
- **Masked data only.** The session only ever receives masked data.
- **No side effects.** It never touches Jenkins, Git or UAT; those actions go through ATH.

**Independent voters.** Three voters on one model tend to make the same mistakes. Once two model families are available, list them under `models`, map them under `modelFamilies`, and set `minFamilies: 2`.

### 6.3 Model policy

[`config/model-policy.yaml`](../config/model-policy.yaml) assigns each stage a data class, and each data class lists the model-name patterns allowed to receive it.

The default is strict. Test results, logs, diffs and TSDs are `restricted` or `internal`, and only `local/*` and `onprem-*` models may receive them.

Three things follow:

- **ATH refuses a run** whose triage voters break the policy.
- **The policy is owned by the platform team.** A product can't opt its own data into cloud models; approved exceptions go under `productOverrides` in this file.
- **There is no permissive fallback.** A missing or invalid policy stops the LLM stages.

Adjust the patterns to your real on-prem model names.

---

## 7. Use ATH

### 7.1 Start a run

```bash
ainxt-ath run --profile test-profile.yaml \
  --commit 9f1c2ab --merge-base 4e77d10 --env uat1 \
  --changed src/main/java/clearing/Dispute.java \
  --mr '!512' --jira SNX-1834 --user dev-asha \
  --diff mr.diff --tsd tsd.md
```

| Flag | Meaning |
| --- | --- |
| `--commit`, `--merge-base` | The MR's commit and its merge-base (`git merge-base origin/main HEAD`). Both are tested. |
| `--env` | Must be listed in the profile's `policy.environments`. |
| `--changed` | Changed paths, used for suite selection (`git diff --name-only <merge-base>..<commit>`). |
| `--mode` | `report-only` (default; no changes made), `assist` or `autofix`. |
| `--user` | Who triggered the run. Gates use it for separation of duties. Defaults to `local-user`. |
| `--diff` | The MR's unified diff (`git diff <merge-base>..<commit>`), given to triage. |
| `--tsd` | A TSD markdown file, given to triage. |
| `--quarantine` | A file of quarantined test ids, one per line. |
| `--db`, `--report-dir` | State store (default `.ath/state.db`) and report folder (default `.ath/reports`). |
| `--config` | Harness config, if not found in the default places. |

For each failing test, triage receives:

- the changed files and TSD sections most related to that test, within a size limit;
- the masked log lines for its correlation keys.

### 7.2 Read the report

The report has three parts:

- **A summary table** with each suite's totals.
- **A verdict.** It is **BLOCKED** when any failure is `code-regression` or `unknown`, and **GREEN** otherwise.
- **Tables of blocking and non-blocking failures**, each with a category, a reason and the masked message.

These are the categories:

| Category | Decided by | Blocks? |
| --- | --- | --- |
| `pre-existing` | Also fails on the merge-base | No |
| `flaky` | Passed on a retry | No |
| `known-failure` | Listed in `knownFailures` and not expired | No |
| `quarantined` | Listed in the `--quarantine` file | No |
| `code-regression` | Triage: the change broke behaviour the test correctly expects | **Yes** |
| `test-outdated` | Triage: behaviour changed as the TSD intends | No |
| `test-data` | Triage: missing or bad fixtures, data or keys | No |
| `environment` | Triage: a dependency is down, config drifted, timeouts | No |
| `unknown` | Triage found too little evidence, or no triage is configured | **Yes**, and needs human review |

Triage verdicts show how many voters agreed and the verified quote they relied on, e.g. `3 of 3 agents agreed; evidence: "… ERROR settlement failed for rrn=…"`.

A vote whose quote isn't found in the inputs ATH sent is discarded. The reason is recorded in the run's results.

### 7.3 Modes

| Mode | Stages | What can change |
| --- | --- | --- |
| `report-only` | T1–T9; T3 skipped | Nothing |
| `assist` | T1–T11 | Test changes (T3) and fixes (T10), each behind a gate |
| `autofix` | T1–T11 | Same gates today |

T10 runs only when triage found `code-regression` failures; otherwise it is skipped.

### 7.4 Approve or reject a gate

A run that reaches a gate stops with status `waiting-gate`:

```bash
ainxt-ath approve <runId> --gate T10 --user lead-ravi --groups settlement-dept \
  --comment "fix reviewed" --profile test-profile.yaml
ainxt-ath reject  <runId> --gate T10 --user lead-ravi --groups settlement-dept \
  --comment "wrong fix" --profile test-profile.yaml
```

The rules:

- **No self-approval.** The person who triggered the run can't approve it, because ATH acted on their behalf.
- **Owners approve.** The approver must be a named person in one of the profile's `owners` groups.
- **No service approvals.** Service identities (`ath-…`, `svc-…`) and the default `local-user` can't decide.
- **Rejecting is open to more people.** The person who triggered the run, or any owner, may reject. A rejected run stops for good; its artifacts are kept and the environment hold is released.
- **Same profile.** A decision is refused if the profile changed since the run started.

`--groups` stands in for the user's SSO groups until SSO is wired.

### 7.5 Resume, report and events

```bash
ainxt-ath resume <runId> --profile test-profile.yaml    # continue from the last checkpoint
ainxt-ath report <runId>                                # print the report again
ainxt-ath events <runId>                                # audit trail
```

Resuming never repeats a deploy or a test run that already finished.

The audit trail lists:

- each stage's start and end;
- every external call;
- each gate decision;
- one `agent.call` per triage vote, with the model, session, attempts, duration and SHA-256 hashes of the prompt and response, never their text.

### 7.6 The API

All requests need `X-ATH-User`. Gate decisions also take `X-ATH-Groups` (comma-separated).

| Request | What it does |
| --- | --- |
| `POST /runs` | Start a run. Body: `product`, `commit`, `merge_base`, `env`, and optionally `mode`, `mr`, `jira`, `changed_paths`, `profile_file`, `diff`, `tsd`. Returns `{run_id}`. |
| `GET /runs/{id}` | The run manifest: status, stages, gate decisions, input hashes. |
| `GET /runs/{id}/report` | The Markdown report. |
| `GET /runs/{id}/events` | The audit trail. |
| `POST /runs/{id}/gates/{T3\|T10\|T11}/{approve\|reject}` | Decide a gate. Body: `{"comment": "..."}`. Returns 403 with the reason when separation of duties forbids it. |

`profile_file` is resolved inside `$ATH_PROFILES`; paths outside it are refused.

---

## 8. Measure triage quality

Before you trust triage, and before approving any change to the triage skill or model, run it against labelled failures:

```bash
ainxt-ath eval tests/evals/triage --profile examples/settlenxt/test-profile.local.yaml \
  --min-accuracy 0.8
```

The report covers:

- accuracy, and precision and recall per category;
- **missed regressions**: a real bug given a non-blocking verdict, the worst mistake;
- **false regressions**: the change blamed wrongly;
- whether each **prompt-injection** case was resisted.

The command exits with code 3 when a gate fails. That happens when accuracy is below `--min-accuracy`, when missed regressions exceed `--max-missed-regressions` (default 0), or when any injection case succeeds.

[`tests/evals/triage/`](../tests/evals/triage/) holds 22 synthetic SettleNxt cases. Its [README](../tests/evals/triage/README.md) explains the format.

Add real cases from failures whose cause has been confirmed. Mask them, and keep them outside this repository.

A run makes 3 CLI sessions per case, one after another, so allow time for it.

---

## 9. Troubleshooting

| Message | Cause and fix |
| --- | --- |
| `[MISSING] env lock: envlock adapter 'dept-env-booking'` | No booking adapter is built yet. Remove `envLock` from the profile for now. |
| `environment variable X is not set` | Export the token the profile names (section 5.4). |
| `jenkins webhook: no job accepted the token` | Wrong `webhookTokenRef`, or the job has no Generic Webhook Trigger with that token. |
| `HTTP 401/403; check user/tokenRef` | The read user's API token is wrong, or the user can't read the job. |
| `published no test report; add a junit step` | Add a `junit` step to the job, or use `collect: artifacts`. |
| `environment 'X' is not allowed by policy.environments` | Use an environment listed in the profile. |
| `refused: model policy: stage 'triage' sees 'restricted' data …` | The voter's model doesn't match an allowed pattern. Use an on-prem model, or have the platform team adjust the policy. |
| `triage voters come from 1 model family …` | `minFamilies` is above 1, but the voters all come from one family. Add models from another family. |
| `agent CLI 'ainxt' not found on PATH` | Install the CLI, or set the full path as the first `command` entry. |
| Every triage vote is `unknown` | Check the votes' notes in the run's results: CLI errors, timeouts, or quotes not found in the inputs. Then run `ainxt-ath eval` to see the pattern. |
| `… triggered this run …, so they cannot approve its gates` | Separation of duties. Someone else in the owners groups must approve. |
| `model policy … not found` | An installed copy is missing its data files. Reinstall from the wheel, or set `modelPolicy:`. |

---

## 10. Reference

### 10.1 Commands

| Command | Purpose | Exit code |
| --- | --- | --- |
| `validate <profile>` | Schema and cross-checks | 0 ok, 1 invalid |
| `doctor <profile> [--config]` | Plus adapters, settings, secrets, agent CLI, model policy | 0 ok, 2 problems |
| `run --profile … --commit … --merge-base … --env …` | Start a run | 0 succeeded or waiting at a gate, 1 otherwise |
| `approve` / `reject <runId> --gate --user [--groups] [--comment]` | Decide a gate | 0 done, 1 refused |
| `resume <runId> --profile …` | Continue a run | 0 or 1 |
| `report <runId>` / `events <runId>` | Print the report or the audit trail | 0 or 1 |
| `eval <dataset> --profile … [--min-accuracy] [--max-missed-regressions]` | Score triage | 0 pass, 1 refused, 3 a gate failed |

### 10.2 Environment variables

| Variable | Used by |
| --- | --- |
| `ATH_CONFIG` | Path of the harness config |
| `ATH_PROFILES`, `ATH_DB` | API server: profiles folder and state store |
| `<NAME>` in `env:<NAME>` | Secrets named by the profile, e.g. Jenkins tokens |
| `AINXT_*` | The AiNxt CLI, when listed in `passEnv` |

### 10.3 Adapters available today

| Kind | Names |
| --- | --- |
| Pipeline (build, deploy) | `jenkins-job`; `gitlab-pipeline` (skeleton); `fake-pipeline` (demo) |
| Suite executor | `jenkins-job`; `fake-executor` (demo) |
| Result parser | `jenkins-test-report`, `junit-xml` |
| Log source | `log-api`; `ssh` (skeleton); `fake-logs` (demo) |
| Env lock | `fake-lock` (demo only) |

To add an adapter, see "Adding an adapter" in the [README](../README.md).
