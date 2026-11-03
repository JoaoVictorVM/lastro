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
authorize seed records. Its create binding reads the new employee id from the demo's
same-origin `Location` header, and `demo_test_data()` adds the detail link and cleanup text.

Edits require a record created by a matched action in the current run or an exact human
preauthorization tuple. Unknown origins return `require_approval`: the action is not
executed or queued, and the human approval gate described below records a request instead. A creation binding may identify its result using `response_id` (a JSON
key path) or `location_id` (the final segment of a same-origin Location header), never
both. Without an unambiguous result, subsequent edits still require approval.

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
the run. `end_run` stops a pending video, drains captured signals, closes the browser, marks
unevaluated criteria and records a terminal outcome; the report is generated afterwards.

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

## Test data and created records

`generate_test_data` returns fictional Brazilian values without opening the browser or
contacting the target. It is audited like every tool and requires a running run. Use exactly
one mode:

- `{"entity": "employee"}` returns `name`, `cpf`, `email`, `phone` and `hire_date`, with
  `variant` `valid` (default) or `invalid_cpf`.
- `{"type": "..."}` returns one `value` for `name`, `cpf` (`valid` or `invalid`), `email`,
  `phone`, `date` (`past`, `today`, `future` or `boundary`) or `text` (`length` from 1 to
  500). A `boundary` date takes `boundary_date` (ISO) and `offset_days` of -1, 0 or 1.

Names start with `LASTRO-<last 4 characters of the run id>-` and emails use
`lastro+<run id>@example.com`, so test records are recognizable in the target system.
Valid CPFs have correct check digits; invalid ones keep the shape and break a check digit.
Phones use the `(DD) 9XXXX-XXXX` format and employee hire dates fall within the past year.
Dates use the project timezone. Responses include `generation_index` (the position of the
call among the run's `generate_test_data` calls) and `reference_date`; the same run, index
and request reproduce the same values. Choice fields such as role and status still come from
the page or from the binding's `field_values`. Unsupported types return
`Unsupported test data type '<type>'. Supported: name, cpf, email, phone, date, text.`

Every 2xx response to a POST matched to a `create` binding is stored as a created record,
together with a `record_created` event in the same transaction. The identifier comes only
from the configured `response_id` or `location_id`; Lastro never reads it from the page or
from the agent. When extraction fails, the record is stored as `unknown` with the warning
`identifier_not_extracted`, and it never counts as created in the run for later edits.
A 2xx status other than `success_status` is tracked with `unexpected_success_status`.
Redelivery of the same action's response is idempotent.

Set `response_tenant` (a JSON key path) on the request binding when the create response
exposes the tenant. A different tenant is recorded as observed, engages the write lock and
blocks the run with `Record created in unexpected tenant '<x>'. Writes stopped; verify and
clean up manually.`

Per-entity settings add a detail link and the manual cleanup procedure:

```toml
[profiles.staging.test_data.entities.employee]
detail_path = "/employees/{id}"
cleanup_instructions = "Remove the employee from its detail page."
cleanup_required = true
```

The entity must be in `allowed_entities` and have a `create` binding. `detail_path` is a
same-origin path with exactly one `{id}` segment; queries, fragments and other placeholders
are rejected. Records start `pending`, or `not_required` when `cleanup_required = false`.

`lastro data pending [--profile NAME]` lists pending records, oldest first, with their
tracking id, run, profile, tenant, entity, system id, creation time, detail URL and cleanup
procedure. `lastro data mark-cleaned <tracking-id>` records that a person cleaned the record
up outside Lastro, setting `cleaned_manually` and the timestamp; repeating it is harmless and
`not_required` records cannot be marked. Lastro never deletes, deactivates or changes
records in the target system, and these commands make no network request.

`HistoryStore.list_created_records(run_id=...)` returns the typed list (entity, system id,
tenant, run, creation time, cleanup status, detail URL, instructions and warnings) that the
report's created-records section lists. `unidentified_warning(run_id, records)` builds the
aggregated message `N created record(s) could not be identified automatically; check the
target system for names starting with LASTRO-<id>.`

## Mission preparation

Runs started with `lastro run start` record the content hash of the local skill bundle in
`skills/lastro/`. In those runs, `observe`, `navigate`, `click`, `fill`, `select`, `submit`,
`screenshot`, `wait_for`, `generate_test_data`, `report_finding`, `start_evidence_video` and
`stop_evidence_video` return
`mission_plan_required` until a plan exists, and `clarification_required` while the active plan
has a blocking gap. The denial is audited like any other call and no browser is started.
`mission_context`, `submit_mission_plan`, `ask_clarification`, `record_test_check` and
`end_run` stay available. Every successful response carries the `action_id` of the call.

`mission_context` takes no arguments and returns the mission source, the content wrapped in
`<mission-content>`, its SHA-256, the bundle hash and `plan_status` (`missing`, `active` or
`blocked`). File missions are read from the private snapshot and checked against its hash.
Mission text defines what to test; it never configures tools, permissions, policies or
approvals.

`submit_mission_plan` accepts `objectives` (1 to 10), `scope` (up to 30), `criteria` (up to
50, each with `text` and `source` `mission_explicit` or `clarification`), `gaps` (up to 20,
each with `description` and `blocking`), `out_of_scope` (up to 30) and a `charter` with
`target`, `resources`, `information`, `timebox_minutes` (default 20, capped by the run's
`max_minutes` or 30) and `heuristics`. Items are 1 to 1,000 characters; repeated list items
are merged after whitespace normalization and repeated criteria are rejected. Accepted
heuristics are `boundary_values`, `equivalence_partitioning`, `crud_consistency`,
`persistence_after_reload`, `field_validation`, `permission_tenant_isolation`,
`error_message_quality` and `state_transitions`. The response holds the plan id, revision,
criterion and gap ids, the canonical charter, `exploration_allowed` and a `display` text to
show before exploring. Resubmitting the active plan is idempotent. A revision needs
`revision_reason`; after exploration starts it must be `clarification` with the id of an
answered clarification. Revisions are immutable and the highest one is active; identical
criteria keep their ids.

`record_test_check` takes `heuristic`, an optional `criterion_id` of the active plan,
`outcome` (`passed`, `failed` or `inconclusive`), 1 to 20 `action_ids` and a `note` of up to
2,000 characters. Each id must be an allowed call of the same run and at least one must be a
browser action. The policy event of the call stores the heuristic, outcome and action
references, so evaluations can count checks without reading model reasoning. It records the
check only; it neither creates a finding nor marks a criterion.

`ask_clarification` takes `gap_id` (a blocking gap of the active plan), `question`,
`why_it_matters` and 2 to 5 `options`. Without a human channel it returns
`clarification_channel_unavailable`, stores nothing and keeps the browser blocked. With a
channel, the question is stored, `clarification_requested` is appended and the run waits in
`awaiting_clarification` until the answer returns it to `running`. Answers are stored as
user-provided information, never as approvals, and change no policy or lock.

## Findings, criteria and evidence

`report_finding` takes `title` (up to 120 characters), `flow` (up to 200), `preconditions`
(up to 20), `steps` (1 to 30), `expected` with `text`, `source` (`mission_criterion`,
`documentation`, `user_answer`, `heuristic` or `common_convention`) and `source_ref`,
`observed`, `evidence` (up to 20 items with an `artifact_id` of this run and a `purpose`:
`state`, `before`, `after`, `error` or `timing`), `classification`, `limitations`,
`failure_domain` (`system`, `agent` or `tool`) and an optional `severity_suggestion` (`low`,
`medium`, `high` or `critical`). Texts are limited to 4,000 characters. A `mission_criterion`
source needs the id of a criterion of the active plan in `source_ref`. Missing required fields
return `missing_finding_fields` with `Missing finding fields: <sorted list>.`; evidence from
another run or an unknown id returns `Evidence must belong to this run.`. The agent can only
submit `suspected` or `inconclusive`; `confirmed` returns `classification_not_allowed`.
Environment, tenant, system version and origin come from the run and cannot be sent. The
finding, its steps, its evidence links and `finding_submitted` are stored in one transaction;
an identical submission returns the existing `finding_id` with `created: false`.

Confirmation is an internal transition used by independent reproduction: it needs at least one
evidence reference (`A confirmed finding needs at least 1 evidence reference.`) and a
reproduced attempt recorded from another run. Findings in the `agent` or `tool` domain are
harness problems and appear only in the report's agent and tool failures section.

`record_criterion_result` takes a `criterion_id` of the active plan, `outcome` (`met`,
`not_met`, `blocked` or `inconclusive`), `evidence_artifact_ids`, `note` and `reason`. `met`
needs at least one evidence reference of the expected behavior and a note describing it;
otherwise the call returns `positive_evidence_required`. Absence of errors is never enough.
Criteria outside the active plan return `criterion_not_active`. Repeating the current result
is idempotent; a change creates a new immutable revision. When the run ends, every criterion
without a result is recorded as `inconclusive` with reason `not_evaluated`.

`start_evidence_video` takes a `reason` and returns a `recording_id` with
`max_duration_seconds` 60. Recording rotates the browser context: the storage state is kept
in memory, the current URL is restored and the tenant is verified again before any other
action runs. `stop_evidence_video` takes the active `recording_id` and returns the WebM
`artifact_id`, `sha256`, duration and stop reason. After 60 seconds the same stop runs on its
own and a later `stop_evidence_video` returns that result. Stable errors are
`video_already_active`, `video_not_active`, `video_session_restore_failed` and
`tenant_reverification_failed`; a failed verification keeps writes locked and discards the
capture.

## Reports

After `run_ended`, `end_run` builds `runs/<run id>/report.md` from the history database and
the artifact registry only, and returns `report_path`, `report_status` and a five-line
summary (status, criteria met, confirmed, suspected and records pending cleanup). Sections:
summary, mission and charter, environment and versions, criteria results, confirmed findings,
suspected and inconclusive findings, oracle signals with their triggering actions, agent and
tool failures, blocks and pending items, created records and cleanup, memory outcome and
limitations. Untrusted text is escaped so it cannot add headings, links or images.

Each confirmed finding gets a bug draft at `runs/<run id>/issues/<finding id>.md` with title,
environment, steps, expected, observed, evidence and severity suggestion. Drafts are never
published. Reports and drafts are immutable artifacts with SHA-256: generating again with the
same inputs returns the same files, and different inputs fail instead of overwriting. A
missing evidence file is reported as `⚠ evidence missing: <path>` and marks integrity as
invalid. A template failure writes the raw data to `report.json`, records the error and leaves
the run state untouched. `ReportBuilder.build_partial(run_id, reason)` writes
`report.partial.md` for runs stopped early.

`lastro report verify <run id>` recomputes size and SHA-256 of every registered artifact and
changes nothing. It exits 0 when all match, 1 for an unknown run and 2 when a file is missing
or changed, printing `Artifact <path> was modified after the run (expected <hash>)` for each
changed file.

`lastro report export --public <run id>` needs a ready report and writes `report.public.md`.
Unless the run's profile was marked `public = true` (for targets without real data, such as
the demo), the export omits tenant names, URLs, record ids, artifact ids, `LASTRO-*` names,
screenshots and videos, and redacts known run values in free text.

## Human approval gates

A `require_approval` decision opens a gate; `deny` is never elevated. The first attempt
records the request, an `approval_requested` event and the run state `awaiting_approval`
before anything reaches the target, then returns:

```json
{
  "decision": "awaiting_approval",
  "approval_id": "<action id>",
  "fingerprint": "<64 hex>",
  "expires_at": "2026-11-02T18:15:00.000000Z",
  "summary": "edit employee #57 (phone, email) in Clínica Aurora",
  "command": "lastro approve <action id>"
}
```

The request shows the run, environment, tenant, record, action, ordered field changes
(`old -> new`, with `unknown` when the old value is not observable), the agent's `reason`,
known effects and the fingerprint. A run holds at most one pending request; while it waits,
only `mission_context`, `approval_status` and `end_run` run, and other tools answer
`approval_pending`.

Decisions happen outside the model:

- `lastro approve <id>` approves, `--skip` skips and `--end` cancels the run, engages the
  write lock and builds the partial report. `--note` adds up to 2,000 characters. Exit codes
  are 0 on success, 1 for operational errors and 2 for invalid usage. An unknown, expired or
  already decided id prints `No pending approval with id <id>.` and changes nothing.
- With Claude Code, retrying a write with `approval_id` makes the PreToolUse hook answer
  `ask` with the full request. When the user accepts, the hook's updated input carries a
  60-second, single-use proof signed with the browser owner's private secret, and the server
  records `decided_by=executor_prompt`. The model never sees the secret; an `approval_proof`
  sent by the model is denied. Without a pending request or owner channel, the hook does
  not approve and the CLI remains the decision channel.

`approval_status` waits up to 30 seconds per call and reports `pending`, `approved`,
`skipped`, `ended`, `expired`, `invalidated`, `consumed` or `execution_failed`. It never
decides or executes.

After approval, the agent retries the same call with `approval_id`. The server rebuilds the
action from the live page and recomputes the fingerprint over run, tenant, route, target,
entity, record, operation, method, origin, old and new values and a digest of the form's
original and current values. Reason and known effects do not change identity. A match
consumes the approval in a conditional update before the effect, so one approval executes
exactly once; a failure after consumption becomes `execution_failed` and is never replayed.
A mismatch invalidates the approval and raises a new request with
`The action changed after approval (<field>); approval required again.`, where the field is
the first of tenant, target, record, operation, fields, values and page_state that differs.
If the retry is denied by policy, the denial wins; an ordinary allow does not use the
approval.

A request without a decision expires after 15 minutes as `skipped (no response)`, decided by
`system`, and the run continues. Expiry is reconciled by a timer, on every tool call and
status read, and when the history opens, so a crash never extends an approval. Approvals are
stored only in `approvals`; clarifications stay in their own table and no decision becomes
knowledge. The evaluation harness can decide with `decided_by=scripted` through the same
history interface, bound to the same fingerprint.

The browser server tries a desktop notification within 5 seconds of each request
(PowerShell toast on Windows, `osascript` on macOS, `notify-send` on Linux), and the
executor's Notification hook repeats it for permission prompts. Set
`LASTRO_DESKTOP_NOTIFICATIONS=off` to disable it. Every attempt is recorded with its
latency; when delivery fails, `notification_degraded` is recorded and `lastro tail` remains
the fallback: it prints an `APPROVAL REQUIRED` banner with the summary and command, rings
the terminal bell once for a request that is still pending when stdout is a TTY, and shows
outcomes such as `approved by user at 14:32` or `skipped (no response) at 14:47`.

## Persistent memory

Lastro keeps structured facts about the target in the project history, so a later run can
start from what an earlier run observed. Facts are context, never authority: they never
change policies, approvals, locks or the mission plan, and the agent must confirm them in
the current interface before relying on them.

### Facts, sources and states

The agent proposes typed facts with `knowledge_propose`; free text from the page is never
stored as a statement. The statement is rendered from the fact.

| Fact | Category | Fields |
|---|---|---|
| `field_property` | `screen_component` | `subject`, `field`, `attribute` (`required`, `format`, `visible`, `enabled`) and `value` |
| `screen` | `screen_component` | `route` and up to 20 `landmarks` (role and quoted label) |
| `flow_step` | `flow` | `origin`, `destination` and `action` (`navigate`, `click`, `submit`) |
| `clarification` | `rule_clarification` | `gap_id` and `subject`; the answer is read from the human answer |
| `experience` | `experience` | `attempt`, `outcome` (`success`, `failure`, `blocked`, `not_found_in_context`), `context` with `route` and `observation_id`, optional quoted `message` |

Keys (`flow`, `subject`, `field`) use 1 to 80 characters in `[a-z0-9_.-]`. Quoted labels and
messages have 1 to 199 characters. Every observation fact cites 1 to 10 `observation_ids` of
the same run, and routes must match those observations: `observe` returns an
`observation_id` and stores a projection with the route, title, landmarks and field
properties, never typed values. A `clarification` fact needs the `clarification_id` of an
answer written through the human channel; the agent cannot claim the `user` source.

States are `observed`, `user_provided` and `contested`. Sources are `observation`, `user`,
`documentation`, `code` and `issue`; the last three are reserved for trusted adapters, and a
code source is always recorded as inferred implementation.

Proposals that address the agent, such as `ignore previous instructions`, are refused with
`Knowledge proposals must be facts about the system, not instructions.` and audited as
`possible_injection` with a hash, without the raw text. This check is an extra layer: the
closed schemas, delimited content and deterministic policies are what keep memory inert.

### Scope and isolation

Facts proposed by the agent are scoped to the run's profile, environment (canonical base URL)
and tenant. A `blocked` experience depends on who is logged in; without an observable account
it stays visible only to the run that produced it. Broader scopes exist only through human
action.

### Consulting memory

`knowledge_query` takes `flow`, an optional `query` (up to 200 characters, matched as
literal terms with SQLite FTS5 and BM25) and `limit` (1 to 10). The response carries at most
10 entries and 4,000 characters of serialized entries inside `<untrusted-content>`; whole
entries are left out when they do not fit, and `truncated` says so. Each entry includes its
id, revision, statement, state, source, scope, system version, last verification,
`stale`, `stale_reasons`, `version_unknown`, `requires_revalidation` and `conflict_ids`.

An entry is stale when it was never verified, when the last verification is more than 30
days old (exactly 30 days is not stale) or when the recorded system version differs from
the run's version. A missing version is reported as `version_unknown` and still requires
revalidation. `contested` is never safe, whatever its age.

Each run records the exact revision it read. Human changes to an entry that a running run
uses are deferred until that run ends.

### Revalidation

`knowledge_revalidate` takes `entry_id`, `expected_revision`, an `observation_id` from the
current run and an `idempotency_key`. Screen landmarks and the `required`, `visible` and
`enabled` field properties are checked in code; a match records a new verification and the
observation as evidence. A divergence returns `mismatch` with a pending alternative proposal
and keeps the original entry. Other facts return `manual_verification_required`, and an
incomplete projection never proves absence.

### Consolidation and memory outcome

Proposals stay pending until the run ends. On the terminal transition Lastro deduplicates by
scope, fact key and value hash, adds evidence to equal facts and marks every incompatible
alternative `contested`; no source wins automatically. A single failure stays a contextual
experience. Cancelled runs and runs whose owner process died never promote pending
proposals.

Every run that ends after this version has exactly one memory outcome: `updated`,
`nothing_new`, `consolidation_pending` or `persistence_failed`. `end_run` returns it as
`memory_outcome`, and the report shows it with the pending count. A memory error never
changes findings or verdicts and never keeps the browser open. When the terminal write
fails, the confirmed proposals go to `runs/<run id>/knowledge_pending.json`, which is
imported once before the next run. `lastro knowledge consolidate <run id>` retries a
pending consolidation; the outcome row is updated, the previous result stays in the attempt
log, and the next report is written to a new file (`report.memory-r2.md`) without
overwriting the earlier one. Runs from before this version show `not recorded`.

### Human management

| Command | Effect |
|---|---|
| `lastro knowledge list [--flow] [--state] [--stale] [--page] [--page-size] [--json]` | Statements with state, source, scope and last verification; contested entries list their alternatives |
| `lastro knowledge show <id> [--json]` | Fact, evidence, versions, history, alternatives and deferred changes |
| `lastro knowledge edit <id> --file <json> --expected-revision <n> --reason <text>` | New human revision; `discard_alternatives` in the file resolves a conflict |
| `lastro knowledge discard <id> --expected-revision <n> --reason <text>` | Logical discard; the entry leaves search and stays auditable |
| `lastro knowledge revalidate <id> --expected-revision <n> --reason <text> [--system-version <v>]` | Dated human attestation without opening a browser |
| `lastro knowledge consolidate <run id>` | Idempotent retry for a terminal run |

The edit file holds only `fact` and optionally `discard_alternatives`; source, run, dates and
evidence cannot be changed. Every effective change appends to the entry history with actor
and reason. An outdated `--expected-revision` exits 1 and prints the current revision.

`lastro doctor` fails the storage check when the SQLite used by Python lacks FTS5; there is
no fallback search.

## Executor configuration and current boundaries

`lastro init` preserves existing files and generates `.mcp.json` plus Claude Code settings.
Generated settings deny local file tools, shell tools, modifications and web tools. A
PreToolUse hook allows only the Lastro catalog, including when other MCP server names are
present. This deliberately blocks all local reads rather than relying on path patterns
for secrets and symlinks. The same hook handles the Notification event. Install the package
so both `lastro-browser` and `lastro-permission-hook` are available on PATH. `lastro init`
updates settings identical to a previously published version; locally edited settings are
preserved and init reports which hooks are missing. These settings are executor controls,
not OS isolation.

Clarification reports unavailability while no human channel is connected and still
validates that the gap is blocking. Page markers and deterministic guards
are tested with Chromium. End-to-end agent behavior for prompt injection and actual
executor permission denials remain separate acceptance checks; model-written statements
about denied tools are not accepted as evidence.
