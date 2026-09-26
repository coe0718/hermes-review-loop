# hermes-review-loop

> **What runs, and when (issue #16).** No gateway agent ever handles a PR event: every gate
> answers `[SILENT]`, so Hermes never falls through to its normal credential-owning agent.
> Seats run only as **isolated turns** — credentialless, in a bubblewrap sandbox, writing
> through the host broker — and nothing runs until *both* switches in
> [First run](#first-run-in-order) are on: the private runtime file
> (`~/.hermes/review-loop-runtime.json`) that the worker needs, and `arm`, which turns on the
> repo hooks `init --hooks` created paused. Without the file an eligible event is queued with
> its reason and held; with it and the hooks armed, **a reviewer turn posts a real GitHub
> review** as the reviewer login, and a spent cap runs the adjudicator. The fixer is the
> exception: unattended fixer pushes stay **off** (below) until you opt in per loop. Each
> run's checkout is a scratch copy, not a security boundary; the sandbox and broker are.
>
> **Adjudication is isolated like the seats.** A spent cap enqueues an isolated
> adjudicator turn in the host run ledger (never the legacy gateway route, which
> stays silent). It runs credentialless in the same sandbox, with a read-only
> checkout, and can only submit one ruling (ACCEPT / REJECT / RESPEC + reason)
> through the broker. The host records it, tells the operator (observer `ruling`
> notice plus the watchdog outbox), and posts it as a PR comment only when a
> distinct `seats.adjudicator.login` identity is configured. It never merges,
> pushes or reviews. See [`docs/configuration.md#adjudication-the-isolated-ruling`](docs/configuration.md#adjudication-the-isolated-ruling).
>
> **Operator decision: unattended fixer pushes are off by default.** A changes-requested
> verdict is held for you and no fixer turn starts until
> `hermes review-loop fixer-push --loop ID --enable --acknowledge-pr-race`. The gate rejects
> verdicts for PRs whose webhook author is not in `fixers`, and the credentialed broker reads
> the live PR author before every fixer write and rejects missing/outsider authors. These
> checks do not close the time-of-check race: a PR can close, become draft, change
> author/target, or close and reopen after the final API read and before Git receives a ref
> update. The lease only compares `refs/heads/<branch>` to the old SHA, not GitHub PR
> metadata, and a post-push readback can flag some transitions but cannot undo a published
> commit. Do not call that an atomic PR policy; read
> [the push policy](docs/issue-16-boundary.md#unattended-fixer-push-policy-host-operator-not-github-owner-consent) before enabling it. With pushes off, make an
> individual fix by hand: inspect the live PR owner, state, draft flag, base/head repo, review
> and intended diff, push, verify the exact PR/ref afterwards, and reconcile an ambiguous
> outcome instead of retrying it.

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
  --reviewer rev-bot \
  --fixer-profile drey --reviewer-profile vex \
  --cap 3 \
  --reviewer-concurrency 2 --fixer-concurrency 1 \
  --clone ~/projects/name \
  --root ~/reviews --root ~/.hermes/cache/scratch \
  --read-token owner-account \
  --token owner-account=~/.hermes/keys/owner-account-pat \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --token dev-account=~/.hermes/keys/dev-account-pat \
  --host https://your-gateway.example \
  --hooks --admin-token owner-account \
  --schedule 15m --watchdog-deliver telegram
```

The reader (`--read-token`), the reviewer seat and the fixer seat are three different accounts, each
with its own token file (a fourth, `--adjudicator-login`, is optional) — `init` and `set` refuse a
reader that is a seat or shares a seat's file, because the broker would refuse every write. Each
seat login must be in its `--reviewer`/`--fixer` allowlist. `--hooks` and `arm` edit the repo
hooks as `--admin-token`'s login (default: the reader). On a user-owned repo only the owner can
manage hooks, and here the owner is also the reader, so its file needs hook write — fine-grained
`repository_hooks: write`, or classic `repo`; `init` prints this, and `arm` exits 1 naming it if
GitHub refuses. To keep the reader read-only, leave `--hooks` off and add and toggle the hooks by
hand (or, on an org repo, name a separate admin login with its own file and pass the same
`--admin-token` to `arm`). A reader can be changed later with
`hermes review-loop set --loop ID --read-token LOGIN --token LOGIN=/path/to/pat`.

Each `--token` is a *path* to one account's **classic** PAT (mode 600) — never the token itself, and
never a fine-grained token, which GitHub refuses for a seat that is a collaborator on someone else's
repo. See [token files](docs/operations.md#token-files-one-pat-per-account) and
[scopes by role](docs/operations.md#token-scopes-by-role).

Replace `--host` with the public origin of **your own** Hermes gateway (no path), or explicitly
set `host` in this plugin's settings. There is no shared webhook host. `init` refuses a missing or
invalid host before writing the loop config or routes; `--hooks` never creates GitHub hooks in that
case. Use HTTPS for a public GitHub webhook (HTTP is useful for local testing).

`init` writes one loop config, three webhook routes, two GitHub hooks and one cron job — all visible
and reversible; see [what `init` writes](docs/operations.md#what-init-writes) and the
[everyday commands](docs/operations.md#everyday-commands).

### First run, in order

`doctor` checks the installation; `selftest` then checks the isolated turn path. Run these in order
(replace `ID` and `N`; `N` should be an open, non-draft, same-repository PR targeting the loop's
base):

```bash
# 0. the private runtime file (host paths; each seat's model comes from its Hermes profile).
#    Switch one of two: with it, an eligible event runs an isolated seat turn instead of being held.
(umask 077; touch ~/.hermes/review-loop-runtime.json); chmod 600 ~/.hermes/review-loop-runtime.json; $EDITOR ~/.hermes/review-loop-runtime.json
hermes review-loop doctor   --loop ID                       # installation preflight
hermes review-loop selftest --loop ID --no-model            # 1,2,4,6: runtime, bwrap, identities, ledger — free
hermes review-loop selftest --loop ID --pr N                # + one tiny completion per seat model + broker dry run
hermes review-loop selftest --loop ID --pr N --live-turn    # + one real isolated reviewer turn, NOT posted
python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
hermes review-loop arm --loop ID                            # switch two: the hooks go live — reviewer turns now post
```

What each step proves, and how to read a failure, is in
[`doctor`](docs/operations.md#preflight-doctor) and
[`selftest`](docs/operations.md#verifying-the-isolated-setup-selftest). When a PR later stops moving,
`hermes review-loop explain --loop ID --pr N` says why
([details](docs/operations.md#why-isnt-this-pr-moving)).

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

## Running the tests

```bash
python3 tests/run_tests.py                                # the offline harness
python3 -m unittest discover -s tests -p 'test_*.py'      # the boundary suite (bubblewrap; skips without it)
```

The plugin is stdlib-only and so are its tests: there is nothing to install. The harness runs in two
modes, and a few checks only decide anything in one of them — **standalone** (no `hermes_cli` on the
interpreter, which is what a stdlib-only CI image gives you) and **installed** (Hermes importable,
which is every real machine). `doctor` resolves a seat's model by running Hermes *as that profile*
and validates stored cron expressions, so a fixture that builds a "complete installation" has to
build it in both, and CI runs the suite once per mode.

## Documentation

| page | what it covers |
|---|---|
| [docs/operations.md](docs/operations.md) | what `init` writes, everyday commands, the `doctor` preflight, `selftest`, `explain`, burst handling |
| [docs/settings.md](docs/settings.md) | the desktop settings form, seat identity defaults, `settings` / `apply` |
| [docs/observer.md](docs/observer.md) | the observer feed: notices to your phone, how to turn it on, its rules |
| [docs/configuration.md](docs/configuration.md) | every loop-config key, the observer block, adjudication, plugin settings, state files, env overrides |
| [docs/architecture.md](docs/architecture.md) | the seats, isolation, escalation, the watchdog, `explain`, observer and preflight design |
| [docs/issue-16-boundary.md](docs/issue-16-boundary.md) | the isolated route-to-agent boundary (issue #16), selftest guarantees, remaining blockers |
| [docs/issue-1-route-self-heal.md](docs/issue-1-route-self-heal.md) | the webhook-registry race (issue #1): self-heal and what it doesn't close |
| [docs/stacked-submission-boundary.md](docs/stacked-submission-boundary.md) | the stacked reviewer submission boundary (not enabled) |
| [docs/README.md](docs/README.md) | the same index, inside `docs/` |

## Repository layout

```
plugin.yaml                manifest (no hidden capabilities: no hooks, no tools, no middleware)
__init__.py                registers the CLI and the skill
review_loop/               the library: config, state, gh, routes (+ route_intent self-heal), prompts, gate runtime,
                           observer, CLI, the read-only `doctor` preflight and the isolated-path
                           `selftest`
scripts/gate_reviewer.py   between a pull_request event and a review run
scripts/gate_fixer.py      between a pull_request_review event and a fix run
scripts/watchdog.py        cron: route self-heal, stall detection, stuck state, queue draining
scripts/cleanup.py         merge/close: reclaim the PR's local disk
scripts/observe.py         the observer route's adapter: republish the loop's notice, wake nobody
skill/SKILL.md             the protocol the seats load
tests/run_tests.py         the proof (stubbed GitHub, real HTTP sink, real git)
docs/                      operations, settings, observer, architecture and configuration (see docs/README.md)
catalog/                   the catalog entry this repo is intended to be listed by
```

## License

MIT. See `LICENSE`.