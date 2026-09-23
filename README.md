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
| reviewer never posts | "head pushed N hours ago, 0 verdicts at this head" |
| verdict ping-pong forever | the cap is counted in **verdicts**; hitting it hands the PR to an adjudicator instead of buying round four |
| two runs, one clone | seats are serialized: a lock is taken, anything else is queued and drained when the seat frees |
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
  --clone ~/projects/name \
  --root ~/reviews --root ~/.hermes/cache/scratch \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --read-token rev-bot \
  --hooks --admin-token owner-account \
  --schedule 15m --watchdog-deliver telegram
```

That writes exactly four things, all of them visible and reversible:

1. one loop config — `~/.hermes/review-loops.d/<id>.json`
2. three webhook routes — `<id>-review`, `<id>-fix`, `<id>-breach` — into the gateway's own
   `webhook_subscriptions.json` (generated prompts, generated secrets, file left at 0600)
3. two GitHub hooks, on `pull_request` and `pull_request_review`, pointing at those routes
4. one cron job plus a 5-line shim in `~/.hermes/scripts/` that forwards to the plugin's watchdog

```bash
hermes review-loop list                 # what is configured
hermes review-loop status --loop name   # locks, queue, breach markers, last watchdog run
hermes review-loop arm --loop name      # arm/pause by flipping the repo hooks
hermes review-loop pause --loop name
hermes review-loop drain --loop name --seat reviewer
hermes review-loop cleanup --loop name --sweep --dry-run
hermes review-loop uninstall --loop name
```

Each seat needs its own GitHub token, and that is deliberate: the token that reviews, the token
that pushes and the token that reads are separate and revocable one at a time. A classic PAT with
`repo` is enough for the seats; creating hooks additionally needs `admin:repo_hook`.

## What the loop guarantees

- **One run per seat.** A lock keyed to the PR; a second request is queued, costs nothing, and
  starts when the seat frees. Locks expire, so a crashed run cannot wedge a loop.
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

- `python3 tests/run_tests.py` — 78 checks, no network: every gate branch, the cap, seat locks and
  queueing, all four watchdog stall shapes, and the cleanup rails against a real git clone.
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
