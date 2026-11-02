# Lastro QA heuristics

Select only the heuristics that apply to the mission and list them in the charter. Explain
relevant omissions as gaps or limitations. Each application ends with `record_test_check`
referencing the real action ids that demonstrate it. A claim without action ids does not
count as testing.

## `boundary_values`

Exercise the edges of each constrained input: minimum, maximum, just inside and just outside,
empty, whitespace only and maximum length. Prefer values from `generate_test_data` and the
limits stated in the mission or shown by the page.

## `equivalence_partitioning`

Group inputs that should behave the same and test one representative of each valid and
invalid class instead of repeating similar values.

## `crud_consistency`

Create, read, update, read again. Confirm that every saved field shows the submitted value
in the list and in the detail view, and that an update changes only what was edited.

## `persistence_after_reload`

After saving, reload the page with `navigate` to the same route and `observe` again. Values
must survive the reload, not only the in-memory state of the page.

## `field_validation`

Submit invalid formats, missing required fields and inconsistent combinations. Expect a
clear rejection before or at save time, never silent truncation or acceptance.

## `permission_tenant_isolation`

Confirm that the session sees only records of its tenant and role. Never try to bypass a
policy denial; record the observation instead.

## `error_message_quality`

Error messages must say what failed and how to fix it, next to the field when possible,
without stack traces, internal ids or sensitive data.

## `state_transitions`

Walk the allowed transitions of a record (for example active and inactive) and check that
forbidden transitions are rejected and that the current state is always shown.

## Recording outcomes

- `passed`: the behavior matched the expectation.
- `failed`: the behavior contradicted the expectation; also submit a finding with evidence.
- `inconclusive`: the check could not be completed or observed.
