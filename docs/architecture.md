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

Two rules keep the seats honest:

- **a seat is a person-sized resource** — one run at a time, via a lock in `locks.json` that
  expires after `ttl_min`, with a queue for anything that arrives while the seat is busy;
- **a seat's turn ends when the other seat observes its artifact** — the reviewer gate releases the
  fixer's lock when a push-and-request arrives; the fixer gate releases the reviewer's lock when a
  verdict lands. Only for the *same* PR: releasing another PR's lock is how you get two runs in one
  clone, which is the failure this whole design is built to avoid.

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

Read from GitHub every 15 minutes, against the heads that postdate the moment the loop was first
seen armed (`armed_since`, which is what keeps a loop's history out of its alerts):

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
