# Configuration reference

One JSON file per loop under `~/.hermes/review-loops.d/` (override the directory with
`REVIEW_LOOP_CONFIG_DIR`). Everything the loop knows about a repository is here, which is what lets
one install serve several repositories with different seats, budgets and credentials.

`hermes review-loop init` writes this file for you; hand-editing is fine, and the loader is strict
on purpose — a loop that cannot be resolved to a repository, a base branch and two seats is a
configuration error, not a run that guesses.

## Required

| key | meaning |
|---|---|
| `repo` | `owner/name`. The gate selects the loop from the webhook payload's `repository.full_name`. |
| `fixers` | GitHub logins allowed to open/push the PRs this loop works on. |
| `reviewers` | GitHub logins whose verdicts count toward the budget. |
| `seats.reviewer.route` / `seats.fixer.route` | the webhook routes that wake each seat. |
| `seats.reviewer.profile` / `seats.fixer.profile` | the Hermes profile each run happens as. |
| `reviewer_seat` | which reviewer login the reviewer route serves (a request aimed at anyone else is not this loop's business). |

## Everything else

| key | default | meaning |
|---|---|---|
| `id` | repo name | loop id; also the prefix of the generated routes |
| `base` | `main` | only PRs targeting this branch are in the loop |
| `cap` | `3` | verdicts allowed before escalation (`cap - 1` fix turns) |
| `seats.<seat>.login` | first fixer/reviewer | the GitHub login that seat acts as |
| `seats.<seat>.agent` | profile name | display name used in start-pings and prompts |
| `seats.<seat>.channel` | profile's `DISCORD_HOME_CHANNEL` | where the start-ping goes |
| `seats.<seat>.emoji` | 🔍 / 🔧 | cosmetic, for the ping |
| `adjudicator.route` | — | route woken when the cap is spent; omit to only write the marker |
| `adjudicator.profile` | `default` | profile the adjudication run happens as |
| `skill` | — | skill the seats are told to load |
| `tokens` | `{}` | `login → path of a file containing that seat's PAT (mode 600)` |
| `read_token` | first token | login whose token performs reads |
| `clone` | — | the local clone reviews may use; cleanup prunes its worktrees |
| `roots` | `[]` | directories the cleanup may ever touch. Anything outside them is out of scope. |
| `concurrency` | `1` | runs allowed at once per seat. `1` = serialized; `2+` = parallel, which requires `clone` because each PR then gets its own isolated clone (and an unisolatable run is queued, not shared). Change it with `hermes review-loop set --concurrency N`. |
| `state_dir` | `~/.hermes/state/review-loops/<id>` | locks, queue, in-flight marks, breach markers, artifacts, watchdog memory |
| `host` | `https://hooks.coemedia.us` | gateway webhook host |
| `grace_min` | `25` | how long a quiet head is allowed to sit before the watchdog speaks |
| `marker_grace_min` | `60` | how long a breach marker may sit unpicked-up |
| `cooldown_h` | `6` | repeat suppression per stall |
| `ttl_min` | `45` | seat-lock lifetime; past this a crashed run has lost its seat |
| `inflight_ttl_min` | `10` | how long a same-head burst is considered already handled |

## State files (per loop, under `state_dir`)

| file | what it holds |
|---|---|
| `locks.json` | `{seat: {"repo#PR": {at, head, why}}}` — live runs per seat; `concurrency` of them may be active at once |
| `pending.json` | `{seat: {"repo#PR": {at, head, url, reason}}}` — queued, not run |
| `inflight.json` | `{"review:PR:sha" / "fix:PR:sha": ts}` — this head is already being handled |
| `breach.json` | `{"repo#PR": {head, rounds, cap, reason, at, status}}` — awaiting adjudication |
| `watchdog.json` | `armed_since` baseline, alert history, last run |
| `watchdog.log` | one line per sweep, and per breach |
| `artifacts/<PR>/` | where a run must keep its worktrees, build dirs and logs |

## Environment overrides

| variable | effect |
|---|---|
| `HERMES_HOME` | where `.hermes` lives (default `~/.hermes`) |
| `REVIEW_LOOP_CONFIG_DIR` | where loop configs live |
| `REVIEW_LOOP_SUBS` | the gateway subscription file to read/write routes from |
| `REVIEW_LOOP_GH_STUB` | test hook: an executable that answers API paths from argv |
| `REVIEW_LOOP_TEST` | ignore the paused check and apply zero grace (real data, for a manual probe) |

## Deliberate non-features

- **No config_schema / settings form.** The interesting configuration is per repository, and a
  per-profile settings form would be the wrong shape for it. Loop files are plain JSON you can read
  and diff.
- **No auto-update, no telemetry, no network beyond GitHub, Discord and your own gateway.**
- **No deploy/hosting integration and no model provider assumptions.** The seats are Hermes profiles;
  what model each profile runs is the operator's business.
