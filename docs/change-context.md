# Change context from issues and Git

A run can start from an issue instead of a free text mission, and it can carry a controlled view of the code under change. Lastro (the trusted Python process) reads, filters, scans and freezes this material before the run exists. The agent only sees the frozen copy, through the same `mission_context` tool it already uses, and never gets a shell or a file reading tool.

## Starting from an issue

Pass exactly one mission source: `--mission`, `--mission-file`, `--issue`, `--issue-file` or `--issue-text`.

```sh
lastro run start --issue https://github.com/org/repo/issues/123
lastro run start --issue-file exported-issue.md
lastro run start --issue-text "Email must be optional on the employee form."
```

- `--issue` accepts only `https://github.com/<owner>/<repo>/issues/<n>`, without credentials, query string or fragment. It uses your existing `gh` login (`gh auth status`) and makes explicit read-only `GET` calls. Lastro never logs in for you, never retries without limit and never falls back to anonymous access. GitHub Enterprise is not supported yet, and a pull request URL is not accepted as an issue.
- The title, body, labels and the first 50 comments in creation order are imported. The snapshot records how many comments exist, how many were included and whether the list was truncated. Attachments and links inside the body are never followed. GitHub has no transactional snapshot, so Lastro records when the capture started and ended and the `updated_at` value it received.
- `--issue-file` reads a local UTF-8 Markdown export (a regular file up to 1 MB, links refused). An optional front matter block may declare `url`, `repository`, `number`, `title` and `labels`; the snapshot still says `source_kind = markdown`. Without metadata the title is the first heading, or `Imported issue`.
- `--issue-text` keeps the text as the body and has no GitHub identity.
- Limits: 1 MB of JSON per GitHub response, 100,000 characters of body, 10,000 per comment and 500,000 in total. Going over a limit stops the command before the run starts, so no criterion is dropped silently.

If the issue cannot be read, no run is created:

```text
Could not read issue (HTTP 404). Check 'gh auth status' and repository access.
```

### Snapshots

The imported content is redacted before it is hashed or stored. When `gitleaks` is installed, lines with a possible secret are replaced as a whole; the snapshot records whether the scanner took part. The SHA-256 identifies the canonical sanitized JSON (identity, title, body, labels and comments), not the local capture times and never the raw content.

Snapshots are immutable. Importing the same sanitized content again reuses the snapshot, and each run still records its own capture time; edited content creates a new snapshot and older runs keep theirs. Two captures that differ only inside redacted values can produce the same hash.

## Adding the code under change

Code context is opt-in. Configure an explicit allowlist and point `SOURCE_REPO_PATH` at the repository root (for example in `.env`; the path is never shown to the model):

```toml
[source]
allow_paths = ["src/**", "templates/**"]
base_ref = "origin/main"
```

```sh
lastro run start --issue https://github.com/org/repo/issues/123 --source
```

Without `--source`, Lastro does not list or read that repository at all. `gitleaks` is required for `--source`: if it is missing, fails or does not detect the built-in synthetic secret, the command stops before the run with a message telling you to run without `--source`. `lastro doctor` checks Git, `gitleaks` and `gh` only when `[source]` is configured.

### Comparison modes

| `--compare` | What is compared |
|---|---|
| `working-tree` (default) | Merge base of the base ref and `HEAD` up to `HEAD`, plus staged, unstaged and untracked allowed files, each as its own layer |
| `feature` | Merge base of the base ref and `HEAD` up to `HEAD`, commits only |
| `pr` | Merge base and head of the pull request given with `--pr URL`, without checkout and without local changes |
| `last-passed` | The last commit proven to have run in a completed run of the same project, profile, tenant and `--flow` key, with passed checks and no failed checks, up to `HEAD` |

- `--base <ref>` replaces `base_ref` for one run. It must be a plain ref name; revision syntax and refs starting with `-` are refused. It cannot be combined with `pr` or `last-passed`.
- A missing base, merge base, pull request object or baseline is reported as `comparison unavailable`. Lastro never substitutes `main` or another run silently.
- A fork pull request needs its objects locally; otherwise the comparison is unavailable instead of analyzing your local `HEAD` as if it were the pull request.
- `--flow <key>` is a stable key you associate with the run (1 to 200 characters). It is required by `last-passed` and is never guessed from titles.
- There is no implicit network access. `--fetch` is your explicit consent to run `git fetch` on an already configured HTTPS or SSH remote; it touches refs, objects and `FETCH_HEAD`, never the current branch, the index or your files. The report says whether the collection was local only (`not_checked`), `fetched` or `fetch_failed`.

### What is never read

Lastro lists names, modes and sizes first, applies the denials before the allowlist and only then reads the approved content. Denials are case-insensitive on every platform and apply to history, the index, removed lines and earlier names too:

- `.env*`, `*.pem`, `*.key`, `secrets.*`, SSH and other credential files, every `appsettings*.json`, any `.git` component;
- submodules, symlinks, Windows junctions and other reparse points, devices and FIFOs;
- binary files by extension or content (NUL bytes or invalid UTF-8) and anything over 1,000,000 bytes;
- absolute, UNC, drive and `..` paths, and paths whose name itself looks like a secret.

Git runs with a sanitized environment and without hooks, fsmonitor, pager, external diff, textconv, filters, submodule recursion or lazy fetch of partial clones. Lastro never calls `checkout`, `switch`, `stash`, `reset`, `git diff` or `git status`; diffs are computed in Python between approved buffers, and renames appear as a removal and an addition.

Every approved buffer, including base and index versions, is scanned with `gitleaks` using Lastro's own rules; repository configuration, baselines, ignore files, `gitleaks:allow` comments and `GITLEAKS_*` variables are ignored. A file with a possible secret is excluded entirely, even when the secret only appears in a removed line, and its diff shows `[REDACTED] excluded (possible secret)`. A symlink or reparse point that would escape the repository is denied and recorded as a `security_event`.

Default limits are 500 candidate files, 20 MB of approved bytes, 200,000 characters of stored diff and 12,000 characters of change summary for the agent. They can only be lowered in `[source]` (`max_candidates`, `max_total_bytes`, `max_diff_characters`, `max_summary_characters`). A result over a limit is marked partial and never treated as complete coverage. A change in `HEAD` or in the index during the capture marks the entries as `unstable`; there is no automatic retry.

## What the agent receives

`mission_context` keeps its empty call. For an imported issue it returns the requirements inside `<mission-content>`; when they exceed 10,000 characters only the first page is shown, `requirements.partial` is true, and the agent must read every page with `{"section": "issue", "cursor": ...}` before `submit_mission_plan` is accepted (`context_not_fully_read` otherwise). With `--source`, the response adds `change_context` (comparison, refs, dirty state, freshness, changed files with `file_id`s, exclusion count) and `{"section": "changes"}` pages the change summary inside `<change-context>`. Cursors are bound to the run; anything else is `invalid_cursor`.

The plan accepts three optional lists: `supporting_information`, `hypotheses` and `affected_flows`, each item citing a reference approved for the run (the snapshot id, `issue`, `comment_<n>` or a `file_id`). Explicit criteria of an imported issue must cite the issue with `source_ref`. Code and diffs are implementation hypotheses, never confirmed business criteria, and nothing in an issue or in code changes policies, approvals or permissions. The canonical charter lists the affected flows first, in priority order.

## Versions and the report

The analyzed code version is the full commit id of the analyzed revision (the pull request head in `pr` mode). The executed system version is read once per browser session from the profile `version_endpoint`, on the target origin. They match only when both are full commit ids, they are equal and there were no local changes; a dirty working tree is `dirty_unverified`, different ids are `mismatched` and tags, branch names or opaque versions stay `unknown`.

The private report has a `Change context` section with the line `Analyzed vs. executed version`, the issue snapshot hash, mode, freshness, local changes, exclusions and warnings. It is built from the history only and works offline, even after the source repository is gone. `lastro report verify` covers the frozen context files, and `lastro report export --public` omits the issue body, code, diffs, paths, URLs, tenant, ids and commits. Backups include the snapshots, the context and its files.

The frozen change context and the controlled source files stay available, by run, to later features such as code-inferred documentation and post-deployment runs, without re-reading Git or GitHub.
