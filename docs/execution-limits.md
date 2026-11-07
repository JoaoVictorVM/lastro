# Execution limits, cancellation and safe resume

Every run has limits enforced by the trusted browser server, not by the agent or the executor. The agent receives notices; it never decides whether a limit was reached, whether the run may continue or whether an interrupted write may be repeated.

## Limits

Limits are configured per profile and frozen in the run's configuration snapshot when the run is created:

```toml
[profiles.homolog.run_limits]
max_minutes = 30              # elapsed time since the run started, including approval waits
max_actions = 150             # MCP tool calls of the agent
max_attempts = 3              # total attempts of the same failing operation
max_no_progress_actions = 10  # consecutive calls without anything new
```

Every value is a positive integer. An unknown key, a boolean, text or a decimal fails before any run is created. Missing keys use the defaults above, and new runs always store all four values.

- **Actions.** Every MCP call of the agent counts, including invalid arguments, policy denials, reads and approved retries. Only `approval_status` and `end_run` are exempt. A call refused because the run already stopped does not count.
- **Duration.** Measured from the run start with the persisted deadline and a monotonic clock, so a wall clock jump never extends the budget. It runs during approval waits and when the agent is silent.
- **Attempts.** The server identifies an operation by tool, tenant, observed route and canonical arguments; approval ids, proofs, reasons and timeouts are ignored. The third failed attempt of the same operation stops the run. Waiting for an approval is not a failure.
- **No progress.** An action makes progress when it brings a new kind of domain event (authentication, human decision, clarification, oracle signal, created record, finding, accepted knowledge proposal, or the first tool call and policy decision), a new page observed by the browser, a new accepted finding or a new accepted knowledge proposal. Query strings, fragments, repeated pages, rejected proposals and the controller's own events do not count.

At 80% of the duration or of the actions, `lastro tail` shows a warning and the next tool result carries an `execution_notice` such as:

```json
{"execution_notice": {"kind": "warning", "dimension": "actions", "used": 120, "limit": 150, "message": "80% of action budget used; wrap up."}}
```

No extra model call is made just to deliver the notice.

## Stopping

When a limit is reached, or a cancellation is requested, the run stops with one terminal reason: `limit_reached` with `action_limit`, `duration_limit`, `attempt_limit` or `no_progress`, or `cancelled` with `user_cancelled`. The first reason recorded wins; later signals are logged without replacing it. Existing reasons such as `cost_cap` use the same closing path.

1. The write lock is engaged immediately, before waiting for anything. Every later write tool call returns `decision=deny` with `reason=write_lock`, and every write request is denied at the network boundary, which rechecks the deadline and the durable state just before forwarding.
2. Reads and responses already in flight may finish for up to 10 seconds. After that, local tasks are cancelled and writes without a response are recorded as having an unknown effect.
3. The state and reason are persisted and the partial report is built (or its JSON fallback, `report.partial.json`). The whole sequence shares one 15 second deadline.

The last action allowed by `max_actions` may send exactly one bound write request; the lock engages in the same step, so no second write leaves. A deadline or a cancellation revokes that allowance.

The terminal prints, for example:

```
Run stopped: action limit (150) reached. Partial report: <path>
```

The partial report lists the limits, the actions used, the reason and the pending items: criteria not evaluated, writes with an unknown effect, withdrawn approvals and created records to clean up.

## Cancelling

```
lastro run cancel [<run id>]
```

Without an id, the only unfinished run of the selected project is used. The request is durable and idempotent; repeating it, or cancelling a run that already ended, prints the existing state and report. Pending approval requests are withdrawn as `skipped` with reason `cancelled`, and approved but unused approvals are invalidated; the human decision stays in the history.

Ctrl+C in `lastro tail` requests the cancellation of the followed run and waits for the summary within the same 15 seconds. A second Ctrl+C stops only the waiting (exit code 130); the request stays active. On a finished run, Ctrl+C just closes the view.

If the browser server is gone, the command finishes the run itself. Without a browser server no write can leave, because the network guard reads the durable state. If the history cannot be written, the command reports the failure and never claims that a report exists. A run created for `lastro run launch` and still waiting to be launched has its deadline checked by `lastro run launch` and `lastro run resume`: once expired, it ends as `limit_reached` with `duration_limit` before any browser opens.

## Resuming

```
lastro run resume <run id>
```

Only `interrupted`, `cancelled` and `limit_reached` runs can be resumed. An active run must be cancelled first; a completed or blocked run needs a new mission. The profile, environment and tenant must match the original run, otherwise nothing is created.

The resume creates a new linked run with a fresh budget, the current validated configuration and the original mission snapshot (the issue is not fetched again and no new diff is collected). It uses the executor of the original run. Previous approvals have no authority; the current policy applies to everything. Evidence and created records stay with the run that produced them.

Before repeating an edit that may have been sent, the agent opens the record's edit page. The server reads the mapped business fields as rendered by the target and compares them with the intended values:

| Current values | Result | Write sent |
|---|---|---|
| Equal to the intended values | `already_applied`, with screenshot evidence | No |
| Equal to the values before the edit | `ready_for_review`; a new send follows the current policy and approval | Only if allowed again |
| Anything else, or a partial change | `conflict` | No |
| Not readable, or the intended values were redacted | `inconclusive` | No |
| Record page returns 404 | `inconclusive`: `Record <id> not found on resume; no write repeated.` | No |

The check applies to every new edit of the same record, whatever tool, action id or values the agent uses. Creations without a response and without a reliable id stay inconclusive for manual review and are never created again. Runs recorded before the write journal existed can be resumed to see their pending items, but their old submits are listed as unverifiable and are never replayed.

The tool results carry the check, for example:

```json
{"resume_check": {"source_write_id": "wi_…", "state": "already_applied", "record_id": "57", "evidence_artifact_id": "…", "write_repeated": false}}
```
