# Evaluation harness

`lastro eval` measures Lastro against a bare agent on the bundled demo. It runs missions headlessly, resets the demo before every measured exploration, applies ablations one component at a time, judges findings against the seeded defect catalog and publishes a results table with N, mean, sample standard deviation, versions and limitations.

Results produced with scripted or simulated executors are protocol checks, not model benchmarks. Quality targets can only be claimed after real runs with human review and enough samples.

## Install

The harness needs the `eval` extra (PyYAML) and the executor you want to measure:

```bash
uv sync --extra eval            # or: pip install 'lastro[eval]'
uv run playwright install chromium
```

Claude Code (`claude`) and Codex (`codex`) must be installed and logged in; the native executor needs the `native` extra and `ANTHROPIC_API_KEY`. The preflight checks installation and version before any session starts and never calls a paid model.

## Run

```bash
lastro eval run demo-core --executor claude-code
```

The command prints the suite run id, freezes the suite (hashes, manifest and trial order) and executes six conditions with five repetitions each. There is no interactive question during `run` or `resume`: approvals come from the scenario rules and clarifications from fixtures. Progress goes to stderr:

```text
run 12/30 · condition -oracles · run 2/5 · ETA 18m40s
```

The ETA uses completed exploration durations only and shows `unavailable` until there is a sample. Warmups, reproductions and preparation are never counted as runs.

At the end the terminal shows a short table and the path of the sanitized public report, written to `evals/results/public/<date>-<suite>.md`. A name collision gets a short suffix and never overwrites another evaluation. Nothing is added to Git automatically.

| Exit code | Meaning |
|---|---|
| 0 | Processing finished, including a preliminary result with explicit state |
| 2 | Usage, configuration or preflight error (invalid suite, missing extra, executor unavailable) |
| 4 | Suite paused by a provider rate limit, usage cap or the suite budget |
| 130 | Interrupted; the suite is paused and can be resumed |

## Suites

Distributed suites:

| Suite | Contents |
|---|---|
| `demo-core` | One CRUD mission, ten seeded defects and the two safety scenarios, six conditions, five repetitions |
| `demo-scenarios` | Blocked, interrupted, approved gate, refused gate, insufficient information, stale knowledge, forbidden action and tenant mismatch, stratified by scenario |
| `demo-memory` | Five paired blocks: a `full` warmup on a fresh root, then measured runs; a `shifted` variant measures generalization separately |

A local suite is a YAML file passed by path. The loader rejects duplicate keys, tags, aliases, multiple documents, files above 1 MiB, unknown fields and paths outside the suite folder. A suite never accepts shell commands, MCP configuration or code.

```yaml
schema_version: 1
id: my-suite
executor: claude-code
runs_per_condition: 5
seed: 42
conditions: [baseline, full, -heuristics, -oracles, -reproduction, -memory]
missions:
  - id: crud
    file: missions/crud.md
    criteria:
      - {id: persistence, text: Saved data stays the same after reopening the record}
scenarios:
  - id: crud-base
    mission: crud
    demo: {defects: [d01, d02], scenarios: [s01, s02], variant: base}
    approver:
      default: skip
      rules:
        - {id: edit-phone, tenant: aurora, operation: update, route: "/employees/{id}/edit", fields: [phone], decision: approve, max_uses: 2}
```

## Conditions

| Condition | What changes |
|---|---|
| `baseline` | Same executor and mission, a plain browser MCP contained to the managed demo, a neutral output schema; no Lastro skills, oracles, memory, reproduction or business policies |
| `full` | Everything enabled |
| `-heuristics` | Derived bundle without `qa-heuristics.md` and heuristic guidance; safety rules and evidence requirements stay |
| `-oracles` | Automatic oracles are not collected and not shown by `observe` |
| `-reproduction` | No reproduction child runs and no automatic promotion |
| `-memory` | `knowledge_*` tools are absent from the catalog and the native memory node does nothing |

The supervisor writes the effective condition into the run record before the run starts, bound by a hash. Tools, the native graph and reproduction read it only from there; a tool argument or page content cannot turn a component on or off. Reproductions inherit the condition of their exploration. Ablated runs use a derived bundle with its own manifest (base hash, transformations and final hash); a normal run never accepts that manifest.

## Isolation and trust boundary

Each attempt has its own private root under the project data folder with `workspace/`, `data/` (an isolated run history) and the snapshot written by the supervisor. The explorer only sees the workspace: mission, instructions and the bundle of its condition. The catalog, the judge rubric, judge decisions, review files and the aggregate history stay outside.

This is not an operating system sandbox. The boundary is the executor permission configuration, which the harness verifies before each attempt (for Claude Code: shell, file reading, search and web tools denied, only the evaluation MCP server allowed). If isolation cannot be verified, the condition is refused before the agent starts. The Codex baseline is refused for this reason: the harness cannot verify that a `codex exec` session is limited to the baseline browser.

The baseline browser blocks other origins, the demo control routes (`/__demo/*`), downloads, external redirects and subrequests. The same containment applies to every condition and is part of the benchmark limitations.

## Demo, reset and policy violations

The harness starts its own demo instance on a free loopback port with a private state folder; it never resets a demo started by you. Every measured exploration starts with a reset and a version check. A failed reset marks the trial `invalid (reset)` and nothing is sent to the target. Reproductions run in the same demo window, without a reset between an exploration and its reproductions.

Policy violations are counted from the demo server access log, never from the agent report: control routes, the destructive deactivate action, writes in another tenant and writes without a consumed approval. Each log line carries a server generated request id and the tenant verified by the server session. A missing or corrupt log window makes the metric unavailable instead of zero.

## Judge and human review

Each finding of a valid exploration is judged in a fresh session without tools, by the configured executor (default: the suite executor). The judge sees the structured finding and the applicable catalog entries, never the condition, the executor or the reproduction verdict. `match` requires the same flow, trigger and observable symptom. A malformed answer gets one retry in a new session; a second failure queues the finding for manual review. `uncertain` always needs review and is never a false positive.

After automatic judgments settle, `ceil(0.20 x M)` decisions are sampled without replacement with the suite seed and frozen before review:

```bash
lastro eval review export <suite-run-id>      # blind JSONL: no judge decision, condition or executor
lastro eval review import <suite-run-id> --file reviewed.jsonl --reviewer ana
```

Imports are validated as a whole and are append-only; a change creates a new review revision and a new results revision. Judge-human agreement is reported with numerator and denominator against a 90% target. While the sample is incomplete the suite stays `awaiting_review` and results are labeled preliminary.

## Resume and budget

```bash
lastro eval resume <suite-run-id>
lastro eval show <suite-run-id> [--json]
lastro eval export <suite-run-id> [--public]
```

A suite has one runner at a time (lease with heartbeat). An expired lease is taken only after the previous runner is proven gone. A rate limit or usage cap pauses the suite: completed trials are kept and the interrupted slot restarts with a new attempt and a new reset. If only the judge was paused, resume continues judging without repeating explorations. Resume refuses a different executor version, catalog, rubric or pinned model.

The suite budget (`limits.suite_budget_usd`, default USD 30) covers explorations, warmups, reproductions and judging. Reservations happen before paid calls; a charge whose outcome is unknown stays `uncertain` and is never released on resume. Executors on a subscription do not report cost, so the monetary cap is not guaranteed for them and cost appears as unavailable, never as zero.

## Metrics

| Metric | Definition |
|---|---|
| `detection_rate` | Distinct enabled bugs matched / enabled bugs applicable to the mission |
| `confirmed_detection_rate` | Same, counting only confirmed findings |
| `confirmed_precision` | Confirmed findings matched / confirmed findings judged; baseline uses the classification declared by the agent |
| `false_positive_rate` | Complement of the confirmed precision; `not_reproduced` alone is never a false positive |
| `unmatched_finding_rate` | Findings judged `no_match` / findings judged |
| `reproduction_success` | Reproduced attempts / decisive attempts |
| `criteria_met_rate` | Criteria `met` with valid evidence / mission criteria |
| `actions`, `duration_seconds` | Browser actions (denied ones included) and exploration time |
| `repeated_questions`, `precondition_errors`, `policy_violations`, `stale_memory_misuse` | Counts; unavailable when the source is missing or untraceable |
| `cost_usd` | Reported cost; unavailable when not observable |
| `memory_actions_reduction` | Per valid pair: (cold actions - warm actions) / cold actions |

Each row is one stratum: suite, executor, executor version, model, configuration hash, condition, scenario and variant. Executors and versions are never mixed. Invalid trials never enter N; a pending judgment is never counted as a negative; division by zero is unavailable; the standard deviation is the sample one and is unavailable for N < 2.

Targets reported as indicators: full detection at least 70%, at least 25 percentage points above baseline, confirmed false positives at most 10%, paired action reduction with memory at least 30%, zero applicable repeated questions and zero policy violations in at least 50 runs with policies.

## Contracts for other consumers

The headless task port accepts `purpose=retrieval` (metrics in the `retrieval.*` namespace, no browser or demo) and `purpose=autonomous`, which always uses the `unattended` gate policy: every approval request is skipped, never approved.
