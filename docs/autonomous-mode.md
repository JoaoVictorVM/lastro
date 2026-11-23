# Autonomous mode

Lastro can explore a system without anyone starting the run: on a daily schedule, after a successful deployment reported by GitHub Actions, or from a manual headless command. Every trigger becomes a durable item in a per-profile queue, a short worker drains the queue, and each item produces a normal run in the project history, a comparison with the previous comparable run, an autonomous report and a short notification. There is no daemon and no local HTTP listener.

Autonomous runs never approve anything. Every action that needs approval is skipped and listed in the report, and every clarification the agent asks for is answered by the system as "information unavailable", kept as a gap and never stored as a fact provided by a person.

## Configuration

Autonomous settings live in `lastro.toml`. Channels reference environment variables by name; webhook URLs, SMTP users and passwords are never written to the file, to the history or to the run snapshot.

```toml
[autonomous]
enabled = true
max_minutes = 30              # cap per run; the effective limit is the lower of this and the profile limit
native_max_cost_usd = "2.00"  # cap for the native executor; the lower of this and the profile cap

[autonomous.github]
workflows = ["Deploy homolog"]   # workflow_run: deployment workflows that may trigger a run
environments = ["homolog"]       # deployment_status: environments that may trigger a run
branches = ["main"]              # allowed branches (default: main)
# repository = "org/repo"        # optional: only events from this repository

[[autonomous.notifications]]
name = "qa-slack"
type = "slack"
webhook_env = "LASTRO_SLACK_WEBHOOK"

[[autonomous.notifications]]
name = "qa-teams"
type = "teams"
webhook_env = "LASTRO_TEAMS_WEBHOOK"

[[autonomous.notifications]]
name = "qa-email"
type = "smtp"
host = "smtp.example.com"
port = 587
starttls = true                  # or ssl = true for implicit TLS; exactly one is required
username_env = "LASTRO_SMTP_USER"
password_env = "LASTRO_SMTP_PASSWORD"
from_address = "lastro@example.com"
to = ["qa@example.com"]
```

Channel names are unique. A webhook must be HTTPS. SMTP always uses TLS with certificate verification and never falls back to plain text.

## Daily schedule

```sh
lastro schedule add --name homolog-daily --daily 07:00 --mission missions/daily.md --profile homolog
lastro schedule list
lastro schedule show homolog-daily
lastro schedule remove homolog-daily
```

- The mission must be a regular file under the project root. Its content is frozen, with its SHA-256, each time the schedule fires; later edits only affect later runs.
- Adding the same schedule again is a no-op. Changing a schedule with the same name requires `--replace`.
- The time is local to the project `timezone`. When a daylight saving change makes the time ambiguous, the run happens once, at the first occurrence; when the time does not exist that day, the run happens at the first valid local minute after the gap.
- A missed time is recovered once if Lastro wakes up within 60 minutes; later than that, the day is recorded as `missed`.
- On Windows the schedule is a Task Scheduler task under `\Lastro\`; on Ubuntu it is a line inside a delimited block of your crontab, and lines outside the block are never touched. The task only runs `lastro schedule tick <id>` every minute: the decision to run, the time zone and the idempotency are Lastro's, not the operating system's. No mission text, webhook or credential appears in the installed command.
- `list` and `show` compare the installed task with what Lastro expects and report `installed`, `drifted`, `disabled` or `missing`. Lastro never reinstalls or overwrites a task changed outside of it; use `schedule add --replace` to install it again.
- `remove` deletes the operating system task first and then marks the schedule as removed, so a failure in the middle can be retried.

## Manual and post-deployment triggers

```sh
lastro --profile homolog autonomous run --mission-file missions/daily.md
lastro --profile homolog autonomous run --mission "Check the employee list" --key nightly-2026-11-23
```

`--key` makes a manual trigger idempotent: the same key with the same content returns the existing item, and the same key with a different content fails with `trigger_conflict`.

After a deployment, a workflow in the repository of the system under test calls `lastro autonomous trigger` with the event file GitHub provides. The command only accepts `GITHUB_EVENT_PATH` inside GitHub Actions, reads a small allowlist of fields (delivery id, repository, workflow, conclusion, commit, branch, environment and run URL) and ignores everything else in the payload. The mission and the profile always come from the trusted workflow step, never from the event. A `workflow_run` triggers only when it `completed` with `success` for a configured workflow and branch; a `deployment_status` triggers only on `success` for a configured environment. [`.github/workflows/autonomous-post-deploy.example.yml`](../.github/workflows/autonomous-post-deploy.example.yml) is a ready-to-copy example with `permissions: contents: read`, actions pinned by SHA and no checkout or execution of artifacts from the deployment run.

When a post-deployment run starts, Lastro first collects the change context (see [change-context.md](change-context.md)) when `[source]` is configured, and the mission tells the agent to list the changed flows first in `affected_flows`. Every criterion of the mission is kept; the link between a change and a flow is recorded as a hypothesis.

## Queue and worker

- At most one autonomous run per profile is active. Up to three items wait behind it; a fourth pending trigger drops the oldest pending one with reason `queue_capacity`. The active item is never dropped, and a duplicate does not take a position.
- The worker that wins the profile lease renews it while it works and checks it before every effect. A worker that loses the lease stops with `lease_lost`; another worker can take over only after the lease expires and the previous process is gone. A trigger accepted while another worker owns the lease returns `queued` with success: the item is durable.
- After a crash, the item is resumed by reconciling the run already created for it: the run is closed as interrupted and the report and notification are produced; a second run is never created.
- `lastro autonomous queue` shows the active and pending items without mission text.

## Preflight, limits and blocked runs

Before creating the run, Lastro checks the configuration, the frozen mission, the executor, the referenced credentials, the target and the login. An unreachable target creates a `blocked (unreachable)` run and an expired or rejected login creates a `blocked (auth)` run (check it with `lastro auth check`). Both produce a report and a notification and are not retried until the next trigger.

Each run is limited to 30 minutes by default and to USD 2.00 with the native executor. Codex and Claude Code keep the duration limit; their cost is reported as unknown when the executor does not report it. The run snapshot records the trigger, the frozen mission hash, the limits and the redacted list of channels.

## Comparison with the previous run

Each autonomous run is compared with earlier runs of the same frozen mission and profile, using the structured findings stored in the history (never the Markdown report):

- Candidates are limited to the same flow. Equal normalized steps are a match; otherwise Lastro computes a weighted Jaccard similarity (steps 0.50, observed result 0.30, title 0.20) and accepts a match with a score of at least 0.80 and an advantage of at least 0.10 over the second candidate. Ties and ambiguity are reported as `new`. Normalization applies Unicode NFKC, case folding and space collapsing and removes punctuation only, keeping numbers, selectors and step order. The algorithm name and version (`finding-match-v1`) are part of the comparison fingerprint.
- For each flow, the baseline is the most recent earlier run that completed, has an intact final report and proved coverage of that flow, either with a finding in it or by evaluating every criterion of the mission.
- `recurring`: matched with a finding of the baseline. `regressed`: matched with a finding whose latest occurrence was `resolved`. `new`: no match. `resolved`: a previous finding is absent from a run that proved coverage of its flow. Blocked, cancelled or crashed runs, and flows without proven coverage, never produce `resolved`; those findings are `not_comparable`.
- Findings with missing or altered evidence are left out of matching and listed as limitations. Resolving a finding never changes or deletes the original record.
- Without a previous comparable run, every finding is shown as new with the message `No previous run to compare; all findings shown as new.`

The autonomous report (`runs/<run id>/report.autonomous.md`, or `report.autonomous.json` when the template fails) is a new immutable snapshot that opens with the comparison and continues with the full report. The final report of the run is never overwritten.

## Notifications

Every configured channel receives the same summary of at most 20 lines with the profile, status, termination reason, new, regressed, recurring and resolved counts, actions skipped awaiting approval, clarification gaps, other pending items and the path of the autonomous report:

```text
Lastro · homolog · 2 new findings, 1 regressed, 3 recurring
status: completed
resolved since previous run: 4
comparison: compared with run_20261122_070012_a1b2
1 action skipped awaiting approval
report: runs/run_20261123_070011_c3d4/report.autonomous.md
```

Slack receives a plain text block, Teams an Adaptive Card and SMTP a `text/plain` UTF-8 message. Deliveries go through a durable outbox, independent per channel, with at most three attempts at 0, 60 and 300 seconds. HTTP 2xx is a delivery; 429 and 5xx are retried; other 4xx answers and redirects fail permanently, and redirects are never followed. Response bodies are never stored. After the third failure the channel is marked `notification_failed`; the run and its report stay as they are. The worker may exit before a retry is due: the next `schedule tick` delivers it, or run `lastro autonomous retry-notifications`.

## Demo workflow

[`.github/workflows/autonomous-demo.yml`](../.github/workflows/autonomous-demo.yml) runs on every push to `main`: it installs the locked environment, starts the bundled demo, runs an autonomous mission end to end with the native executor driven by a scripted model and the demo's fictitious credentials, and uploads `report.autonomous.md`, `report.md` and the notification summary as the `lastro-autonomous-report` artifact. It uses no real secret.

## Error codes

| Code | Meaning |
|---|---|
| `invalid_autonomous_input` | invalid input, event or combination of options |
| `schedule_conflict` | a different schedule with this name exists; use `--replace` |
| `scheduler_unavailable` | Task Scheduler or cron is missing, denied or cannot be reconciled |
| `trigger_conflict` | the idempotency key was reused with a different body |
| `queue_unavailable` | the history or the queue transaction is unavailable |
| `lease_busy` | another worker owns the profile; not a failure of the item |
| `lease_lost` | the worker lost the lease and stopped |
| `mission_snapshot_invalid` | the mission cannot be frozen safely |
| `autonomous_preflight_failed` | executor or configuration unavailable |
| `comparison_unavailable` | not enough data or invalid integrity for the comparison |
| `notification_failed` | a channel used all its attempts |

Human output is in Portuguese and `--json` prints a stable envelope. Invalid usage exits with 2, operational failures with 1 and interruption with 130.
