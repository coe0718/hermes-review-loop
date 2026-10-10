# Command reference

Every `hermes dk` command, what it is for, what it changes, and every flag it takes.
New to the loop? Read [How it works](concepts.md) first; the words used here (seat, route, hook,
reader, arm) are defined there.

The flag tables are generated from `diaktoros/cli.py:register_cli` with an empty
settings form; tests check them against that parser. They describe this checkout, not every
installed release. `hermes dk "<command>" --help` shows your installed parser.
The prose also follows the handlers, configuration validation and delegated scripts.

## Contents

- [Conventions and shared option rules](#conventions-and-shared-option-rules)
- [Seeing what you have](#seeing-what-you-have)
- [Installing and changing a loop](#installing-and-changing-a-loop)
- [Checking an install](#checking-an-install)
- [Turning it on and off](#turning-it-on-and-off)
- [When something is stuck](#when-something-is-stuck)
- [Housekeeping](#housekeeping)

Start with [Getting started](getting-started.md) and [Concepts](concepts.md). For credentials,
see [Accounts](accounts.md); for the operational workflow, [Operations](operations.md).
The companion references are [Configuration](configuration.md) and [Desktop settings](settings.md).
Read [Security](security.md) before authorizing writes. [Troubleshooting](troubleshooting.md),
[Observer](observer.md), [Issues](issues.md), [Architecture](architecture.md) and
[Development](development.md) cover recovery, optional features and implementation.

## Conventions and shared option rules

Replace every quoted angle-bracket placeholder before running an example. Quotes prevent
shell redirection; they do not make placeholder values valid configuration. A login is a GitHub
username; a profile selects a local Hermes identity/model. A loop id defaults to the repository
name and appears in `list`. PR numbers in examples are illustrative.

- Every command accepts argparse's `-h` / `--help`: print usage and stop, exit 0.
- A blank generated default cell means `None`, empty string/list, zero or a false switch,
  **not necessarily an optional value at runtime**. Boolean switches are off unless present.
  Repeatable options append occurrences; normal single-value options use the last occurrence.
- `init` starts from plugin defaults. `set` leaves omitted options unchanged. `apply` overlays
  only nonblank, explicitly stored form fields; it does not reset a loop to all schema defaults.
- `--loop` requires one exact id unless its table explicitly says all loops or the only loop.
  `--read-token` and `--admin-token` take **logins**, never token values; `--token` takes a
  `LOGIN=FILE` mapping. Token content is read on the host when validating/using credentials,
  never printed by these references. See [Accounts](accounts.md).
- “Read-only” here means no intended loop/config/route/GitHub mutation. Network reads, local
  subprocesses, lock creation or catalog caches are not a promise of zero filesystem activity.
  `explain` uses non-pruning state readers; `trace` writes temporary diagnostic copies.
- Exit 0 normally means completion, 1 a failed/incomplete check, and 2 refused input/config.
  argparse errors exit 2. `drain` and `cleanup` propagate their scripts' exit codes; no generic
  success code proves every remote effect or that a run has finished. Exceptions and later
  teardown failures may still leave partial work: inspect output and read back `status`.

### Accepted values and effective defaults

These rules supplement the generated tables and apply wherever the option is offered.
No setting named here is an extra CLI flag.

| Option family | Accepted value and effective default | Behavior / boundary |
| --- | --- | --- |
| `--repo`, `--id` | Repository in owner/repository form; id defaults to repository component | `init` rejects leading-dot/path ids. Loader checks one slash, not complete GitHub name syntax; use real repository identifiers. |
| Profile options | Existing profile name; reviewer/fixer blank until supplied or saved in settings | Names start alphanumeric and use letters, digits, `.`, `_`, `-`; reviewer/fixer profile homes must differ. Adjudicator profile comes from flag, setting, then launch profile. |
| Login / allowlist options | GitHub login; working-seat allowlists must not be empty | Reviewer/fixer/reader and optional adjudicator comment identity must differ. `/user` checks in selftest/broker establish actual token principals, not spelling alone. |
| Token-file options | File path, not a PAT; absolute paths or `~` paths recommended | Form paths, `set --token` and adjudicator mappings require an owned regular file with no group/other permission bits; symlinks are followed for metadata. `init --token` is checked the same way; doctor/selftest are still required. |
| `--cap` | Integer >= 2; fresh default 3 | Counts verdicts, not pushes; at cap, adjudication requires an adjudicator route. |
| Concurrency options | Integer >= 1; fresh default 1 | Reviewer/fixer inherit loop default unless pinned. Above 1 requires a nonblank clone path; validity of the checkout is checked separately. No CLI flag clears a seat override. |
| `--turn-budget`, per-seat budget options | Integer seconds 60–14400; fresh loop default 900 | Seat override wins; queued rows retain recorded budgets. `retry` uses current seat budget. Prefetch, kill grace and broker drain are outside the model budget. |
| Daily-turn options | Integer 1–1000; omitted means existing value or no cap; CLI 0 removes cap | Later turns wait for local midnight; applies only to the named seat. |
| Grace / TTL options | Integer minutes; fresh grace 35, marker grace 60, slot TTL 45, in-flight TTL 10 | Parser enforces integer type but loader has no positive range for these fields. Use positive values; actual slot/stall clocks also cover the seat's whole worst-case turn. |
| `--base` | Branch string; fresh default `main` | Gates select eligible PR targets; loader does not validate Git ref syntax. |
| `--clone`, `--root`, `--state-dir` | Local path strings; fresh clone/roots empty, state derived under `$HERMES_HOME/state/diaktoros/` | Roots authorize destructive cleanup of PR-named children; use dedicated directories. Dangerous broad roots are refused. Clone is not a seat's writable shared checkout. |
| `--host` | HTTP(S) gateway origin, optional port, no path/query/fragment/userinfo | Fresh unset; `init` requires it even without hooks. HTTPS is recommended. Trailing slash stripped. `set` refuses clearing it. |
| `--skill`, display-name options | String; fresh empty | Skill is a prompt instruction, not installed by this flag. Plugin skill identifier: `diaktoros:review-loop` (a loop that still names `hermes-review-loop:review-loop` is read as the new name). Agent display names are cosmetic, not identities. |
| `--attribution`, `--comment` | Exactly `on` or `off` | Attribution fresh on; triage comments fresh off. Attribution signs only plugin-mediated writes. |
| Observer events | Comma-separated `opened,handoff,verdict,approved,escalation,ruling,stall,closed,triaged,fixing,fixed,failed,held`; blank = all | Unknown names remain stored without a misconfiguration warning. Unknown-only input matches no transitions, so the feed is silent; use the listed names. |
| Observer digest | Integer minutes; non-positive = per-transition, positive = watchdog-flushed batches | Negative values silently normalize to immediate delivery, not a misconfigured feed. Hand-edited unparseable values do the same; CLI flags require integers. Muting preserves configuration; disabling removes its route but retains delivery history. |
| Delivery targets | Gateway/Hermes delivery string; observer fresh `telegram`, watchdog fresh `local` | Parser does not enumerate or verify configured destinations. Observer sends notices to another chat; verify privacy first. |
| `--schedule` | Hermes cron schedule string, e.g. `15m`; init omitted = no cron, setup default `15m` | Delegated to Hermes cron parsing, not independently validated by plugin argparse. One shared watchdog job is reused. |

Every flag used in each example below is explained either in that command's table or in these
shared rules. Tables inside `flags:` markers are generated; implementation caveats remain prose.

| I want to… | command |
| --- | --- |
| see my loops | [`list`](#list), [`status`](#status) |
| see what a loop did this week, and how long turns took | [`stats`](#stats) |
| install a loop | [`setup`](#setup) (guided), or [`init`](#init) (flag by flag) |
| check an install before going live | [`doctor`](#doctor), [`selftest`](#selftest) |
| move to a renamed plugin or repository | [`migrate`](#migrate) |
| back up an install, or restore one | [`backup`](#backup), [`restore`](#restore) |
| turn the loop on or off | [`arm`](#arm) |
| change a setting | [`set`](#set), [`apply`](#apply), [`settings`](#settings) |
| see every setting a loop has, its value and where it came from | [`show`](#show) |
| let the fixer push on its own | [`fixer-push`](#fixer-push) |
| label new issues automatically | [`triage`](#triage) |
| find out why a PR is not moving | [`explain`](#explain) |
| ask for a fresh review of a PR | [`review`](#review) |
| send a PR to the adjudicator now | [`escalate`](#escalate) |
| find out why a webhook started nothing | [`trace`](#trace) |
| run a failed turn again | [`retry`](#retry) |
| start a queued turn now | [`drain`](#drain) |
| check the reviewer still catches known problems | [`corpus`](#corpus) |
| see which models a seat can use | [`models`](#models) |
| free disk space | [`cleanup`](#cleanup) |
| remove a loop | [`uninstall`](#uninstall) |

---

## Seeing what you have

### list

Lists every configured loop on one line each: its id, repository, verdict cap, how many PRs
each seat may work at once, and the allowlisted fixer and reviewer logins. Read-only.

```bash
hermes dk list
```

A loop file that does not load is named on a `skipping <file>: <reason>` line instead, and the
command exits 2 so a script notices.

<!-- flags:list -->
No flags.
<!-- /flags -->

### status

Shows a loop's configuration and its live state: who serves each seat, the routes as they are
actually installed, the token files (paths, never contents), the observer feed, pacing (daily
caps, and any seat waiting for its usage window), queued and running turns, and when the
watchdog last ran. Read-only.

```bash
hermes dk status --loop "<loop-id>"
```

Without `--loop` it shows every loop. Use it to answer "what is this loop set to?" Use
[`explain`](#explain) to answer "why is this PR stuck?"

<!-- flags:status -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
<!-- /flags -->

### settings

Shows the plugin's settings form: the defaults a new loop starts from, and what `apply` would
push onto a loop. Each value is marked `[set]` (you changed it) or `[default]`. It also shows
the seat mapping the form holds and what each existing loop actually runs as. Read-only.

```bash
hermes dk settings
```

The form lives in the Hermes desktop app under **Capabilities → Plugins → diaktoros**. See
[settings.md](settings.md).

<!-- flags:settings -->
No flags.
<!-- /flags -->

### show

Every setting one loop has: the whole settings schema, the loop-file-only keys, and the nested
`triage`, `observer` and `adjudicator` keys. Each row shows the effective value (the one the loop
runs with), where it came from (`loop file`, `default`, or `derived`, e.g. `review_only_cap`
falling back to `cap`) and a one-line meaning. Settings that are on come first; settings that are
off but matter say what that means (`fix_ci: off — red CI on fixer PRs waits for a review`).
The plugin settings form is not a source for a running loop: a form value the loop does not hold
is a note, since `apply` pushes it. Token values are never read, only file paths. Read-only;
`--json` prints the same rows as JSON.

<!-- flags:show -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--json` |  |  | machine-readable output |
<!-- /flags -->

### stats

What one loop did over a window, and how long it took. Two sources:

- **The run ledger** (local; always shown): every seat turn in the window, by how it ended
  (succeeded, failed, waiting, cancelled, uncertain), how many needed a retry, how long a
  succeeded turn **ran**, and how long turns **waited** before starting (queues, daily caps,
  usage windows, CI holds). GitHub cannot tell you how long a turn took; this can.
- **GitHub**, with `--github`, read as the reader (one request per PR): PRs opened, merged and
  still open, by author; open → merge time; each reviewer's verdicts and time to their first
  review; and how many change-request rounds the loop's reviewer needed per PR.

Read-only: the ledger is opened read-only, and GitHub is only read.

```bash
hermes dk stats --loop "<loop-id>"
hermes dk stats --loop "<loop-id>" --since 30d --github --html ~/stats/index.html
```

`--json` prints the same data as JSON. `--html FILE` also writes one self-contained page, with no
scripts or remote assets. Both hold totals and timings only, with no error text, paths or prompt
content, so they are safe to publish: see [Publishing stats](operations.md#publishing-stats).

<!-- flags:stats -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: the only configured loop) |
| `--since` | `SINCE` | `7d` | window start: 7d, 24h or a date like 2026-09-28 (default 7d) |
| `--github` |  |  | also read the window's PRs and reviews from GitHub, as the reader (one request per PR) |
| `--json` |  |  | print the data as JSON |
| `--html` | `FILE` |  | also write one self-contained HTML page to FILE |
<!-- /flags -->

---

## Installing and changing a loop

### setup

A first install in one command, and the easiest way to start. It walks through five steps, each
using the same code as the command named in it:

1. **Runtime paths.** It finds the Hermes checkout, its virtualenv, the Python installation and a
   Rust toolchain, and writes the private runtime file `~/.hermes/diaktoros-runtime.json`
   (mode 600). A path already in the file that still works is kept; a broken one is replaced. The
   file is written only when every path passes the same checks `selftest` makes.
2. **The loop.** It asks the [`init`](#init) questions (repository, accounts, profiles, token
   files, reader, gateway address, observer, signing, hook admin), with the settings form's values
   as defaults. It shows `init`'s dry run, asks you to confirm, then runs `init`. A loop that
   already exists is kept as it is.
3. **The watchdog.** It schedules the shared watchdog job, only if it is missing.
4. **Checks.** It runs [`doctor`](#doctor) and [`selftest --no-model`](#selftest). Any ❌ stops it
   here, with the fix line above.
5. **Arm.** Only after a clean pass, and only if you say yes (the question defaults to no).

```bash
hermes dk setup --repo "<owner>/<repository>"
hermes dk setup --repo "<owner>/<repository>" --dry-run
```

Running it again is safe: whatever is already in place is kept, so a second run only repairs what
is missing or broken. `--dry-run` shows every step and writes nothing. `--yes` asks no questions:
your flags and the settings form are the answers and every confirmation is yes, except arming,
which still needs `--arm`. Without a terminal (in a script), it refuses unless you pass `--yes`.

`setup` asks "Adjudicate a PR whose rounds are spent? Profile [blank = no]" (or takes
`--adjudicator-profile`; the plugin setting is the default) and, on a profile, passes
`--adjudicator-route <id>-breach` to `init`. It never turns on issue triage. Turn adjudication on or off
later with `set --adjudicator-profile P` / `set --adjudicator off`
([configuration](configuration.md#adjudication)). For triage, see [`triage`](#triage).

**Defaults and limits.** Seat/login/host defaults come from the form; missing answers are
prompted interactively. Token paths default to the form or a suggested key-file path. Runtime
`--source` (Hermes checkout), `--venv` (its Python environment), `--runtime` (Python base install)
and `--rust` (toolchain root) override detection. `--admin-token` is a login; its separate file
flag maps it. `--observer-profile` names a destination profile, not a model seat. The setup
example uses `--repo` to select a repository and `--dry-run` to preview every step. Re-running
keeps an existing loop; it does not automatically repair every missing hook/route. Check output.

<!-- flags:setup -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--repo` | `REPO` |  | owner/name (asked when not given) |
| `--id` | `ID` |  | loop id (default: the repository name) |
| `--yes` |  |  | no questions: the flags and the plugin settings are the answers, and every confirmation is yes (arming still needs --arm) |
| `--dry-run` |  |  | show every step, write nothing |
| `--arm` |  |  | arm the hooks after a clean doctor and selftest |
| `--reviewer` | `REVIEWER` |  | reviewer GitHub login |
| `--fixer` | `FIXER` |  | fixer GitHub login |
| `--reviewer-profile` | `REVIEWER_PROFILE` |  | reviewer's Hermes profile |
| `--fixer-profile` | `FIXER_PROFILE` |  | fixer's Hermes profile |
| `--reviewer-token` | `REVIEWER_TOKEN` |  | reviewer's token file |
| `--fixer-token` | `FIXER_TOKEN` |  | fixer's token file |
| `--read-token` | `READ_TOKEN` |  | reader login (its own account) |
| `--read-token-file` | `READ_TOKEN_FILE` |  | reader's token file |
| `--host` | `HOST` |  | your gateway's webhook origin |
| `--admin-token-file` | `ADMIN_TOKEN_FILE` |  | hook admin's token file |
| `--schedule` | `SCHEDULE` |  | watchdog interval (default 15m) |
| `--watchdog-deliver` | `WATCHDOG_DELIVER` |  | where watchdog alerts go (default local) |
| `--admin-token` | `ADMIN_TOKEN` |  | hook admin login: the hooks are created (paused) as it |
| `--observer-profile` | `OBSERVER_PROFILE` |  | Hermes profile whose chat gets the loop's notices |
| `--adjudicator-profile` | `ADJUDICATOR_PROFILE` |  | Hermes profile that rules when a PR's verdict cap is spent; turns adjudication on (blank: off) (default: the plugin setting) |
| `--review-only-cap` | `N` |  | verdicts the reviewer gives one review-only PR before it waits for `review --another-round`, 1-1000 (default: the plugin setting, else the review cap) |
| `--ci-fix-cap` | `N` |  | CI-fix turns per PR before a red head goes to the reviewer, 1-10 (default: the plugin setting, else 3) |
| `--review-only-daily` | `N` |  | reviewer turns a day on review-only PRs, 1-1000 (default: the plugin setting, else no cap) |
| `--review-only` | `REVIEW_ONLY` (repeatable) |  | a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it) (default: the plugin setting) |
| `--required-check` | `REQUIRED_CHECK` (repeatable) |  | a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting) |
| `--human-path` | `HUMAN_PATH` (repeatable) |  | a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting) |
| `--fixer-check` | `FIXER_CHECK` |  | one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting) |
| `--review-only-update` | `on` \| `off` |  | push a clean merge of the base into a review-only author's PR branch (same repository only; off by default; turning it on needs --acknowledge-branch-push) |
| `--acknowledge-branch-push` |  |  | accept that the host pushes to a branch the loop does not own; required to turn --review-only-update on |
| `--review-after-ci` | `on` \| `off` |  | start each review after the head's checks finish (up to an hour) (default: the plugin setting, off) |
| `--fix-ci` | `on` \| `off` |  | hand a failed required check on a fixer's PR to the fixer (default: the plugin setting, off) |
| `--attribution` | `on` \| `off` |  | sign what the loop posts with 'Automated by Diaktoros' (default: the plugin setting, on) |
| `--reviewer-max-steps` | `REVIEWER_MAX_STEPS` |  | agent steps one reviewer turn may take, 8-200 (0 = default 60) (default: the plugin setting) |
| `--fixer-max-steps` | `FIXER_MAX_STEPS` |  | agent steps one fixer or issue-fix turn may take, 8-200 (0 = default 80) (default: the plugin setting) |
| `--fix-daily-turns` | `FIX_DAILY_TURNS` |  | issue-fix turns per day: refused here, a new loop has no fix label; set it with `triage --fix-daily-turns N` (0 = ignore) |
| `--source` | `SOURCE` |  | runtime file's source path (default: detected) |
| `--venv` | `VENV` |  | runtime file's venv path (default: detected) |
| `--runtime` | `RUNTIME` |  | runtime file's runtime path (default: detected) |
| `--rust` | `RUST` |  | runtime file's rust path (default: detected) |
<!-- /flags -->

### init

Installs a new loop for one repository. Through a staged installation it writes:

1. the loop config: `~/.hermes/diaktoros.d/<id>.json`;
2. the webhook routes in the gateway's registry: `<id>-review`, `<id>-fix`, plus the adjudicator
   route with `--adjudicator-route NAME` (any name; `<id>-breach` by convention) and `<id>-observe`
   with `--observer-profile`. Each gets a fresh secret. The adjudicator route never wakes an agent
   itself: naming it is what turns adjudication on;
3. the gate shims in each seat profile's `scripts/` directory, the small files the gateway runs
   when a route is called;
4. with `--hooks`, the two GitHub repo hooks, created **paused** (nothing happens until
   [`arm`](#arm));
5. with `--schedule`, the shared watchdog cron job, created only if it does not exist yet.

Everything is checked before the first file is written: the profiles exist, the logins are in
their allowlists, the four accounts (reader, reviewer, fixer, optional adjudicator) are distinct
and each has its own token file, and no route name belongs to someone else. A refused `init`
writes no installed loop. Config/route and hook-install failures attempt rollback, but shims,
cron and ping failures can leave a partial install. This is not one atomic transaction across
GitHub, routes and cron. Follow the failure output before retrying.

```bash
hermes dk init --repo "<owner>/<repository>" --fixer "<fixer-login>" --reviewer "<reviewer-login>" --fixer-profile "<fixer-profile>" --reviewer-profile "<reviewer-profile>" --read-token "<reader-login>" --token "<reader-login>=<absolute-reader-token-file>" --token "<reviewer-login>=<absolute-reviewer-token-file>" --token "<fixer-login>=<absolute-fixer-token-file>" --host "<gateway-origin>" --hooks --admin-token "<reader-login>" --schedule 15m --dry-run
```

**Always run it with `--dry-run` first.** That prints the seat mapping, the route URLs and the
credentials it would use, and writes nothing. Drop `--dry-run` to install.

Notes:

- `init` refuses a loop id that already exists. To change a loop, use [`set`](#set) or
  [`apply`](#apply).
- Flags you leave out come from the settings form (`--fixer`, `--reviewer`, the profiles, the
  token files, `--host`, the numbers). Explicit flags normally win. When the form names a
  fixer login, it must be in the explicit fixer allowlist; the handler selects that login.
  The reviewer comes from `--reviewer-seat`, a matching form login or a single reviewer.
- Exit `1` means it installed but something was not finished, for example the watchdog job could
  not be created or a hook's ping failed. The output names the command that finishes it.
- Accounts and tokens, step by step: [accounts.md](accounts.md).

**Example flag guide.** The long preview names the repository, fixer/reviewer allowlists,
two distinct seat profiles, reader login and three PAT-file mappings. It sets the gateway origin,
requests paused hooks using the mapped reader login as hook admin, schedules a 15-minute
watchdog and stops before installation with `--dry-run`. That reader account must have hook
write permission for this example. A separate hook-admin account instead requires an extra
`--token "<hook-admin-login>=<absolute-admin-token-file>"` mapping and its login in `--admin-token`.
`--arm` requires `--hooks` and makes hooks live immediately, without doctor's/setup's arming
check sequence. New loops always start with unattended fixer pushes off.

<!-- flags:init -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--repo` | `REPO` | **required** | owner/name |
| `--id` | `ID` |  | loop id (default: the repository name) |
| `--fixer` | `FIXER` (repeatable) |  | GitHub login that pushes (repeatable; default: the plugin setting) |
| `--reviewer` | `REVIEWER` (repeatable) |  | GitHub login that may review (repeatable; default: the plugin setting) |
| `--reviewer-seat` | `REVIEWER_SEAT` |  | the login the reviewer route serves |
| `--reviewer-profile` | `REVIEWER_PROFILE` |  | Hermes profile for the reviewer seat (default: the plugin setting) |
| `--fixer-profile` | `FIXER_PROFILE` |  | Hermes profile for the fixer seat (default: the plugin setting) |
| `--reviewer-agent` | `REVIEWER_AGENT` |  | display name for the reviewer (default: profile) |
| `--fixer-agent` | `FIXER_AGENT` |  | display name for the fixer |
| `--cap` | `CAP` | `3` | verdicts allowed before adjudication |
| `--concurrency` | `CONCURRENCY` |  | default PRs per seat at once: 1 = serialized (default: 1, from the plugin settings). Above 1 needs --clone, because each run then gets its own clone. Override per seat with --reviewer-concurrency / --fixer-concurrency. |
| `--reviewer-concurrency` | `REVIEWER_CONCURRENCY` |  | PRs the reviewer may work at once (overrides --concurrency; default: the loop's, settings 1) |
| `--fixer-concurrency` | `FIXER_CONCURRENCY` |  | PRs the fixer may work at once (overrides --concurrency; default: the loop's, settings 1) |
| `--base` | `BASE` | `main` | the branch PRs target; only PRs against it are reviewed |
| `--clone` | `CLONE` |  | local clone the runs may use |
| `--root` | `ROOT` (repeatable) |  | a directory reviews may clean (repeatable) |
| `--state-dir` | `STATE_DIR` |  | where this loop keeps its state files (default: ~/.hermes/state/review-loops/<id>) |
| `--token` | `TOKEN` (repeatable) |  | login=/path/to/pat (repeatable) |
| `--read-token` | `READ_TOKEN` |  | required: login whose token reads GitHub — its own account, never a seat or the adjudicator login (the four-identity rule) |
| `--skill` | `SKILL` |  | skill the seats are told to load. A plugin-provided skill is qualified, e.g. diaktoros:review-loop |
| `--adjudicator-route` | `ADJUDICATOR_ROUTE` |  | route name for the adjudicator (e.g. <id>-breach): setting it turns adjudication on when the verdict cap is spent |
| `--adjudicator-login` | `ADJUDICATOR_LOGIN` |  | optional fourth GitHub account the ruling is also posted as (needs --adjudicator-route and its own --token LOGIN=/path) |
| `--adjudicator-profile` | `ADJUDICATOR_PROFILE` |  | Hermes profile for the adjudicator (default: the plugin setting, else the launch profile) |
| `--observer-route` | `OBSERVER_ROUTE` |  | route name for the read-only observer feed (default: <id>-observe) |
| `--observer-profile` | `OBSERVER_PROFILE` |  | Hermes profile the observer feed belongs to (its chat) — naming one switches the feed on |
| `--observer-deliver` | `OBSERVER_DELIVER` | `telegram` | where the gateway delivers the feed (telegram, discord, ...); the feed never wakes an agent |
| `--observer-events` | `OBSERVER_EVENTS` |  | comma-separated transitions to send, from opened,handoff,verdict,approved,escalation,ruling,stall,closed,triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths (default: all) |
| `--observer-digest-min` | `OBSERVER_DIGEST_MIN` |  | batch the feed into one message per this many minutes (0 = one notice per transition) |
| `--observer-urgent-route` | `OBSERVER_URGENT_ROUTE` |  | second route for urgent notices (failed, held, escalation, ruling, stall, conflict, uncertain); routine ones keep the main feed |
| `--observer-urgent-profile` | `OBSERVER_URGENT_PROFILE` |  | profile for the urgent route (default: the observer profile) |
| `--observer-urgent-deliver` | `OBSERVER_URGENT_DELIVER` |  | where the gateway delivers urgent notices (default: the feed's) |
| `--host` | `HOST` |  | your gateway webhook origin (required unless set in plugin settings) |
| `--grace-min` | `GRACE_MIN` | `35` | minutes a PR may sit quiet before the watchdog reports a stall |
| `--ttl-min` | `TTL_MIN` | `45` | how long a run may hold its seat slot |
| `--inflight-ttl-min` | `INFLIGHT_TTL_MIN` | `10` | how long an in-flight mark blocks a second run at the same head |
| `--review-only-update` | `on` \| `off` |  | push a clean merge of the base into a review-only author's PR branch (same repository only; off by default; turning it on needs --acknowledge-branch-push) |
| `--acknowledge-branch-push` |  |  | accept that the host pushes to a branch the loop does not own; required to turn --review-only-update on |
| `--review-after-ci` | `on` \| `off` |  | start each review after the head's checks finish (up to an hour) (default off) |
| `--fix-ci` | `on` \| `off` |  | hand a failed required check on a fixer's PR to the fixer, one turn per head; needs unattended fixer pushes (default off) |
| `--attribution` | `on` \| `off` |  | sign what the loop posts with 'Automated by Diaktoros' (default on) |
| `--review-only-cap` | `N` |  | verdicts the reviewer gives one review-only PR before it waits for `review --another-round`, 1-1000 (default: the plugin setting, else the review cap) |
| `--ci-fix-cap` | `N` |  | CI-fix turns per PR before a red head goes to the reviewer, 1-10 (default: the plugin setting, else 3) |
| `--review-only-daily` | `N` |  | reviewer turns a day on review-only PRs, 1-1000 (default: the plugin setting, else no cap) |
| `--review-only` | `REVIEW_ONLY` (repeatable) |  | a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it) (default: the plugin setting) |
| `--required-check` | `REQUIRED_CHECK` (repeatable) |  | a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting) |
| `--human-path` | `HUMAN_PATH` (repeatable) |  | a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting) |
| `--fixer-check` | `FIXER_CHECK` |  | one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting) |
| `--turn-budget` | `TURN_BUDGET` | `900` | seconds one isolated seat turn may run, build and tests included (default 900; the sandbox is killed past it) |
| `--reviewer-turn-budget` | `REVIEWER_TURN_BUDGET` |  | the reviewer seat's own turn budget in seconds (overrides --turn-budget) |
| `--fixer-turn-budget` | `FIXER_TURN_BUDGET` |  | the fixer seat's own turn budget in seconds (overrides --turn-budget) |
| `--reviewer-max-steps` | `REVIEWER_MAX_STEPS` |  | agent steps one reviewer turn may take, 8-200 (0 = default 60) (default: the plugin setting) |
| `--fixer-max-steps` | `FIXER_MAX_STEPS` |  | agent steps one fixer or issue-fix turn may take, 8-200 (0 = default 80) (default: the plugin setting) |
| `--fix-daily-turns` | `FIX_DAILY_TURNS` |  | issue-fix turns per day: refused here, a new loop has no fix label; set it with `triage --fix-daily-turns N` (0 = ignore) |
| `--hooks` |  |  | create the GitHub hooks too, paused until `arm` |
| `--arm` |  |  | with --hooks: create them armed (live at once) instead of paused |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can create hooks |
| `--schedule` | `SCHEDULE` |  | e.g. 15m — install the watchdog cron job |
| `--watchdog-deliver` | `WATCHDOG_DELIVER` | `local` | cron delivery target for watchdog alerts |
| `--dry-run` |  |  | print the seat mapping and what would be written, write nothing |
<!-- /flags -->

### set

Changes one loop's settings in place, through the same checks `init` uses. Only the flags you
pass change; everything else stays as it is.

```bash
hermes dk set --loop "<loop-id>" --cap 4
hermes dk set --loop "<loop-id>" --reviewer-turn-budget 1800
hermes dk set --loop "<loop-id>" --reviewer-daily-turns 40
hermes dk set --loop "<loop-id>" --read-token "<reader-login>" --token "<reader-login>=<absolute-reader-token-file>"
hermes dk set --loop "<loop-id>" --observer-events opened,verdict,approved,escalation,ruling
```

What it is for:

- numbers: the verdict cap, concurrency, turn budgets, daily caps, the watchdog's patience
  (`--grace-min`, and `--marker-grace-min`: how long an adjudication may wait before the watchdog
  reports it);
- signing: `--attribution off` stops adding the "Automated by Diaktoros" footer and commit
  trailer to what this loop posts, and `on` restores it (see
  [what the loop signs](operations.md));
- the reader account, or the adjudicator's comment account;
- the gateway host (`--host`), which updates the loop config; use `apply` to reconcile installed route/hook origins afterwards;
- the observer feed: its route, profile, destination and events, plus `--observer-mute` and
  `--observer-unmute` to pause and resume it.

`set` never changes the reviewer/fixer profile or login. That comes from the settings form
through [`apply`](#apply), so one place decides seat identity. Concurrency above 1 needs a
`--clone`, because each parallel run gets its own copy. Without one it is refused, exactly as at
`init`.

**Write and validation limits.** There is no `set --dry-run`. A cap-only/numeric update does
not rerun every profile/credential preflight; the handler normalizes the result and selectively
checks changed reader/adjudicator credentials and observer destinations. `--token` can also
map an extra hook-admin account, despite the narrower generated help; it cannot rotate a
working-seat credential or the current reader without explicitly naming that reader.
An empty adjudicator login removes only PR-comment attribution; it does not disable adjudication.
Blank clone/base strings are ignored, not deletion requests. Seat concurrency/budget flags
pin overrides; changing the loop default afterwards does not remove those pins.
Observer destination/host changes refuse unsettled notices; see [Observer](observer.md).
Do not combine observer-disable with new observer fields or mute/unmute together: the parser
accepts these combinations, and the handler applies flags in order (unmute wins over mute).

<!-- flags:set -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--concurrency` | `CONCURRENCY` |  | default PRs per seat at once (1 = serialized; above 1 needs a clone, since each run gets its own) |
| `--reviewer-concurrency` | `REVIEWER_CONCURRENCY` |  | how many PRs the reviewer may work at once |
| `--fixer-concurrency` | `FIXER_CONCURRENCY` |  | how many PRs the fixer may work at once |
| `--cap` | `CAP` |  | verdicts allowed before adjudication |
| `--clone` | `CLONE` |  | local clone the runs isolate from |
| `--base` | `BASE` |  | base branch the loop watches |
| `--grace-min` | `GRACE_MIN` |  | quiet minutes before the watchdog speaks |
| `--marker-grace-min` | `MARKER_GRACE_MIN` |  | minutes an adjudication may sit claimed with no live run before the watchdog reports it |
| `--ttl-min` | `TTL_MIN` |  | how long a run may hold its slot |
| `--inflight-ttl-min` | `INFLIGHT_TTL_MIN` |  | minutes an in-flight mark blocks a second run at the same head |
| `--turn-budget` | `TURN_BUDGET` |  | seconds one isolated seat turn may run (loop default) |
| `--attribution` | `on` \| `off` |  | sign what the loop posts ('Automated by Diaktoros'), or stop |
| `--review-only-update` | `on` \| `off` |  | push a clean merge of the base into a review-only author's PR branch (same repository only), or stop; turning it on needs --acknowledge-branch-push |
| `--acknowledge-branch-push` |  |  | accept that the host pushes to a branch the loop does not own; required to turn --review-only-update on |
| `--review-after-ci` | `on` \| `off` |  | start each review after the head's checks finish (up to an hour), or start at once |
| `--fix-ci` | `on` \| `off` |  | hand a failed required check on a fixer's PR to the fixer (needs unattended fixer pushes), or stop |
| `--review-only-cap` | `N` |  | verdicts the reviewer gives one review-only PR before it waits for `review --another-round`, 1-1000; 0 = the review cap |
| `--ci-fix-cap` | `N` |  | CI-fix turns per PR before a red head goes to the reviewer, 1-10; 0 = the default (3) |
| `--review-only-daily` | `N` |  | reviewer turns a day on review-only PRs, 1-1000; 0 = no cap |
| `--review-only` | `REVIEW_ONLY` (repeatable) |  | a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it); replaces the list (one of `--review-only`, `--no-review-only`) |
| `--no-review-only` |  |  | clear the review-only list (one of `--review-only`, `--no-review-only`) |
| `--required-check` | `REQUIRED_CHECK` (repeatable) |  | a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates); replaces the list (one of `--required-check`, `--no-required-checks`) |
| `--no-required-checks` |  |  | clear the list: every check gates again (one of `--required-check`, `--no-required-checks`) |
| `--human-path` | `HUMAN_PATH` (repeatable) |  | a glob pattern for paths only a human may approve (repeat it); replaces the list (one of `--human-path`, `--no-human-paths`) |
| `--no-human-paths` |  |  | clear the list: no path is reserved for a human (one of `--human-path`, `--no-human-paths`) |
| `--fixer-check` | `FIXER_CHECK` |  | one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) |
| `--reviewer-turn-budget` | `REVIEWER_TURN_BUDGET` |  | the reviewer seat's own turn budget in seconds |
| `--fixer-turn-budget` | `FIXER_TURN_BUDGET` |  | the fixer seat's own turn budget in seconds |
| `--reviewer-daily-turns` | `REVIEWER_DAILY_TURNS` |  | most reviewer turns per day on this loop; later ones wait for midnight (0 removes the cap) |
| `--fixer-daily-turns` | `FIXER_DAILY_TURNS` |  | most fixer turns per day on this loop (0 removes the cap) |
| `--reviewer-max-steps` | `REVIEWER_MAX_STEPS` |  | agent steps one reviewer turn may take, 8-200 (0 = default 60) |
| `--fixer-max-steps` | `FIXER_MAX_STEPS` |  | agent steps one fixer or issue-fix turn may take, 8-200 (0 = default 80) |
| `--host` | `HOST` |  | gateway webhook host |
| `--adjudicator-login` | `ADJUDICATOR_LOGIN` |  | optional fourth GitHub account the ruling is also posted as; "" clears it (rulings go to the operator only) |
| `--adjudicator-profile` | `ADJUDICATOR_PROFILE` |  | turn adjudication on: the Hermes profile that rules when the verdict cap is spent; creates the <id>-breach route and its shim |
| `--adjudicator-route` | `ADJUDICATOR_ROUTE` |  | with --adjudicator-profile: the route's name (default: <id>-breach) |
| `--adjudicator` | `off` |  | turn adjudication off: removes the route and the block (breach markers and rulings stay in the ledger) |
| `--read-token` | `READ_TOKEN` |  | the login the gates read GitHub as — its own account, never a seat or the adjudicator login (the four-identity rule); map a new login with --token LOGIN=/path |
| `--token` | `TOKEN` (repeatable) |  | LOGIN=/path/to/pat for the --read-token or --adjudicator-login login only (a path, never the token) |
| `--observer-route` | `OBSERVER_ROUTE` |  | route the observer feed delivers through |
| `--observer-profile` | `OBSERVER_PROFILE` |  | profile that owns the observer destination |
| `--observer-deliver` | `OBSERVER_DELIVER` |  | where the gateway delivers the feed (telegram, discord, ...) |
| `--observer-events` | `OBSERVER_EVENTS` |  | comma-separated transitions to send, from opened,handoff,verdict,approved,escalation,ruling,stall,closed,triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths (blank = all) |
| `--observer-digest-min` | `OBSERVER_DIGEST_MIN` |  | batch the feed into one message per N minutes (0 = per transition) |
| `--observer-urgent-route` | `OBSERVER_URGENT_ROUTE` |  | route for urgent notices only (blank = one feed for everything) |
| `--observer-urgent-profile` | `OBSERVER_URGENT_PROFILE` |  | profile that owns the urgent destination (blank = the feed's) |
| `--observer-urgent-deliver` | `OBSERVER_URGENT_DELIVER` |  | where the gateway delivers urgent notices (blank = the feed's) |
| `--observer-mute` |  |  | stop the feed without forgetting it |
| `--observer-unmute` |  |  | resume a muted feed |
| `--observer-disable` |  |  | drop this loop's observer config entirely |
<!-- /flags -->

### apply

Pushes the settings form onto **one** loop. It shows the difference first, then rewrites the
loop config and any route whose profile, prompt or event no longer matches. A form that changed
never touches a running loop until you run `apply` for it.

```bash
hermes dk apply --loop "<loop-id>" --dry-run
hermes dk apply --loop "<loop-id>"
```

It also repairs:

- `--recreate-routes` writes routes the gateway's registry lost, from the loop config, with a
  new secret, and re-keys the repo hooks that point at them. Use it when `doctor` says a route
  is missing and `doctor --repair` cannot restore it.
- `--hooks` makes the loop's two repo hooks match its routes: it creates a missing one (paused),
  repoints one at the route's exact URL, and adds a missing event. It needs a token with hook
  write access (`--admin-token`).
- `--watchdog-shim` rewrites the small script the watchdog cron job runs, pointing it at this
  version of the plugin.

If a seat is in the middle of a turn, rebinding its profile or login is refused until the turn
ends. `--while-busy` does it anyway; that turn finishes as the identity it started with.

**Reconciliation and caveats.** Omitted switches are off; the admin login defaults to the
reader. `--hooks` also covers an enabled triage hook, not just the two working-seat hooks.
A plain apply may fix an owned hook URL's trailing slash even without `--hooks`. Dry runs can
read hook listings but do not make requested writes. Route/hook rebinds are read back and
rolled back on failure; installed gate shims and explicitly requested route recreation have
separate lifecycles, so inspect partial-failure output. Missing routes require intent repair
or explicit recreation; apply never silently invents them.
To change signing explicitly, use `hermes dk set --loop "<loop-id>" --attribution on`
or `--attribution off`, then verify `status`.
A dry run also previews before the live busy-seat check; it is not proof a busy rebind will run.

<!-- flags:apply -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--dry-run` |  |  | show the diff without writing it |
| `--while-busy` |  |  | rebind a seat's profile/login even while a run is in flight (that run keeps the identity it started with) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can write the repo's hooks, for the hook moves and re-keys apply makes (default: the reader) |
| `--recreate-routes` |  |  | write this loop's routes the registry lost, from the loop config, with a new secret, and re-key the repo hooks that point at them (when no intent record can restore them) |
| `--hooks` |  |  | make this loop's two repo hooks what its routes need: create a missing one (paused until arm), repoint one at the route's exact URL, add its gate's event (hook write access, see --admin-token) |
| `--watchdog-shim` |  |  | rewrite the cron shim the watchdog job runs, pinned to this plugin's watchdog |
<!-- /flags -->

### fixer-push

Turns **unattended fixer pushes** on or off for one repository. They are off by default. While
off, a "changes requested" verdict is held for you and no fixer turn starts. You fix it by hand,
or turn this on.

```bash
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race --dry-run
hermes dk fixer-push --loop "<loop-id>" --enable --acknowledge-pr-race
hermes dk fixer-push --loop "<loop-id>" --disable
```

`--acknowledge-pr-race` is required to enable it. Read
[the push policy](security.md)
first: the host checks the PR right before every push, but a PR can still be closed or
retargeted in the moment between that check and Git accepting the push. Enabling is refused
while a fixer turn is running.

<!-- flags:fixer-push -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | exact loop id (never all loops) |
| `--enable` |  |  | opt this repository in (one of `--enable`, `--disable`) |
| `--disable` |  |  | turn unattended pushes off (one of `--enable`, `--disable`) |
| `--acknowledge-pr-race` |  |  | accept the residual non-atomic PR-metadata/ref race; required for --enable |
| `--dry-run` |  |  | show action without writing |
<!-- /flags -->

### triage

Turns **issue triage** on or off for one loop, or shows its settings. When an issue opens, a
sandboxed triage seat reads it and adds labels from a fixed list, before any agent works on it.
Off unless you turn it on. New to it? [Issue triage and issue fixes, step by step](issues.md)
walks through the whole setup and a first test.

```bash
hermes dk triage --loop "<loop-id>"
hermes dk triage --loop "<loop-id>" --enable --triage-profile "<triage-profile>" --author "<issue-author-login>" --labels bug,feature,docs,question,P0,P1,P2,P3 --dry-run
hermes dk triage --loop "<loop-id>" --enable --triage-profile "<triage-profile>" --author "<issue-author-login>" --labels bug,feature,docs,question,P0,P1,P2,P3 --admin-token "<hook-admin-login>"
hermes dk triage --loop "<loop-id>" --disable --admin-token "<hook-admin-login>"
```

- **Only issues from `--author` logins are triaged.** Anyone else's issue is dropped before any
  model sees it, so spam or a prompt-injection attempt on a public repository costs nothing.
- **Only labels from `--labels` can be applied**, at most `--max-labels` per issue (default 3),
  and a comment only with `--comment on`. The issue text is untrusted, and this list is the
  boundary; the host broker enforces it, not the model.
- **People win.** Labels are only ever added. If the issue already has a label from the list,
  triage writes nothing.
- **It labels as `--login`** (default: the reviewer seat's account), which needs `issues: write`
  and can never be the reader.
- With `--admin-token`, `--enable` also reconciles the repo hook for `issues` events. A new hook
  is created paused: run [`arm`](#arm) afterwards. An existing hook can remain active.
  Without it, `apply --hooks` creates the hook later.

**Issue fixes.** With `--fix-label LABEL --maintainer LOGIN`, a maintainer applying that label
to an allowlisted author's open issue hands it to the fixer seat. It opens a PR from a new branch
`diaktoros/issue-N` that the loop then reviews, or comments on the issue when it cannot fix it.
This needs unattended fixer pushes on ([`fixer-push`](#fixer-push)), and the label can't be one of
the triage labels, so only a person can trigger it.

```bash
hermes dk triage --loop "<loop-id>" --enable --fix-label agent-fix --maintainer "<maintainer-login>"
```

Step by step: [issues.md](issues.md). Details: [Issue triage](operations.md) and
[issue fixes](operations.md).

**Accepted labels and defaults.** Fresh enable needs a profile, nonempty authors and labels;
subsequent enables reuse omitted values. Authors and maintainers are repeatable replacement
lists, not append-to-old-list updates. Labels: 1–100 distinct names (case-insensitive), each
1–50 characters, no comma, braces, backtick or control characters; `--max-labels` is 1–10,
fresh default 3. The handler treats zero as omitted, not an allowed zero-label limit.
Triage capacity and budget are file-only settings (`seats.triage.concurrency` and
`seats.triage.turn_budget_s`); this command offers only `--daily-turns` for seat pacing.
`--fix-label ""` disables issue handoff; a nonempty fix label must not be a triage label and
requires maintainers. `--daily-turns 0` removes the triage cap. `--token` maps paths for the
selected writing/admin identities; it never accepts token values. A first enable's default
login is the reviewer. With neither `--enable` nor `--disable` and no other flag, the command
only displays current triage (including the `auto-offer:` line). Any update flag given alone
(for example `hermes dk triage --loop "<loop-id>" --auto-fix-label P3 --auto-fix-daily 25`)
applies like `--enable` when triage is on, keeping the route's secret and hook; when triage is
off it is refused, naming `--enable`. Disable without admin leaves any GitHub issues hook
posting to a removed route (404); delete it separately. Triage enable rewrites its route;
verify secrets/hooks when changing an already installed route. Existing hook reconciliation
may preserve activation; do not assume every update pauses a live hook.

<!-- flags:triage -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--enable` |  |  | turn triage on (or change it): writes its route, shim and, with --admin-token, its issues hook (paused until arm) (one of `--enable`, `--disable`) |
| `--disable` |  |  | turn triage off: removes its route, shim and (with --admin-token) hook (one of `--enable`, `--disable`) |
| `--triage-profile` | `PROFILE` |  | Hermes profile whose model triages |
| `--author` | `AUTHOR` (repeatable) |  | GitHub login whose new issues are triaged (repeatable); anyone else's are ignored |
| `--labels` | `LABELS` |  | comma-separated labels triage may apply, e.g. bug,feature,docs,question,P0,P1,P2,P3 |
| `--max-labels` | `MAX_LABELS` |  | at most this many labels per issue (default 3) |
| `--comment` | `on` \| `off` |  | allow one short comment with the labels (default off) |
| `--login` | `LOGIN` |  | account that labels (default: the reviewer seat); needs issues: write, never the reader |
| `--token` | `TOKEN` (repeatable) |  | login=/path/to/pat for --login (or --admin-token), if not mapped |
| `--fix-label` | `FIX_LABEL` |  | a label a maintainer applies to hand an issue to the fixer (#214; needs unattended fixer pushes on); '' turns it off |
| `--maintainer` | `MAINTAINER` (repeatable) |  | login whose applying --fix-label counts (repeatable) |
| `--daily-turns` | `DAILY_TURNS` |  | at most this many triage turns per day (0 removes the cap) |
| `--fix-daily-turns` | `FIX_DAILY_TURNS` |  | at most this many issue-fix turns per day (0 = the default, 10); issue fixes are always capped |
| `--auto-fix-label` | `AUTO_FIX_LABEL` (repeatable) |  | a triage label that hands an issue to the fixer without a maintainer (#232; repeatable; never P0-P2; '' clears the list) |
| `--auto-fix-daily` | `AUTO_FIX_DAILY` |  | at most this many automatic issue fixes per day (0 = the default, 25) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can create or delete repo hooks |
| `--dry-run` |  |  | show the change, write nothing |
<!-- /flags -->

---

## Checking an install

### doctor

A read-only preflight: can this machine run the loop at all? It checks the config, the profiles,
the token files, the routes in the gateway's registry, the gate shims, the runtime file's paths,
each seat's model (from its profile, without using a credential), the watchdog job, the clone,
the state directory, the gateway's reachability and the repo hooks. Each line is ✅ verified,
❌ absent or mismatched (with a `fix:` line), or ⚠️ unknown (it could not tell).
An unknown hook state is not a paused hook. Verify it with an authorized hook-admin account;
see [account access and token permissions](accounts.md#choose-pat-type-and-permissions).

```bash
hermes dk doctor --loop "<loop-id>"
hermes dk doctor --loop "<loop-id>" --offline
```

- Exit `1` means at least one ❌. With `--strict`, a ⚠️ counts as a failure too.
- `--offline` skips the two network checks (gateway reachability and the repo hooks).
- `--repair` is the one write `doctor` can make: it puts this loop's own routes back from the
  plugin's record of them (same secret), and heals plugin-owned gate shims, when another tool overwrote or removed them.

`doctor` never starts a turn and never fires a route: a test call to a seat's route could enqueue
a real isolated seat turn. To prove the isolated path end to end, use [`selftest`](#selftest).
`doctor` is the quick check; `selftest` is the authoritative one for the sandbox and models.

<!-- flags:doctor -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
| `--offline` |  |  | skip the two network probes (gateway reachability, repo hooks) |
| `--strict` |  |  | treat a check that could not be decided as a failure |
| `--repair` |  |  | restore this loop's own routes from the plugin's intent record (same secret) before checking; the only write doctor makes |
<!-- /flags -->

### migrate

Moves an install to a renamed plugin, a renamed GitHub repository, or both, in one step run
between turns. It copies the old plugin's settings into the new plugin's form, keeping any value
already set there. It moves a renamed repository's ledger rows, state and loop file to the new
name, after checking by id that both names are the same repository, and refuses while one of its
runs is in flight. It points the gate and watchdog shims at this plugin's scripts, then lists the
`doctor` checks that aren't verified. See
[moving to a renamed plugin or repository](operations.md#moving-to-a-renamed-plugin-or-repository).

```bash
hermes dk migrate --dry-run
hermes dk migrate
```

`--rename-loop OLD=NEW` also renames a loop: its file, its default state directory, its routes
and the URLs its repo hooks post to. Each hook is pinged on its new route before the old route
is removed. See [renaming a loop](operations.md#renaming-a-loop).

The install is paused while it runs: gates defer their deliveries for a later re-drive, and the
worker and the watchdog wait.

Exit `1` means a step was refused or isn't finished, and the line says why. Running it again
finishes what an interrupted run began.

<!-- flags:migrate -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--dry-run` |  |  | report every step and write nothing |
| `--rename-loop` | `OLD=NEW` |  | also give a loop a new id: its file, default state directory, routes and the URLs its repo hooks post to (each hook is pinged before the old route goes) |
| `--admin-token` | `LOGIN` |  | mapped login whose token may edit the repo hooks (--rename-loop) |
<!-- /flags -->

### backup

Writes one archive of everything the plugin owns: the loop files, the runtime file, pacing and
the gate-failure ledger; the run ledger (through SQLite's online backup, so it is consistent
while the loop is live); each loop's state directory; this plugin's route-registry entries; and
the watchdog job's schedule and delivery target. `migrate` takes one automatically before it
moves anything and prints the path.

The archive holds the routes' HMAC secrets, so restored hooks keep working. It is created mode
`0600`, the command warns about it, and the secrets are never printed. It leaves out GitHub
tokens (PATs), Hermes profiles and model logins. It never overwrites a file. There is no
output-directory or retention setting yet; when one is added it must be settable from the
Desktop form, `init`, `setup`, `set` and the loop file.

```bash
hermes dk backup --out "<archive-file>"
```

<!-- flags:backup -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--out` | `FILE` |  | where to write it (default: $HERMES_HOME/backups/…); never overwrites a file |
<!-- /flags -->

### restore

Puts a backup back. It holds `migrate`'s pause marker for its run (gates defer, the worker
claims nothing), refuses to overwrite existing files, routes or the watchdog job without
`--force`, rewrites the shims, recreates the watchdog job through `hermes cron` and reads it
back, then runs `doctor`. It checks that the token files the loops name exist and lists any that
are missing; the tokens themselves are not in the archive. `--dry-run` lists what it would
restore and overwrite and writes nothing. Exit `1` means a step is not finished or `doctor`
listed a check that is not verified.

An archive is treated as outside input. `restore` refuses it (exit `2`, nothing written) when:
- a file would land outside this plugin's places (the Hermes home, the loop files'
  directory and the state directories the archive declares) or behind a symlink;
- a loop's state directory lies outside the Hermes home (a custom `state_dir`) and you did not
  name it with `--allow-state-dir DIR`. **The directory comes from the archive**, not from this
  machine, so check it in the dry run (which lists each one) before you allow it. Only the exact
  directory you name is written: naming a parent allows nothing, and repeat the flag for each
  directory;
- a route in it is not one of this plugin's gates;
- a live route of the same name belongs to something else (even with `--force`);
- a run is in flight or uncertain.

```bash
hermes dk restore "<archive-file>" --dry-run
hermes dk restore "<archive-file>"
```

<!-- flags:restore -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--dry-run` |  |  | report what would be restored and overwritten; write nothing |
| `--force` |  |  | overwrite existing state (refused without it) |
| `--allow-state-dir` | `DIR` (repeatable) |  | write this loop state directory, which the archive declares outside the Hermes home (the dry run lists them; repeat per directory) |
<!-- /flags -->

### selftest

Proves the isolated turn path step by step: the runtime file and its paths, the bubblewrap
sandbox (no network, no credentials visible inside), each seat's model, the GitHub identities,
the broker and the run ledger. It never writes to GitHub, except `--ping`, which asks GitHub to
ping the hooks.

Run it in three stages, each one costing a little more:

```bash
hermes dk selftest --loop "<loop-id>" --no-model
hermes dk selftest --loop "<loop-id>" --pr 12
hermes dk selftest --loop "<loop-id>" --pr 12 --live-turn
```

1. `--no-model` skips tiny completions, not all model/credential resolution. It can read and
   refresh host credentials and make GitHub reads; it does not spend completion tokens.
2. Without `--no-model`, each distinct seat model resolution gets one tiny real completion (a few tokens).
   `--pr N` adds a dry run of the reviewer's write authorization on that PR.
3. `--live-turn` runs one real reviewer turn on that PR in the sandbox. Its verdict is printed
   and **never posted**. It takes as long as a real review (up to the reviewer's turn budget).

Every failed step prints a `fix:` line. Exit `1` means a step failed. When `doctor` and
`selftest` disagree (for example about the runtime path), trust `selftest`: it runs the same
checks the worker does.

**Side effects.** This is not a no-op preflight: it creates temporary sandbox/broker/ledger
fixtures, resolves host model credentials (OAuth may refresh), queries GitHub and optionally
calls providers. `--ping` is the explicit GitHub POST exception. `--live-turn` needs `--pr`
and cannot be combined with `--no-model`. `--timeout` accepts any positive integer, unlike the
production budget's 60–14400 range; setting it only changes this diagnostic live-turn budget.
It is ignored for ordinary tiny probes. Live turn verdicts never post a review or push.

<!-- flags:selftest -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` |  | dry-run the reviewer write authorization on this PR |
| `--no-model` |  |  | skip the one tiny real completion (costs a few tokens) |
| `--live-turn` |  |  | with --pr: run one real isolated reviewer turn whose verdict is printed and never posted |
| `--ping` |  |  | ask GitHub to ping each loop hook and report whether the gateway accepted its signature (the selftest's only GitHub write) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token may ping hooks (admin:repo_hook or repo) |
| `--timeout` | `TIMEOUT` |  | live turn budget in seconds (default: the loop's reviewer turn_budget_s — the budget the production worker enforces) |
<!-- /flags -->

### corpus

Golden corpus (#491): replays historical PRs with known P1s through the reviewer and reports,
per case, which findings were caught and which missed. On demand only (it costs model tokens)
and no-write, like `selftest --live-turn`. Cases are JSON files in `<state dir>/corpus/`:
`{"id": "...", "pr": 12, "head": "<sha>", "findings": [{"id": "...", "pattern": "<regex>"}]}`;
`head` is optional and, when given, must be the PR's current head: a case at an earlier head is
refused with "only the PR's final head is supported yet" (exit 2), never scored as missed. The
replay is a first look (round 1: no earlier review or fixer answer reaches the prompt). A finding
is caught only when the review the turn would submit is REQUEST_CHANGES **and** its regex
(case-insensitive) matches inside a numbered finding line (`F1: ...`, #475); a mention in an
APPROVE, or outside a finding line, is a miss. Seed cases (PRs #460, #497, #503, #506; their
patterns are starting points to tune) are in `docs/corpus/`. Each run is
appended to `<state dir>/corpus_scores.jsonl` with the prompt revision and model. Exit 1 when
anything was missed; a malformed case file stops the run (exit 2).

<!-- flags:corpus -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id |
| `--dir` | `DIR` |  | directory of case files (default: corpus/ in the loop's state directory) |
| `--timeout` | `TIMEOUT` |  | per-case turn budget in seconds (default: the reviewer's) |
| `--history` |  |  | print the recorded scores and run nothing |
<!-- /flags -->

### models

Lists the models a profile's provider offers, from Hermes's own catalog. Read-only, and it never
uses a credential.

```bash
hermes dk models --seat reviewer --loop "<loop-id>"
hermes dk models --profile-name "<reviewer-profile>"
```

To change the model a seat uses, change that profile's model in Hermes (`hermes -p "<reviewer-profile>" model`).
Normally the loop uses the profile model; explicit runtime overrides and legacy fallback take precedence as documented in [Configuration](configuration.md).

Pass exactly one of `--profile-name` or `--seat`; the handler, not an argparse mutually exclusive
group, enforces this. `--loop` selects the seat's loop (omission requires exactly one loop);
it has no role with an explicit profile. Catalog and profile-declared models are reported;
this is not a live provider entitlement check. Exit 1 means profile/provider/catalog could
not supply models. The Hermes example's `-p` selects which profile's model to change.

<!-- flags:models -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--profile-name` | `PROFILE` |  | Hermes profile name |
| `--seat` | `reviewer` \| `fixer` \| `adjudicator` |  | use this seat's profile from the loop config |
| `--loop` | `LOOP` |  | loop id for --seat (default: the only loop) |
<!-- /flags -->

---

## Turning it on and off

### arm

Arms (turns on) or pauses (turns off) the loop's repo hooks. `init --hooks` creates them paused,
so nothing happens until you arm. After arming, it asks GitHub to ping each hook and reports
whether your gateway accepted the signature. A hook that looks armed but cannot authenticate
wakes nothing, and this catches it.

```bash
hermes dk arm --loop "<loop-id>" --admin-token "<hook-admin-login>"
hermes dk arm --loop "<loop-id>" --pause --admin-token "<hook-admin-login>"
```

Run it after `doctor` and `selftest` pass. It reads back each hook and exits `1` unless GitHub
confirms every one is in the state you asked for. It edits the hooks as `--admin-token`'s login
(default: the reader), which needs hook write access (`repository_hooks: write`, or classic
`repo`).

**Safety.** Omission of `--loop` affects every configured loop; prefer an exact id.
Pause stops new hook deliveries, not already running turns. Arming is not fixer-push consent.
Hook activity is read back, but a missing ping receipt within its bounded wait is a warning,
not necessarily failure; exit 0 is not proof every gateway delivery was observed.

<!-- flags:arm -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: every configured loop) |
| `--pause` |  |  | pause instead of arming |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can edit the repo's hooks (default: the reader) |
<!-- /flags -->

---

## When something is stuck

### explain

Answers "why is this PR not moving, and what has to happen next?" for one pull request. It reads
GitHub and the loop's state and shows:

- the PR's head and whose turn it is;
- the verdicts counted against the cap;
- any queued, running, waiting or failed turn, with its reason and its next retry;
- holds (fixer pushes off, no runtime file, a usage window);
- the one event that would move the PR.

Read-only.

```bash
hermes dk explain --loop "<loop-id>" --pr 12
```

Its conclusions come from the same checks the live gates run, so it is not a second opinion. It
exits `2` only when it cannot ask: an unknown loop, a refused loop file, or several loops and no
`--loop`.

<!-- flags:explain -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: the only configured loop) |
| `--pr` | `PR` | **required** | pull request number to explain |
<!-- /flags -->

### review

Asks for a fresh review of a PR's current head, as the operator. Use it after you push to a loop
PR yourself, after reconciling a quarantined run, or whenever `explain` or the watchdog says the
reviewer never posted a verdict. A review request on GitHub only counts from a fixer, the
reviewer or a maintainer (`triage.maintainers`), so this is the path for everyone else, and for
loops without triage.

```bash
hermes dk review --loop "<loop-id>" --pr 12
```

The real reviewer gate decides, fed a `ready_for_review` event built from the live PR, so the
rules are the webhook's: open, not a draft, a fixer's PR on the loop's base, no verdict or run
already at this head, and the verdict cap. It makes no GitHub write; the review itself is the only
write, as usual. It prints `review queued` (exit 0), or `no review started —` and the gate's own
reason (exit 1). Exit 2 when the loop or the PR cannot be read, several loops are configured and
no `--loop` names one, or the gate did not finish within 120 seconds.

<!-- flags:review -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: the only configured loop) |
| `--pr` | `PR` | **required** | the pull request to review |
| `--another-round` |  |  | allow exactly one more verdict on a review-only PR that has reached its review cap (maintainer or operator only) |
<!-- /flags -->

### escalate

Sends one PR to the adjudicator **now**, whatever its verdict count: the reviewer and the fixer
are talking past each other, or you want to see a ruling. The PR gets the same breach marker and
isolated ruling turn a spent cap gives it, marked as your escalation, and it's parked at its
current head as if its cap were spent:
- the reviewer and the fixer start nothing more there;
- a review or fix already queued for that head is retired;
- the watchdog retries a pending delivery;
- `explain` shows it parked.

A new head (someone pushes) supersedes the escalation.

```bash
hermes dk escalate --loop "<loop-id>" --pr 12 --reason "the fix and the finding disagree on scope"
```

It's refused unless the loop has an adjudicator, and only for an open, ready PR by a fixer that
has at least one verdict, isn't approved at its head, isn't already awaiting a ruling and has no
turn running. Exit `1` means it was refused, and the line says why.

<!-- flags:escalate -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` |  | loop id (default: the only configured loop) |
| `--pr` | `PR` | **required** | the pull request to escalate |
| `--reason` | `REASON` |  | why, for the ruling's record (one line) |
<!-- /flags -->

### trace

Answers "why did this webhook start nothing?" A gate that declines an event exits quietly, and
GitHub shows the same `200 {"status": "ignored", "reason": "script"}` for every delivery,
declined or queued alike. `trace` replays one delivery through the **real** gate script, on a
temporary copy of this loop's home, and prints the gate's own reasons:

```bash
hermes dk trace --loop "<loop-id>" --delivery "<delivery-guid-or-id>" --admin-token "<hook-admin-login>"
hermes dk trace --loop "<loop-id>" --payload "<payload-json-file>" --event pull_request
```

- `--delivery` takes the GUID from the hook's **Recent Deliveries** page on GitHub (repo →
  Settings → Webhooks → the hook → Recent Deliveries), or its numeric id. Reading deliveries
  needs hook read access (`--admin-token`).
- `--payload` replays a payload you saved to a file instead.

The last line is the outcome: `would start a reviewer run` (or `would queue a … run`),
`held — <why>` or `declined — <why>`. Anything the gate would send out (GitHub writes, a run
starting, a notice) is listed as `would …` and never done. Your real state is untouched.

`trace` cannot replay `issues` deliveries (issue triage and issue fixes) yet (#230). For those, see
[an issue opened and nothing was labelled](troubleshooting.md).

<!-- flags:trace -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--delivery` | `DELIVERY` |  | a recorded delivery to this loop's hooks: GitHub's numeric id or the X-GitHub-Delivery GUID (one of `--delivery`, `--payload`) |
| `--payload` | `PAYLOAD` |  | a webhook payload JSON file instead (one of `--delivery`, `--payload`) |
| `--event` | `pull_request` \| `pull_request_review` \| `issues` |  | with --payload: the event it was (default: read from the payload) |
| `--route` | `ROUTE` |  | the route it was sent to (default: from the delivery's hook, or the event) |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can read hook deliveries (admin:repo_hook or repo) |
<!-- /flags -->

### retry

Runs a failed turn again. A turn that failed **before writing anything** retries on its own:
four attempts in all, waiting 2, 4 and 8 minutes between them, then it ends as `failed`.
`retry` re-arms it once more, for example after you raised its turn budget or fixed its model.

```bash
hermes dk retry --loop "<loop-id>" --pr 12
hermes dk retry --loop "<loop-id>" --pr 12 --seat fixer
```

It selects the newest offerable ledger head (a GitHub PR read breaks a supersession tie).
It can re-arm `failed`, `waiting` and push-policy-cancelled turns, not arbitrary cancellations.
`--seat triage` / `issue_fixer` use an issue number in `--pr`, despite that option's PR wording. A run that **may have written** (it posted a review,
pushed, or ended `uncertain`) is never replayed: `retry` refuses it and prints how to inspect
and reconcile it instead.

<!-- flags:retry -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` | **required** | the pull request whose failed run to re-arm |
| `--seat` | `reviewer` \| `fixer` \| `adjudicator` \| `triage` \| `issue_fixer` |  | only that seat's run (default: whichever failed at the PR's newest head) |
<!-- /flags -->

### drain

Starts a queued turn now, if its seat has room, instead of waiting for the next watchdog sweep.
Turns queue when a seat is already busy (see `concurrency`).

```bash
hermes dk drain --loop "<loop-id>" --seat reviewer
```

`--seat` defaults to reviewer; only reviewer/fixer queues are exposed by this command.
It delegates to `scripts/watchdog.py --drain` and can mutate queues, reconcile routes and
launch workers. It still respects live PR state, runtime availability, pacing, seat capacity
and fixer-push policy; it does not force a blocked turn or wait for completion.

<!-- flags:drain -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--seat` | `reviewer` \| `fixer` | `reviewer` | which seat's queue to drain |
<!-- /flags -->

---

## Housekeeping

### cleanup

Gives a finished PR's disk space back: review checkouts, build directories, logs and locks under
the loop's clone and its `roots`. A merged PR can leave gigabytes behind. Branch checkouts (your
own working copies) are never touched.

```bash
hermes dk cleanup --loop "<loop-id>" --dry-run
hermes dk cleanup --loop "<loop-id>"
hermes dk cleanup --loop "<loop-id>" --pr 12
```

Without `--pr` it sweeps every closed PR the clone knows about. Closing a PR also runs cleanup
for it automatically. Use `--dry-run` first to see what would go.

**Destructive boundary.** There is no confirmation prompt. `--dry-run` previews deletions;
`--pr` targets one closed/merged PR, omission sweeps. It removes detached PR worktrees and
PR-named directories only within configured roots, plus associated state; it does not delete
GitHub PRs or remote branches. Broad roots are refused but dedicated roots still authorize
recursive deletion. Review [Operations](operations.md) and [Security](security.md) first.

<!-- flags:cleanup -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--pr` | `PR` |  | clean one closed PR (default: sweep every closed PR the clone knows about) |
| `--dry-run` |  |  | list what would be removed, remove nothing |
<!-- /flags -->

### uninstall

Removes a loop, the reverse of `init`: its repo hooks, the watchdog job (only when no other loop
still needs it), its routes and gate shims, and its config. With `--purge` it also removes the
loop's default state directory.

```bash
hermes dk uninstall --loop "<loop-id>" --admin-token "<hook-admin-login>"
```

It undoes the parts that need the config first. If any of them cannot be undone (say, deleting a
hook is refused), it stops, keeps the config so you can retry, and prints the commands that
finish the job. It refuses to leave live hooks behind unless you pass `--keep-hooks`.
`--keep-config` removes everything except the config file.

**Destructive options.** Switches default off. There is no uninstall dry-run or confirmation.
`--keep-hooks` explicitly leaves live hooks posting to removed routes; it is not a safe pause.
`--purge` is allowed only for the default, plain nonsymlink loop state directory, refuses a busy
working seat, and conflicts with `--keep-config`; custom state must be inspected/removed
separately. Uninstall is staged, not atomic: already deleted hooks/jobs stay gone after a later
failure. Credential files, profiles and the shared run ledger are not purged. Stop/verify active
workers separately; ordinary uninstall does not cancel every live turn.

<!-- flags:uninstall -->
| flag | value | default | what it does |
| --- | --- | --- | --- |
| `--loop` | `LOOP` | **required** | loop id (its config file name; `list` shows them) |
| `--keep-config` |  |  | remove hooks, cron job and routes but keep the loop config file |
| `--admin-token` | `ADMIN_TOKEN` |  | login whose token can delete hooks (admin:repo_hook or repo) |
| `--keep-hooks` |  |  | leave the repo hooks live (explicit opt-out) |
| `--purge` |  |  | also delete the loop's default state directory |
<!-- /flags -->
