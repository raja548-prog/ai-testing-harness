# Plan: TSD-driven test creation/update in ATH (standalone + T3 in the run, toggleable)

## Context
Testers need to create or update tests from a TSD against a test suite checked out locally,
without running the whole ATH pipeline (no MR, no deploy, no env). The same work should also run
as stage T3 of a normal `ainxt-ath run`, where it can be switched on or off. Today T3 is a stub
that only raises its gate (`ath_server/orchestrator/runner.py:268`), and `git_mr.apply` is
`NotImplementedError`. Agreed choices:
- Standalone writes straight into the local checkout. `--dry-run` stores a proposal for review
  only. `ainxt-ath author apply <id>` applies a stored proposal later without a second model call.
- In a run, T3 is on or off by the profile's `testAuthoring.enabled`, and a per-run
  `--author` / `--no-author` flag overrides that. T3 is still skipped in `report-only` and always
  waits at the T3 gate.
- Only the `local-fs` mode is built in this change. `git-mr`, `test-server-api` and
  `simulator-scenarios` profiles keep validating, and T3 skips them with the reason
  `mode <m> is not implemented yet`.
- Inputs besides the TSD and test suite: test conventions, the test-data catalogue, the product
  skill, source code signatures, simulator templates, and in a run the MR diff, T1 impact and
  outdated tests found by triage. See "Inputs to the authoring model".
- The work ships in four phases, each its own MR (see "Phases").

## Design

### Inputs to the authoring model
Each input becomes one `<data name="...">` block in the prompt. Everything is masked with
`Masker` before it is sent. Selection is deterministic: the same inputs always produce the same
prompt (files and paths are sorted before ranking). Selection reuses the word-overlap ranking in
`ath_core/context.py`, with `_tokens`/`_pick` made public; don't copy it. Each block has its own
size cap. If a block is cut, it ends with "(N more … not shown)", as the triage excerpts do.

**Always sent** (both entry points)

| Block | Source | How it is picked |
| --- | --- | --- |
| `tsd` | `--tsd` or `--rh-package` (approved version, hash-checked; reuse `read_rh_package`, `tsd_sections`) | All sections, or only those named with `--section` (title or `ⓘ` id). `ⓘ` footers are kept for tracing. |
| `tests` | Test files in the local checkout matching `testAuthoring.sources` | Ranked by word overlap with the selected TSD sections. **Files are sent whole or not at all**: the cap limits the number of files, never cuts one midway. Each file is labelled with its kind and format, and with `editable: true`. Only files sent whole can be updated. |
| `tree` | The same checkout | Paths only, no content, so new files follow the existing layout and names. Capped by path count. |
| `conventions` | `testAuthoring.conventions` file | Whole file |
| `testdata` | `testAuthoring.testData` catalogue | Names, kinds, descriptions and field names only; never values |

**Sent when configured** (both entry points)

| Block | Source | How it is picked |
| --- | --- | --- |
| `product` | **Product skill**: new top-level profile field `productSkill` (path relative to the profile), built from `templates/product-package/product-skill.md` | Whole file, capped. Its front-matter `version` is recorded in the proposal. |
| `code` | **Source code signatures** from a code checkout (`--code PATH`) and the globs in new `testAuthoring.codeContext` (default: the paths in the product skill's module table) | Signatures only: classes, interfaces, enums, records, public methods, and the fields of request/response models. Extracted deterministically in new `ath_core/authoring/signatures.py` (see "Source signatures"). Ranked by overlap with the TSD. Lets the model call real methods, so new test code compiles. |
| `templates` | **Simulator templates**: new `testAuthoring.templates` glob, for products that use simulator scenarios | Templates named by the TSD or by the scenario files being changed. The model fills in template parameters and never invents structure (existing skill rule). |

**Inside a run, or given to the standalone command**

| Block | Source | How it is picked |
| --- | --- | --- |
| `diff` | Run: the stored `context/diff`. Standalone: optional `--diff FILE`. | The changed files most related to the TSD sections (`split_diff` + ranking). Shows which behaviour changed, so the model updates the existing tests that check it. |
| `impact` | T1 output, once T1 is built (skipped today) | Affected modules and risk areas, to narrow which tests are chosen |
| `outdated` | Cases that triage marked `test-outdated`, with their failure messages. Standalone: `--from-run RUN_ID` reads them from that run's ATRF artifacts. | Exact tests to update and why. Lets a tester run ATH, then fix the stale tests it found with one command. |

**Never sent:** logs, raw results, unmasked test data, real values from the test-data catalogue,
Jira content (see Later).

**Data class.** Because `tests` and `outdated` carry masked test data, CSV transaction rows and
failure messages, the author stage sends `restricted` data, as `config/model-policy.yaml` defines
it. Change `author: internal` to `author: restricted` there (the file header says policy changes
need security review, so this goes through that review). Today both classes allow the same
models, so nothing breaks. It stops a later `productOverrides: {author: public}` from sending
masked transaction data to a cloud model.

**Wiring the product skill once for every stage.** `productSkill` is loaded by a shared helper
(`load_product_skill(profile)` in `ath_core/context.py`). It is passed to triage too: today
triage sends `(not attached)` (`ath_core/agents/cli.py:331`), and the orchestrator's evidence
gets a `product` entry. T1, T10 and T11 use the same helper when they are built.

### Masking and how edits map back to the original file
The model sees masked text. The files on disk are not masked. Two things follow.

- **Span-aware masking.** Add `Masker.mask_spans(text) -> (masked, spans)` next to `mask()`.
  `spans` lists each masked region as `(orig_start, orig_end, masked_start, masked_end, category)`.
  `mask()` stays as it is and calls the same code.
- **Matching a `find`.** A `find` is accepted only when all of these hold:
  - it contains no mask token (`[PAN****`, `[AADHAAR]`, `[MOBILE]`, `[TRACK2]`, `[PINBLOCK]`, `***`);
  - it occurs exactly once in the masked text the model saw;
  - it occurs exactly once in the original file;
  - the original occurrence doesn't overlap any masked span, and it is the same place as the
    masked occurrence (mapped through `spans`).

  Otherwise the edit is dropped with "edit touches masked data; edit by hand".
- **Several edits to one file.** All `find`s are matched against the original file, not against
  the result of earlier edits. If two matches overlap, the whole change is dropped. The edits
  are then applied together, from the end of the file backwards.
- **Line endings and encoding.** Files are read as UTF-8. A BOM is removed and CRLF line endings
  are changed to LF before masking and matching, and both are put back on write. Files that
  aren't UTF-8 are not sent and can't be updated.

**False positives.** The masking regexes also hit ordinary test literals. `_MOBILE_RE` has no
checksum, so any 10-digit number starting 6–9 becomes `[MOBILE]`. About 1 in 10 random
13–19 digit numbers (for example millisecond timestamps) pass Luhn and become `[PAN…]`. So:
- The skill tells the model that mask tokens in the input are redacted values. They are not
  literals to copy, and it must not write them.
- The output check reports hits per category and shows the masked token, for example "model
  wrote a value that looks like a mobile number ([MOBILE]); use a testData reference or another
  value". That way the reviewer can tell a false positive from a real leak.
- The catalogue supports every masked category (`card`, `account`, `mobile`, `aadhaar`), so the
  model always has an allowed way to write such a value.

### One authoring engine, used from two places
New `ath_core/authoring/engine.py`. Both the `author` command and T3 call it.

1. **Gather context**: build the blocks listed in "Inputs to the authoring model", masked and size-capped.
2. **One model call** to skill `t03-author`, run through the existing `CliAgentRunner`
   (`ath_core/agents/cli.py`). It's a single author, not voters: a human reviews the output.
   - Model: `profile.models.for_stage("author")`.
   - Before calling, check the model policy (`load_policy(...).problem(models, "author", product)`)
     and `model_problem`.
   - Put the setup in `build_author_agent(cfg, profile)` next to `build_triage_agents`.
3. **Output format.** The agent CLI cannot edit files (empty temp dir, `Edit` denied), so it returns JSON:
   - `create`: `{path, clauses[], reason, content}`
   - `update`: `{path, clauses[], reason, edits: [{find, replace}]}`. Find/replace is used
     because masking can't be undone. See "Masking and how edits map back" for the rules on `find`.
   - `update` on a CSV file uses row operations instead of find/replace (see "Test files that hold transaction data").
   - `spec_questions`: `[{clause, question}]`, for when the TSD and the code disagree. This is
     already a rule in the skill.
   - `summary`
   - The schema lives in a new `ath_core/schemas/authoring.py` (pydantic). `validate_output`
     only checks top-level keys, so the rest is checked here.
4. **Deterministic guards.** A change that fails a guard is dropped and listed with the reason
   in the proposal. It is never written.
   - The path is relative, has no `..`, stays inside the checkout (after resolving symlinks), and
     matches a `testAuthoring.sources` glob. The edit type must match that source's kind and
     format. So no edits to source, build or CI files.
   - `create` only for a file that doesn't exist. `update` only for a file that exists and was
     sent to the model whole (`editable: true`).
   - Each `find` passes the matching rules in "Masking and how edits map back".
   - `replace` and `content` contain no mask tokens, and no value `Masker` would mask (see
     "Sensitive values").
   - **Traceability.** Every `clauses` id must exist in the TSD sent. A new
     `tsd_clause_ids(md)` in `ath_core/context.py` reads the `ⓘ` footers (format
     `ⓘ R-3 · AC-7`; ids are `R-`, `AC-` and `IMP-` followed by digits, separated by ` · `).
     When the TSD sent has any `ⓘ` ids, only those ids are accepted. Section titles are accepted
     only for a TSD with no `ⓘ` footers, and the checklist then marks the change "traced by
     section title only".
   - Caps on the number of files and the content size.
5. **Review flags.** These don't drop the change. They are listed in the checklist for the
   reviewer, because model-written test code runs in CI with CI credentials, and the inputs
   (TSD, diff, code, tests) can carry prompt injection:
   - Weakened tests: assertion-like lines removed vs added, removed test methods, and added
     `@Disabled` / `@Ignore` / `assume*` / skip tags.
   - Changed tolerances, timeouts and expected-exception types.
   - New imports.
   - Calls to network, process, file-system, reflection or environment APIs (`java.net`,
     `HttpClient`, `Runtime.exec`, `ProcessBuilder`, `java.nio.file`, `System.getenv`,
     `Class.forName`, and the like). Kept as a list in `ath_core/authoring/flags.py`.
6. **Proposal**: the accepted changes, the dropped changes with reasons, spec questions, the
   SHA-256 of each base file, the review flags, and the `agent.call` audit record. Its own
   SHA-256 (over its canonical JSON) is the **proposal hash**.
   - It is rendered as `changes.patch` (unified diff via `difflib`) and `checklist.md`
     (clause → test, assertions, flags).
   - **The patch and checklist are built from the masked base text**, so no stored artifact
     holds a raw value from an existing test file. That matches how the runner treats raw
     results (held only until T6, then wiped). The patch is for review, not for `git apply`.
     Changes are applied only from the proposal, through the adapter.
   - Catalogue references stay as references in the proposal, patch and checklist.
7. **Apply** through a new `TestAuthor` adapter `local-fs`
   (`ath_core/adapters/author/local_fs.py`, registered next to `git-mr`).
   - The engine now builds proposals, so change the `TestAuthor` interface in
     `ath_core/adapters/base.py` to one method:
     `apply(proposal, base_dir: Path, approved_by: str) -> AppliedResult`. Drop `propose`, which
     needs a `RunContext` that standalone runs don't have. Update `git_mr.py` to the new
     signature; it still raises `NotImplementedError`.
   - Before writing, it re-checks that each base file's SHA-256 is unchanged, so the tests are
     not edited after the proposal. It also re-runs the guards, then fills in catalogue values.
     If any file fails, it writes nothing: all files are checked first, then written.

### Test files that hold transaction data (CSV, JSON/YAML)
A suite can be test code (JUnit and the like), data files with transaction details, or both.
The profile describes each kind. This replaces the flat `paths`:
```yaml
testAuthoring:
  enabled: true
  mode: local-fs
  sources:
    - { kind: code, paths: ["src/test/**/*.java"] }
    - { kind: data, format: csv,  paths: ["testdata/**/*.csv"], keyColumn: test_id }
    - { kind: data, format: json, paths: ["scenarios/**/*.json"] }
    - { kind: data, format: yaml, paths: ["scenarios/**/*.yaml"] }
  testData: testdata/catalogue.yaml   # approved test cards, accounts, mobiles, Aadhaar numbers
```
- **Edits by format.** The model is told each file's kind and format and must use the matching edits:
  - **code / text**: `create` (whole content) or `update` with find/replace edits, as above.
  - **CSV (and TSV)**:
    - Edits are row operations: `add_row {values: {col: val}}` and
      `update_row {key: <keyColumn value>, set: {col: val}}`. Columns the edit doesn't name keep
      their original values. So a masked PAN in an existing row is never sent back or written
      over, which the find/replace edits on whole lines can't guarantee.
    - ATH checks every column name against the header and that the key matches exactly one row.
      The key is matched against the original file. A key column that holds masked values can't
      be used, and its edits are dropped.
    - **Only touched lines change.** The file is parsed with the `csv` module, but written as
      text: every untouched line stays byte-identical. A changed row is re-written in place,
      and an added row is appended. The new lines copy the delimiter, quote character and
      quoting style of the existing rows (read from the file with a fallback order of `,` `;`
      `\t` `|` rather than trusting `csv.Sniffer` alone), and use the file's line endings.
    - New CSV files must use the header of a sibling file in the same folder, and its dialect.
  - **JSON / YAML**:
    - Use find/replace edits or a whole-file `create`. Text edits keep the file's formatting and
      YAML comments.
    - After the edits are applied, ATH must be able to parse the file (`json` / `yaml.safe_load`),
      or the change is dropped.
    - An edit whose `find` would have to include masked text is dropped with "edit touches masked
      data; edit by hand". This is rare because each field is usually on its own line.
- **Sensitive values come only from the approved catalogue.** The model is never given or asked
  to invent card numbers or other masked values.
  - `testData` lists approved test data of kinds `card`, `account`, `mobile` and `aadhaar`:
    `{name: visa-credit-01, kind: card, description: "Visa credit, domestic, active", values: {pan: ..., expiry: ..., cvv: ...}}`.
  - The model sees only names, descriptions and field names, never values. It writes a reference
    such as `{{testdata:visa-credit-01.pan}}`, and ATH fills in the value when applying.
  - Guards:
    - An unknown reference drops the change.
    - Model output (with references left in) is passed through `Masker`. Any hit drops the
      change, with its category and masked token (see "False positives").
  - The proposal and patch keep the references, not the values. Values appear only in the files
    written to the checkout.
  - `doctor` checks the catalogue: it parses, names are unique, card PANs pass Luhn, and
    Aadhaar numbers pass Verhoeff.
- `.xlsx` is out of scope. Export it to CSV, or add it later with `openpyxl`.

### Source signatures
`ath_core/authoring/signatures.py`, no new dependency:
- A **generic declaration-line extractor** for any language: lines that start a class,
  interface, enum, record, or a public or protected method, with their annotations, and with
  bodies left out.
- A **Java extractor** on top of it that joins declarations written over several lines, handles
  generics, annotations, records, and nested types, and keeps the fields of model classes. It
  is tested on fixtures. If it can't parse a file, it falls back to the generic extractor for
  that file and notes it in the block.
- If a Java parser library is approved as a dependency later, it can replace the Java
  extractor behind the same function.

### Entry point 1: standalone command (for testers)
```sh
ainxt-ath author --profile P --tests ./checkout (--tsd F | --rh-package D | --from-run RUN_ID)
                 [--section S]... [--code ./code-checkout] [--diff FILE] [--dry-run] [--force] [--out DIR]
ainxt-ath author apply <id> --profile P --tests ./checkout [--force]
```
- Runs engine steps 1–7 with no RunContext, orchestrator, env or deploy. With `--dry-run`, it
  stops after step 6.
- `--code` adds the `code` block (source signatures). `--diff` adds the `diff` block.
- `--from-run` adds the `outdated` block from that run's triage results. It also reuses the TSD
  and diff stored with that run, unless `--tsd`/`--rh-package`/`--diff` are given.
- The product skill, conventions, catalogue and templates come from the profile.
- Refuses when `testAuthoring.sources` is empty, or when a target file has uncommitted changes
  (`git status --porcelain`), unless `--force` is passed. If the checkout isn't a Git
  repository, the uncommitted-changes check can't run, so `--force` is required.
- Writes the files, then prints the change summary, the dropped changes, the review flags and
  the spec questions. The tester reviews with `git diff` and commits or opens an MR as usual.
- Saves the proposal, patch and checklist under `.ath/authoring/<id>/`, and records
  `agent.call` and `author.applied` events in the state store under id `author-<hex>`.
- `author apply <id>` loads `.ath/authoring/<id>/proposal.json`, checks its proposal hash, and
  applies it through `local-fs` with the base-file hash check. It makes no model call. This is
  how a `--dry-run` proposal is applied after review.
- There is no ATH gate here. The tester is the reviewer, and the changes reach main only through
  the test repo's own MR review.
- Exit codes:
  - 0: changes applied (or proposed, with `--dry-run`)
  - 1: refused (bad input, policy, uncommitted files, base files changed before apply)
  - 4: the model returned only spec questions
  - 5: the model proposed changes, but every one was dropped by a guard

  The numbers 2 and 3 are left out because `doctor` and `eval` already use them.

### Entry point 2: T3 inside `ainxt-ath run` (on/off switch)
- Profile `TestAuthoring` (`ath_core/schemas/profile.py:88`) gets new fields:
  `enabled: bool = True`, `sources: list[...] = []`, `testData`, `codeContext`, `templates`,
  plus limits. `mode` also accepts `local-fs`.
  - `sources` is optional, so existing profiles keep validating:
    [examples/settlenxt/test-profile.yaml](../examples/settlenxt/test-profile.yaml) and
    [templates/product-package/test-profile.yaml](../templates/product-package/test-profile.yaml)
    both have `testAuthoring` with `mode: git-mr` and no `sources`.
  - A model validator requires `sources` when `mode` is `local-fs`. For the other modes, `doctor`
    warns that `sources` is empty.
  - `TestProfile` gets a top-level `productSkill` (shared by all LLM stages):
  ```yaml
  productSkill: skills/settlenxt-product-skill.md
  testAuthoring:
    conventions: skills/settlenxt-test-conventions.md
    codeContext: ["src/main/java/**/clearing/**/*.java", "src/main/java/**/model/**/*.java"]
    templates: "simulator/templates/**/*.yaml"   # only for products with simulator scenarios
  ```
- `RunContext` (`ath_core/schemas/run.py`) gets new fields:
  - `author: bool | None = None`. `None` follows the profile; `--author` / `--no-author` override it.
  - `tests_dir: str | None`, the local checkout given with `--tests`.
  - `code_dir: str | None`, the code checkout given with `--code`, used for the `code` block.
  - These are local paths on the machine that runs the orchestrator. They are stored in the run
    manifest, and `start()` compares the whole context, so the same run id with a different
    `--tests` path is refused as "different inputs". That is intended. A run started on one
    machine can be approved only where `tests_dir` exists (the CLI for now).
- `GateDecision` (`ath_core/schemas/run.py:80`) gets `ref: str | None`, the hash of what was
  approved.
  - When T3 raises its gate, the gate summary shows the proposal hash. `record_gate` copies the
    hash of the stored `author/proposal.json` into `ref`.
  - `ainxt-ath approve` takes an optional `--ref HASH`. If it is given and doesn't match the
    stored proposal, the approval is refused.
- Inputs in a run: everything in "Inputs to the authoring model", with `diff` taken from the run.
  `impact` is added once T1 is built. `outdated` isn't available here, because T3 runs before
  triage (T7). Updating tests that triage finds outdated is a pass after triage (see Later);
  until then testers use `ainxt-ath author --from-run`.
- In `t3_author`, T3 runs only when all of these hold. Otherwise it is skipped, with the exact reason.
  - mode is not `report-only`
  - the profile has `testAuthoring`
  - the effective switch is on
  - `testAuthoring.mode` is `local-fs`. Other modes: `mode <m> is not implemented yet`.
  - `sources` is not empty
  - the TSD context exists
  - `tests_dir` is set
- **The proposal is made once.** If `author/proposal.json` already exists for the run, T3 always
  reuses it, whether the gate is approved or not (for example after `resume`, or a crash after
  the model call). The model is called only when there is no stored proposal.
- First pass: call the engine, store `author/proposal.json`, `author/changes.patch` and
  `author/checklist.md` as run artifacts, then raise `GateRequired` with a summary and the
  proposal hash.
- After approval (resume):
  - Load the stored proposal and check that its hash equals the approved `GateDecision.ref`.
    If it doesn't, fail T3 ("proposal changed after approval").
  - Apply it with the profile's adapter (`local-fs` now, `git-mr` later).
  - Return the applied paths and the proposal hash.
  - If T3 is rejected, the existing gate logic stops the run.
- T9 report: add a "Test authoring (T3)" section listing applied/dropped changes, review flags
  and spec questions. Use a small extension to `render_report` in `ath_core/report/markdown.py`.
- API: `RunRequest` gets `author: bool | None`. The server has no test checkout yet, so T3
  there is skipped with that reason until `git-mr` can clone the test repo.

### Skill and docs
- `skills/t03-author/SKILL.md`:
  - Add `output_schema` and the rules for the edit format. Keep the injection-rules include.
  - List every input `<data>` block (`tsd`, `tests`, `tree`, `conventions`, `testdata`,
    `product`, `code`, `templates`, `diff`, `impact`, `outdated`) and say which are optional.
    A missing block is sent as `(not attached)`, as triage does.
  - Rules for the model:
    - Call only methods and types that appear in `code`. If `code` isn't attached, say so in `summary`.
    - Use `diff` and `outdated` to decide what to update. Expected values still come only from the TSD.
    - The product skill gives context but never overrides the TSD.
    - Mask tokens are redacted values: never copy or write them. Write every card, account,
      mobile or Aadhaar value as a `{{testdata:...}}` reference.
    - Update only files marked `editable: true`. A `find` must not include a mask token.
    - When the TSD has `ⓘ` ids, cite those ids in `clauses`, not section titles.
    - Don't add network, process, file-system or environment access to tests unless the
      conventions say to.
- `config/model-policy.yaml`: `author: restricted` (security review).
- `templates/product-package/test-profile.yaml` and `examples/settlenxt/test-profile.yaml`: add
  `productSkill` and the new `testAuthoring` fields (comment out `sources` for `git-mr`, keep it
  valid).
- `doctor`: when `testAuthoring.enabled` is set, check:
  - the author skill can be read, and the author model passes the model policy;
  - `sources` is set (an error for `local-fs`, a warning otherwise), and every `sources` /
    `codeContext` / `templates` glob matches at least one file (a warning if no checkout is given);
  - the `testData` catalogue is valid;
  - the `productSkill` file can be read and has front matter.
- README / `docs/GUIDE.md`: document the `author` and `author apply` commands, the switch, T3
  behaviour, and the exit codes. Update the status tables.

## Phases
Each phase is its own MR, with its own tests, and leaves `main` working.

1. **Product skill to all stages.** `productSkill` in the profile, `load_product_skill`, the
   `product` block in triage evidence, `doctor` check. Small and independent.
2. **Standalone `author` (code/text and CSV).**
   - The engine, the `tsd`, `tests`, `tree`, `conventions`, `testdata` and `product` blocks,
     `mask_spans`, all guards, review flags, the catalogue, `local-fs` with the new `TestAuthor`
     interface, `author` and `author apply`.
   - The model-policy change goes in here, because this is the first phase that sends test data.
3. **T3 in the run.** `RunContext` fields, `--author/--no-author/--tests/--code`,
   `GateDecision.ref`, proposal reuse, the T9 section, `RunRequest.author`.
4. **More formats and inputs.** JSON/YAML edits, the `code` block (signatures), `templates`,
   `--diff`, `--from-run`.

## Later (out of this change)
- `git-mr` apply: push branch `ath/<run>/tests` and open an MR through the credential broker.
- Run authored tests in T5 by passing a `${testsRef}` executor param (Jenkins params already
  substitute `${commit}` and friends). Until then, the T3 changes made in a run are applied
  locally and reported, not executed in the same run.
- Authoring eval dataset (with injection cases), like `tests/evals/triage/`.
- Update outdated tests inside a run: after T8, send `test-outdated` cases back through the
  engine behind a gate. It reuses the T3 proposal and apply path.
- More inputs: the Jira story and acceptance criteria (through the credential broker) when the
  TSD doesn't include them. The latest results for the selected tests (from the baseline
  cache), so the model knows which tests exist and pass today.
- Signature extractors for languages other than Java (Kotlin, TypeScript, Python), if products need them.
- A checksum or context rule for `mobile` masking, to cut false positives in code (its own
  security review).

## Critical files
- New: `ath_core/authoring/{__init__,engine,formats,testdata,signatures,flags}.py` (formats:
  text/CSV/JSON/YAML edits and checks; testdata: catalogue loading, references, filling in
  values; signatures: source signature extraction; flags: review flags for the checklist),
  `ath_core/schemas/authoring.py`, `ath_core/adapters/author/local_fs.py`,
  `tests/test_authoring.py`, `tests/fixtures/authoring/` (TSD with `ⓘ` footers, mini test
  repo, mini code repo, product skill, catalogue)
- Change:
  - `ath_cli/main.py`: new `author` and `author apply` commands; `run --tests/--code/--author/--no-author`; `approve --ref`; doctor
  - `ath_server/orchestrator/runner.py`: `t3_author`; `record_gate` sets `ref`; product skill passed to T7 as evidence
  - `ath_core/schemas/{profile,run}.py`: `TestAuthoring` fields and validator, `productSkill`,
    `RunContext` fields, `GateDecision.ref`
  - `ath_core/masking/masker.py`: `mask_spans`
  - `ath_core/adapters/base.py`: `TestAuthor.apply` only; `ath_core/adapters/author/git_mr.py` to match
  - `ath_core/agents/cli.py`: `build_author_agent`
  - `ath_core/context.py`: public ranking helper, `load_product_skill`, `tsd_clause_ids`
  - `config/model-policy.yaml`: `author: restricted`
  - `templates/product-package/test-profile.yaml`, `examples/settlenxt/test-profile.yaml`,
    `examples/settlenxt/test-profile.local.yaml` (`testAuthoring` block)
  - `ath_core/report/markdown.py`, `ath_server/api/app.py`, `skills/t03-author/SKILL.md`
  - `tests/fakes/fake_agent_cli.py`: new `author` mode plus bad-output modes
  - README, `docs/GUIDE.md`

## Verification
- `pytest` (no network). New tests use the fake agent CLI:
  - Standalone:
    - Create plus update are applied to a temp checkout.
    - `--dry-run` leaves files untouched. `author apply <id>` then applies the stored proposal
      with no model call.
    - Uncommitted target files are refused. A checkout that isn't a Git repository is refused
      without `--force`.
    - Exit codes 0, 1, 4 and 5 each come back in the right case.
  - Guards: path escape (including a symlink), a non-test path, an unknown clause id, a section
    title used when the TSD has `ⓘ` ids, a mask token in a replacement, an ambiguous `find`, an
    update to a file that wasn't sent whole, overlapping edits, and a file changed between
    proposal and apply. Each one is dropped with its reason. When one file fails at apply time,
    no file is written.
  - Masked test data:
    - A test file containing `4111 1111 1111 1111` is masked in the prompt and stays unchanged
      on disk after the update.
    - A `find` such as `1111` that occurs once in the masked text but also inside the raw PAN
      is dropped.
    - CRLF and BOM files keep their line endings and BOM after an update.
  - CSV:
    - `add_row` with a `{{testdata:...}}` reference writes the catalogue value.
    - `update_row` changes only the named columns, and the masked PAN column keeps its original value.
    - An unknown column, a duplicate key or a missing key are each dropped.
    - Untouched lines are byte-identical after an update, including all-quoted rows and `;` files.
  - JSON/YAML: an edit that breaks parsing is dropped. YAML comments survive an update.
  - Sensitive output:
    - A change where the model wrote a raw Luhn-valid PAN is dropped, with the category in the reason.
    - An unknown `testdata` reference is dropped.
    - The proposal and patch contain references, never values.
    - The stored patch and checklist contain no raw PAN from an existing test file.
  - Review flags: removed asserts, an added `@Disabled`, a new `java.net` import and a
    `Runtime.exec` call each appear in the checklist.
  - Run:
    - `assist` with `--tests` and `--tsd` stops at T3 with a stored proposal.
    - `resume` before approval reuses that proposal (one model call in total).
    - After `approve`, it applies the proposal without a second model call.
    - A proposal changed after approval fails T3. `approve --ref` with a wrong hash is refused.
    - `--no-author`, `enabled: false`, empty `sources` and `mode: git-mr` each give a skipped
      T3 with their reason. `report-only` still skips T3.
    - `examples/settlenxt/test-profile.yaml` still validates.
  - Policy: `author` is `restricted`. An author model the model policy forbids is refused
    before any call.
  - Inputs:
    - The prompt holds each configured block. A missing optional block reads `(not attached)`.
    - The `testdata` block never contains catalogue values.
    - The same inputs always give the same prompt (same hash).
    - Over-size blocks say how much was left out, and no test file in `tests` is cut midway.
  - Signatures: a fixture Java class gives its class, enum, record and public method signatures
    (including a declaration written over several lines, generics and annotations), and no
    method bodies.
  - `--from-run`: the `outdated` block holds the run's `test-outdated` cases, and the run's TSD and diff are reused.
  - Product skill: `productSkill` reaches both the author prompt and the triage voters'
    `product` block (no longer `(not attached)`).
- Manual: `ainxt-ath author --profile examples/settlenxt/test-profile.local.yaml --tests tests/fixtures/authoring/repo --tsd tests/fixtures/authoring/tsd.md --dry-run`
  (with `ATH_CONFIG` pointing at a config that uses the fake CLI), then `ainxt-ath author apply <id>`,
  then the same `run --mode assist` flow.

## Review notes: decisions made during implementation
Gaps found in the plan above, and how the code settles them.
- **Path bases.** `productSkill` and `conventions` are relative to the profile file (it now records
  its own path, kept out of the profile hash). `sources`, `testData` and `templates` are relative to the
  test checkout. `codeContext` is relative to `--code`.
- **Mask tokens.** A bare `***` is not treated as a token, because it is ordinary text (`/*****`). Only
  the masked-CVV form counts (`cvv=***`, `"cvv": "***"`), plus the bracket tokens.
- **Masking categories.** Authoring masks every category, whatever `logs.masking` says. Test files
  are not logs, and turning a category off for logs must not send raw test data to the model.
- **The skill's `output_schema`** must not contain `|`: `validate_output` treats any `a|b` value as
  an enum.
- **Nothing to apply.** When no change passes the guards (only spec questions, or every change dropped),
  T3 finishes `done` without a gate. The questions appear in the T9 report.
- **Repeat applies.** Each accepted change also records the SHA-256 of the file once applied, so applying
  the same proposal again (e.g. a resume after a crash between writing and checkpointing) reports
  "already applied" instead of failing the base-hash check.
- **Stored free text.** The model's `reason`, `summary`, spec questions and dropped reasons are masked
  before they are stored. `update_row` keys go through the same written-value check as replacements.
- **Standalone checks.** A non-Git checkout is refused before the model call (no wasted call).
  `--dry-run` skips the uncommitted-files check. `--from-run` reads the run from `--db`.
  `--out` is the parent folder: proposals go in `<out>/<id>/`.
- **Exit code 4** covers "the model proposed no change" (with or without spec questions). **Exit code 5**
  means it proposed changes and every one was dropped.
- **Run inputs.** In a run, T3 builds the author agent lazily, so a policy violation fails T3 before any
  call, not the whole run at start. The API's gate endpoint also takes `ref`.
- **Profile hash.** New `testAuthoring` defaults change the profile hash of existing profiles that have
  `testAuthoring`. Gates of runs started before the upgrade must be decided with the old version.
