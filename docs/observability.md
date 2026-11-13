# Observability and tracing

Lastro can export each run as one OpenTelemetry trace to [Langfuse](https://langfuse.com), self-hosted with the bundled Docker Compose stack or on Langfuse Cloud. The run history stays the source of truth: spans are derived from events already committed to SQLite, telemetry failures never change a run's status or its policy decisions, and nothing is sent unless the profile opts in.

## Install

Tracing needs the optional `obs` extra. The base installation never loads the OpenTelemetry SDK.

```sh
uv sync --extra obs
# or
pip install 'lastro[obs]'
```

## Enable it per profile

Observability is off by default. A missing block means `enabled = false`, and neither `OTEL_*` variables nor Langfuse keys in the environment turn it on.

```toml
[profiles.homolog.observability]
enabled = true
endpoint = "http://localhost:3000/api/public/otel/v1/traces"
ui_url = "http://localhost:3000"
langfuse_project_id = "<project id shown in the Langfuse URL>"
public_key_env = "LANGFUSE_PUBLIC_KEY"
secret_key_env = "LANGFUSE_SECRET_KEY"
```

| Field | Rule |
|---|---|
| `enabled` | strict boolean, default `false` |
| `endpoint` | required when enabled; absolute URL ending in `/api/public/otel/v1/traces`, without user info, query or fragment |
| `ui_url` | required when enabled; same origin as `endpoint`; an installation prefix is kept |
| `langfuse_project_id` | required when enabled; 1 to 128 characters among `A-Z a-z 0-9 _ -` |
| `public_key_env`, `secret_key_env` | names of the environment variables with the keys; must differ |

HTTPS is required, except plain HTTP on the literal hosts `localhost`, `127.0.0.1` and `[::1]`. Redirects are never followed, so the Basic Auth header never reaches another host, and TLS verification is never disabled. Arbitrary headers are not accepted.

Put the keys in `.env` next to `lastro.toml`. They are read only by the Lastro processes (CLI, MCP browser owner, native runner), never stored in the history, the traces or the reports, and removed from the environment given to external executors.

With `enabled = true`, `lastro run start` and `lastro run resume` stop with exit code 2 before creating the run when the extra is missing (`obs_extra_missing`) or a key variable is empty (`obs_credentials_missing`). Network availability is never part of this check.

## What gets exported

When a run is created, its destination (`endpoint`, `ui_url`, `langfuse_project_id`) is frozen with it. Editing `lastro.toml` later never redirects that run. Turning `enabled` off in the current profile suspends new sends for every run (`obs_suspended`) without deleting anything; a different destination is refused (`obs_destination_mismatch`) until the original one is restored.

- One trace per run. Resumed runs and reproduction child runs get their own trace, with `lastro.origin.run_id` pointing at the origin.
- One span per committed event, including events written internally (approvals, memory consolidation, run end). The root span is `run_started` and covers the whole run; it is exported when the run ends. Every other span is a direct child of the root: Lastro does not invent causal nesting.
- Span names are `<stage>.<event type>[.<tool>]`. Stages: `mission_preparation`, `memory_lookup`, `observation`, `action`, `policy_evaluation`, `approval`, `verification`, `reproduction`, `consolidation`, plus `model_call`, `lifecycle` and `knowledge` for support events.
- A tool call denied by policy or waiting for approval has `lastro.operation.status = not_executed`.
- Durations are reported only when measured or derived from paired records (root interval, model call latency, approval request to decision, reproduction start to verdict). Other spans are instants with `lastro.duration.status = unavailable`.
- Every span repeats `lastro.run.id`, `lastro.executor`, `lastro.stage`, `langfuse.trace.name = Lastro run`, `langfuse.trace.tags` (executor, stage, decision) and `langfuse.trace.metadata.run_id`, so you can filter by stage or decision in Langfuse.

### Model data

For the `native` executor, the terminal event of each model call attempt (`model_call_completed` or `model_call_failed`) carries the model, node, step, attempt, latency, and, when the usage was reported, a Langfuse generation with exclusive token buckets (`input`, `input_cache_read`, `input_cache_write`, `output`) and the realized cost in USD. Reserved budget is only metadata, never cost. Unknown usage stays unknown (`lastro.model_data.status = partial`), never zero.

For `codex` and `claude-code`, every span carries `lastro.model_data.status = unavailable` and `lastro.model_data.reason = "executor does not expose it"`. Lastro never reads transcripts, terminals or personal files to estimate usage.

### Privacy boundary

Only a closed list of attributes leaves Lastro, after the same redaction used by the history: identifiers, sequence numbers, enums, validated codes, the argument hash and numeric usage. Mission text, typed values, arguments and previews, free-text reasons, target URLs, headers, cookies, tenant names, provider request ids, prompts, model responses, DOM content, memory content, screenshots and videos are never exported. No `traceparent` or baggage is propagated to the system under test, and no global tracer provider or automatic instrumentation is installed.

The descriptor of each span is stored in the local history before sending, so you can audit exactly what was or will be exported.

## Delivery and recovery

The MCP browser owner exports while the run is active: it polls the history every second and sends batches of up to 128 spans, with a 5 second budget per batch. One lease per run (15 seconds, renewed every cycle) keeps a single exporter active across processes. When the run ends, the owner, the native runner, the Codex launcher and the reproduction round drain for up to 5 seconds.

- Timeouts, network errors and HTTP 429, 502, 503 or 504 keep the spans pending with backoff (1, 2, 4, 8, 16, 32, then 60 seconds), honoring `Retry-After`.
- HTTP 401 or 403 and other definitive answers block the export (`export_unauthorized`, `export_rejected`) without retry loops.
- A response that rejects part of a batch marks the whole batch `rejected` (`export_partial`): it is kept for diagnosis and never resent, because OTLP does not say which spans were accepted.
- Delivery is at least once. A crash between the remote accept and the local acknowledgment resends the same trace and span ids.

Pending spans stay in the history until delivered; nothing is dropped silently.

```sh
lastro obs status [run-id] [--json]   # local state only: no network, SDK or Docker
lastro obs export <run-id> [--json]   # one drain of up to 5 seconds; never touches the target
```

`obs export` exits 0 when the current spans were delivered (or only the root of an active run is waiting), 1 when spans are still pending, were rejected or the export is blocked, and 2 when the run has no observability opt-in (`obs_disabled`) or the configuration is invalid. Runs created without opt-in are never exported retroactively.

## Local Langfuse stack

```sh
lastro obs up
```

`obs up` needs Docker with Compose v2 (not the `obs` extra). It writes the bundled Compose file and a private `langfuse.env` with random secrets into the project's private data folder (`<private data root>/<project id>/observability/langfuse/`), starts the stack under a name derived from the project id and waits up to 120 seconds for the UI. Only the Langfuse UI and OTLP endpoint are published, on `127.0.0.1:3000`; the database, ClickHouse, Redis and MinIO stay on an internal network. Running it again reuses the same secrets and volumes; a timeout or failure never removes data. A port already in use is reported (`obs_port_in_use`) instead of stopping other processes.

`obs up` does not create users, projects or keys, and does not change any profile. After it prints the URL:

1. Open the URL, sign up and create an organization and a project.
2. Create an API key pair in the project settings and put it in `.env`.
3. Fill `[profiles.<name>.observability]` with the endpoint, UI URL and project id, and set `enabled = true`.

Images are pinned by version and digest; their provenance is in `src/lastro/observability/resources/README.md`. Docker output goes to a private, redacted `docker.log` in the same folder.

## Reports

`lastro report build` (and the final and partial reports written at the end of a run) include a private link `<ui_url>/project/<project id>/traces/<trace id>`, computed locally from the frozen destination. Building or reading a report never makes a network call, and the link does not claim that every span was ingested: use `lastro obs status`. Public exports (`lastro report export --public`) never contain the trace section, even for profiles marked `public = true`, and the link never makes a trace public or skips the Langfuse login.

## Evaluation labels

A trusted caller (the future evaluation runner) can attach `suite_id` and `condition` to a run when creating it; agents and missions cannot set them. They are repeated on every span as `langfuse.trace.metadata.suite_id` and `langfuse.trace.metadata.condition`, and inherited by resumed runs and reproduction attempts, which keep their own trace ids.
