# Architecture

Five processes, four state files, one rule: **the control plane never guesses.**

```
GitHub ──pull_request──────────▶ gate_reviewer.py ──┬─▶ [SILENT]      (no run, no tokens)
       │                                            ├─▶ breach ──▶ adjudicator route
       │                                            └─▶ payload + _loop ──▶ reviewer agent
       │
       ├──pull_request_review───▶ gate_fixer.py ────┬─▶ [SILENT]
       │                                            ├─▶ breach
       │                                            └─▶ payload + _loop ──▶ fixer agent
       │
       └──(merge/close)─────────▶ gate_reviewer.py ──▶ cleanup.py   (disk, no agent)

cron (15m, no agent) ──▶ watchdog.py ──▶ alerts in the operator's chat, drains the queue
```

## The seats

A **seat** is a role, not an agent: `reviewer` and `fixer`. Each seat is bound to a Hermes profile
(the run happens as that profile), a GitHub login (attribution and permissions) and a webhook route
(the way it is woken). Both seats are declared per loop, so the same install can run a different
pair of agents on a different repository with a different budget.

A seat is a **capacity, not a mutex**, and each seat has its own:

| setting | effect |
|---|---|
| `concurrency` (loop) | the default limit for both seats |
| `seats.reviewer.concurrency` | the reviewer's own limit (Vex: two reviews at once) |
| `seats.fixer.concurrency` | the fixer's own limit (Drey: one fix at a time) |

`hermes review-loop set --reviewer-concurrency 2 --fixer-concurrency 1` is the shape Jeremy asked
for: *Drey works on X PRs at once MAX, Vex reviews Y at once, everything else queues.* A seat-level
value wins over the loop default; `1` (serialized) is the fallback everywhere. Above 1 a loop
**must** have a `clone`, because a parallel run that cannot be isolated would share a checkout —
and that rail is checked against the *effective* value per seat, so a seat-level 2 is caught even
when the loop default stays 1.

The ledger is keyed by **PR**, so the same PR never runs twice even with a free slot — and the same
*head* never runs twice at all (in-flight marks). A slot expires (`ttl_min`), so a crashed run
cannot wedge a loop.

### One PR, one seat — and who frees it

Per-seat capacity answers *how many PRs a seat may hold*. A second, stricter rule sits under it:
**one PR is held by one seat at a time.** A review must never run against a PR the fixer is mid-fix
on, and a fix must not start on a PR under review — `held_by_other()` is that claim, and a gate that
finds the other seat holding the PR queues itself instead of starting.

Which raises the question the loop cannot answer directly: *when is a seat done with a PR?* The loop
sees events, not process exits. So it uses the events that already mean the turn is over:

| signal | what it ends |
|---|---|
| `review_requested` from the fixer | the fixer's turn — the push-then-ask handoff |
| a verdict at the current head (approve **or** changes-requested) | the reviewer's turn |
| `ttl_min` | a run that died without either. The backstop, not the mechanism. |

That is also why **order matters inside each gate**: the gate frees the other seat *before* it claims
its own. Claim first and the two gates deadlock against each other — the reviewer waits for the fixer
to hand off, the fixer waits for the reviewer to hand off. Both gates therefore look like:

```
observe the peer's handoff → free the peer → claim own slot (queue if the peer still holds it)
```

An approval is the case worth naming: the fixer has nothing to do on an approved PR, but the
reviewer's slot is still held, and a slot that leaks for `ttl_min` on a busy repo is the difference
between ten review slots and nine. So the approval path frees the reviewer and drains the queue
without waking anyone.

## Isolation (parallel without the shared-clone bug)

Isolation is not a performance feature; it is the difference between a parallel loop and a loop that
publishes **wrong verdicts**. A review mutates its checkout — worktrees, `checkout --detach`,
stashes, `merge --abort` — so two runs in one clone delete each other's working trees mid-review.

Per PR **and per seat**, the gate prepares:

```
{state_dir}/artifacts/<PR>/
├── reviewer/   own clone, target/, tmp/ — what the review runs in
└── fixer/      own clone, target/, tmp/ — what the fix runs in
```

Per seat, not merely per PR, because the two seats genuinely overlap on one PR: a fix run is only
released when the reviewer's gate *observes* the push, so the fixer's process may still be winding
down while the review starts. One workspace per PR would have them sharing a checkout — the exact
corruption isolation exists to prevent.

Each clone is its own `git clone --local` (hardlinked object store), its own `origin`, its own
credential helper, and its own detached checkout at the head under review.

Cheap by construction: the clone is `git clone --local`, which hardlinks the object store, and git
data is tiny next to build output (the loop this was built for: **25 MB of git, 177 GB of build
artifacts**). The root is the same path the cleanup already deletes at merge, so isolation adds no
new garbage and needs no new lifecycle.

The token never lands in the tree: the clone's `origin` is the plain GitHub URL and
`credential.helper` reads the PAT out of its 0600 file at use time, so the artifacts directory
holds a **path**, not a key — and the fixer can still push from its own clone.

When a sandbox cannot be built, `ensure` returns `None` and the gate decides honestly: at
`concurrency: 1` the run proceeds against the shared clone (nothing else is running), at
`concurrency: 2+` it is **queued**, because starting it beside another run is exactly the
wrong-verdict case.

## Why the reviewer is woken by a request, not by a push

GitHub clears a pending review request the moment a review is submitted. So the loop continues for
exactly one reason: the fixer re-requests review after pushing. That makes the request the only
honest trigger:

- `opened` / `ready_for_review` / `reopened` — a new PR needs a first look (a request will not
  exist yet);
- `review_requested` — the fixer asked, and only when the request names *this* seat and comes from
  the fixer side;
- `synchronize` — **never**. Intermediate pushes cost nothing, and a reviewer that wakes on every
  push reviews half-finished work and burns the budget.

## Why the budget is counted in verdicts

Wall-clock budgets cannot tell "the loop is thinking" from "the loop is stuck". A verdict is a
thing that either exists on the PR or does not, so the count comes from the reviews themselves —
never from a local counter that can drift from reality. `cap = 3` means three verdicts and two fix
turns; the third `changes_requested` escalates.

## Escalation

When the cap is spent, the gate writes a breach marker (keyed by PR *and* head) and POSTs at the
adjudicator route — a route bound to the adjudicator's own profile, so the ruling happens as the
adjudicator and not as the seat that just went quiet. One wake per head: a PR parked at sha X stays
parked, so repeated events cannot spawn a ruling each time.

The adjudicator is told to read both positions, rule with a reason, post the ruling on the PR, and
**not** merge or push. The operator is the veto, not the reviewer — overriding a ruling should cost
one message, not a re-read of the whole thread.

## The watchdog's four shapes

Read from GitHub every 15 minutes. The first successful armed sweep snapshots existing PR heads
as history. Each subsequent SHA change gets a durable first-observed timestamp in `watchdog.json`;
the reviewer grace starts then, not at the commit's authored/committed date. A first-seen PR created
since arming also gets a clock; an old PR first seen later is conservatively baseline-only until its
head changes. Existing `watchdog.json` files without `heads` establish this conservative snapshot on
their next successful sweep. Unreadable PR listings neither advance the snapshot nor drain queues;
unreadable review lists do not produce verdict-dependent alerts. An old PR whose head changed before
the first successful observation cannot be distinguished from an unchanged old PR without an event
record, so it remains baseline-only until the next observed SHA change. An observation survives a
brief omission from the listing, a draft transition, or close/reopen at the same SHA. Absent heads
expire after 30 days since last seen; a reappearing old head after expiry is baseline-only, never
falsely treated as a recent push. Corrupt observation clocks are also treated as unknown. The four
shapes are:

1. reviewer never posted a verdict for a quiet head;
2. fixer never pushed after a verdict;
3. a PR parked awaiting adjudication;
4. the cap is spent at this head with no approval and no escalation marker — i.e. *the gate did not
   fire*, which is the failure the loop cannot see about itself.

It also reports stuck seats (a lock older than a run could plausibly live, a request waiting past
the grace period) and drains the queue when it can be proven safe: the seat is free, the PR is still
open, the head has not moved, and the verdict has not already landed. A drain that fails these
checks drops the entry instead of firing — a stale queue entry must die quietly, not start a run
against a head that moved on.

## Cleanup

A finished PR gives its disk back: worktrees, build directories, probe logs, plus the loop's own
state (locks, queue, in-flight marks, breach marker). Rails, because this deletes real directories:

- only paths inside the loop's configured `roots` are considered;
- a worktree with a **branch** checked out is never touched — that is somebody's working tree, not
  a review artifact (only detached checkouts are cleaned);
- evidence patterns (`phase3`, `evidence`, `soak`, `release-verification`) are skipped: regenerable
  build output is not the same thing as a receipt;
- a PR that is still open is refused; `--force` is required to override that;
- the clone itself is out of scope by construction.

## What a plugin can and cannot own

This ships as a general Hermes plugin, which means it can register a CLI command, tools, hooks,
middleware and skills — and it **cannot** own webhook routes, GitHub hooks or cron jobs. That is
why `init` writes those through the operator-visible config surfaces instead of inventing a second
registry. The consequence is good: nothing about the install is hidden, `uninstall` is the inverse
of `init`, and a broken loop can always be inspected with the tools the gateway already has.
