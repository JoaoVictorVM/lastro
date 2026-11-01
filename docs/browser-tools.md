# Policy-enforced browser tools

Lastro exposes a Python MCP server over stdio through `lastro-browser --profile NAME`.
The caller must keep the active run owner alive. The command refuses tools without a live,
running run; it does not create or resume a mission. Executor launch orchestration is a
separate integration. Diagnostics go to stderr; stdout is reserved for MCP.

The server owns one Chromium context per run and audits calls before performing actions.
Observations contain untrusted page data and are capped at 8,000 characters. Screenshots
and finding submissions reference the run's artifact registry. Console and response events
carry the run; response action IDs are present only for an authorized, matched request.

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
the run. `end_run` records a terminal outcome and closes the browser.

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
