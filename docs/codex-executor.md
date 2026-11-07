# Codex executor

Lastro can run a mission with the Codex CLI you already have installed, using your own ChatGPT login. Lastro starts and supervises the CLI, points it at the same MCP tool server and skill bundle used by other executors, and records the run under executor `codex` with the installed version. Lastro never calls an inference API for this executor, never asks for an API key and never falls back to API billing.

## Requirements

- Codex CLI 0.166.0 or newer (`npm install -g @openai/codex`). Lastro does not install it.
- A ChatGPT login managed by the CLI (`codex login`). `codex login status` must report `Logged in using ChatGPT`. A missing login, an API key login or an unknown method stops the start.
- Target credentials in the project `.env`. The Codex process does not inherit your environment, so the tool server reads them from that file.

## Start a mission

```text
lastro init --executor codex
lastro doctor
lastro run start --executor codex --mission "Check the employee list"
lastro run launch --run-id <id>
```

`lastro init --executor codex` writes `AGENTS.md` (at most 60 lines), `.codex/config.toml`, `.codex/lastro.json` and the shared skill bundle in `skills/lastro/`. Running it again with identical files changes nothing and exits 0. Edited files are preserved; a conflict is reported and blocks the start until you fix it. A new project gets `executor = "codex"`. An existing project keeps its default, so use `--executor codex` per run or change the key yourself.

`lastro run start` checks the installation, login, rules and bundle before anything is written. It then creates the durable run and prints the launch command. No browser opens and the target is not contacted until you launch.

`lastro run launch --run-id <id>` opens the interactive Codex CLI in the project folder. `lastro run launch --run-id <id> --headless` runs `codex exec` with structured follow-up, needs no terminal and prints a normalized JSON result as its last line. The launcher accepts no extra arguments for Codex.

A run can be launched only once. `lastro run resume <id>` continues an interrupted, cancelled or limit-stopped run in a new linked run that keeps the Codex executor and prints its own launch command (see [execution-limits.md](execution-limits.md)). A new `lastro run start` closes a run that was created and never launched, as `interrupted` with reason `launch_abandoned`.

## What the launcher enforces

Before opening Codex the launcher rereads the run and rechecks the binary, version, login, bundle hash and rules. It refuses a run from another project or profile, a run that ended, a run of another executor and a second session.

Each session receives its policy as explicit overrides, because Codex loads a project `.codex/` folder only for trusted projects. The policy forces ChatGPT login and the OpenAI provider, disables web search, shell and execution tools, apps, plugins, hooks, skills and multi-agent tools, and applies a read-only permission profile with network disabled. MCP servers from your Codex configuration are disabled for that session. The launcher does not edit your global Codex configuration or your login.

The agent cannot read these paths through its tools: the private data root, `.env` and its variants (also nested), `.auth/`, `evals/catalog/` and the Codex `auth.json`.

Checks run on the real CLI, without inference, on every launch, and any failure stops the launch with `Codex sandbox rules are invalid`:

- `codex mcp list` must report `lastro` as the only enabled server, pinned to this run.
- `codex debug prompt-input` must report only tools from a closed allowlist. A tool Lastro does not know blocks the launch.
- `codex sandbox` must deny a read of a canary file in the private data root and of `.env`, a write in the project and an outbound connection. The connection goes to a reserved documentation address, so no third party receives traffic.

### Known limits

- The CLI still exposes `apply_patch` and its JavaScript `exec` orchestrator, and no setting removes them. `exec` runs in an isolate with no file system and no network. `apply_patch` runs under the read-only profile, and the launch check proves that the profile denies writes.
- On Windows the sandbox blocks external addresses but still allows connections to the same machine, including `127.0.0.1`. This matters for a target running locally. The agent has no tool that can open a socket, so this path stays closed by the tool allowlist and not by the sandbox.
- Headless mode ignores your user configuration with `--ignore-user-config`, so it uses the default model of your account. Interactive mode keeps your user `model` settings.
- A `profile`, `openai_base_url` or `chatgpt_base_url` key in your Codex user configuration cannot be neutralized, so interactive launch refuses it.

## Human gates

Gates work as with other executors: when the server asks for approval, decide in another terminal with `lastro approve`. The agent keeps polling `approval_status`. No Codex prompt counts as a business approval, and the adapter never issues an approval proof on your behalf. The tool catalog is auto-approved in Codex only so that calls reach the Lastro policy server, which remains the authority.

## How a run ends

A zero exit from Codex does not mean the mission succeeded. The run is `completed` only when it ended through the Lastro tools.

| Situation | Status and reason | Launcher exit code |
|---|---|---|
| Mission ended through `end_run` | As recorded by the tool server | 0 when completed, otherwise 1 |
| Codex exits without ending the run | `interrupted`, `executor_exited` | 1 |
| Tool server fails to start | `interrupted`, `mcp_unavailable` | 1 |
| Invalid or oversized CLI event | `interrupted`, `executor_protocol_error` | 1 |
| Temporary rate limit or overload | `interrupted`, `executor_rate_limit` | 1 |
| Network failure | `interrupted`, `executor_network` | 1 |
| Other turn failure | `interrupted`, `executor_error` | 1 |
| Account usage limit | `interrupted`, `executor_limit` | 3 |
| Ctrl+C or termination signal | `cancelled`, `user_cancelled` | 130 |
| Refused before opening Codex | Run unchanged | 2 |

On a usage limit or fatal failure the launcher asks the tool server to lock effects and finish, closes the process tree and writes a partial report with no new inference. Lastro does not retry a session or a write. Usage and cost of a subscription session are shown as unknown, never as zero.

The usage limit is recognized only from CLI protocol data: in headless mode from error events, and in interactive mode from the session record that the CLI writes under `$CODEX_HOME/sessions`. Lastro reads only event records from that file and never scrapes terminal output or model text.

The usage limit codes come from the 0.166.0 CLI itself, but no real limit event has been captured yet, so this path is covered only by simulated events.

## Diagnostics

`lastro doctor` shows the installed and minimum versions, the login method and whether `.codex/config.toml` and `.codex/lastro.json` are valid.

| Message | Fix |
|---|---|
| `installed: not found; required: >=0.166.0` | Install the Codex CLI |
| `installed: <version>; required: >=0.166.0` | Update the Codex CLI |
| `Codex has no ChatGPT login` or `logged in with an API key` | Run `codex login` and sign in with ChatGPT |
| `Codex sandbox rules not found; run 'lastro init --executor codex'.` | Run that command |
| `Codex sandbox rules are invalid` | Follow the detail; restore edited files and run `lastro init --executor codex` |

## Platforms

Support is limited to the combinations below. macOS is not covered.

| Check | Windows 11, Codex 0.166.0 | Ubuntu, Codex 0.166.0 |
|---|---|---|
| Automated tests with a simulated CLI | Passing | Run by CI |
| File, network and tool checks on the real CLI, no inference | Verified | Not verified |
| Full mission with real inference | Not verified | Not verified |

Run the opt-in probes on your machine with `LASTRO_CODEX_PROBES=sandbox uv run pytest tests/integration/test_codex_sandbox.py`. `LASTRO_CODEX_PROBES=inference` also opens a real session and consumes your account quota.
