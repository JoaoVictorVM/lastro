# Policy-enforced browser tools

Lastro exposes a Python MCP server over stdio through `lastro-browser --profile NAME`.
The caller must keep the active run owner alive. The command refuses tools without a live,
running run; it does not create or resume a mission. Executor launch orchestration is a
separate integration. Diagnostics go to stderr; stdout is reserved for MCP.

The server owns one Chromium context per run and audits calls before performing actions.
Observations contain untrusted page data and are capped at 8,000 characters. Screenshots
and finding submissions reference the run's artifact registry. Console and response events
carry the run. Response policy events use the matched write request as their provenance;
oracle action references use the temporal window described below.

## Configure writes explicitly

Profiles without bindings allow observation and navigation, and deny unclassified writes.
A write binding maps the page route, semantic target, form, business fields and exact network
request. Ambiguous bindings, extra request fields and mismatched values are denied. Bindings
use literal path segments and named parameters such as `/employees/{id}/edit`.

```toml
[profiles.staging.policy]
version = 1
auxiliary_origins = []
rules = []
preauthorized_records = [{tenant = "test", entity = "employee", id = "42"}]

[[profiles.staging.policy.bindings]]
id = "employee-update"
entity = "employee"
operation = "update"
route = "/employees/{id}/edit"
target = {role = "button", name = "Save"}
form = "employee-form"
fields = {email = "email"}

[profiles.staging.policy.bindings.request]
method = "POST"
path = "/employees/{id}"
encoding = "form"
```

The entity must also appear in `allowed_entities`, and every business field in
`editable_fields`. Configure every submitted business field, including unchanged fields.
`technical_fields` are excluded from the action fingerprint and cannot be filled through
business tools. `field_values` restricts mapped HTML fields to literal allowed values, for
example `field_values = {status = ["active"]}` to prevent deactivation through a select.
The package's `lastro.browser.demo.demo_policy()` provides demo form bindings; it does not
authorize seed records or claim creation proof from the demo's HTML-only create response.

Edits require a record created by a matched action in the current run or an exact human
preauthorization tuple. Unknown origins return `require_approval` without executing or
queuing the action. A creation binding may identify its result using `response_id` (a JSON
key path) or `location_id` (the final segment of a same-origin Location header). Without
an unambiguous result, subsequent edits still require approval.

Rules use ordered first-match evaluation after mandatory safety checks. Match properties
are `operations`, `entities`, `fields`, `record_origins` and `methods`, with nonempty lists.
Properties combine with AND; alternatives within a list combine with OR. A fields match
must cover every changed business field. Unknown rules and operators fail validation.
Legacy endpoint patterns ending in `/*` conservatively block the entire subtree, as does
`/**`. Ambiguous paths fail closed.

## Network and tenant protection

Unbound POST, PUT, PATCH and DELETE requests are blocked, including page JavaScript.
The guard is installed at context scope, service workers are disabled, and WebSockets and
downloads are blocked. Auxiliary origins permit HTTPS resource reads only, never navigation
or writes. The auxiliary transport validates every resolved address and pins each TLS
connection to a validated public IP while validating the original hostname. It forwards
neither the browser cookie jar nor authorization headers. Redirects stay on the approved
origin. Private, mixed, mapped, reserved and rebinding destinations fail closed.

Tenant verification runs before writes. Authentication failure, limits and history failure
have independent lock reasons. `lastro run reverify-tenant` asks the live server through an
owner-only authenticated local channel to verify again. It removes only the tenant lock,
never a limits or history lock. Missing servers are reported without launching a browser.

The first page crash in five minutes recreates the context and verifies the session; the
caller must observe again and the failed action is never retried. A second crash blocks
the run. `end_run` drains captured signals, closes the browser and records a terminal outcome.

## Automatic oracle signals

The exploration context captures console errors and unhandled exceptions as `js_error`,
HTTP 400-499 responses as `http_4xx` (except 401/403), HTTP 500-599 as `http_5xx`, and
transport failures without an HTTP response as `request_failed`. Authentication responses,
guard aborts and their identifiable Chromium diagnostics are excluded. Login and tenant
verification browsers remain private. A signal records an observation, not a confirmed bug.

Each signal references the latest dispatched navigate, click, fill, select or submit on
the same page within an inclusive five-second window. Popups inherit their opener's action
until their first own action. Background signals have no action. This timing is not proof
of causation; observe, denied actions, screenshots and waits do not replace the trigger.

Repeated type, URL path and redacted message aggregate within a run, with a stable signal
ID and occurrence count. Every occurrence has an append-only event; duplicate delivery of
one occurrence does not increment the count. URLs omit userinfo, query and fragment.
Messages are redacted before truncation and hashing. History failure blocks continuation.

Configure known noise before starting a new run:

```toml
[profiles.staging.oracles]
ignore = [{url_path = "/analytics/*"}, {message = "*Known noisy widget*"}]
```

Rules combine with OR and match the full sanitized path or message, case sensitively on
all platforms. `*` matches any characters including `/`, and `?` matches one character.
Other characters are literal; classes and backslash escapes are rejected. Each rule has
exactly one property, patterns contain 1-256 characters, paths start with `/`, and at most
100 distinct rules are accepted. Matching occurrences create neither aggregates nor events.

The next `observe` returns `oracle_signals` inside its untrusted JSON content, containing
the five most recently updated aggregates, newest first. Empty runs return an empty list.
Observations do not consume signals. Summaries include the stable signal ID, type, path,
method, status, message, timestamp, action ID, actual tool event sequence and count.
Paths and messages are shortened to at most 256 characters, with `...` marking shortening.
The snapshot shrinks to keep complete JSON and untrusted markers within 8,000 characters;
`truncated` indicates shortened content. Full values remain in history.

`lastro tail` shows each occurrence, for example
`ORACLE http_5xx PUT /api/employees/12 -> 500 (after action #34) occurrences: 2`.
Uncorrelated events show `no action (background)`; external terminal controls are removed.

`HistoryStore.list_oracle_signals(run_id, limit=None)` exposes typed aggregates, including
first and last occurrence metadata and the last event ID, even after the run ends. This is
the durable input for a future report section. Report generation and findings tagged
`source: oracle` are not implemented by the collector; signals do not create findings.

## Executor configuration and current boundaries

`lastro init` preserves existing files and generates `.mcp.json` plus Claude Code settings.
Generated settings deny local file tools, shell tools, modifications and web tools. A
PreToolUse hook allows only the Lastro catalog, including when other MCP server names are
present. This deliberately blocks all local reads rather than relying on path patterns
for secrets and symlinks. Install the package so both `lastro-browser` and
`lastro-permission-hook` are available on PATH. Existing settings need manual reconciliation;
init does not overwrite them. These settings are executor controls, not OS isolation.

Human clarification, approval execution, generated test data and knowledge tools report
unavailability until their services are integrated. Page markers and deterministic guards
are tested with Chromium. End-to-end agent behavior for prompt injection and actual
executor permission denials remain separate acceptance checks; model-written statements
about denied tools are not accepted as evidence.
