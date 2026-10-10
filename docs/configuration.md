# Configuration reference

[Getting started](getting-started.md) · [Operations](operations.md) · [Security](security.md) · [Troubleshooting](troubleshooting.md)

Diaktoros has three configuration layers: a repository's loop JSON, per-profile plugin defaults, and a private host runtime JSON. A Hermes profile supplies the seat's model; it is not an account credential embedded in the loop file. This reference describes the implemented loaders and their limits, not a JSON Schema that rejects every unknown key.

## Contents

- [Files and precedence](#files-and-precedence)
- [Example loop file](#example-loop-file)
- [Repository and identity keys](#repository-and-identity-keys)
- [Capacity and pacing](#capacity-and-pacing)
- [Timers and turn budgets](#timers-and-turn-budgets)
- [Paths and cleanup boundaries](#paths-and-cleanup-boundaries)
- [Write policy and attribution](#write-policy-and-attribution)
- [Adjudication](#adjudication)
- [Observer configuration](#observer-configuration)
- [Issue triage and issue fixes](#issue-triage-and-issue-fixes)
- [Plugin settings and safe application](#plugin-settings-and-safe-application)
- [Runtime file and seat models](#runtime-file-and-seat-models-review-loop-runtimejson)
- [Environment overrides](#environment-overrides)
- [State files](#state-files)
- [Validation and coverage limits](#validation-and-coverage-limits)

## Files and precedence

| Layer | Location | What it controls |
|---|---|---|
| Loop | `$HERMES_HOME/diaktoros.d/<id>.json` | Repository, branches, identities, routes, budgets, policy, and per-loop state paths. `DIAKTOROS_CONFIG_DIR` overrides the directory. |
| Plugin settings | `plugins.entries.diaktoros.settings` in the active Hermes profile's configuration | Defaults for `init`; explicitly named values pushed to one existing loop by `apply`. |
| Host runtime | `$HERMES_HOME/diaktoros-runtime.json` | Four host installation paths and optional model overrides. |
| Seat profile | The selected Hermes profile's configuration and authentication sources | Provider, model, endpoint, and credentials resolved on the host for that seat. |

`HERMES_HOME` defaults to `~/.hermes`. In this document it means the home used by the Diaktoros host process; do not assume a command launched under another profile sees the same loops or runtime file.

**Names from before the rename.** Diaktoros was called hermes-review-loop before v0.2.0, and what was set or written under the old names keeps working:
- **Host files and the watchdog job:** an install's `review-loops.d`, `review-loop-runtime.json`, `review-loop-runs.sqlite` and the rest are used where they are until `hermes dk migrate` moves them (see [moving to a renamed plugin](operations.md#moving-to-a-renamed-plugin-or-repository)). `doctor` names any that are left. The same goes for the `review loop watchdog` cron job.
- **Environment variables:** every `DIAKTOROS_*` setting is also read as `REVIEW_LOOP_*` when the new name isn't set.
- **Records already written:** fixer-answers comments that carry the old `<!-- review-loop:fixer-answers` marker are still records. Open issue-fix PRs on `review-loop/issue-N` branches still count as issue fixes. Log lines that start with `[review-loop]` are still read by `trace` and `review`.

The plugin itself only writes the new names.

`init` writes a loop file. `set` changes a named loop; `apply` explicitly overlays plugin settings and coordinates identity changes with installed routes/hooks. `fixer-push` separately controls unattended writes. Changing the settings form alone does not update existing loops. See [Operations](operations.md) for lifecycle commands and [Concepts](concepts.md) for roles and review rounds.

Use a regular, non-symlink `<id>.json` file whose `id` matches its filename. ID lookup rejects path components and `.`/`..`. One repository must have exactly one owning loop. Gates select ownership from the webhook's `repository.full_name`, not from a route's prompt or a sandbox request.

A refused sibling file with a readable, different `repo` is skipped with a warning on the wake path. If an invalid file might own the event's repository—including unreadable JSON—or two files claim it, the gate fails closed. Read-only listings and the cron watchdog can report bad files while continuing with readable loops; operations acting on the entire set may instead refuse the set.

## Example loop file

Replace every quoted angle-bracket placeholder before use. These placeholders illustrate structure; they are not valid profile names, identities, or gateway origins. All credential entries below are **file paths, never token values**.

```json
{
  "id": "<loop-id>",
  "repo": "<owner>/<repository>",
  "base": "main",
  "cap": 3,
  "fixers": ["<fixer-login>"],
  "reviewers": ["<reviewer-login>"],
  "reviewer_seat": "<reviewer-login>",
  "seats": {
    "reviewer": {
      "route": "<reviewer-route>",
      "profile": "<reviewer-profile>",
      "login": "<reviewer-login>",
      "agent": "<reviewer-display-name>"
    },
    "fixer": {
      "route": "<fixer-route>",
      "profile": "<fixer-profile>",
      "login": "<fixer-login>",
      "agent": "<fixer-display-name>"
    }
  },
  "read_token": "<reader-login>",
  "tokens": {
    "<reader-login>": "<absolute-reader-token-file>",
    "<reviewer-login>": "<absolute-reviewer-token-file>",
    "<fixer-login>": "<absolute-fixer-token-file>"
  },
  "host": "<https-gateway-origin>",
  "concurrency": 1,
  "turn_budget_s": 900,
  "unattended_fixer_push": false,
  "attribution": true,
  "fixer_check": "",
  "review_after_ci": false,
  "fix_ci": false,
  "required_checks": [],
  "review_only": []
}
```

This example leaves adjudication, observer delivery, triage, cleanup roots, and seat-specific overrides off. Add those only when configured. Use [Accounts](accounts.md) for credential preparation and [Getting started](getting-started.md) for an installation sequence.

## Repository and identity keys

Defaults below are loader defaults unless explicitly marked as CLI behavior. A required operational mapping may be checked by installation/preflight or the broker rather than by `normalize()` alone.

| Key | Default | Accepted value and behavior |
|---|---|---|
| `repo` | Required | Repository full name in `owner/name` form. Normalization trims and lowercases it and checks that it contains exactly one slash; that check alone is not complete GitHub name validation. |
| `id` | Repository name component | Loop identifier; must match the JSON filename on ID lookup. Prefer a simple filename-safe identifier; no path components. Generated route names use it as a prefix. |
| `base` | `main` | Branch the loop serves. PRs based elsewhere do not receive ordinary reviewer/fixer authorization. Stacked PR visibility and same-head retarget boundaries are described in [Architecture](architecture.md). |
| `cap` | `3` | Whole number at least `2`; no explicit loader maximum. Counted changes-requested verdicts spend this budget; comments, pending, and dismissed reviews do not. At the cap, no additional ordinary fix round is purchased: the PR is escalated unless approved. Normally this allows `cap - 1` fix turns. |
| `fixers` | Required, non-empty | List of trusted GitHub logins whose PRs the loop serves. Values are lowercased. This is an authorization allowlist, not a list of profile names. |
| `reviewers` | Required, non-empty | List of GitHub logins whose reviews count. Values are lowercased. |
| `reviewer_seat` | Reviewer seat login | Login served by review requests. Required after fallback. Keep it aligned with `seats.reviewer.login` and included in `reviewers`; settings application updates the seat login and this selector together. The loader does not independently enforce their equality or this selector's allowlist membership. |
| `seats.reviewer.route`, `seats.fixer.route` | Required | Non-empty route names; the two must differ even on load. Installed routes must be owned by this loop and run the correct role's gate. |
| `seats.reviewer.profile`, `seats.fixer.profile` | Required | Hermes profiles used by the seats. Installation checks existence and independent profile homes. The reviewer and fixer must not share a profile or login. |
| `seats.reviewer.login` | First `reviewers` entry | GitHub identity used by reviewer writes; must be allowlisted and have its own token mapping when installing/moving the seat. |
| `seats.fixer.login` | Only `fixers` entry, otherwise empty | GitHub identity used by fixer writes. With multiple fixers, explicitly choose the seat login; membership in an author allowlist does not pick a write identity. |
| `seats.<working-seat>.agent` | Profile name capitalized | Display name in prompts and attribution. Attribution sanitizes display text to letters, digits, spaces, and hyphens; this is not an account or model selector. |
| `skill` | Empty | Optional skill the seats are instructed to load. It does not grant network access or broker permissions. |
| `read_token` | Required; never inferred | Reader's own GitHub login. A file without it is refused; a first entry in `tokens` is never treated as the reader. |
| `tokens` | `{}` | Mapping from login to a PAT file path. Installation needs the reader and both write identities mapped. Keep spelling consistent with the configured logins. |
| `host` | Empty | Gateway HTTP(S) origin, optional on loader-only reads but required by `init`/installation. No path other than an optional trailing `/`, query, fragment, userinfo, whitespace, or malformed authority; explicit ports must be 1–65535. A trailing `/` is removed. Use HTTPS for public GitHub hooks. |

A named profile must start with an ASCII letter or digit and then contain only letters, digits, `.`, `_`, or `-`. Installation accepts `default` as the launch Hermes home; other profiles need a non-symlink directory under `$HERMES_HOME/profiles/` with a non-empty `config.yaml`. It also compares actual profile directory identity, so different names do not make aliases independent.

### Credential separation

The reader, reviewer, fixer, and optional adjudicator comment identity must be separate accounts with separate token files. Three identities suffice when rulings are operator-only. The adjudicator comment login must also be outside `fixers` and `reviewers`. File identity checks include resolved paths and, where available, filesystem identity: symlinks or hardlinks do not establish independence. Runtime broker checks additionally verify live `/user` principals before writes.

On a user-owned repository, a separate reader account is a collaborator, not a read-only repository role, and commonly needs a classic `repo` PAT because of fine-grained collaborator restrictions. Repository access and token scope are separate from the reader's read-only loop role. Do not assume collaborator access grants hook administration; use an authorized, mapped hook admin where needed. An unknown hook listing/state is not evidence that hooks are paused. See [Accounts](accounts.md#choose-pat-type-and-permissions).

Token paths supplied through settings and the optional adjudicator identity are inspected for an absolute path (`~` expands), regular-file target, current-user ownership, and no group/other permissions. Mode `0600` is the recommended shape; the metadata check is not a requirement for an exact `0600` bit pattern. GitHub token-path inspection follows symlinks and checks their targets. General installation credential verification also reads mapped files to reject empty/unreadable credentials. Metadata inspection and model-description preflight are not the same as credential verification or a live selftest.

To repair a missing reader, `set` has a narrow path that first verifies the rest of the file as if the reader were present:

```bash
hermes dk set --loop "<loop-id>" \
  --read-token "<reader-login>" \
  --token "<reader-login>=<absolute-reader-token-file>"
```

Do not paste PATs into JSON, plugin settings, command arguments, issue text, or model prompts. See [Security](security.md) and [Accounts](accounts.md) for scopes and account isolation.

## Capacity and pacing

| Key | Default | Accepted value and behavior |
|---|---|---|
| `concurrency` | `1` | Whole number at least `1`, with no explicit loader maximum. Default simultaneous runs **per working seat**, not a combined loop-wide total. Effective reviewer/fixer capacity above `1` requires `clone`, even though production turns use isolated exports. |
| `seats.reviewer.concurrency`, `seats.fixer.concurrency` | Loop `concurrency` | Whole number at least `1`. An explicit value pins that seat: later loop-wide changes do not override it. Missing, `null`, or empty values inherit. |
| `seats.adjudicator.concurrency` | `1` | Whole number at least `1`; does not inherit loop capacity. |
| `seats.triage.concurrency` | `1` | Whole number at least `1`; does not inherit loop capacity. |
| `seats.<seat>.daily_turns` | No cap | JSON integer 1–1000 for `reviewer`, `fixer`, `adjudicator`, or `triage`. Booleans and numeric strings are refused. Omit to remove the cap; zero is not a valid stored cap. |
| `seats.<seat>.max_steps` | 60 (reviewer); 40 (adjudicator); 24 (triage); 80 (fixer) | Agent steps one turn may take, JSON integer 8–200, for any of those four seats. An issue fix takes the fixer's value. The turn's model-call quota follows (`steps + max(8, steps/4)`: 24 → 32, 60 → 75, 80 → 100, 200 → 250). Set with `set --reviewer-max-steps N` / `--fixer-max-steps N` (0 returns to the default), or in the file for the adjudicator and triage. The turn budget still bounds the wall clock. |

The host run ledger enforces capacity. Every isolated turn receives its own exact-head export and sandbox, so increasing concurrency does not permit a shared writable checkout. One PR is not authorized for conflicting seats at once. Legacy files that explicitly pin each working seat to `1` retain those pins; `status` can explain why raising loop `concurrency` alone did not increase them.

Daily caps count turns started per loop/seat per **local day**. A capped turn waits until midnight without spending a retry. Provider usage-window holds are account-level and can affect several seats or loops using the same provider account. Subscription limits are shared with the operator's ordinary use; separate GitHub identities do not imply separate inference quotas. See [Operations](operations.md) for pacing status and recovery.

```bash
hermes dk set --loop "<loop-id>" --concurrency 2 --clone "<absolute-clone-path>"
hermes dk set --loop "<loop-id>" --reviewer-concurrency 2 --fixer-concurrency 1
hermes dk set --loop "<loop-id>" --reviewer-daily-turns 20
hermes dk set --loop "<loop-id>" --reviewer-daily-turns 0
```

The last command removes the reviewer's stored cap. Adjudicator overrides are file-level settings. Triage concurrency and turn budget are also file-only; `triage --enable --daily-turns` changes its daily cap (zero removes it). `issue_fixer` has no independent persisted seat configuration; see [Issue triage and issue fixes](#issue-triage-and-issue-fixes).

## Timers and turn budgets

| Key | Default | Meaning |
|---|---|---|
| `turn_budget_s` | `900` | One agent turn's wall-clock budget, 60–14400 seconds. Includes reading, building, testing, and submission inside the turn, not host prefetch/drain. |
| `seats.<seat>.turn_budget_s` | Loop budget | Override for reviewer, fixer, adjudicator, or triage; same 60–14400 range. Missing, `null`, or empty inherits. |
| `grace_min` | `35` | Quiet-head stall grace in minutes, raised per seat to cover its whole worst-case turn. |
| `marker_grace_min` | `60` | Grace for a marker awaiting adjudication. For an `adjudicating` marker without a live run, the threshold is at least the adjudicator's whole turn; with a live run it is not a stall. |
| `cooldown_h` | `6` | Hours between repeated alerts for the same persistent stall or stuck entry. A cleared condition that returns can alert immediately. |
| `ttl_min` | `45` | Seat-claim backstop in minutes, raised to the seat's whole worst-case turn. The watchdog's dead-claim threshold is twice that effective TTL. |
| `inflight_ttl_min` | `10` | Minutes a same-head in-flight mark suppresses duplicate handling. The run ledger provides additional durable deduplication. |

Use positive JSON integers for minute/hour timers. Unlike capacity and budget fields, the loop normalizer does **not** comprehensively type/range-check these timers. A malformed hand edit may survive load and fail in a consumer; zero can trigger fallback behavior in some consumers. This is not a supported way to disable a timer.

Budgets accept values convertible to integer seconds within range, but use JSON integers: the current budget checker rejects booleans yet can truncate a fractional numeric value through `int()`. Do not rely on that coercion. `cap` and concurrency use a stricter whole-number check.

### The complete turn clock

A production turn can include:

1. Host dependency prefetch, bounded at 300 seconds, before the agent budget starts.
2. The selected seat budget, passed to Hermes as `--run-budget`.
3. A 30-second sandbox kill grace after the agent budget.
4. Up to 600 seconds of broker drain for a write already in flight.

The default worst case is **1830 seconds (30.5 minutes)**. Grace/TTL calculations use the relevant seat's complete clock, not another seat's larger budget. The budget is recorded on the ledger row at enqueue; changing it affects new turns, not an already queued row. A claim's recorded budget prevents lowering a setting from shortening its live claim protection. The worker keeps its lease heartbeating through drain.

Hermes prompts the agent to wrap up near 80% of its budget; the sandbox's complete process tree is killed after the kill grace. A budget-exhausted turn is not automatically replayed. If no write occurred, increase the budget and use `retry` or a new eligible event. If a write completed during drain, that run is final and retry refuses it; a new head can have a fresh turn. If broker drain cannot settle a possible write, the run becomes `uncertain` and requires reconciliation, not automatic replay. A killed adjudicator returns its marker for re-arming. Other timeouts retain their own failure/retry classification.

```bash
hermes dk set --loop "<loop-id>" --turn-budget 1200
hermes dk set --loop "<loop-id>" --reviewer-turn-budget 1500 --fixer-turn-budget 1800
hermes dk doctor --loop "<loop-id>"
```

`status` and `doctor` show effective seat budgets and raised thresholds. `selftest --live-turn` uses the reviewer budget unless `--timeout` overrides it. See [Troubleshooting](troubleshooting.md) for timeout and uncertain-write recovery.

## Paths and cleanup boundaries

| Key | Default | Behavior |
|---|---|---|
| `clone` | Empty | Optional local clone; required when effective reviewer/fixer concurrency exceeds `1`. Cleanup uses its registered worktrees. A loader accepting a path is not proof of a valid Git clone; preflight checks the installation. |
| `roots` | `[]` | Dedicated cleanup discovery directories. Filesystem root, the current home directory, and ancestors of home are refused. Shared roots do not authorize deletion of another repository's PR artifacts. |
| `state_dir` | `$HERMES_HOME/state/diaktoros/<id>` | Per-loop locks, queues, markers, observations, diagnostics, run working directories, and dependency caches. Use a dedicated absolute path if overriding. |

The `_path()` helper expands `~`; it does not generally expand shell variables inside JSON paths. Runtime host-path consumers use literal `Path` values, so use absolute paths there rather than `~` or `$VARIABLE` strings. Do not use relative paths merely because a loader can retain them: their meaning depends on the launch directory.

Cleanup considers configured roots and the per-loop artifacts tree, rejects symlink components, and applies ownership checks. A plain child in a shared root must carry both the repository name and the PR token; a PR number alone is insufficient. Registered worktree ownership and detached-state checks also matter. The clone itself and unrelated paths are outside scope. These are discovery/deletion boundaries, not permission to delete every child. See [Security](security.md) for filesystem trust requirements.

## Write policy and attribution

| Key | Default | Accepted value and behavior |
|---|---|---|
| `unattended_fixer_push` | `false` | Strict JSON boolean. Only literal `true` in trusted host configuration opts into unattended fixer writes. Not inherited from plugin defaults. |
| `fix_ci` | `false` | Strict JSON boolean (#306). Set with `init`/`setup`/`set --fix-ci on\|off`, the settings form, or the loop file; `doctor` shows its state and reports a mismatch when it is on but `unattended_fixer_push` is off (the setting then does nothing). `true`, with `unattended_fixer_push` on: a failed *required* check (every check when `required_checks` is empty) on a fixer's PR becomes one fixer turn per head. The host gives the fixer the failing jobs' name, failing step and last 60 log lines as data; it fixes and pushes, and CI's next result answers it (no answers comment). Bounds: `ci_fix_cap` CI-fix turns per PR, outside the verdict `cap` (#539); once it is spent the reviewer reviews the red head (the `ci_failed` notice says so); the fixer's daily cap and pacing apply. A red head goes to the fixer first and no review verdict is spent on it while the budget lasts, whatever `review_after_ci` says (#579; that setting only decides whether a review waits for *running* checks). It never re-runs jobs, edits workflows or merges. |
| `ci_fix_cap` | `null` | CI-fix turns per PR (across its heads) before a red head goes to the reviewer, whole number 1-10; `null` = 3 (#539). Set with `init`/`setup`/`set --ci-fix-cap N` (`set` 0 = the default), the settings form, or the loop file; `doctor` names it with `fix_ci`. |
| `review_after_ci` | `false` | Strict JSON boolean. `true`: a reviewer turn whose head still has checks running waits instead of starting, with no model call, daily turn or retry spent, re-reading CI about every 90 seconds. It starts once nothing is running, or an hour after it was queued, and its prompt says what never finished. `status` and `explain` show the wait (`held: waiting for CI …`); no notice is sent for it. A *cancelled* check holds every review the same way, with this setting on or off, and sends one notice, since it needs a re-run by a person. Set with `init`/`setup`/`set --review-after-ci on\|off` or the settings form. |
| `human_paths` | `[]` | Glob patterns (#478; at most 50, unique, one printable line of up to 200 characters each; `fnmatch` rules, case-sensitive, `*` also matches `/`, so `.github/**` covers everything below it). If the PR's diff (read live from GitHub, including a renamed file's old name) touches one and the reviewer would approve, the approval is left to a person: no approval is written, the reviewer's turn ends as done (not retried, not a failure), the head is held (no further loop review there; `explain` says a person must approve; the watchdog does not call it a stall), and one `human_paths` notice per PR and head names the paths. A diff too large for GitHub to list in full (3,000 files) is held the same way. REQUEST_CHANGES and issue filing work as usual. An unreadable file list refuses the approval, and the reviewer may retry. A new head is reviewed afresh. Only the reviewer's approval through the broker is covered: it does not stop a person, another tool or branch protection from approving or merging. With no observer configured, or `human_paths` left out of its events, the refusal still happens but no notice is sent. Set with `init`/`setup`/`set --human-path GLOB` (repeat; `set --no-human-paths` clears), the settings form (comma-separated) or the loop file. |
| `required_checks` | `[]` | The check runs or status contexts that gate an approval, named exactly as GitHub shows them (at most 50, unique, one printable line of up to 100 characters each). Empty: every check gates. Set: only these gate the broker's APPROVE (a failed one is refused, a cancelled one is refused as needing a re-run), the review-after-CI hold (a required check that has not reported yet counts as running) and, later, #306. Others are shown to the reviewer as *optional* and never block. Host-owned: never read from a PR. Set with `init`/`setup`/`set --required-check NAME` (repeat; `set --no-required-checks` clears) or the settings form (comma-separated; a comma inside parentheses, as in a matrix name, stays part of the name). |
| `review_only` | `[]` | GitHub logins (at most 50, unique, lowercased) whose PRs the reviewer reviews but the fixer never touches, for example your own account. A review-only author's PR gets reviewer turns like a fixer's; a changes-requested verdict goes back to the author (the notice says "returned to the author") and starts no fixer turn. There is no adjudication on these PRs, and the watchdog never reports a fixer stall for them (a missing review is still reported). They have their own verdict cap, `review_only_cap` (below); at the cap the author re-requesting the reviewer is no bypass, and only a maintainer's `review --another-round` allows one more verdict. Below the cap the author may re-request the reviewer on their own PR. A login can't be both review-only and a fixer or a reviewer. Set with `init`/`setup`/`set --review-only LOGIN` (repeat; `set --no-review-only` clears) or the settings form (comma-separated). |
| `review_only_update` | `false` | Strict JSON boolean (a successful push sends an `updated` notice). A review-only PR that conflicts after a merge always gets one notice per head naming the merged PRs, the conflicting files (a host dry merge, nothing pushed) and the commands. `true`, with `unattended_fixer_push` on, also lets the host push a clean merge of the base into a same-repository review-only branch with a lease on the head it read, then one notice ("merged into #N at sha; review resumes"). A real conflict, a workflow change or a fork gets the notice only. Turning it on needs `--acknowledge-branch-push` (`init`, `setup`, `set`); `doctor` checks the pushing token can push. |
| `review_only_cap` | `null` | Verdicts per review-only PR, whole number 1-1000; `null` = `cap`. At the cap the reviewer declines with nothing spent until `hermes dk review --pr N --another-round` grants one more verdict on the current head. |
| `review_only_daily` | `null` | Reviewer turns per local day on review-only PRs, whole number 1-1000; `null` = no cap. Once spent they wait until midnight; the fixer's PRs are unaffected. |
| `fixer_check` | `""` | One command, one printable line of at most 500 characters (chain several with `&&`). Fixer and issue-fix turns are told to run it from `/work` with `CHANGED` set to the paths they changed (space-separated), and make it pass before they publish, besides the tests they touched: name the checks CI always runs (a lint, a suite-wide guard test) so a fix doesn't go red on them. `""` names none. A check can use `$CHANGED` to pick the tests that depend on the change; this repository's own is `python3 tests/affected.py --run $CHANGED` (#362), which runs the dependent test modules and the harness within a time budget. Set with `init`/`setup --fixer-check`, `set --fixer-check` (`''` clears it) or the settings form. Alongside it, every fixer turn gets a host-written registry checklist (#577): what else to update when a change adds a CLI command or flag, an observer event, a doctor check, a setting or a broker refusal, and the test that checks each. A fix round lists only the kinds its PR's diff adds; issue-fix, CI-fix and conflict turns list them all. |
| `attribution` | `true` | Strict JSON boolean. Adds footers to the loop's own reviews/comments/PR descriptions where applicable and an `Automated-By:` commit trailer. `false` adds neither. It does not rewrite manual posts. |

While unattended fixer pushes are off, a changes-requested verdict is held for the operator: no fixer ledger row, worker, or model turn starts. The command below is the supported policy-change path; `set` and `apply` do not change this switch.

```bash
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes dk fixer-push --loop "<loop-id>" --disable
hermes dk set --loop "<loop-id>" --attribution off
```

Enabling is **host-operator consent**, not proof that a PR owner or repository maintainer consented. The acknowledgement concerns the unavoidable race between checking a live PR and publishing. CLI policy writes serialize with broker admission/final push checks; uncoordinated manual edits are not an equivalent authorization mechanism. Opt-in does not upgrade old runs into write-authorized ones. See [Security](security.md) before enabling it.

## Adjudication

| Key | Default | Accepted value and behavior |
|---|---|---|
| `adjudicator` | `{}` | Disabled unless it has a route. A profile-only block normalizes to disabled; a nontrivial route-less block is refused. |
| `adjudicator.route` | None | Enables an isolated ruling when the cap is spent, or when the operator escalates a PR sooner with `escalate --pr N`. The legacy gateway route remains silent: it does not run a credential-owning adjudicator agent. |
| `adjudicator.profile` | `default` when enabled | Profile used to resolve the ruling model; installation requires independence from both working seats. |
| `seats.adjudicator.login` | Unset | Optional fourth GitHub identity used **only** to also post the ruling as a PR comment. Needs its own token mapping and private file. |
| `seats.adjudicator.concurrency` | `1` | Independent capacity, not inherited from loop concurrency. |
| `seats.adjudicator.turn_budget_s` | Loop budget | Ruling-turn budget override. |
| `seats.adjudicator.daily_turns` | No cap | Optional local-day cap, 1–1000. |

`seats.adjudicator` accepts only `login`, `concurrency`, `turn_budget_s`, `daily_turns`, and `max_steps`. Its profile and enabling route belong in the separate `adjudicator` block, not in that seat object.

```json
{
  "adjudicator": {
    "route": "<adjudicator-route>",
    "profile": "<adjudicator-profile>"
  },
  "seats": {
    "adjudicator": {
      "login": "<adjudicator-login>",
      "concurrency": 1,
      "turn_budget_s": 1200,
      "daily_turns": 10
    }
  }
}
```

This is a fragment: merge it into the existing loop without replacing the working seats, and add the adjudicator's token path. Omit `login` and its token entirely for operator-only rulings.

`init --adjudicator-route` and `setup` (which asks "Adjudicate a PR whose rounds are spent? Profile") create the route at install. On an existing loop, turn adjudication on or off with `set`:

```bash
hermes dk set --loop "<loop-id>" --adjudicator-profile "<profile>"   # route <id>-breach; --adjudicator-route NAME overrides
hermes dk set --loop "<loop-id>" --adjudicator off                   # removes the route and block; markers and rulings stay
```

It writes the route through the same path `init` uses (ownership check, intent record, gate shim) and the `adjudicator` block. Then add the optional comment identity:

```bash
hermes dk set --loop "<loop-id>" \
  --adjudicator-login "<adjudicator-login>" \
  --token "<adjudicator-login>=<absolute-adjudicator-token-file>"
```

`set --adjudicator-login ""` clears the comment identity. A settings form's blank identity instead means “leave unchanged.”

The gate writes a durable breach marker, then enqueues a ruling keyed by `breach:<rounds>` at the head. The worker rechecks PR state, draft/base/head/author, spent cap, approval, and marker before exporting the head read-only. The adjudicator may record one `ACCEPT`, `REJECT`, or `RESPEC` ruling through the broker; it never merges, pushes, or submits a review. The host records the ruling, issues the observer notice, and optionally posts the PR comment. The watchdog's operator outbox carries every ruling's reason even with no feed, a muted feed, or event filtering.

Without a route, only the marker is written and the operator decides what to do; run `set --loop ID --adjudicator-profile PROFILE` (above) to add adjudication later. Failed enqueue leaves `delivery-pending` for watchdog retry; ledger uniqueness deduplicates re-delivery. An ambiguous ruling-comment POST is `uncertain` and is not automatically retried.

## Observer configuration

The `observer` block is an optional **delivery-only feed**, not a model seat. It consumes no seat slot and has no agent on its route. `{}` or an absent block means no feed. See [Observer](observer.md) for notices, destination binding, and delivery recovery.

| Key | Default | Accepted value and behavior |
|---|---|---|
| `observer.route` | None | Required for a hand-written active feed. CLI `init`/`set` can generate `<id>-observe` when only a profile is named. |
| `observer.profile` | `default` | Hermes profile supplying the delivery destination. |
| `observer.deliver` | `telegram` | Gateway-supported real delivery target, such as `telegram` or `discord`. `init`/`set` refuse `log`: a delivery-only file/log destination cannot provide the feed. |
| `observer.events` | All eighteen events | List or comma/whitespace-separated string. Missing or empty means **all**, not none. Normalization lowercases, deduplicates, and sorts strings. Unknown names remain stored without a misconfiguration warning; unknown-only input silently matches no transitions. Use the event names below. |
| `observer.urgent_route` | unset | Second delivery-only route for urgent notices (`failed`, `held`, `escalation`, `ruling`, `stall`, `conflict`, uncertain); must differ from `observer.route`. Unset: one feed. `urgent_profile` and `urgent_deliver` default to the feed's. |
| `observer.digest_min` | `0` | Positive integer minutes batch routine notices (urgent ones are never batched) for a watchdog flush. Unparseable/non-positive values silently normalize to immediate mode, not a misconfigured feed. CLI flags require integers. No explicit upper bound. |
| `observer.mute` | `false` | Stop delivery while retaining configuration. Use a JSON boolean: this lenient loader uses truthiness, so the string `"false"` is truthy and would mute it. |

| Event | Transition |
|---|---|
| `opened` | Initial look at opened, ready-for-review, or reopened PR. |
| `handoff` | Fixer pushed and requested review. |
| `verdict` | Changes requested: fixer queued or held for the operator under push-off policy. |
| `approved` | Reviewer approved. |
| `escalation` | Durable cap-breach marker written; next is adjudication or the operator. |
| `ruling` | Isolated adjudicator recorded a ruling; the reason also reaches the operator outbox. |
| `stall` | Watchdog reports a quiet/stuck head. |
| `closed` | PR merged or abandoned and cleanup attempted; sent only for PRs this loop worked on (reviewed author, or breach, transition, queue or observer-ledger state). |
| `triaged` | Issue triage ended (labels, none fit, skipped, denied, uncertain). Links the issue. |
| `fixing` | Fix label handed an issue to the fixer, or the handoff was held. Links the issue. |
| `fixed` | Issue-fix write ended: PR opened/review requested, could-not-fix comment, or uncertain. Links the issue. |
| `failed` | Any isolated run's first failed attempt and its terminal failed/uncertain state. Links the issue for triage and issue-fix runs. |
| `held` | A run waiting on its seat's daily cap or its provider's usage window: when it resumes, and how to run it sooner. Once per hold. |
| `conflict` | A loop PR no longer merges into its base (GitHub reports a merge conflict): once per head. Urgent tier. |
| `ci_failed` | A required check is red at a loop PR's current head: once per head, naming the failed check(s) and linking the run. |
| `updated` | With `review_only_update` on, the host merged the base into a review-only PR's branch and pushed it (clean merge only). |
| `main_red` | A required check is red at the head of the base branch: once per main head, naming the failed check(s) and the PRs merged since main's last green head. Urgent tier. |
| `stale_approval` | A loop PR is approved at head H, and a required check at H is then red, cancelled, or never reported within 30 minutes of the approval: once per PR and head. Urgent tier. |
| `human_paths` | The reviewer would approve, but the diff touches a `human_paths` glob (or is too large to list), so the approval is left to a person: once per PR and head. Urgent tier. |

```bash
hermes dk set --loop "<loop-id>" --observer-profile "<observer-profile>"
hermes dk set --loop "<loop-id>" --observer-route "<observer-route>"
hermes dk set --loop "<loop-id>" --observer-events "verdict,escalation,closed"
hermes dk set --loop "<loop-id>" --observer-digest-min 30
hermes dk set --loop "<loop-id>" --observer-mute
hermes dk set --loop "<loop-id>" --observer-unmute
hermes dk set --loop "<loop-id>" --observer-disable
```

A malformed feed is deliberately not a loop-load failure: unusable shape/missing route is reported as `misconfigured`, while seats can continue. Notices carry transition metadata and a PR link, not PATs, signing secrets, diffs, or review bodies. The destination can still reveal private repository metadata; choose its audience accordingly.

Delivery is durably keyed by transition. Only definite **pre-POST** failures with explicit retryable evidence receive automatic retry, up to three attempts. A failed/ambiguous POST, stale delivery claim, or legacy failure without that evidence becomes `uncertain` for reconciliation; “HTTP 500” is not proof that nothing was delivered. Disabling/muting stops sending but does not erase owed records or their original destination binding. Re-enabling onto another host, route, profile, or target is refused while those records remain unsettled.

## Issue triage and issue fixes

`triage` is off by default. An enabled block uses an `issues` webhook and `gate_triage.py`; it is separate from review events. See [Issues](issues.md) for enablement, label creation, and maintainer handoff.

```json
{
  "triage": {
    "route": "<triage-route>",
    "profile": "<triage-profile>",
    "authors": ["<issue-author-login>"],
    "labels": ["<allowed-label>"],
    "max_labels": 3,
    "comment": false,
    "login": "<triage-login>"
  }
}
```

| Key | Default | Accepted value and behavior |
|---|---|---|
| `triage.route`, `triage.profile` | Required when enabled | Non-empty route/profile; the profile supplies the model. |
| `triage.authors` | Required | Non-empty list of non-blank author logins, trimmed/lowercased/deduplicated. Other authors are ignored before a model sees their issue. |
| `triage.labels` | Required | 1–100 case-insensitively distinct label names, each 1–50 characters without surrounding whitespace, commas, braces, backticks, or control characters matched by the loader. Only these labels may be applied. |
| `triage.max_labels` | `3` | JSON integer 1–10, not a boolean or string. Maximum labels applied to an issue. |
| `triage.comment` | `false` | Strict JSON boolean; enables one optional comment of at most 1000 characters. |
| `triage.login` | Reviewer seat login | Identity performing labels/comments; requires its own mapped token with `issues: write`, never the reader. It need not be a separate fourth adjudicator account. |
| `triage.fix_label` | Unset | Maintainer-applied label that hands an issue to issue-fixing. Same label syntax; must **not** be among `triage.labels`, so triage cannot authorize its own fixes. |
| `triage.auto_fix_labels` | Empty (person-only trigger) | Labels from `triage.labels` (never P0, P1 or P2) that hand an issue to the fixer automatically, host-side, after the triage write is recorded; the triage seat never applies `fix_label`. Needs `fix_label`. The lineage depth, held-for-unmerged-PR and already-claimed-by-a-PR guards apply; an issue also labelled P0–P2 is never offered. Set with `triage --auto-fix-label`, the settings form (`auto_fix_labels`) or the loop file. |
| `triage.auto_fix_daily` | 25 | Automatic issue fixes per loop per local day, 1–1000; separate from the fixer's turn cap, and it holds a hand-off once spent. Needs `auto_fix_labels`. Set with `triage --auto-fix-daily`, the form (`auto_fix_daily`) or the loop file. |
| `triage.maintainers` | Required with `fix_label` | Non-empty list of authorized label-applier logins. Refused when supplied without `fix_label`. The same logins may also request the reviewer seat on a fixer's PR to start a fresh review at its head (#375); that request never frees a fixer still holding the PR. |
| `triage.fix_daily_turns` | 10 | Issue-fix turns per local day, JSON integer 1–1000 (`triage --fix-daily-turns N`; 0 there restores the default). Issue fixes are **always** capped: each opens a new PR, so the per-PR verdict cap never bounds how many happen (#247). |
| `seats.triage.concurrency` | `1` | Whole number at least `1`. |
| `seats.triage.turn_budget_s` | Loop budget | 60–14400 seconds. |
| `seats.triage.daily_turns` | No cap | JSON integer 1–1000. |

Only the listed keys are allowed in `triage`. `seats.triage` accepts only capacity, turn budget, daily cap, and `max_steps`; put its identity in `triage.login` and model profile in `triage.profile`.
Concurrency and turn budget must be edited in the loop file; the `triage` command exposes
`--daily-turns` but no capacity or budget flags.

### Issue-fixer inheritance

An authorized handoff uses the runtime seat `issue_fixer`, but that seat has no independent persisted configuration:

- Profile/model and GitHub write identity come from the fixer.
- Capacity is one issue fix at a time, independently of fixer concurrency.
- Budget comes from loop `turn_budget_s`, **not** `seats.fixer.turn_budget_s`.
- No daily cap is inherited from `seats.fixer.daily_turns`.
- Agent steps **are** the fixer's: `seats.fixer.max_steps` (default 80) applies to issue fixes too.
- It creates only a fresh branch `diaktoros/issue-N`, not an existing branch.
- It requires enabled triage, a configured `fix_label`, and `unattended_fixer_push: true`.

Do not add `seats.issue_fixer` and expect it to tune this behavior: normalization does not retain it as a configurable seat. Its inference resolution uses the fixer seat; the runtime override seat names likewise do not include `issue_fixer`.

## Plugin settings and safe application

The desktop renders `plugin.yaml`'s `config_schema` under **Capabilities → Plugins → diaktoros**. `diaktoros/config.py::SETTINGS_SCHEMA` mirrors the manifest; tests check agreement. Settings are per Hermes profile and written through Hermes's configuration writer. See [Operations](operations.md) for the settings workflow.

| Setting | New-loop default | Loop destination |
|---|---|---|
| `cap` | `3` | `cap` |
| `reviewer_concurrency`, `fixer_concurrency` | `1` each | Working-seat capacities; placement rules below. |
| `clone` | Blank | `clone`; blank preserves an existing clone. |
| `base` | `main` | `base` |
| `grace_min`, `ttl_min`, `inflight_ttl_min` | `35`, `45`, `10` | Same loop timer keys. |
| `turn_budget_s` | `900` | Loop budget; explicit seat budgets still win. |
| `host` | Blank | Gateway origin; blank preserves an existing explicit host. |
| `reviewer_profile`, `fixer_profile` | Blank | Working-seat profiles; agent display name follows only when no explicit name exists. |
| `reviewer_login` | Blank | `seats.reviewer.login` and `reviewer_seat` together. |
| `fixer_login` | Blank | `seats.fixer.login`. |
| `reviewer_token_file`, `fixer_token_file` | Blank | `tokens` entry for the selected login; path only, with metadata checks before writes. |
| `adjudicator_profile` | Blank | `adjudicator.profile`, only on a loop already having an adjudicator route. |
| `adjudicator_login`, `adjudicator_token_file` | Blank | Optional adjudicator comment identity/path, only when an adjudicator route exists. |
| `review_after_ci` | `false` | Overlay targets `review_after_ci` when explicitly named. |
| `fix_ci` | `false` | Overlay targets `fix_ci` when explicitly named. |
| `required_checks` | Blank | `required_checks`, split on commas outside parentheses; blank keeps the loop's own list. |
| `review_only` | Blank | `review_only`, split on commas; blank keeps the loop's own list. |
| `fixer_check` | Blank | `fixer_check`; blank keeps the loop's own check. |
| `attribution` | `true` | Overlay targets `attribution` when explicitly named. For an explicit signing change, use `set --attribution on` or `off` for the named loop. |

Use `hermes dk set --loop "<loop-id>" --attribution on` or `--attribution off`
to change signing, then verify `status`.

For a new loop, CLI flags and supplied settings contribute to its initial values. For `apply`, absent, blank, or whitespace-only form values mean **not set here**, not “reset to schema default.” An empty form leaves an existing loop's numbers and identities alone. Form booleans accept `true/on/yes/1` and `false/off/no/0`; loop JSON booleans remain strict. Invalid setting conversions can fall back to schema defaults before loop validation; do not use that as input validation for hand edits.

Concurrency placement is significant:

- Both seat values explicitly set and equal: write their value as loop `concurrency`, remove redundant seat pins.
- Both explicitly set and different: loop default is `1`; only seats differing from it carry pins.
- Only one set: change only that seat's pin; leave loop capacity and the other seat alone.
- Neither set: leave all capacity values unchanged.

Allowlists, route names, reader identity, observer/triage blocks, roots, marker grace, cooldown, daily caps, and unattended push consent are not settings-form subscriptions. A form does not own all repositories.

```bash
hermes dk settings
hermes dk apply --loop "<loop-id>" --dry-run
hermes dk apply --loop "<loop-id>"
hermes dk apply --loop "<loop-id>" --while-busy
```

### Identity changes are staged

Installation/application checks profiles, allowlist membership, identity/file distinctness, credentials, and route ownership before writing. Profile existence/allowlist checks can be scoped to the roles being changed for legacy compatibility; do not mistake a successful numeric-only update for a full installation audit. `apply` checks the resulting reader separation even when it did not move the reader.

Moving a profile changes its webhook path (`/p/<profile>/webhooks/<route>`), so `apply` first reads repository hooks, snapshots owned routes, rebinds and reads routes back, updates and reads matching hooks back, then writes loop config. Failed writes/readbacks attempt rollback; incomplete rollback is reported for manual repair. Other loops' routes/secrets are not rewritten. `init` also restores previous owned state after partial route-installation failure.

`apply` refuses identity/token-path moves for a live seat unless `--while-busy` explicitly overrides that protection. The old run finishes under the identity it started with. Numeric changes are not protected by this busy-seat guard; they do not change an existing row's recorded budget. A dry run previews changes; it is not proof that live hooks or providers will work.

Use `doctor` for preflight, `status` for mapping/registry drift, and `selftest` only when ready for its live credential/provider probes. Ordinary `doctor` is read-only; `doctor --repair` is a different, state-changing operation. See [Troubleshooting](troubleshooting.md).

## Runtime file and seat models (`diaktoros-runtime.json`)

The isolated worker requires a private regular host runtime file at `$HERMES_HOME/diaktoros-runtime.json`. `setup` detects and atomically writes it with mode `0600`, preserving usable chosen paths and existing model overrides. Missing/invalid runtime configuration is a fail-closed hold, not permission to dispatch a gateway agent.

```json
{
  "source": "<absolute-hermes-source-path>",
  "venv": "<absolute-hermes-venv-path>",
  "runtime": "<absolute-python-installation-path>",
  "rust": "<absolute-rust-toolchain-path>"
}
```

| Key | Requirements and detection |
|---|---|
| `source` | Hermes Git checkout with `run_agent.py` and `.git`. Detection uses the imported `hermes_cli` location, then the virtualenv's parent. |
| `venv` | Has `bin/python` and `bin/hermes`. Detection tries the running interpreter's prefix, Hermes on `PATH`, then checkout `venv/` or `.venv/`. Packaged Hermes may run on a bundled interpreter outside this venv. |
| `runtime` | Directory containing both the venv interpreter's literal symlink target and its resolved installation. Detection finds their common installation root, not `/`. This path is bound at its same absolute location in the sandbox. |
| `rust` | Toolchain directory with `bin/cargo`. Detection prefers rustup's default, then stable/available toolchains, then a system cargo prefix; not the `~/.cargo/bin` rustup proxy. |

These four keys are mandatory non-empty strings; extra top-level runtime keys are rejected. Path-shape parsing does not prove the installation is usable: detection/selftest check the filesystem and interpreter. Use absolute paths. Unlike GitHub token-path helpers, runtime host paths are not generally tilde-expanded by consumers.

### Model precedence

For reviewer, fixer, adjudicator, and triage, resolution is:

1. Runtime `seats.<seat>` override, if explicitly present.
2. The seat's selected Hermes profile.
3. Legacy runtime top-level `model`, `upstream`, and `key_file`, only if profile resolution fails.

The normal path is each profile's model, resolved with Hermes's own provider/auth logic in a separate host child process. That child starts with a fresh environment, rather than inheriting another seat's key. Credentials remain in the host inference proxy; sandbox configuration contains the resolved model and local bridge with a dummy key. Change a profile's model with:

```bash
hermes -p "<seat-profile>" model
hermes dk models --profile-name "<seat-profile>"
hermes dk models --seat reviewer --loop "<loop-id>"
```

An unresolved seat never borrows another seat's credentials. With no explicit/legacy override it fails before its turn proceeds; the reason is recorded in the run ledger. Legacy fallback is warned about because all falling-back seats share one model/key.

### Explicit model overrides

A runtime `seats` object permits only `reviewer`, `fixer`, `adjudicator`, and `triage`. Each override must contain **exactly** these three non-empty strings:

```json
{
  "seats": {
    "reviewer": {
      "model": "<model-id>",
      "upstream": "<full-https-chat-completions-url>",
      "key_file": "<absolute-model-key-file>"
    }
  }
}
```

This is a fragment to merge with all four host-path keys. `upstream` must be a full HTTPS endpoint ending in `/chat/completions`, without credentials, query, or fragment. `key_file` holds a non-empty single-line static key, is a regular non-symlink file owned by the current user, and has no group/other permissions. These model-key checks differ from GitHub token references, which can follow symlinks.

Overrides always use `chat_completions` with a static key; OAuth seats come from profiles. A legacy top-level trio must supply all three keys or none. `setup` preserves overrides; it does not silently migrate or remove a legacy shared-model fallback.

### Interpreter dependencies and preflight limits

The runtime venv's interpreter must read the seat's `config.yaml` and import Hermes from `source`. YAML reading tries PyYAML (`yaml`), then `ruamel.yaml`, then JSON (valid YAML). A real YAML file with neither parser cannot resolve. A packaged launch interpreter having no YAML package does not imply the selected runtime venv lacks one; check the actual interpreter named here.

Every `anthropic_messages` seat needs the `anthropic` optional Hermes package in that runtime venv. This includes Claude subscription/API-key seats, MiniMax, and any other provider Hermes resolves onto Messages. `doctor`'s `extras:<seat>` is:

- Pass when the required import is available or no extra is needed.
- Fail when a known required package is absent.
- Warn when the wire is credential/session-dependent or the probe cannot decide.
- Skipped when the model itself is unresolved.

The documented install command is `hermes pm install --extra anthropic`; ensure it installs into the runtime venv used here. Named custom providers take their wire from their provider entry (`api_mode`/`transport` or URL), not simply `model.api_mode`. Built-in Anthropic, MiniMax OAuth, Nous, and OpenCode families can fix/derive their wire themselves. For example, Nous `anthropic/*` with `nous.anthropic_wire: native` needs Messages; `auto` can promote a session; unset/`chat` does not. Kimi Code without explicit endpoint/mode can depend on key type. OpenCode model-family decisions can depend on credential pooling. Doctor does not read those credentials to guess.

Description asks the selected runtime's own Hermes resolver helpers for endpoint/wire facts without credential lookup. If Hermes cannot be imported, the model fails; if required helper functions have moved, the result warns that Hermes was not asked rather than claiming agreement. A provider Hermes itself refuses fails preflight. Read-only model description is not proof of login validity, quota, or upstream success; `selftest` performs credential resolution and a small request per distinct resolution. See [Development](development.md) for pinned-Hermes agreement tests and [Troubleshooting](troubleshooting.md) for model failures.

### Supported inference wires

Support follows the **resolved `api_mode`**, not a promise that every provider name works. Upstream URLs/headers/model are host-selected. Streams relay as they arrive.

| Resolved mode | Typical supported providers/auth | Sandbox request path | Host upstream suffix |
|---|---|---|---|
| `chat_completions` | API-key custom/OpenRouter/DeepSeek providers; `qwen-oauth`; Nous on chat wire | `/v1/chat/completions` | `/chat/completions` |
| `codex_responses` | `openai-codex`, `xai-oauth`; API-key providers Hermes routes to Responses | `/v1/responses` | `/responses` (including the host-selected Codex backend path) |
| `anthropic_messages` | `anthropic`/Claude aliases with subscription or API key; `minimax-oauth`; other Messages endpoints | `/anthropic/v1/messages`, except the Claude-subscription bridge's Anthropic client path | `/v1/messages` |

A Claude subscription uses host-resolved Claude Code identity/headers and sandbox Anthropic configuration with a dummy OAuth-shaped token, preserving the system/tool-name conventions that subscription transport requires. API-key Messages seats use the named `review-loop-seat` provider at the bridge. Responses seats also use that named provider with their resolved wire.

The proxy drops sandbox authorization, API-key, beta/account/user-agent headers; only permitted Responses session-affinity headers (`session_id`, `x-client-request-id`) pass through. Host Hermes supplies upstream authentication and provider headers. A turn's model-call quota follows its seat's agent steps (`seats.<seat>.max_steps`, see above). Output-token caps are proxy policy, not loop JSON keys. In every mode a request for more than the cap is **clamped** to it, never refused; only a malformed or ambiguous limit is refused:

- Chat completions: 16384 output tokens.
- Responses: 16384. On the ChatGPT Codex backend the validated token-limit field is dropped because that backend rejects it, so call quota and subscription limits—not that field—bound output.
- Messages: 16384, including extended-thinking budget adjustment below the ceiling.

The experimental provider `claude-subscription-directsdk-experimental` is a host-process backend, not an HTTP upstream. It uses the plugin's DirectSDK client/native Claude login on the host; the sandbox still speaks chat completions to its bridge. It requires the experimental Hermes plugin and profile setup. Consult [Security](security.md) and [Troubleshooting](troubleshooting.md) before treating experimental transport as equivalent to an API-key provider.

Refused provider names include `copilot`, `copilot-acp`, `github-copilot`, `bedrock`, `aws-bedrock`, `vertex`, `google-vertex`, `vertex-ai`, `gcp-vertex`, `vertexai`, `azure-foundry`, and `moa`. Auto-detected/missing `model.provider`, `model.openai_runtime: codex_app_server`, and `bedrock_converse` are not supported. Other non-API-key authentication types outside explicitly supported OAuth/process paths are refused; resolved modes outside the three proxied modes are refused. Name/config policy refusals occur before credential lookup where possible; an unknown resolved wire can only be refused after Hermes resolution.

### OAuth refresh and shared limits

OAuth refresh happens on the host through Hermes's own profile/auth stores and lock behavior. The sandbox never receives refresh tokens. The proxy resolves again within 60 seconds of reported expiry (or a JWT's expiry when needed), and once after upstream 401, requesting rotation of that rejected token and retrying once. Per-profile thread/file locks under `$HERMES_HOME/state/diaktoros-seat-locks/` serialize concurrent refresh.

`openai-codex`, Claude subscriptions, `xai-oauth`, `qwen-oauth`, and Nous draw from their configured accounts' existing plans/windows. A busy loop can exhaust the operator's subscription, and operator use can starve the loop. Setting more concurrency or changing GitHub accounts does not create more provider quota.

## Environment overrides

Set production limits in the environment of the process launching the supervisor, normally the gateway, then restart that process. The worker launcher propagates the three size-limit variables. A shell-only export does not reconfigure an already running gateway.

| Variable | Default / accepted value | Effect |
|---|---|---|
| `HERMES_HOME` | `~/.hermes` | Host loop/runtime/state home; `~` expands. |
| `DIAKTOROS_CONFIG_DIR` | `$HERMES_HOME/diaktoros.d` | Alternative loop-file directory; `~` expands. |
| `DIAKTOROS_SUBS` | Default gateway subscriptions location | Alternative subscription/route registry path; useful only when the serving gateway reads the same registry. |
| `DIAKTOROS_HERMES` | `hermes` found on `PATH` | Hermes executable used when composing scheduler commands. |
| `HERMES_REAL_HOME` | Current home | Operator-home hint used by runtime detection when a gateway profile has redirected `HOME`. |
| `RUSTUP_HOME` | Operator home's `.rustup` | Toolchain discovery input to runtime detection. |
| `DIAKTOROS_CHECKOUT_SIZE_GIB` | `8`; integer 1–1024 | Writable `/work` tmpfs cap for working seats; also `/target` build tmpfs cap when checkout is read-only. |
| `DIAKTOROS_SCRATCH_SIZE_GIB` | `2`; integer 1–1024 | `/tmp` tmpfs for scratch, `TMPDIR`, `CARGO_HOME`, and `RUSTUP_HOME`. |
| `DIAKTOROS_CRATE_CACHE_GIB` | `2`; integer 1–1024 | Per-repository host dependency-cache byte cap. Prefetch exceeding it is killed and its additions removed. |
| `DIAKTOROS_GATE_BUDGET_S` | `20`; numeric seconds, `0 < value < 600` | Requested webhook gate clock; effective clock also fits the serving gateway script timeout. |
| `DIAKTOROS_WATCHDOG_BUDGET_S` | `600`; numeric seconds, `0 < value <= 86400` | Watchdog sweep clock. A gate-initiated drain supplies a shorter budget from its remaining time. |

Unparseable/out-of-range size limits fall back to defaults and are reported by `selftest`; mount-size refusals are also reported by `doctor`. They do not remove containment. Invalid gate/watchdog budgets fall back to defaults. The gate reads possible gateway timeout configurations and fits the smaller applicable limit when topology is ambiguous; increasing the requested budget alone cannot override the gateway's script timeout. See [Operations](operations.md).

### Resource bounds are not complete host quotas

The sandbox has no host networking; Cargo stays offline and prefetched registry data mounts read-only. Reviewer/fixer `/work` is a sized writable tmpfs populated from a read-only staged export. Adjudicator `/work` remains read-only and gets a separate sized `/target` for builds. The namespace root and `/dev` are read-only. Size values are caps, not reservations.

Tmpfs sizes bound data, not all inode metadata or process memory. `/home/agent` is a per-turn **host-backed writable bind** for Hermes state/output; these size caps do not bound that filesystem. Set host service memory limits and disk quotas where required, and account for simultaneous seats. No JSON setting enables arbitrary host binds, disables containment, or grants the sandbox network access. See [Security](security.md).

### Test-only and internal variables

`DIAKTOROS_GH_STUB` substitutes a test executable for GitHub responses; live selftest refuses it. `DIAKTOROS_TEST` makes watchdog probes bypass pause/grace checks and can operate on real data: it is **not** a dry run and should not be inherited by production services.

`DIAKTOROS_TEST_HOME_GUARD` plus `DIAKTOROS_TEST_GUARD_SENTINEL` arm the test harness's real-home/network tripwires; the guard variable alone does not. Related `DIAKTOROS_TEST_*`, `DIAKTOROS_GATE_REDRIVE`, `DIAKTOROS_WORKER`, sandbox workspace markers, `DIAKTOROS_TURN_BUDGET`, token-file handoff variables, and `DIAKTOROS_LEAK_LOG` are internal/test plumbing, not supported operator configuration. Follow [Development](development.md) rather than exporting them into a gateway.

## State files

State is operational data, not additional user configuration. Do not hand-edit locks, delivery statuses, or run rows to bypass admission or replay an uncertain write. See [Operations](operations.md) and [Troubleshooting](troubleshooting.md) for supported retry/reconcile paths.

| Per-loop location under `state_dir` | Purpose |
|---|---|
| `locks.json` | Visible copy of seat claims with head, timestamp, budget, and run identity; a run's own release frees only the claim that run wrote, but the gate handoffs (approval, verdict, review request, no-workspace requeue) free the seat's claim for that PR without naming a run, and a claim written before the run field existed carries none, so a release that names a run leaves it to its seat TTL. An uncertain run retains its claim until reconciliation. Capacity is enforced by the host ledger. |
| `pending.json` | Held turns, including runtime-unavailable and unattended-push-off holds; watchdog drains eligible entries. |
| `inflight.json` | Same-head reviewer/fixer marks written/cleared by workers and subject to in-flight TTL. |
| `breach.json` | Durable cap marker: `delivery-pending`, `awaiting-adjudication`, or `adjudicating`; current-head checks protect re-arming. |
| `route-intent.json` | Private mode-0600 record of owned installed routes, **including signing secrets**. Watchdog/doctor repair uses it; never share it as a diagnostic attachment. |
| `watchdog.json`, `watchdog.log` | Arming/head-observation clocks, alerts, sweep history and diagnostics. `watchdog.log` is bounded: past 256 KiB it is rewritten to its last 1000 lines. Invalid arming clocks re-baseline on a successful listing rather than pretending old heads are newly stalled. |
| `gate-failures.json`, `gate-failures/` | Failed gate events, bounded errors/tracebacks, and saved payloads for reporting/re-drive. |
| `github-reads.json` | Failed GitHub-read evidence used by diagnostics. |
| `stack-transitions.json` | Same-head base-retarget holds/fresh-review boundaries; old approvals and verdicts cannot authorize the new situation. |
| `observations.json` | Observer delivery ledger, receipts, attempts, destination binding, digest membership, and uncertain outcomes. |
| `artifacts/<PR>/` | Legacy per-PR workspaces still eligible for cleanup; production isolated turns do not use a shared clone-per-run workspace here. |
| `isolated-runs/`, `deps/` | Host-created private run work roots and dependency caches for isolated workers. |

| Host-wide location under `$HERMES_HOME/state/` | Purpose |
|---|---|
| `diaktoros-runs.sqlite` | Isolated run states/leases/budgets/retries, broker write-ahead records, rulings, triage, and issue-fix results. |
| `diaktoros-runs.sqlite.workers.log` | Detached worker diagnostics, rotated once to `.1` after 256 KiB. |
| `diaktoros-pacing.json` | Account usage-window holds and local-day start counts; account metadata, not credentials. |
| `diaktoros-seat-locks/` | Per-profile model-resolution/refresh serialization locks. |

`status` reports these structures; `explain` combines their non-pruning read views with live GitHub facts. It does not claim, prune, drain, or submit a write. Diagnostic files can still contain private repository metadata even when credentials are redacted. See [Architecture](architecture.md) for ledger ownership and [Security](security.md) before exporting logs/state.

## Validation and coverage limits

The authoritative implementation is `diaktoros/config.py`, with host-path detection in `runtime_detect.py`, model resolution in `seat_model.py`, containment in `contained.py`, and wake/admission behavior in `gate.py`. CLI installation, broker checks, the run supervisor, observer, watchdog, dependency prefetch, and cleanup add validations that cannot be inferred from a successfully parsed JSON file.

Important boundaries:

- Loop top-level and working-seat objects are not closed schemas: unknown keys can survive normalization without acquiring behavior. Conversely, unknown persisted seat objects such as `issue_fixer` are not retained as configurable seats.
- `seats.adjudicator`, `seats.triage`, `triage`, and runtime files/override blocks have explicit allowed-key checks. Observer parsing is deliberately lenient.
- Loader validation is not a complete credential, profile, route, hook, model-package, clone, or safety audit. Some identity checks are deferred/scoped for legacy updates; live principals and current PR facts are rechecked before writes.
- No configuration selects automatic merging, makes an observer an agent, grants an adjudicator push/review permission, or turns an unknown write outcome into permission to retry.
- This reference covers supported configuration surfaces and named operational state, not every internal/test environment variable or all provider-specific Hermes authentication options. Hermes provider behavior depends on the installed runtime revision; use preflight and live selftest rather than assuming a provider name proves compatibility.

Before arming a changed loop, run `doctor` for the named installation checks and review `status`. Follow [Getting started](getting-started.md) for first setup, [Operations](operations.md) for arming/applying/recovery, [Troubleshooting](troubleshooting.md) for holds, [Issues](issues.md) for issue workflows, and [Development](development.md) for offline tests. Keep loop configuration operator-owned and out of repository-controlled sandbox input.
