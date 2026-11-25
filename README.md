# launch

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

`launch` selects an explicit model target for a T3 child from `worker_pool` in `model-policy.yaml`, enforces the native Claude quota rule, queries the bounded T3 thread API, and writes one JSONL receipt line per launch. It runs on Linux with Python 3.9+ and PyYAML. Run `make check` for the tests and the 250-line limit. Install the commands with `make install`.

```sh
launch pick --app example --change settings-icon --role implementation --tier strong
launch pick --app example --change settings-icon --risk high
launch done --thread "$child_thread_id"
launch receipt installed --app example --change settings-icon --by maintainer --evidence 'Sprint ready: main <sha>'
```

Pass `target` (`{providerInstanceId, model, options}`) to `delegate_task` with `clientRequestId` set to `launch_id` and `isolation` set to `worktree`.

### Rules

- **Explicit model for the child:** `pick` filters enabled `worker_pool.models` by `--tier` and `--role` (`rebase` for `mechanical`, `feature` otherwise). It uses the row's own effort or `thought_level` (default `medium`), or `high` with `--risk high`, mapped to the provider's option id (`codex`: `reasoningEffort`, `cursor`: `reasoning_effort`, `antigravity`: `thought_level`, `claudeAgent`: `effort`).
- **Zecori quota:** With a fresh local feed, `pick` skips exhausted, cooling down and unverified pools. It selects the largest remaining fraction, with policy order breaking ties. Zecori aggregates the limiting windows per account and publishes the best available account for each provider or Antigravity model group. Verified Codex promo credits keep the pool eligible when its windows are empty, behind pools with a positive measured fraction. If the feed is unavailable or older than 30 minutes, `pick` uses policy order and explains the fallback in `why`.
- **Native Claude quota rule:** Native Claude (`claudeAgent`) is eligible only with `--risk high`, when an active account in `snapshot.json` `claude_usage` has **> 50%** of its 7-day window left, and native Claude stays **≤ 15%** of launches over the last 7 days.
- **T3 API + receipt:** `pick` appends one JSONL line per launch to `receipts.jsonl`. `done` reads the latest run via the bounded T3 HTTP API (`/api/orchestration/threads/<id>/bounded`).
- `pick` accepts the legacy `--size S|M|L` flag without affecting model selection. `receipt installed` appends installation evidence to the same JSONL journal.

### Configuration

- `LAUNCH_LOG_ROOT` defaults to `~/.local/state/launch`, with receipts at `receipts.jsonl`.
- `LAUNCH_POLICY` defaults to `~/.config/launch/model-policy.yaml`.
- `LAUNCH_FEED` defaults to `~/.local/state/launch/quotas-live.json`.
- `LAUNCH_SNAPSHOT` defaults to `~/.local/state/launch/snapshot.json` and supplies native Claude windows and Codex promo-credit eligibility.
- `LAUNCH_T3_ENV` defaults to `~/.config/t3/api-token.env` (`T3_URL` and `T3_TOKEN`).

## launch-schedule

`bin/launch-schedule` replaces T3 scheduled tasks, whose model is frozen at creation. A systemd user timer (`systemd/launch-schedule.timer`, every minute) runs it on the operator host. For each job in `~/.config/launch/schedule.yaml` whose slot started less than 2 hours ago and has not run yet, it calls `launch pick` and dispatches the job's prompt file into the bound T3 thread (`message.dispatch` over the T3 WebSocket, `queue_after_active`) with the picked `modelSelection`; `fresh: true` adds `freshContext`.

- **Once per slot:** the state file (`~/.local/state/launch/schedule-state.json`) records each job's last slot. `commandId` is derived from job and slot, so T3 deduplicates a replay. A failed dispatch retries with the same target until the 2-hour window closes.
- **Deny is never silent:** a pick deny or a first dispatch failure sends a notice to the Telegram destination in `LAUNCH_TG_ENV` and the T3 thread in `LAUNCH_NOTIFY_THREAD`, when configured.
- `launch-schedule --dry-run [--date YYYY-MM-DD]` prints the picks for a day (default tomorrow) using a copy of the receipts journal; it sends nothing.
- Schedules use `LAUNCH_TIMEZONE` (an IANA zone name such as `Europe/London`, defaults to UTC). Both commands read `launch/instance.env` under `$XDG_CONFIG_HOME` (default `~/.config`). Environment variables take precedence. Use one literal `LAUNCH_KEY=value` assignment per line.
- Overrides: `LAUNCH_SCHEDULE`, `LAUNCH_SCHEDULE_STATE`, `LAUNCH_BIN`, `LAUNCH_T3_ENV`, `LAUNCH_TG_ENV`, `LAUNCH_NOTIFY_THREAD`.
- To seed a missing operator configuration during install, run `make install INSTANCE_CONFIG=/path/to/instance.env`. An existing instance configuration is preserved. After checking `launch-schedule --dry-run`, reload the user systemd manager and enable the timer.
