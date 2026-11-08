# Finding reproduction and independent verification

A finding only becomes `confirmed` after a fresh session, without access to the explorer's reasoning or transcript, repeats its preconditions and steps and observes the same defect. Lastro creates that session, interprets every attempt on the server and records an immutable verdict per finding.

## What the reproducer receives

Each attempt is a new child run of the explored run, with a new browser, a new login and a new tenant check. Its mission is a brief built by the server from the stored finding, never from text sent by the agent:

- the flow and, when known, the entity;
- the numbered preconditions;
- the steps;
- the expected result and its source.

The observed result, limitations, evidence, severity, failure domain, the finding id, the explorer's messages, plan and transcript are never part of the brief. Persistent memory is not available in a reproduction attempt (`knowledge_*` tools answer `knowledge_unavailable`), so derived knowledge cannot leak the explorer's reasoning.

The child run uses the current validated profile configuration: the same policies, tenant, approvals flow and tool catalog as any run. Approvals from earlier runs never apply. Test data comes from `generate_test_data` with the child run's own marker; records created by the explored run are not reused.

## Attempts

| Setting | Default | Rule |
|---|---|---|
| `attempts` | 2 | 1 to 5 attempts per finding |
| `intermittent_attempts` | 3 | used when the explorer tagged the finding `intermittent`; at least `attempts`, at most 7 |
| `max_minutes` | 10 | budget of each attempt; the child run gets the smaller of this value and `run_limits` |
| `max_actions` | 50 | same rule as `max_minutes` |
| `mode` | `same` | `same` reproduces with the explorer's executor; `cross` uses the paired verifier |
| `verifiers` | `{}` | explorer = verifier pairs for `cross`, e.g. `{ claude-code = "codex" }` |
| `auto` | `true` | reproduce automatically when a run ends `completed` |

```toml
[profiles.homolog.reproduction]
attempts = 2
intermittent_attempts = 3
mode = "cross"
verifiers = { claude-code = "codex" }
```

An unknown key, a verifier equal to its explorer, an executor outside `claude-code`, `codex` and `native`, or `cross` without pairs fails when the configuration loads. The explored run freezes the effective values in its configuration snapshot.

Attempts of one finding run in sequence, and findings are processed in creation order. Only suspected findings of the system under test are reproduced; agent and tool failures are not. A `blocked` or `preconditions` attempt stops the remaining attempts of that finding, because repeating does not change the cause.

## Reporting an attempt

A reproduction child run exposes one extra tool, `report_reproduction_result`. Other runs never publish it.

| Field | Rule |
|---|---|
| `result` | `reproduced`, `not_reproduced`, `diverged` or `precondition_missing` |
| `evidence_artifact_ids` | required for `reproduced`; every id must belong to this child run |
| `precondition_index` | only with `precondition_missing`; an index of the brief's preconditions |
| `note` | up to 1,000 characters, redacted before it is stored |

```json
{"result": "reproduced", "evidence_artifact_ids": ["art_ab12cd34"], "note": "Duplicate CPF accepted on second save."}
```

The response is `{"recorded": true, "attempt_ordinal": 2}`. Errors: `invalid_evidence`, `invalid_precondition_index`, `already_reported` (one report per attempt) and `not_a_reproduction_run`. The tool never returns data about the round or the finding.

## How the server decides

The reported result is one input. The server also reads the child run and the explored run:

- A child run that failed authentication or the tenant check, or received `deny` for a tool, operation and entity that were allowed in the explored run, is `blocked`, never `not reproduced`. This holds even when the reproducer claims `reproduced`.
- A child run stopped with `action_limit` or `duration_limit` is `budget`.
- `reproduced` needs evidence from the child run. For a finding raised by an oracle, the same oracle signal (type, method, status and path with record ids normalized) must also appear in the child run.
- `precondition_missing` names the precondition from the brief, never from the agent's text.
- A missing report, an unavailable executor or a diverging flow are recorded with their reason.

With `r` reproduced attempts and `d` decisive attempts (`reproduced` or `not_reproduced`):

- `r >= 1`: `confirmed`, shown as `confirmed (2/2)`, or `confirmed (1/3, intermittent)` when `r < d`. The observed frequency comes from the reproduction, not from the explorer's tag.
- `r = 0`, all attempts decisive: `inconclusive` with reason `not_reproduced`, shown as `not reproduced`.
- `r = 0` with non decisive attempts: `inconclusive` with the highest priority reason among `blocked`, `preconditions`, `budget`, `executor`, `diverged` and `evidence`; the others stay in the notes.

The verdict, the attempts and the classification change are written in one transaction. A verdict is written once per finding and is never overwritten or downgraded by a later round.

## Running a round

When a run ends `completed`, reproduction starts automatically: native and Codex reproduce in the CLI process that ended the run; with Claude Code, the browser owner starts a detached `lastro run reproduce --auto` when it closes. Runs that end `cancelled`, `interrupted` or `limit_reached` are only reproduced on request.

```bash
lastro run reproduce <run id>                      # every suspected finding without a verdict
lastro run reproduce <run id> --finding <id>       # one finding
lastro run reproduce <run id> --verifier codex     # only with mode = "cross", must match the pair
```

An active run is refused with `Run <id> is still running.`. A run without eligible findings exits 0. Another active run in the project stops the command before any finding changes. If the automatic round cannot start, the findings stay `suspected` and the report says which command to run.

A round is durable. If the coordinator stops in the middle, the next `lastro run reproduce` takes over the open round and continues with the missing attempts, without repeating completed ones. Only one round per run can be active at a time.

`lastro tail` shows the round in the explored run's log and only exits when the run ended and no round is open:

```text
Reproducing 3 suspected findings…
✓ confirmed (2/2)  Duplicate CPF accepted
◐ confirmed (1/3, intermittent)  Name sort ignored
✗ not reproduced  Phone not saved
? inconclusive (preconditions): Employee with CPF 123 exists  Duplicate email accepted
```

## Executors

The verifier is launched headless:

| Executor | Launch | Unavailable when |
|---|---|---|
| `native` | the same LangGraph loop as `lastro run start` | the `native` extra or `ANTHROPIC_API_KEY` is missing |
| `codex` | `codex exec`, as `lastro run launch --headless` | Codex is missing, not logged in or its rules changed |
| `claude-code` | `claude -p` with the project's `.mcp.json` and generated permissions | `claude` is not in `PATH` or `lastro init` files are missing |

In `cross` mode the verifier is the executor paired with the explored run's recorded executor. When the verifier is unavailable, the result is `inconclusive (executor)` with the install hint; Lastro never falls back to the explorer's executor. Both executors are recorded in the verdict and in the report, and a verdict from `same` mode is never described as executor independent.

## Reading verdicts

After a round, Lastro writes a new report of the explored run, `report.reproduction-v<n>.md`, with each finding's verdict, observed frequency, attempts and executors; earlier reports are never overwritten. The public export keeps the verdict label, frequency and executors, without internal notes. Evaluation code reads verdicts through `HistoryStore.list_reproduction_verdicts(run_id)`.
