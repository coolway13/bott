> Security update: only MLB Stats API, Discord and GitHub state storage are enabled. Kalshi and legacy Airtable network access are disabled. Credentials are environment-only. See SECURITY-REPORT.md.

# GitHub Actions deployment

Target: https://github.com/coolway13/bott (public). Upload the contents of this project at repository root, including `.github/workflows/mlb.yml`. Do not upload `.discord-webhook`, `.env`, `data/`, caches, or machine-specific launchers/configuration. The cloud entry point is `cloud.py`; it exits after one cycle and does not run a web server.

## First activation / migration

1. Add repository Actions secret **DISCORD_WEBHOOK_URL** in Settings → Secrets and variables → Actions. Copy the existing saved webhook privately; do not commit it. GitHub supplies its short-lived `GITHUB_TOKEN` automatically. MLB and Kalshi public reads need no API keys. Any future credentials must also be Actions secrets.
2. Stop the Mac worker with `launchctl bootout gui/$(id -u)/com.coolway.mlb-tracker`. Do not run local Discord delivery and cloud delivery simultaneously.
3. Run `python seed_cloud.py` with the working Python on the Mac. This creates `data/state.json.gz`, containing only games, saved original predictions, hashed webhook destination identifiers, Discord message IDs and delivery records. No webhook, API key, event logs or metadata is exported. These game and delivery records will be publicly readable on the state branch.
4. Create a branch named `bot-state` and place the exported file at its root as `state.json.gz`. This carries existing message IDs across the migration so current games are edited rather than reposted. Never start with an empty state to recover from a failed run. Keep the state branch and history; do not reset it.
5. Enable Actions and allow the workflow's `contents: write` permission. Branch rules must allow the workflow to update `bot-state`. The workflow must be on the default branch. Run **MLB predictions and Discord → Run workflow**, then verify a green run and an updated Discord card before treating the migration as complete.
6. After successful verification, remove/disable the Mac LaunchAgent so it cannot resume on next login. If cloud activation fails, keep cloud disabled while reverting to the local worker.

## Schedule and cost

Standard `ubuntu-latest` public-repository runners; Python standard library only. Every 15 minutes at minutes 07/22/37/52 from 11:00 through 06:59 UTC, plus hourly overnight checks at :22 from 07:00 through 10:59 UTC. This covers the ordinary MLB day, with overnight checks for finals and international games. UTC does not change with daylight saving time. No Actions artifact/cache storage is used for state.

GitHub schedules can be delayed or dropped under load; this is not minute-by-minute hosting. Public repository scheduled workflows may be disabled after 60 days without repository activity. Check Actions health periodically and re-enable if needed. Standard public runner compute is free under GitHub's current policy; private repositories and larger runners have different billing rules.

Sources: [GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).

## Restart safety

One concurrency group serializes scheduled and manual runs. State is an allowlisted compressed JSON snapshot on `bot-state`, written through the Contents API with its previous SHA (compare-and-swap). Missing, corrupt, inaccessible, or conflicting state stops delivery. Original model probabilities/inputs are saved before sending. New cards get a durable reservation before the webhook POST; confirmed Discord message IDs are saved immediately. Existing cards use PATCH. Restarted jobs reuse these records; state does not expire like a cache or artifact.

Discord webhook creation has no transactional connection to GitHub state. If posting succeeds but the response or next state write is lost, the reservation is held and the workflow reports the affected MLB game ID. It never blindly reposts. This favors avoiding duplicates over guaranteed delivery. A crash after reserving but before posting can also leave an unsent held card. Explicit 429 rejections can safely retry next run. Missing/deleted cards are not automatically recreated in cloud mode.

To resolve a reservation, inspect the Discord channel: if the card exists, retrieve its message ID and update the matching cloud delivery record to `sent` with that ID and an empty fingerprint; the next cycle edits it. Remove a reservation only after verifying no message was created. Save the repaired compressed snapshot on `bot-state` while the workflow is disabled, then re-enable it. Never wipe state to fix a delivery error. Changing webhook destinations intentionally creates separate cards in the new destination.

No trades are placed. Kalshi comparisons are disabled under the external-service policy. The untrained model still excludes pitcher/lineup adjustments and does not support spreads. Predictions require sufficient prior-day regular-season stats. Existing predictions stay fixed; scores and prices refresh each scheduled cycle.
