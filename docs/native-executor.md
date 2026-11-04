# Native executor

The native executor runs a mission with Lastro's own agent loop. It is a LangGraph graph that calls Claude through the Anthropic SDK and uses only the Lastro MCP tool server, the same server and catalog that external executors use. Policies, approvals, evidence and reports work exactly as they do with Claude Code. No browser tool is implemented twice.

## Install

The native executor is an optional extra, so the base install never needs LangGraph or the Anthropic SDK:

```sh
uv sync --extra native
# or
pip install "lastro[native]"
```

Create a project for it with `lastro init --executor native`. This writes `lastro.toml` with `executor = "native"` and a `[profiles.<name>.native]` table, `.env.example` with an empty `ANTHROPIC_API_KEY=` line, the skill bundle and `missions/`. It does not create `CLAUDE.md`, `.claude/settings.json` or `.mcp.json`, and `lastro doctor` does not look for the `claude` binary. Doctor checks that the extra is installed and that `ANTHROPIC_API_KEY` is set; it never makes a paid call.

## API key

Put the key in the `.env` file next to `lastro.toml`, or in the environment, which wins over `.env`:

```sh
ANTHROPIC_API_KEY=sk-ant-...
```

The key lives only in the `lastro run start` process. It is never written to the history, the reports, the run folder or the configuration snapshot, it is never sent to the model, and it is removed from the environment of the MCP server process. Before a run is created, Lastro validates the key with a token count request, which generates nothing and is not billed. A missing key, or a key the API rejects, stops the command with exit code 2 and this message, before any run or browser exists:

```text
ANTHROPIC_API_KEY is missing or invalid; the native executor cannot start.
```

A network failure or provider outage during that check is reported as unavailability, not as an invalid key.

## Configuration

```toml
executor = "native"

[profiles.demo.native]
model = "claude-sonnet-5-5"
max_output_tokens = 2048
cost_cap_usd = "2.00"
suite_cost_cap_usd = "30.00"
context_token_limit = 64000

[profiles.demo.native.node_models]
observe = "claude-haiku-5-5"
```

| Field | Default | Rules |
|---|---|---|
| `model` | `claude-sonnet-5-5` | Must be in the local capability and pricing registry (`claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`). An unknown model fails validation; it is never priced at zero. |
| `node_models` | empty | Keys are graph nodes only: `prepare_mission`, `consult_memory`, `observe`, `decide`, `act`, `verify`, `record`, `wrap_up`. |
| `max_output_tokens` | 2048 | Integer from 256 to 8192. |
| `cost_cap_usd` | `"2.00"` | Decimal text, positive, up to six decimal places. |
| `suite_cost_cap_usd` | `"30.00"` | Same rules; the cap of an evaluation suite. |
| `context_token_limit` | 64000 | At least 4096 and within the model window. |

`lastro run start --executor native` overrides the project executor for that run only; `lastro.toml` is not changed and the run records the executor that actually ran.

## Running a mission

```sh
lastro --profile demo run start --executor native --mission "Explore the employee list"
lastro --profile demo run start --mission-file missions/cadastro.md
```

The command prints the run id and folder, the executor line with the model, cost cap, catalog hash and bundle hash, then one line per model call and per tool call while the run progresses. The graph follows this order: prepare the mission and submit the plan, consult memory, observe, decide, act, verify, record, and decide again until the agent calls `end_run`. Exploration never starts before `submit_mission_plan` is accepted, and the canonical plan and charter are printed before the first observation. Tools run one at a time, in the order the model proposed them.

At the end it prints the status and reason, the report path, total input tokens (with cache reads and writes), output tokens and the confirmed cost. Exit codes: 0 when the run completed, 1 for any other terminal state after the run was created, 2 for configuration, key or extra problems before the run exists, 130 for Ctrl+C.

## Approval gates

A write that needs approval stops the loop: no model call and no browser effect happen while the request waits. The terminal shows the full durable request (environment, tenant, record, changes, reason, known effects, fingerprint and expiry) and asks:

```text
Decision [approve/skip/end]:
```

Only `approve`, `skip` or `end` decide anything. Any other answer asks again, and end of input or silence never approves. The decision goes through the same path as `lastro approve`, so you can also decide from another terminal with `lastro approve <id> [--skip|--end]`; without an interactive terminal the executor prints that command and waits. After approval the original call is repeated once with its `approval_id` and the server compares the fingerprint again. If the fingerprint changed, the server opens a new request and you are asked again. The model never sees or calls the approval interface, and a model-supplied `approval_proof` is rejected before dispatch. When a gate interrupts a batch of tool calls, the remaining calls are reported back to the model as not executed.

## Cost control

Every model call attempt is reserved in the run history before it is sent. The reservation counts the exact request with the provider's token counting, adds a margin of `max(1024, 10%)` tokens priced at the highest input rate (normal input or cache write), and reserves the full `max_output_tokens`. A reservation is accepted only when the confirmed cost, plus reservations still open, plus attempts with unknown usage, plus the new reservation fit within both the run cap and, when the run belongs to a suite, the suite cap. Equality is allowed. Reservations of the same suite are checked in one database transaction, so parallel workers cannot oversubscribe it.

When the next reservation does not fit, the browser owner locks writes, the run ends as `limit_reached` with reason `cost_cap`, and a partial report keeps the findings and evidence collected so far. There is no final call to the model to say goodbye. Retries (up to three after the first attempt, on rate limits, overload, server errors and timeouts, with 1, 2 and 4 second waits plus jitter and `Retry-After` respected) each need a new reservation. The SDK's own retries are disabled so no attempt happens outside the ledger. After the retries the run ends as `interrupted (api_error)`. A key revoked during the run ends it as `blocked (api_auth)`.

A reservation is released only when the provider's response proves nothing was generated (an HTTP rejection such as 429 or 529). An attempt that was sent and got no answer, timed out or was cancelled with Ctrl+C keeps its full reservation as *uncertain*. The summary and the report show the confirmed cost and, when there is uncertainty, the conservative committed amount and the number of attempts whose usage is unknown; unknown usage is never shown as zero. If the reported usage of an attempt is above its reservation, Lastro stores the real values, flags `budget_accounting_violation`, makes no further calls and ends the run as `limit_reached`.

### What the cap does not guarantee

The cap is enforced by this client. It guarantees that no attempt is sent without a reservation that fits, that uncertain charges never give credit back, and that no call is made after the budget is exhausted or the accounting diverges. It cannot guarantee the final invoice: token counting is the provider's estimate, prices can change outside Lastro, and a request whose answer was lost may still be billed. Costs shown by Lastro are computed from reported usage and the pricing table frozen in the run (`native_run_contexts.pricing_json`); they are not a reconciled invoice. For a hard spending limit, also set a limit in your Anthropic account.

## Prompt caching

The executor instructions, the skill bundle and the tool catalog form a stable prefix marked for the 5 minute ephemeral cache. Reservations never assume a cache hit. Mission text, page content and memory results are always sent as user data inside their envelopes and never become system instructions.

## Failure handling

| Situation | Result |
|---|---|
| Tool not in the catalog, invalid JSON or arguments, repeated tool ids, truncated response | Recorded as `agent_failure`, never sent to the server, never a product finding. Three invalid responses in a row end the run as `interrupted (agent_protocol_error)`. |
| Message history above `context_token_limit` | `interrupted (context_limit)` with a partial report; nothing is truncated or summarized silently. |
| MCP session lost | `interrupted (tool_transport_error)`; no tool is replayed. |
| More than 512 graph transitions | `interrupted (graph_limit)`. |
| Ctrl+C | `cancelled (user_cancelled)`, partial report, exit code 130. |
| Run ended from another terminal | The executor reads the terminal state and respects it. |

Every ending goes through the browser owner: it stops admitting actions, closes video capture and the browser, seals criterion results, records the terminal state and builds the final or partial report. If the owner does not answer, the executor stops the MCP server, waits for it to exit and then finishes the run and the partial report locally. A report failure never changes the run status.

## Telemetry

Each attempt is a row in `native_model_calls` and has `model_call_started`, `model_call_completed` or `model_call_failed` events in the run log, with node, step, attempt, model, token counts by category (input without cache, cache read, cache creation, output), cost, reservation, latency, request id and tool call names. Prompts, responses and reasoning are never stored. `lastro tail` shows these events inline. `lastro.native.telemetry.model_calls` returns one record per attempt with nullable usage, for the observability exporter planned for a later release; no exporter or collector is part of this executor.
