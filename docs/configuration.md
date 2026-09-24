# Configuration reference

One JSON file per loop under `~/.hermes/review-loops.d/` (override the directory with
`REVIEW_LOOP_CONFIG_DIR`). Everything the loop knows about a repository is here, which is what lets
one install serve several repositories with different seats, budgets and credentials.

`hermes review-loop init` writes this file for you; hand-editing is fine, and the loader is strict
on purpose — a loop that cannot be resolved to a repository, a base branch and two seats is a
configuration error, not a run that guesses.

Before arming a loop, `hermes review-loop doctor --loop <id>` checks that everything this file
*names* actually exists — the seat profiles, the token files, the routes and their secrets, the
GitHub hooks, the cron shim and job, the clone and the gateway — and writes nothing while doing it.
See [Preflight](architecture.md#preflight-can-this-installation-run).

## Required

| key | meaning |
|---|---|
| `repo` | `owner/name`. The gate selects the loop from the webhook payload's `repository.full_name`. |
| `fixers` | GitHub logins allowed to open/push the PRs this loop works on. |
| `reviewers` | GitHub logins whose verdicts count toward the budget. |
| `seats.reviewer.route` / `seats.fixer.route` | the webhook routes that wake each seat. |
| `seats.reviewer.profile` / `seats.fixer.profile` | the Hermes profile each run happens as; it must exist on this machine, and the two must differ. |
| `reviewer_seat` | which reviewer login the reviewer route serves (a request aimed at anyone else is not this loop's business). Validated against `reviewers`: a route that serves a login this loop does not count is a loop that never wakes. |

## Everything else

| key | default | meaning |
|---|---|---|
| `id` | repo name | loop id; also the prefix of the generated routes |
| `base` | `main` | only PRs targeting this branch are in the loop |
| `cap` | `3` | verdicts allowed before escalation (`cap - 1` fix turns) |
| `seats.<seat>.login` | first fixer/reviewer | the GitHub login that seat acts as; `init`/`apply` refuse one outside that seat's allowlist, and two seats may not share one |
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
| `concurrency` | `1` | default runs at once *per seat*. `1` = serialized; above 1 requires `clone`, because every run then gets its own isolated clone. |
| `seats.<seat>.concurrency` | loop default | this seat's own limit, overriding the default. Set with `hermes review-loop set --reviewer-concurrency N` / `--fixer-concurrency N`. |
| `state_dir` | `~/.hermes/state/review-loops/<id>` | locks, queue, in-flight marks, breach markers, artifacts, watchdog memory |
| `host` | unset | your gateway's HTTP(S) webhook origin; `init` requires `--host` or an explicit plugin setting before it writes config/routes/hooks |
| `grace_min` | `25` | how long a quiet head is allowed to sit before the watchdog speaks |
| `marker_grace_min` | `60` | how long a breach marker may sit unpicked-up |
| `cooldown_h` | `6` | repeat suppression per stall |
| `ttl_min` | `45` | seat-lock lifetime; past this a crashed run has lost its seat |
| `inflight_ttl_min` | `10` | how long a same-head burst is considered already handled |

## Plugin settings (the desktop form)

`plugin.yaml` declares a `config_schema`, so the desktop renders a form at **Capabilities → Plugins →
review loop**. Those values are **defaults for a new loop**; pushing them onto an existing loop is
explicit, because a form that quietly renumbers a running loop is a miserable thing to debug:

```bash
hermes review-loop settings                      # what the form holds, [set] vs [default], per key
hermes review-loop apply --loop <id> --dry-run   # the diff
hermes review-loop apply --loop <id>             # write it
```

| setting | default | lands on |
|---|---|---|
| `cap` | 3 | `cap` |
| `reviewer_concurrency` / `fixer_concurrency` | 1 | `seats.<seat>.concurrency` |
| `reviewer_profile` / `fixer_profile` | — | `seats.<seat>.profile` (and `seats.<seat>.agent`, when the loop has not named one) |
| `reviewer_login` | — | `seats.reviewer.login` **and** `reviewer_seat` — the login the review route serves |
| `fixer_login` | — | `seats.fixer.login` |
| `adjudicator_profile` | — | `adjudicator.profile`, on a loop that already has an `adjudicator.route` |
| `clone`, `base`, `host` | —, `main`, unset | the same loop keys; a blank host in the form preserves an existing loop's explicit host |
| `grace_min`, `ttl_min`, `inflight_ttl_min` | 25, 45, 10 | the same loop keys |

Settings are per profile (`plugins.entries.hermes-review-loop.settings`, written through Hermes'
single config writer), and `review_loop/config.py::SETTINGS_SCHEMA` mirrors the manifest — the suite
fails if the two drift, because a form that writes keys nothing reads is worse than no form.

Blank `clone` means *not set here*: it never erases the clone a loop already uses, since the cleanup
prunes worktrees through that path. The rails still apply — `reviewer_concurrency: 2` with no clone
is refused at `init`, at `set` and at `apply` alike.

There is no built-in webhook host. Set `host` to your own gateway origin (for example,
`https://your-gateway.example`) in the plugin settings, or pass `--host` to `init`; the CLI rejects
missing, relative, and malformed hosts before writing any config or routes, even without `--hooks`.
Existing loop files with an explicit `host` continue to load, and an unset form setting does not
erase one when you run `apply`. A public GitHub webhook should use HTTPS.

## Seat identity (who serves each seat)

A seat is a **Hermes profile** (the model, its budget, its credentials) plus a **GitHub login** (the
identity it acts as and the attribution its reviews carry). The form carries a per-profile default
for both, plus an optional adjudicator profile; the allowlists, the route names and the adjudicator
route stay per repository, because one form cannot honestly own every repository.

A blank profile or login means **not set here**. It never unsets what a loop file says, so a
repository that needs its own pair keeps it until somebody pushes the form onto that loop:

```bash
hermes review-loop settings                            # the form's mapping, and what each loop runs as now
hermes review-loop init --repo owner/name --dry-run    # preview seats + routes for a new loop
hermes review-loop apply --loop name --dry-run         # what a push would change, including routes
hermes review-loop apply --loop name                   # stage it: config and routes together
hermes review-loop apply --loop name --while-busy      # ...even while a seat has a run out
```

**Validation, before anything is written.** `init` and `apply` refuse a mapping that cannot drive a
run, and they refuse it *before* the loop config, the routes or the hooks are touched:

| check | why |
|---|---|
| the profile exists (`~/.hermes/profiles/<name>`, or `default` for the launch profile) | otherwise the first event wakes nobody |
| the login is in that loop's `fixers` / `reviewers` allowlist | a seat may only act as a login the repository already trusts |
| reviewer and fixer differ in profile, in login, and in token file | one seat reviewing its own work is not a review |
| an adjudicator differs from both seats | it is judging them |
| every `tokens` mapping points at a real, non-empty file, and `read_token` is one of them | a missing PAT reads as an unauthenticated call, hours later, in a log nobody reads |
| a loop that maps tokens maps one for each seat it is writing | otherwise that seat pushes as the read identity |
| a route is not claimed by another loop, and still runs this role's gate script | the registry is shared by every plugin on the host |

Existence and allowlist membership are checked for the roles an operation *writes*: a loop created
before this validation existed keeps loading when you tune its `cap`. Distinctness, credentials and
route ownership always apply, because the unsafe shape is the combination.

**Applying is staged, not half-applied.** A route URL carries the seat's profile
(`/p/<profile>/webhooks/<route>`), so changing who serves a seat must also move its installed
GitHub hook URL. `apply` first reads the repo hooks; if that listing is unavailable it refuses to
move the profile. It snapshots the affected routes, rebinds and reads them back, updates matching
hook URLs and reads each hook back, then writes the loop config. Any failed route write, readback,
or hook update returns failure and attempts to restore prior routes and hook URLs; an incomplete
rollback is reported loudly for manual repair. Other loops' routes and their secrets are untouched.
`init` likewise restores the previous config bytes and owned routes if a retry fails partway
through route installation. Installing or moving a seat requires token-file mappings for the
read login and both seat logins before any installation writes.

**A seat with a run in flight is not rewritten underneath itself.** `apply` refuses while the seat
it would move has a live run, and says who is running and for how long. `--while-busy` is the
explicit override: the change lands now, and that run finishes under the identity it started with.
Numeric knobs (`cap`, concurrency, timers) are not gated this way — they take effect on the next
event and cannot strand a run.

`status` shows the mapping and checks it against the registry:

```
  seats:      reviewer=vex-coat (vex) · fixer=drey-coe (drey)
  adjudicator:tuck (route widgets-breach)
  routes:     reviewer widgets-review → vex (ok) · fixer widgets-fix → drey (ok) · adjudicator widgets-breach → tuck (ok)
  token refs: reviewer vex-coat → ~/.hermes/keys/vex-coat-pat · fixer drey-coe → ~/.hermes/keys/drey-coe-pat
```

`MISMATCH — hermes review-loop apply --loop <id>` in that line means the config moved and the route
did not. Token values are never printed: only which login reads which file.

## State files (per loop, under `state_dir`)

| file | what it holds |
|---|---|
| `locks.json` | `{seat: {"repo#PR": {at, head, why}}}` — live runs per seat; `concurrency` of them may be active at once |
| `pending.json` | `{seat: {"repo#PR": {at, head, url, reason}}}` — queued, not run |
| `inflight.json` | `{"review:PR:sha" / "fix:PR:sha": ts}` — this head is already being handled |
| `breach.json` | `{"repo#PR": {head, rounds, cap, reason, at, status}}` — `delivery-pending` retries on a current-head watchdog sweep; `awaiting-adjudication` means POST accepted; `adjudicating` means the ruling run was claimed |
| `watchdog.json` | `armed_since`, `{heads: {PR: {sha, observed_at, last_seen_at}}}` (null observation for baseline/invalid clocks; absent PR clocks retained 30 days since last seen). A missing, malformed, boolean, non-finite, or future `armed_since` re-arms at the first successful PR listing and baselines all current heads rather than trusting old observations; a failed listing leaves state and queue unchanged. Alert history and last run are also stored here. |
| `watchdog.log` | one line per sweep, and per breach |
| `artifacts/<PR>/<seat>/` | where a run must keep its worktrees, build dirs and logs — per PR *and* per seat, so the two never share a checkout |

`hermes review-loop status` prints the shape of these files, and `hermes review-loop explain --pr N`
reads them (with the same predicates the gates use) to say why one PR is not moving. `explain` is
read-only down to the byte: it uses the non-pruning readers, so asking twice leaves every file above
exactly as it was.

## Environment overrides

| variable | effect |
|---|---|
| `HERMES_HOME` | where `.hermes` lives (default `~/.hermes`) |
| `REVIEW_LOOP_CONFIG_DIR` | where loop configs live |
| `REVIEW_LOOP_SUBS` | the gateway subscription file to read/write routes from |
| `REVIEW_LOOP_GH_STUB` | test hook: an executable that answers API paths from argv |
| `REVIEW_LOOP_TEST` | ignore the paused check and apply zero grace (real data, for a manual probe) |

## Deliberate non-features

- **No config_schema for *loop* config.** The interesting configuration is per repository, so a
  per-profile form cannot own it — the form carries the plugin-level defaults (including the seat
  profiles/logins above), and the loop file stays plain JSON you can read and diff. Pushing those
  defaults onto a loop is explicit (`apply --loop <id>`), never a subscription; blank fields never
  erase a loop's own answer.
- **No loop-specific editor in the plugin.** The form is a per-profile default, not a per-repository
  editor: it cannot claim to live-edit every loop. The one-loop-at-a-time path is
  `apply --loop <id>` (same validation, same staged route rebind), and a dashboard editor for a
  single loop would have to be built on top of that path, not beside it.
- **No auto-update, no telemetry, no network beyond GitHub, Discord and your own gateway.**
- **No deploy/hosting integration and no model provider assumptions.** The seats are Hermes profiles;
  what model each profile runs is the operator's business.
