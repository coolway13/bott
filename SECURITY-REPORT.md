# MLB bot security audit

Audit date: 2026-09-20 (Pacific). Repository: `coolway13/bott`.

## Result and scope

No matching credentials were found in the public Git history scanned: **107 reachable commits, 124 unique file blobs, and 103 decompressed state snapshots**, covering both `main` and `bot-state`; no tags or pull requests existed at the audit snapshot. The audit checked commit messages/metadata as well as files. Detection included an exact in-memory match against the configured Discord webhook and patterns for Discord webhooks, GitHub tokens, Airtable tokens, AWS access keys, JWTs and private keys. Values were not written to the report. Pattern scans cannot prove the absence of arbitrary, encoded or unknown-format credentials. Deleted/unreachable server objects and outside copies are not accessible to a mirror audit.

All seven historical Actions runs and zero artifacts were inventoried. All seven raw job logs were inspected through the authenticated GitHub log viewer: the webhook environment value was masked in every run, and no Discord webhook or GitHub token patterns were found. No raw log content is included in this report. Archive access required authentication; browser log controls were used instead.

The Discord webhook was previously shared in this private task conversation and remains in an ignored local credential file from the original setup. This is separate from the public-repository audit. Consider rotating it in Discord and updating the existing Actions secret. No credential was rotated, deleted or newly exposed as part of the repository changes.

Post-deployment verification: the hardened [GitHub run](https://github.com/coolway13/bott/actions/runs/35555775250) succeeded, including all **59 tests**. All 15 confirmed Discord delivery records were preserved, with zero unconfirmed reservations. A second full-history scan after code/workflow deployment covered **125 commits, 152 blobs and 119 compressed snapshots**, again with no matching credentials.

During browser log inspection, a temporary signed read-only log-download URL appeared in browser-tool metadata. It was not a bot webhook/API token and was not committed or emitted by the bot. Further log scans suppressed URLs and contents; keep audit transcripts private.

## Changes

- Pinned the two official GitHub Actions to full commit SHAs verified through their official repositories: checkout `d23441a48e516b6c34aea4fa41551a30e30af803`; setup-python `ece7cb06caefa5fff74198d8649806c4678c61a1`. Ubuntu is `ubuntu-24.04`; Python is 3.12. SHA pins need deliberate future security updates.
- Set workflow-wide permissions to `{}` and granted only `contents: write` to the delivery job. State writes require it; other token scopes are not requested. The built-in token is ephemeral and only passed to the bot through `GH_TOKEN` in its execution step. Checkout does not persist credentials.
- Added `security.py`: outbound HTTPS host allowlist, disabled redirects and proxy environment handling, exact environment-secret redaction, webhook/token pattern redaction, and line-buffered stdout/stderr protection. Nested Discord payloads and serialized public state are scrubbed too. Exceptions reported by the bot do not include request URLs, response bodies or secret values.
- Removed webhook-file fallback and credential-file creation. `DISCORD_WEBHOOK_URL` must come from the environment. Disabled legacy Airtable credential configuration/network calls and all Kalshi network calls. No analytics, new service, telemetry, or trading integration was added.
- Removed Kalshi fields and refreshes from production/local Discord cycles to comply with the requested external-service restriction.
- Validate Discord message identifiers before appending them to request paths. Validate real MLB game IDs before emitting workflow warnings, preventing injected newlines/workflow commands from those identifiers.
- Expanded `.gitignore` for environment variants, credentials, local configs, private keys, databases, logs, caches, virtual environments and backups. Ignore rules are defense in depth: they do not remove historical/tracked files and can be bypassed with force-add or browser uploads.
- Added security tests covering rejected hosts, redirects, split log writes, nested payload redaction, public-state redaction, environment-only credentials, disabled services and path injection. Existing original-prediction/deduplication tests remain.

## Secrets and permission inventory

| Item | Where used | Exposure boundary / action |
| --- | --- | --- |
| `DISCORD_WEBHOOK_URL` | One repository Actions secret; environment of the bot step | Grants posting/editing for its Discord webhook. Never committed. Request URL necessarily contains it when contacting Discord; redirects are forbidden and payload/log/state copies are scrubbed. |
| `GITHUB_TOKEN` → `GH_TOKEN` | GitHub supplies it per run; bot environment only | `contents: write` is needed to checkpoint `bot-state`. This scope is repository-wide, not branch-specific. No PAT is needed or stored. |
| MLB credentials | None | MLB Stats API reads are public. |
| Kalshi/Airtable credentials | None in active bot | Both integrations disabled; legacy helpers cannot contact their services or create credential files. |
| Local `.discord-webhook` | Original Mac project, ignored, owner-only file from earlier setup | New code never reads it. Still exists; remove or rotate it deliberately after confirming cloud operation. Do not upload it. |
| Repository/environment secrets | Settings read-only review | One repository secret (`DISCORD_WEBHOOK_URL`), no environment secrets listed. Secret values were not read from GitHub. |
| GitHub security settings | Read only | No account, repository security, secret, collaborator, or credential setting was changed. |

## External service and domain inventory

| Service/domain | Purpose and data |
| --- | --- |
| `statsapi.mlb.com` | MLB schedules, game IDs, team IDs, prior-day public statistics, probable pitchers and live game statistics. No bot credentials sent. Public player/team names are necessary baseball data. |
| `discord.com` | Posts/edits prediction and score embeds using the webhook. No private user profile data is gathered. |
| `api.github.com` | Restores/checkpoints compressed game and delivery state using the ephemeral token. Required for restart safety under GitHub Actions. |
| GitHub Actions infrastructure, official `actions/checkout`, `actions/setup-python`, GitHub-hosted runner/tool downloads and log storage | Required hosting/toolchain services. These platform operations are outside the Python host allowlist; the allowlist is an application boundary, not a runner-wide firewall. No third-party Action is installed. |
| Kalshi and Airtable domains in legacy source/docs | Not used after this change. Calls are disabled, including direct helper calls. |
| `127.0.0.1` / `localhost` | Optional Mac-only dashboard. Cloud workflow starts no HTTP server. |

## File and workflow inventory

| Files | Information risk and treatment |
| --- | --- |
| `.github/workflows/mlb.yml` (only workflow) | Secret names only; no values. Schedule/manual triggers only, default-branch/repository guard, serialized runs, 12-minute timeout. No `pull_request_target`, PR trigger, external untrusted checkout, interpolated shell input or dynamic shell commands. Full-SHA official Actions. |
| `cloud.py` | Holds GitHub token in memory; restores and saves public state. Fixed API host and repository guard. SQL parameters for data, fixed allowlisted table names. Safe exception summary. |
| `security.py` | Redacts environment secrets/patterns; enforces network hosts/no redirects. Does not store secret values or collect data. |
| `local.py` | Discord HTTP transport and optional loopback dashboard. Environment-only webhook, scrubbed payloads, sanitized errors. Local form has host/CSRF checks. |
| `automatic.py`, `model.py` | Public baseball statistics and probability calculations. No secret configuration. MLB HTTPS only. |
| `discord_cards.py` | Constructs public baseball embeds and tracks message IDs. No webhook in cards. |
| `kalshi.py`, `bot.py` | Retained legacy calculation/formatting helpers; Kalshi/Airtable HTTP and credential setup disabled. Legacy tests use fake clients only. |
| `seed_cloud.py` | Explicit one-time state export using the same scrubbed allowlist. No credential export. |
| `state.json.gz` on `bot-state` (all historical versions) | Public, not encrypted. Contains public games/stats, original predictions/inputs, hashed destination identifiers, Discord message IDs/fingerprints/reservations. No webhook/token found. These IDs enable deduplication; they are metadata, not authorization credentials. Compression is not privacy protection. |
| `dashboard.html` | Local UI, same-origin `/api/state`, no analytics or external script libraries. Public baseball data, temporary local CSRF placeholder. |
| `games.example.json` | Synthetic baseball example. No credential value. |
| `test_automatic.py`, `test_bot.py`, `test_cards.py`, `test_cloud.py`, `test_kalshi.py`, `test_local.py`, `test_security.py` | Synthetic fixtures/mocked networking; no real credentials. Test failure-simulation output is suppressed. |
| `.gitignore` | Blocks common accidental local data/credential additions, not forced uploads or existing history. |
| `DEPLOYMENT.md`, `SECURITY-REPORT.md` | Operational instructions, names/locations only; no credential values. |
| Ignored Mac files: `.discord-webhook`, possible `.env*`, credential/local config files, `data/*.sqlite3`, `data/*.log`, disabled LaunchAgent backup, caches | Must remain local. Original saved webhook still exists. These are not published by the workflow. No full local-directory upload should be used. |
| Actions logs/artifacts/caches | Logs are public for this public repository and stored by GitHub. No artifact/cache steps; zero artifacts found. Application output is restricted/redacted; GitHub also masks configured secrets. Debug tracing/printing environments must never be enabled. |
| Git metadata | Commit authorship identities are inherently public in Git history. Configure GitHub's no-reply commit email if desired; history was not rewritten. |

## Recommended settings (not changed)

1. Enable Secret Protection/secret scanning and push protection. The security page currently offers **Enable** for Secret Protection; partner alert scanning alone is not the same protection.
2. Require full-length Action SHA pins and restrict allowed Actions to the two official dependencies. Currently all Actions are allowed and the SHA-enforcement checkbox is off.
3. Require approval for all external-contributor workflows if PR workflows are added. Currently first-time-contributor approval is selected. The present bot has no PR execution trigger, so fork PRs do not receive production secrets through it.
4. Protect `main` changes with review, especially workflow/Python changes. Do not block necessary bot-state updates without changing persistence design. Anyone who can change trusted production code can intentionally access/exfiltrate its secrets; redaction cannot defend against malicious authorized code.
5. Consider enabling dependency graph/Dependabot alerts (currently disabled) and lowering log retention from 90 days. These are recommendations, not automatic security-setting changes.
6. Rotate the previously chat-shared webhook if you want a fresh credential; update the existing Actions secret privately. No public Git leak was found requiring history rewriting. Do not post the replacement in chat or a commit.

No audit can promise a credential will “never” leak under all future code changes, compromised dependencies/accounts or novel encodings. These changes close identified application paths; ongoing review, protected writes, credential hygiene and GitHub secret protection remain necessary.
