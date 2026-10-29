# Security Policy

## Supported versions

Lastro has not published a release yet. Until the first release, security fixes land on the
`main` branch only. After releases start, only the latest minor release receives fixes.

| Version | Supported |
|---|---|
| `main` (0.1.0, unreleased) | Yes |
| Any other version | No |

## Reporting a vulnerability

Please do not open a public issue for a vulnerability.

- Preferred channel: GitHub private vulnerability reporting, from the **Security** tab of this
  repository (**Report a vulnerability**), when it is enabled for the repository.
- Alternative channel: email `jvmartinscv@gmail.com` with the subject `Lastro security report`.

Include the affected version or commit, the steps to reproduce and the impact you observed. Do
not include real credentials, session cookies or personal data from any system in the report.

You will receive a response within 7 days. Once the report is confirmed, the fix is developed
privately and disclosed in the CHANGELOG after it is available.

## Threat model summary

Lastro lets an AI agent explore a web application that may hold real data. The main risks are:

- **Secret and evidence leakage.** Credentials, session cookies, browser traces, screenshots and
  run databases could reach this public repository or a contributor's fork.
- **Unsafe actions on the target system.** An agent could change or delete data it was not
  asked to touch, act on the wrong tenant, or follow instructions injected into page content.
- **Executor bypass.** An agent executor could reach the target system through tools that do
  not go through Lastro.
- **Supply chain.** Dependencies, pre-commit hooks and CI actions could be tampered with.

Today the repository addresses leakage (ignore policy, secret scanning, private data root
outside the repository) and pins hook and action revisions. Controls on agent actions are
planned and are not guarantees yet.

## Guarantees demonstrated by tests

Each guarantee links to the test that demonstrates it. If a guarantee has no passing test, it
is not a guarantee.

- Every sensitive path pattern of the ignore policy (secrets, keys, browser session material,
  private data, evidence media, imported content, private evaluation results, executor local
  settings, logs) is ignored by Git. Test:
  [`test_sensitive_samples_are_ignored`](tests/repository/test_gitignore.py)
- Committing a file that contains an AWS-style access key is blocked by the pre-commit secret
  scan, which reports the file and line and creates no commit. Test:
  [`test_test_aws_key_is_blocked_by_gitleaks`](tests/repository/test_precommit.py)
- Committing a file larger than 1 MB is blocked by pre-commit. Test:
  [`test_large_file_over_one_megabyte_is_blocked`](tests/repository/test_precommit.py)
- `lastro init` writes only variable names for credentials and never writes secret values or
  a `.env` file. Test: [`test_no_secret_values_written`](tests/unit/test_scaffold.py)
- `lastro doctor` reports a failure when the private data root is inside a Git working tree.
  Test: [`test_private_root_inside_git_is_failure`](tests/unit/test_doctor_checks.py)
- `lastro doctor` reports a failure when `.env` is not git-ignored. Test:
  [`test_env_not_ignored_is_failure`](tests/unit/test_doctor_checks.py)
- The bundled demo binds to `127.0.0.1` by default. Test:
  [`test_default_port_and_host_constants`](tests/demo/test_runner_lifecycle.py)
- The demo's seeded defect catalog is kept outside the package and is never served by the demo
  app. Test:
  [`test_catalog_outside_package_and_not_served`](tests/demo/test_catalog_consistency.py)
- The built wheel contains no test files and no defect catalog. Test:
  [`test_wheel_contains_package_data`](tests/demo/test_packaging.py)

## Non-guarantees

- Lastro cannot prevent an executor configured without the provided permission rules from
  reaching the target system by other means.
- Lastro does not yet enforce policies on browser actions, require human approval for
  sensitive actions or limit run duration and cost. These controls are planned; until they are
  released, do not point an agent at a system whose data you cannot afford to change.
- Secret scanning detects known secret formats only. It cannot detect every credential, and it
  does not protect data that was committed before the scan was in place.
- The ignore policy protects the paths it lists. Files saved under other names or locations are
  not covered.
- The bundled demo is a deliberately defective application for local testing. Do not expose it
  to a network or reuse its code in production.

## Secret scanning allowlist policy

Gitleaks runs in pre-commit and in CI with its default rules. An allowlist entry may be added
only for a reviewed false positive, never to make CI pass. Every entry lives in
[`.gitleaks.toml`](.gitleaks.toml), matches the narrowest possible value and carries a comment
that explains why the match is not a secret. Allowlist changes are approved in pull request
review like any other code change.
