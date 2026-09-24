# hermes-review-loop

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

That writes exactly four things, all of them visible and reversible:

1. one loop config — `~/.hermes/review-loops.d/<id>.json`
2. three webhook routes — `<id>-review`, `<id>-fix`, `<id>-breach` — into the gateway's own
   `webhook_subscriptions.json` (generated prompts, generated secrets, file left at 0600)
3. two GitHub hooks, on `pull_request` and `pull_request_review`, pointing at those routes
4. one cron job plus a 5-line shim in `~/.hermes/scripts/` that forwards to the plugin's watchdog

Route edits are serialized only among cooperating review-loop plugin processes, using a sibling
lock file and atomic replacement. Native Hermes CLI and dashboard subscription edits do **not**
take that lock, so concurrent native/plugin edits can still overwrite each other. Fully solving
that race requires an upstream shared lock/protocol for every registry writer. If directory sync
fails after replacement, the plugin raises `RegistryDurabilityError(published=True)`: the new
registry is visible, but crash durability is unconfirmed; do not assume the operation rolled back.

```bash
hermes review-loop list                 # what is configured
hermes review-loop status --loop name   # parallel setting, live runs, queue, breaches
hermes review-loop settings             # the plugin-level defaults, and where each came from
hermes review-loop apply --loop name    # push those defaults onto an existing loop (--dry-run)
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
change takes effect on the next event with nothing to re-install.

Each seat needs its own GitHub token, and that is deliberate: the token that reviews, the token
that pushes and the token that reads are separate and revocable one at a time. A classic PAT with
`repo` is enough for the seats; creating hooks additionally needs `admin:repo_hook`.

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
| `clone` | — | the local clone runs isolate from (required above 1) |
| `base` | main | base branch the loop watches |
| `grace_min` | 25 | quiet minutes before the watchdog speaks |
| `ttl_min` | 45 | how long a seat slot survives a run that died without a verdict |
| `inflight_ttl_min` | 10 | how long a mark blocks a second run at the same head |
| `host` | unset | your gateway's webhook origin; required for `init`, or supply `--host` |

The form shows friendly labels (`Reviews at once`, `Watchdog grace (minutes)`, `Clone path (required
above 1)`); the keys in the table are what `hermes review-loop settings` prints and what the loop
file holds.

Two rules, because a settings form that quietly renumbers a running loop is a miserable thing to
debug at 2am:

* settings are **defaults for a new loop**, and
* they reach an existing loop only when you push them: `hermes review-loop apply --loop <id>`,
  which prints the diff first (`--dry-run` to stop there).

`hermes review-loop settings` prints the same table from the CLI with `[set]` / `[default]` beside
each value, so you can tell what the form actually holds without opening it. Settings follow the
**profile** they were saved in, and the rails still apply: a concurrency above 1 with no clone is
refused at `init`, at `set` and at `apply` alike.

The settings form and the loop config are different surfaces on purpose: the form is per profile and
holds defaults, the loop file is per repository and holds the truth. Point a seat at this plugin's
own skill by its qualified name — `--skill hermes-review-loop:review-loop` — since plugin skills are
never copied into `~/.hermes/skills/`.

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
  it drains a queued run only once the wait has passed the grace period.
- **Paused means silent.** With the repo hooks off, the watchdog says nothing and drains nothing: a
  parked loop must never spend a run.

## Status and honesty

Exercised and passing:

- `python3 tests/run_tests.py` — 218 checks, no network: every gate branch, the cap, the one-PR-one-
  seat rule (including the handoff that must *not* deadlock the gates), per-seat capacity and
  queueing, an approval freeing its slot and starting the next queued PR, **real isolation** (real
  clones — one per PR *and* per seat — checked out at the head, with no token in them), the `set` /
  `apply` / `settings` verbs (including the round trip a stranger's install depends on, and that
  `plugin.yaml`'s `config_schema` still matches the keys the code reads), all four watchdog stall
  shapes, and the cleanup rails against a real git clone.
- Live use on a private repository: two seats, dozens of PRs, review → verdict → fix → cleanup.

Not proven, and worth knowing before you trust it:

- The plugin's own `init` path has been exercised against a test gateway, not against every gateway
  layout in the wild. The intended checks after `init` are `hermes plugins validate` and
  `hermes review-loop status`.
- Cleanup reports **file bytes removed** (`du`), which is not the same as disk recovered on a
  compressed or reflink-sharing volume — quote the `df` delta too.
- One gateway host is assumed for the routes (`host` in each loop config). A fleet of gateways is
  untested.

## Repository layout

```
plugin.yaml                manifest (no hidden capabilities: no hooks, no tools, no middleware)
__init__.py                registers the CLI and the skill
review_loop/               the library: config, state, gh, routes, prompts, gate runtime, CLI
scripts/gate_reviewer.py   between a pull_request event and a review run
scripts/gate_fixer.py      between a pull_request_review event and a fix run
scripts/watchdog.py        cron: stall detection, stuck state, queue draining
scripts/cleanup.py         merge/close: reclaim the PR's local disk
skill/SKILL.md             the protocol the seats load
tests/run_tests.py         the proof (stubbed GitHub, real HTTP sink, real git)
docs/                      architecture and configuration reference
catalog/                   the catalog entry this repo is intended to be listed by
```

## License

MIT. See `LICENSE`.
