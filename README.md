# hermes-review-loop

> **Safety hold (issue #16): PR-facing reviewer, fixer and adjudicator agents are
> disabled in this branch.** Eligible gate events are queued with an actionable
> reason and emit `[SILENT]`, so Hermes never falls through to its normal
> credential-owning gateway agent. The checkout is NOT a security sandbox.
> Do not install this branch expecting an operational review loop. The broker
> has isolated transport and exact-ref lease primitives, but an exact-ref lease
> cannot atomically authorize PR state/author at receive-pack. The descriptions
> below document the previous operational design, not current enabled behavior.
>
> **Adjudication is isolated like the seats.** A spent cap enqueues an isolated
> adjudicator turn in the host run ledger (never the legacy gateway route, which
> stays silent). It runs credentialless in the same sandbox, with a read-only
> checkout, and can only submit one ruling (ACCEPT / REJECT / RESPEC + reason)
> through the broker. The host records it, tells the operator (observer `ruling`
> notice plus the watchdog outbox), and posts it as a PR comment only when a
> distinct `seats.adjudicator.login` identity is configured. It never merges,
> pushes or reviews. See `docs/configuration.md#adjudication-the-isolated-ruling`.
>
> **Operator decision: no unattended fixer push rollout.** The gate rejects
> verdicts for PRs whose webhook author is not in `fixers`; the credentialed
> broker independently reads the live PR author before every fixer write and
> rejects missing/outsider authors. These checks do not close the time-of-check
> race: a PR can close, become draft, change author/target, or close and reopen
> after the final API read and before Git receives a ref update. The lease only
> compares `refs/heads/<branch>` to the old SHA, not GitHub PR metadata. A
> post-push readback can flag some transitions but cannot undo a published
> commit or observe a transient close/reopen. Do not call that an atomic PR
> policy. Keep the production worker config absent, PR-facing routes silent,
> and fixer credentials unavailable to agents. For an individual fix, a human
> operator must inspect the live PR owner, state, draft flag, base/head repo,
> review and intended diff, then make the authorized branch write manually and
> verify the exact PR/ref afterwards; stop and reconcile ambiguous outcomes,
> never blindly retry. Before enabling any automation, require a provider-side
> transaction/authorization mechanism spanning PR metadata and ref mutation (or
> an explicitly accepted weaker threat model), plus uncertain-worker manual
> reconciliation, notification delivery, credential-transport validation, and
> end-to-end production-like tests. None is established by this branch.

**Two agents review each other's pull requests, unattended — and every step in between is a
script, not a model.**

One seat writes code and asks for review; a *different* seat (different model, different account)
reviews it and returns a verdict; the first answers the verdict and asks again; the loop runs until
someone approves it, or until the budget runs out and a human gets handed the decision.

The agents do the work. The loop — who runs, when they are allowed to run, how many rounds are
left, whether the thing has quietly died — is decided by deterministic Python, because that is the
part that must not be creative.

```
        ┌────────────── open / request ───────────────┐
        │                                             ▼
   ┌─────────┐   push + ask for review          ┌──────────┐
   │  fixer  │ ───────────────────────────────▶ │ reviewer │
   └─────────┘                                  └──────────┘
        ▲                                             │
        │            changes requested ◀──────────────┘
        │                    │
        └──── under the cap ─┘
                             │
                    cap spent │ → adjudicator rules, human merges
```

## Why it exists: unattended loops fail quietly

The hard part of an agent loop is not the agents. It is that a loop which stopped being driven
looks *exactly* like a loop with nothing to do:

- GitHub clears a pending review request the moment a verdict lands — so a fixer that pushes
  without re-requesting review silently ends the loop. Nobody notices for days. (In the loop this
  was built from, the fixer leg was also dead on arrival because webhook payloads spell review
  states `changes_requested` while the REST API spells them `CHANGES_REQUESTED`. Synthetic tests
  written from the author's own assumptions passed the whole time.)
- A verdict that keeps coming back as "changes requested" burns a night and a budget with no exit.
- Two runs sharing one clone corrupt each other's worktrees and produce **wrong verdicts**, which
  is worse than a failed run.
- Every review leaves a checkout and a build directory behind. Merged PRs used to leave all of it:
  one measured at **8 GB**, and a backlog sweep reclaimed **87 GB**.

Every one of those is a *silent* failure, so this plugin makes each one loud or impossible:

| failure | what the loop does |
|---|---|
| push without a request | the gate only wakes the reviewer on an explicit request (and the fixer's prompt spells out the `gh api` call) |
| fixer never pushes | the watchdog reports "changes requested N hours ago at head X, fixer never pushed" |
| reviewer never posts | "head first observed N hours ago, 0 verdicts at this head" |
| verdict ping-pong forever | the cap is counted in **verdicts**; hitting it hands the PR to an adjudicator instead of buying round four |
| two runs, one clone | a seat is a capacity with a per-PR ledger; `concurrency: 2+` gives each PR its own clone, build dir and tmp dir, and an unisolatable run is queued rather than shared |
| a run dies mid-way | the lock expires; a stalled head frees itself |
| disk creep | a merged/closed PR runs the cleanup: worktrees, build dirs, logs, locks, counters |
| another registry writer erased or rewrote a route | the watchdog restores it from the plugin's own intent record, same secret, and says so; `doctor` flags it; `doctor --repair` restores it now |
| "did the loop ever run?" | every branch of every gate either fires or logs *why not*; the watchdog reads GitHub state directly instead of trusting anyone's summary |

## Install

```bash
hermes plugins install coe0718/hermes-review-loop
```

Then configure one loop per repository:

```bash
hermes review-loop init \
  --repo owner/name \
  --fixer dev-account \
  --reviewer rev-account --reviewer-seat rev-bot \
  --fixer-profile drey --reviewer-profile vex \
  --cap 3 \
  --reviewer-concurrency 2 --fixer-concurrency 1 \
  --clone ~/projects/name \
  --root ~/reviews --root ~/.hermes/cache/scratch \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --read-token rev-bot \
  --host https://your-gateway.example \
  --hooks --admin-token owner-account \
  --schedule 15m --watchdog-deliver telegram
```

Replace `--host` with the public origin of **your own** Hermes gateway (no path), or explicitly
set `host` in this plugin's settings. There is no shared webhook host. `init` refuses a missing or
invalid host before writing the loop config or routes; `--hooks` never creates GitHub hooks in that
case. Use HTTPS for a public GitHub webhook (HTTP is useful for local testing).

When the plugin settings name the seats (`fixer_profile`, `reviewer_profile`, `reviewer_login`,
`fixer_login`, `adjudicator_profile`), the seat flags above become optional — the form supplies them
and an explicit flag still wins. `--dry-run` prints the whole plan (who serves each seat, the route
URLs, what would be written) and stops there, which is the way to check a form before it reaches a
running loop.

That writes exactly four things, all of them visible and reversible:

1. one loop config — `~/.hermes/review-loops.d/<id>.json`
2. three webhook routes — `<id>-review`, `<id>-fix`, `<id>-breach` — into the gateway's own
   `webhook_subscriptions.json` (generated prompts, generated secrets, file left at 0600)
3. two GitHub hooks, on `pull_request` and `pull_request_review`, pointing at those routes
4. one cron job plus a 5-line shim in `~/.hermes/scripts/` that forwards to the plugin's watchdog

Route edits are serialized only among cooperating review-loop plugin processes, using a sibling
lock file and atomic replacement. Native Hermes CLI and dashboard subscription edits do **not**
take that lock (issue #1; upstream fix pending in NousResearch/hermes-agent#120964), so the plugin
mitigates the race from its side — it does not close it:

* **Optimistic writes.** Each plugin edit records the registry's inode, mtime, size and content
  hash when it reads, re-checks them immediately before `os.replace`, and re-reads and re-applies
  its edit (up to 5 attempts, then `RegistryConflictError` with nothing published) if a native
  write landed in between — so the plugin no longer overwrites a concurrent native change.
* **Intent record + self-heal.** Every route the plugin installs or rebinds is also copied,
  secret included, to `<state_dir>/route-intent.json` (0600, atomic). Every armed watchdog sweep
  compares this loop's routes with it and restores any route a native writer erased or changed
  (secret, script, prompt, events, profile, `deliver_only`, host) with the **same secret**, so
  GitHub's hook keeps authenticating, and says what it restored in its cron output. Other routes
  are never touched; a name now held by a non-review-loop script is reported, not overwritten; a
  malformed registry is never overwritten. Change or remove routes through `set`/`apply`/
  `uninstall` — they update the record — or self-heal will put a native edit back.
* **What remains.** A few syscalls between the final identity check and the rename, and a native
  writer that read *before* a plugin publish and writes *after* it, can still drop a plugin edit
  (or a native one). The plugin's lost routes come back on the next armed sweep; a native edit
  the plugin overwrote in that window does not. Between sweeps a broken route can miss
  deliveries. See [docs/issue-1-route-self-heal.md](docs/issue-1-route-self-heal.md).

If directory sync
fails after replacement, the plugin raises `RegistryDurabilityError(published=True)`: the new
registry is visible, but crash durability is unconfirmed; do not assume the operation rolled back.

```bash
hermes review-loop list                 # what is configured
hermes review-loop status --loop name   # seats, profiles, routes, live runs, queue, breaches
hermes review-loop explain --loop name --pr 123   # why that PR is not moving, and what is next
hermes review-loop doctor --loop name   # preflight the install: profiles, tokens, routes, hooks, cron
hermes review-loop settings             # the plugin-level defaults, and where each came from
hermes review-loop init --repo owner/name --dry-run   # preview a loop: seats, routes, nothing written
hermes review-loop apply --loop name    # push those defaults onto an existing loop (--dry-run)
hermes review-loop apply --loop name --while-busy     # rebind even while a seat has a run out
hermes review-loop set --loop name --reviewer-concurrency 2   # two reviews at once, one fix at a time
hermes review-loop arm --loop name      # arm/pause by flipping the repo hooks
hermes review-loop pause --loop name
hermes review-loop drain --loop name --seat reviewer
hermes review-loop cleanup --loop name --sweep --dry-run
hermes review-loop uninstall --loop name
```

`set` is how you change the knobs after install — `--reviewer-concurrency`, `--fixer-concurrency`,
`--concurrency` (the default for both seats), `--cap`, `--clone`, `--base`, `--grace-min`,
`--ttl-min` — through the same validation `init` uses, so a capacity above 1 without a clone is
refused here exactly as it is at init. Prompts are rendered from the payload at fire time, so a
change takes effect on the next event with nothing to re-install. The observer feed is changed the
same way: `--observer-profile`, `--observer-route`, `--observer-deliver`, `--observer-events`,
`--observer-digest-min`, and `--observer-mute` / `--observer-unmute` / `--observer-disable`.

### Preflight: `doctor`

`init` writes the config, the routes and (optionally) the hooks and the cron job — but a
syntactically valid file is not proof that any of it can run. The reviewer's profile may not
exist, the token file named for the fixer may have gone in a key rotation, the route in the
gateway registry may wake a *different* profile than the loop config says, the repo hook may
point at your previous gateway, or the cron shim may still be pinned to the plugin directory a
previous upgrade left behind. Every one of those is a loop that looks armed and cannot wake a
seat — so `doctor` checks the installation itself, read-only, before anyone arms it:

```bash
hermes review-loop doctor --loop attest             # one loop; without --loop it preflights them all
hermes review-loop doctor --loop attest --offline   # skip the gateway probe and the hooks read
hermes review-loop doctor --loop attest --strict    # an undecided check counts as a failure
```

One line per check, in one of four states:

| state | meaning |
|---|---|
| ✅ verified | checked, and correct |
| ❌ absent | the thing is not there — a missing profile, token file, route, hook, job or script |
| ❌ mismatch | present, but not what this loop needs — a route waking another profile, a hook on another gateway, a shim pinned to a stale plugin path, a world-readable PAT |
| ⚠️ unknown | could not be decided *from here* — a hooks read the token was not allowed to make, or a probe skipped with `--offline` |

Each failure is followed by the one command that fixes it, failures exit 1, and `unknown` is never
reported as `absent`: "the API refused to tell me" and "there are no hooks" are different claims,
and printing the second when the first is true sends you hunting for a hook that exists (reading
the repo's hooks needs `admin:repo_hook`, so a token without it shows ⚠️, not ❌).

It writes nothing — no config, no route registry, no state, no GitHub hook — unless you pass
`--repair`, whose one write is restoring this loop's own routes from the plugin's intent record
(same secret) before the read-only checks run. It never fires a
route, because a synthetic POST at a seat's route is a real agent run with a real budget. The
network side is a TCP connect to the gateway (is anything listening?) and, when the token is
allowed to, a read of the repo's hooks.

A correct installation:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → a nonempty token file (identity and API access not checked)
  ✅ profile:fixer        fixer-profile → doctor-demo/hermes-home/profiles/fixer-profile
  ✅ credential:fixer     dev-fixer → a tokens entry
  ✅ token:dev-fixer      doctor-demo/fix.pat (mode 600, non-empty)
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           rev-coach (mapped in tokens)
  ✅ route:widgets-review reviewer-profile · pull_request · http://127.0.0.1:43651/p/reviewer-profile/webhooks/widgets-review
  ✅ route:widgets-fix    fixer-profile · pull_request_review · http://127.0.0.1:43651/p/fixer-profile/webhooks/widgets-fix
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /home/jeremy/projects/rl-15-doctor/scripts (watchdog, both gates, cleanup)
  ✅ cron:shim            doctor-demo/hermes-home/scripts/review-loop-watchdog.py → /home/jeremy/projects/rl-15-doctor/scripts/watchdog.py
  ✅ cron:job             8f21c0 every 15m, next 2026-09-23T22:15:00Z
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                1 configured: doctor-demo/reviews
  ✅ gateway              127.0.0.1:43651 accepts a connection
  ✅ hook:widgets-review  hook 41 → http://127.0.0.1:43651/p/reviewer-profile/webhooks/widgets-review (pull_request, active)
  ✅ hook:widgets-fix     hook 42 → http://127.0.0.1:43651/p/fixer-profile/webhooks/widgets-fix (pull_request_review, active)

widgets: 20 verified, 0 failed, 0 unknown (of 20 checks)
  every check passed — this loop can wake a seat and post a verdict.
```

and the same loop with six of the ways it really breaks:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → a nonempty token file (identity and API access not checked)
  ❌ profile:fixer        no profile home at doctor-demo/hermes-home/profiles/fixer-profile
      fix: `hermes profile create fixer-profile`, or re-run init with --fixer-profile pointing at a profile that exists: the run happens as this profile
  ✅ credential:fixer     dev-fixer → a tokens entry
  ❌ token:dev-fixer      no file at doctor-demo/fix.pat
      fix: write the PAT for dev-fixer to doctor-demo/fix.pat (chmod 600), or re-run init with --token dev-fixer=<a path that exists>
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           rev-coach (mapped in tokens)
  ❌ route:widgets-review registered at https://old-gateway.example, but the loop is armed at http://127.0.0.1:43651
      fix: re-run init to rewrite the route for http://127.0.0.1:43651: a hook or a manual POST still goes to the recorded origin
  ❌ route:widgets-fix    wakes profile 'some-other-agent', but seats.fixer.profile is 'fixer-profile' — the wake would run the wrong agent
      fix: re-run init with --fixer-profile fixer-profile so the route and the loop config agree
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /home/jeremy/projects/rl-15-doctor/scripts (watchdog, both gates, cleanup)
  ❌ cron:shim            pinned to /opt/old/plugins/hermes-review-loop/scripts/watchdog.py, this install runs /home/jeremy/projects/rl-15-doctor/scripts/watchdog.py
      fix: re-run init --schedule 15m for this loop: the shim was written by a different plugin install, and the scheduler keeps running that path
  ❌ cron:job             8f21c0 (review loop watchdog (widgets)) is paused
      fix: `hermes cron resume 8f21c0`: a paused watchdog never reports a stall
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                1 configured: doctor-demo/reviews
  ✅ gateway              127.0.0.1:43651 accepts a connection
  ⚠️ hooks                could not read /repos/acme/widgets/hooks — nothing was proved about 2 hook(s) (a token without admin:repo_hook reads as denied)

widgets: 12 verified, 6 failed, 1 unknown (of 19 checks)
  6 failed: profile:fixer, token:dev-fixer, route:widgets-review, route:widgets-fix, cron:shim, cron:job — fix the ❌ lines above before this loop is armed.
```

(Both transcripts are real output from the suite's isolated demo home — a loopback gateway
sink for the probe, a stubbed GitHub, short relative paths. A run against a live install
prints the same lines with absolute paths and the real hook list.)

*Who* serves each seat is the settings form's business (below), not `set`'s: a per-profile form holds
the defaults, and `apply --loop` pushes them onto exactly one loop — with the seat diff, the route
profiles it rebinds and the credentials it checked. For one repository that needs a shape no form
should own (a different allowlist, its own route names), the loop file is still plain JSON you can
read and diff; `init` is the only verb that writes routes from scratch.

Each seat needs its own GitHub token, and that is deliberate: the token that reviews, the token
that pushes and the token that reads are separate and revocable one at a time. A classic PAT with
`repo` is enough for the seats; creating hooks additionally needs `admin:repo_hook`. A loop that
names tokens must name one per seat, and the file has to be there — checked before `init` or `apply`
writes anything, because a missing PAT otherwise surfaces hours later as an unauthenticated read.

### Why isn't this PR moving?

`status` shows the loop's shape; `explain` answers the question you actually have at 2am, for one
PR: what GitHub says about the head, how much of the budget is spent *at that head*, who holds the
seat, what is queued or marked in flight, whether the loop is paused, and — last line, always — the
one event that has to happen next.

```bash
hermes review-loop explain --loop widgets --pr 7    # --loop may be omitted when it is the only loop
```

```
[widgets] acme/widgets#7 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/7
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head aaaaaaa
  budget:     1/3 verdicts spent · 1 at head aaaaaaa — changes requested 2026-01-01T00:00:00Z by rev-coach
  seat:       nobody holds it
  queue:      not queued
  in-flight:  none
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  blocked:    the changes-requested verdict at head aaaaaaa has no fix run out — the fixer gate did not start one for that delivery
  next:       re-deliver the changes-requested review event for head aaaaaaa to the fixer gate after checking why its run did not start — no fixer is running to push a fix
```

A PR that is waiting rather than broken says so, instead of looking like a failure:

```
[widgets] acme/widgets#9 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/9
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head bbbbbbb
  budget:     0/3 verdicts spent · nothing at head bbbbbbb
  seat:       nobody holds it
  queue:      reviewer 1 of 1 (waiting 9m) — reviewer at capacity 1/1: acme/widgets#7 (720s)
  in-flight:  none
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  blocked:    no capacity: queued with the reviewer seat — reviewer at capacity 1/1: acme/widgets#7 (720s)
  next:       a reviewer slot frees — the queued run starts then (a verdict or a handoff ends the run holding it; the lock expiry at 45m is the backstop)
```

Three rules keep it honest:

* **No second engine.** The conclusions come from the same predicates the live gates run
  (`verdicts`, `reviewed_at_head`, `changes_at_head`, `approved_at_head`, the seat ledgers, the
  queue, the breach marker, the armed check), in the gates' own guard order. A gate stops at the
  first guard that silences it; `explain` reports every guard and names the one that is holding the
  PR. What it says cannot drift from what the loop would do, because it is the same code.
* **Unknown is not a guess.** A transient GitHub failure, an unreadable review list or hook list,
  or a malformed PR head is printed as unknown and needs a retry, not a guessed verdict or review
  request. HTTP 404 means missing *or inaccessible* and calls for checking the number and access,
  not treating it as a transient failure. Every timestamp is labelled with where it came from — the
  read itself, the verdict's `submitted_at`, or the state file's own mark.
* **Read-only, byte for byte.** No claim, no queue entry, no inflight mark, no drain, no webhook
  POST, no token printed, and it does not even prune an expired lock while looking at it. Run it
  twice and GitHub, the loop's state directory and your routes file are untouched. The suite asserts
  exactly that.

It needs to read the repo's hooks to tell "paused" from "armed", so the read token wants enough
scope to see them (`repo` is normally enough); if it cannot, the line says the hook state is unknown
rather than claiming the loop is parked. `explain` exits 2 only when the question cannot be asked at
all — an unknown loop, or several loops and no `--loop`.

### How it handles a burst

Fifty PRs arrive in an hour. Ten of them wake the reviewer, and forty-one queue — the queue costs
nothing but disk-less JSON. The moment a review ends, that slot is filled from the queue:

```
review #101 finishes (approve or changes-requested)
  → its slot is freed
  → the queue is drained immediately, up to the free slots
  → the next queued PR's review starts
```

A slot is not freed by a timer. The **verdict** frees it, either kind; the fixer's **request** frees
the fixer's. The watchdog sweep is only the backstop, and `ttl_min` is the last resort for a run that
died without a verdict. Capacity is per seat, so `reviewer 10 · fixer 2` is a legitimate shape —
reviews are cheap and parallel, fixes are not.

Do the arithmetic before setting it high: each in-flight run is a whole agent plus its own clone and
its own cold build. On a big Rust repo, ten at once is ten parallel builds — the machine, not GitHub,
is what decides how high this number can go.

### Settings, in the desktop

The plugin declares a `config_schema`, so it has a settings form at
**Capabilities → Plugins → review loop** — no hand-edited JSON required:

| setting | default | what it does |
|---|---|---|
| `cap` | 3 | verdicts before the loop stops and hands the PR to an adjudicator |
| `reviewer_concurrency` | 1 | reviews at once; the rest queue |
| `fixer_concurrency` | 1 | fixes at once; the rest queue |
| `reviewer_profile` | — | Hermes profile the reviewer seat runs as |
| `fixer_profile` | — | Hermes profile the fixer seat runs as |
| `reviewer_login` | — | GitHub login the reviewer acts as (and the login the review route serves) |
| `fixer_login` | — | GitHub login the fixer acts as |
| `adjudicator_profile` | — | Hermes profile that rules when the budget is spent (optional) |
| `clone` | — | the local clone runs isolate from (required above 1) |
| `base` | main | base branch the loop watches |
| `grace_min` | 25 | quiet minutes before the watchdog speaks |
| `ttl_min` | 45 | how long a seat slot survives a run that died without a verdict |
| `inflight_ttl_min` | 10 | how long a mark blocks a second run at the same head |
| `host` | unset | your gateway's webhook origin; required for `init`, or supply `--host` |

The form shows friendly labels (`Reviews at once`, `Reviewer's Hermes profile`, `Clone path
(required above 1)`); the keys in the table are what `hermes review-loop settings` prints and what
the loop file holds.

**Blank means *not set here*, never "unset what the loop has".** A blank profile or login leaves
that seat exactly as the loop file has it, which is what lets one profile-level form hold defaults
without silently rewriting the seats a repository already answered for itself. Profiles and logins
are validated before anything is written — the profile must exist on this machine, the login must be
in that loop's allowlist, the two seats must not share a profile, a login or a token file, and every
token the loop names must be a file that is there. A form that names a seat is a promise that the
seat can run, so `init` and `apply` refuse rather than write a loop that fails at its first event.

Two rules, because a settings form that quietly renumbers a running loop is a miserable thing to
debug at 2am:

* settings are **defaults for a new loop**, and
* they reach an existing loop only when you push them: `hermes review-loop apply --loop <id>`,
  which prints the diff first (`--dry-run` to stop there).

Changing *who* serves a seat is staged rather than half-applied: the routes whose URLs carry the old
profile are rebound first, the loop config is committed second, and every rebind is verified by
reading the route registry back. A registry that refuses the rebind leaves the loop exactly as it
was — there is no config to put back. A seat with a run in flight is refused rather than rewritten
underneath itself — the live run holds its old profile, login and credential until it ends — and
`--while-busy` is the explicit override that says "rebind now, I know that run finishes under the
identity it started with". `apply --dry-run` shows all of it without writing: the seat diff, the
route profiles it would rebind, and nothing else.

`apply` is also the upgrade path for a loop installed before the dedicated adjudicator gate: its
breach route still runs `gate_reviewer.py`, `doctor` flags it, and `apply --loop <id>` rebinds that
route to `gate_adjudicator.py` in place, secret kept. Only a script this plugin itself once
installed for the route (with its own prompt) is rewritten; anything else is left alone.

Preview a loop before installing it with `hermes review-loop init ... --dry-run`: it prints the
effective seat mapping (profile, login, route, and the URL the profile is part of) and stops
without writing config, routes, hooks or cron.

```bash
hermes review-loop settings                      # the form's seat mapping, and what each loop runs as today
hermes review-loop init --repo owner/name --dry-run   # preview: seats, routes, nothing written
hermes review-loop apply --loop name --dry-run   # what a push would change, including routes
hermes review-loop status --loop name            # each seat, its profile, and whether its route agrees
```

`status` is the honest surface: it prints what the *installed* route serves next to what the config
claims, and says `MISMATCH` with the command to fix it when a seat moved but its route did not.
Token *references* are shown (which login reads which file); token values never are — they live in
the per-profile 0600 file the seats read at use time.

The form stays a per-profile default on purpose. Fixer/reviewer **allowlists**, route names, the
adjudicator route and everything else that is per repository stay in the loop config, because one
form cannot honestly claim to own every repository. Pushing the form onto one loop with
`apply --loop` is the loop-specific editor: it goes through the same validation and the same staged
apply, and it never touches a loop you did not name.

`hermes review-loop settings` prints the same table from the CLI with `[set]` / `[default]` beside
each value — and the seat mapping underneath it — so you can tell what the form actually holds,
and what each configured loop runs as today, without opening either. Settings follow the
**profile** they were saved in, and the rails still apply: a concurrency above 1 with no clone is
refused at `init`, at `set` and at `apply` alike.

The settings form and the loop config are different surfaces on purpose: the form is per profile and
holds defaults, the loop file is per repository and holds the truth. Point a seat at this plugin's
own skill by its qualified name — `--skill hermes-review-loop:review-loop` — since plugin skills are
never copied into `~/.hermes/skills/`.

### Watching from your phone (the observer feed)

The seats drive each other in the historical design; on this branch, PR-facing turns are held
for isolated workers or queued fail-closed, and no gateway agent is dispatched. An observer
can still report a verified transition, but an `opened`, `handoff`, or `verdict` notice says the
next turn is **queued**, not that a reviewer or fixer has started. Give a loop an **observer** and
it sends one short notice per transition to a chat you choose — Telegram, Discord, wherever
that Hermes profile already talks:

```
🔧 [widgets] #7 `aaaaaaa` fix pushed · review requested (dev-fixer) · round 1/3 · next: reviewer queued
https://github.com/acme/widgets/pull/7
```

That is the whole payload: the loop, the PR, the head at the recorded transition, the seat and
event, the outcome, an optional next-turn hint, and a direct link to the PR. Delayed retries and
digests omit next-turn hints because the PR or review may have changed since the transition.
Never a token, an HMAC secret, a private diff, or a review body. Escalation reaches the observer
after the cap marker is durable and before the adjudicator turn is enqueued; it explicitly says
delivery is pending, not that the adjudicator received it. A `ruling` notice carries the verdict
and counts, never the adjudicator's reason text (that goes to the watchdog outbox and, when an
adjudicator identity is configured, the PR).

Turn it on at init, or add it to a loop that is already running:

```bash
hermes review-loop init --repo owner/name ... --observer-profile tuck   # one flag turns it on
hermes review-loop set --loop widgets --observer-profile tuck           # or add it later
hermes review-loop set --loop widgets --observer-events verdict,escalation,closed
hermes review-loop set --loop widgets --observer-digest-min 30          # batch instead of pinging
hermes review-loop set --loop widgets --observer-mute                   # quiet, config kept
hermes review-loop set --loop widgets --observer-disable                # stop/remove route; retain owed ledger and old destination binding
```

The flags write this block into the loop file, the only place the feed is configured:

```json
"observer": {
  "route": "widgets-observe",
  "profile": "tuck",
  "deliver": "telegram",
  "events": ["opened", "handoff", "verdict", "approved", "escalation", "ruling", "stall", "closed"],
  "digest_min": 30
}
```

`init` and `set` write the keys you asked for and nothing else: `mute: true` for a muted feed,
`digest_min` above zero to batch, `events` to narrow the feed (leave it out for all of them).

`init` also installs the route (`<id>-observe`) through the seats' own mechanism — the same signed
POST at the same gateway — but with `deliver_only: true` and a two-line prompt, because the notice
is *already written*: nothing wakes an agent, and there is no third seat to hold a lock or take a
turn. The eight transitions are `opened` (a new PR needs its first look), `handoff` (a fix was
pushed and review requested), `verdict` (a changes-requested verdict started a fix), `approved`
(the reviewer approved), `escalation` (the cap is spent and adjudicator delivery is pending),
`ruling` (an isolated adjudicator's ruling was recorded), `stall` (the watchdog decided a quiet PR is worth reporting) and `closed` (merged or abandoned;
cleanup was attempted, but disk reclamation is not confirmed by this notice).

Four rules keep the feed from becoming a gate:

* **Emitted from state, not from prose.** A notice is written by the gate, by the seat scripts and
  by the watchdog, at the transition they just made — never parsed out of an agent's summary, and
  never on the strength of an agent's claim.
* **One transition, one notice.** The ledger key is loop + PR + head + event + verdict/round
  identity, so a redelivered webhook, a re-run gate or a retried sweep cannot produce a duplicate.
* **Ambiguous delivery is not replayed.** A missing route or secret is a definite pre-POST failure
  and the watchdog retries it (up to three attempts). A timeout or 5xx after posting may have sent
  the notice; it remains `uncertain` in `status` for manual reconciliation, never automatically
  retried. Legacy `failed` receipts without proof of a pre-POST failure are quarantined the same
  way. Neither outcome consumes a seat or blocks the queue.
* **Off means off.** No observer, a muted feed, an event filtered out: the loop behaves exactly as
  it would with no observer at all. A misconfigured feed (a route that was never installed, or a
  bare profile with no route) never refuses a loop — the seats keep running and `status` says what
  is wrong with the feed.

Private PRs are safe to watch this way: the link goes to the chat the operator configured for that
profile, and nowhere else.

## What the loop guarantees

- **One PR, one seat.** A PR is held by the reviewer *or* the fixer, never both: a review never
  runs against a PR the fixer is mid-fix on. The handoff is what frees the other seat — the fixer's
  `review_requested` ends the fixer's turn, the reviewer's verdict ends the reviewer's. Any other
  trigger that arrives while the other seat holds the PR queues instead of starting.
- **Capacity is per seat.** `reviewer 2 · fixer 1` means two reviews in flight and one fix — Drey
  and Vex are different models on different budgets, and wanting two reviews rarely means wanting
  two fixes. Everything above a seat's limit queues, and starts when a slot frees.
- **Parallel only when it is safe.** A capacity above 1 gives every run its own clone and its own
  build/temp dirs — per PR *and per seat*, because the two seats can overlap on one PR. A run that
  cannot be isolated is queued, never started beside another.
- **The cap is a wall, not a suggestion.** `cap` verdicts, `cap - 1` fix turns. The verdict that
  reaches the cap escalates instead of buying another round. The human is the veto, not the
  reviewer: the adjudicator rules and reports, and never merges or pushes.
- **One wake per head.** Every marker is keyed by PR *and* commit: a new commit is a new situation,
  the same commit is not. Redelivered webhooks do nothing.
- **Unknown is not a guess.** If the review list cannot be read, the gate stays silent rather than
  assuming round 1 — a skipped round beats a miscounted one.
- **The watchdog is read-only until it has a reason.** Four stall shapes, read from GitHub state;
  each armed sweep drains eligible queued runs when a seat is free, without waiting for a stall alert.
  A queued head that no longer matches the PR is dropped, never silently retargeted.
- **Asking why changes nothing.** `explain` reads GitHub and the loop's own files, reaches its
  conclusion through the *same* predicates the gates run, and writes nothing at all — no queue
  entry, no claim, no drain, no webhook POST, no token. Run it twice and the loop is byte-for-byte
  as it was.
- **Paused means silent.** With the repo hooks off, the watchdog says nothing and drains nothing: a
  parked loop must never spend a run.
- **A seat is who the config says it is — or the loop refuses to run.** The profile it runs as, the
  login it acts as and the route that wakes it are validated together before a config, a route or a
  hook is written, and `status` prints the installed route next to the configured seat so a
  half-applied identity change is visible instead of silent.
- **The observer is not a seat.** An opt-in feed of short notices, emitted from the transitions the
  loop already made, delivered by a `deliver_only` route with no agent behind it. A refused
  delivery costs a retry — never a queue entry, a lock, or a turn; and with the feed off the loop
  is byte-for-byte the loop without one.

## Status and honesty

**Current branch:** the hermetic canonical harness now checks `[SILENT]`,
held eligible turns, and the guarded breach marker instead of expecting legacy
`FIRE` payloads or an adjudicator POST. The focused pytest suite also exercises
an isolated worker with disposable credentials and local fakes. Neither suite
proves a safe GitHub-side PR-metadata/ref transaction; see
`docs/issue-16-boundary.md`. The historical claims below describe pre-hold live
use and must not be used to certify this branch.

Historical pre-hold evidence (not current rollout authorization):

- `python3 tests/run_tests.py` — full offline suite: every gate branch, the cap, the one-PR-one-
  seat rule (including the handoff that must *not* deadlock the gates), per-seat capacity and
  queueing, an approval freeing its slot and starting the next queued PR, **real isolation** (real
  clones — one per PR *and* per seat — checked out at the head, with no token in them), the `set` /
  `apply` / `settings` verbs (including the round trip a stranger's install depends on, and that
  `plugin.yaml`'s `config_schema` still matches the keys the code reads), **seat identity** (the form
  choosing reviewer/fixer/adjudicator profiles and logins, a preview that writes nothing, several
  loops staying isolated from each other, an identity change refused while a seat is in flight and
  staged — config *and* route — once it is not, and invalid mappings refused before any write),
  the `doctor` preflight (missing profiles, tokens, routes, hooks or cron jobs fail with remediation;
  an API-denied hooks read is `unknown`, never "absent"), all four watchdog stall shapes, `explain`'s
  golden cases (in-flight/no-verdict/no-fix reviews, unrequested head, queued/full seat, spent
  budget, paused loop, closed/missing PR, failed GitHub read) and its read-only proof, the route/hook
  reconciliation rollback cases, the cleanup rails against a real git clone, and the observer feed
  (one notice per verdict and handoff, no duplicate on redelivery, a 5xx destination never blocking
  queue drain, escalation delivered before adjudication, and mute/digest/misconfiguration inert).
- Live use on a private repository: two seats, dozens of PRs, review → verdict → fix → cleanup.

Not proven, and worth knowing before you trust it:

- The plugin's own `init` path has been exercised against a test gateway, not against every gateway
  layout in the wild. The intended checks after `init` are `hermes plugins validate` and
  `hermes review-loop doctor --loop <id>` — and `doctor` has itself only been run against the
  suite's stubbed GitHub and isolated homes, not against a live repo's hook list. Check
  `hermes review-loop status` after installation too.
- **The seat-identity surfaces are exercised against local files, not the desktop form.** The suite
  calls `register_cli` with a settings dict (the shape the form writes), so the plugin-side
  behaviour — defaults, validation, preview, staged apply — is covered; whether the desktop renders
  the new fields the way the manifest asks is not something these tests can see.
- A live run that spans an identity change keeps the profile and login it started with. `--while-busy`
  is honest about that but cannot retro-fit a run already in flight.
- Cleanup reports **file bytes removed** (`du`), which is not the same as disk recovered on a
  compressed or reflink-sharing volume — quote the `df` delta too.
- `explain` has been exercised by the suite and by hand against stubbed GitHub, not yet against a
  live 2am stall. Its armed/paused line depends on the read token being able to see the repo's
  hooks; where it cannot, the line says unknown instead of claiming the loop is parked.
- One gateway host is assumed for the routes (`host` in each loop config). A fleet of gateways is
  untested.

## Repository layout

```
plugin.yaml                manifest (no hidden capabilities: no hooks, no tools, no middleware)
__init__.py                registers the CLI and the skill
review_loop/               the library: config, state, gh, routes (+ route_intent self-heal), prompts, gate runtime,
                           observer, CLI, and the read-only `doctor` preflight
scripts/gate_reviewer.py   between a pull_request event and a review run
scripts/gate_fixer.py      between a pull_request_review event and a fix run
scripts/watchdog.py        cron: route self-heal, stall detection, stuck state, queue draining
scripts/cleanup.py         merge/close: reclaim the PR's local disk
scripts/observe.py         the observer route's adapter: republish the loop's notice, wake nobody
skill/SKILL.md             the protocol the seats load
tests/run_tests.py         the proof (stubbed GitHub, real HTTP sink, real git)
docs/                      architecture and configuration reference
catalog/                   the catalog entry this repo is intended to be listed by
```

## License

MIT. See `LICENSE`.
