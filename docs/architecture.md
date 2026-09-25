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

operator ──▶ review-loop explain ──▶ reads GitHub + the state files, writes neither
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

### Who serves a seat, and why the route is part of it

The profile and the login are the seat's **identity**, and they are per loop like everything else:
the same install can run Vex/Drey on one repository and a different pair on another. The plugin
settings carry a per-profile *default* for them (`reviewer_profile`, `fixer_profile`,
`reviewer_login`, `fixer_login`, `adjudicator_profile`); a new loop starts from it, and an existing
loop only moves when `apply --loop <id>` pushes it. Blank means *not set here* — never "forget what
this loop uses".

A route is bound to a profile by its URL: `/webhooks/<name>` is the launch profile, and
`/p/<profile>/webhooks/<name>` is every other one. So an identity change is **also a route change**,
and `apply` treats it as one staged operation: validate (the profile exists, the login is in the
allowlist, the two seats share no profile/login/token file, every token reference resolves, the route
is not another loop's and still runs this role's gate script) → write the loop config → rebind
exactly the routes whose profile moved → read the registry back and report. Only the routes this loop
owns are touched, and a rebind keeps the route's own secret, so unrelated routes and their secrets
survive.

Two consequences worth stating plainly:

* **A seat in flight is not rewritten underneath itself.** `apply` refuses an identity change while
  the seat it would move has a live run, and `--while-busy` is the explicit override — the run that
  is already out finishes under the identity it started with, and `status` is where that shows.
* **The config and the route can disagree, so `status` prints both.** `reviewer widgets-review → vex
  (ok)` is the check; `MISMATCH — hermes review-loop apply --loop widgets` is the loop that would
  run as the old agent while every config file claims otherwise.

Existence, allowlist and credential checks run for the roles an operation *writes*, not for every
role in the file: a loop created before the checks existed must not start failing because someone
tuned its `cap`. The combination checks (distinctness, credentials, route ownership) always run,
because the unsafe shape is the combination.

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

When the cap is spent, the gate verifies the live PR head, writes a durable `delivery-pending`
breach marker, and POSTs at the adjudicator route under a cross-process lock. A successful 2xx
promotes it to `awaiting-adjudication`; a failed delivery remains pending for a later event or
watchdog sweep to retry. The route is bound to the adjudicator's own profile, not the seat that
went quiet. One accepted wake per head: repeated events cannot spawn a second ruling, and a late
event for an older head cannot replace the current marker. After an ambiguous transport timeout,
a retry may deliver another POST, but the adjudicator gate atomically claims only one run per head.

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

It also retries pending adjudicator deliveries for current heads with a verified spent cap and
reports stuck seats (a lock older than a run could plausibly live, a request waiting past
the grace period) and drains the queue when it can be proven safe: the seat is free, the PR is still
open, the head has not moved, and the verdict has not already landed. A drain that fails these
checks drops the entry instead of firing — a stale queue entry must die quietly, not start a run
against a head that moved on.

The "is this loop armed at all?" question is answered by `gate.hooks_read`, which the watchdog and
`explain` share: both seat routes must exist as active repo hooks. An unreadable hook list is
**not** "paused" — a token without `admin:repo_hook` cannot see hooks that may well be active — so
the watchdog stays silent there and `explain` prints "unknown" rather than guessing in either
direction.

## Explain — why is this PR not moving?

A loop that stopped being driven looks exactly like a loop with nothing to do, and no single file
answers "why". Half the answer is in GitHub (the head, the verdicts *at that head*, whether an
approval exists) and half is on disk (who holds the PR, what is queued, what is marked in flight,
what the watchdog last saw). `hermes review-loop explain --pr N` reads both and prints one report.

It is **the gates' own logic, walked differently**, and that is the design constraint that matters:

| | a gate | `explain` |
|---|---|---|
| input | a webhook payload | GitHub + the state directory |
| guards | the same guards, in the same order | the same guards, in the same order |
| at a guard | stops — `silence()`, and the operator sees only that nothing happened | reports all of them, and names the one that is holding the PR |
| output | `[SILENT]`, or a payload with `_loop` | labelled facts, `blocked:` reasons, one `next:` event |
| effect | a claim, a queue entry, a mark, a POST | none |

The predicates are shared, not copied: `verdicts` (the round count), `reviews_at_head` /
`reviewed_at_head` / `changes_at_head` / `approved_at_head`, `seat_key`, `seat_capacity`,
`breach_delivery_status`, the seat ledgers, the queue and `hooks_read`. Re-deriving any of them would be
the drift this report exists to rule out — an operator who is told "awaiting the fixer" while the
fixer gate would in fact have fired has learned nothing.

The guards report in the gates' own order, so the first one that names an action *is* the guard the
loop would stop at:

1. can GitHub be read at all (a failed read is unknown, never "closed");
2. is the PR closed or merged (the loop is over; the closed path reclaims the disk);
3. is the loop armed — a paused loop can be woken by nothing;
4. is this a PR the reviewer gate serves at all (draft, wrong base, author is not a fixer);
5. the budget: an approval ends the loop; a spent cap with pending delivery needs retry before any
   ruling can be expected, while an acknowledged marker means adjudication;
6. who holds the PR right now (one PR, one seat) — only a lock for the live head can imply its next
   verdict or push; an old-head lock must be released or expire;
7. is it queued for this exact head (a stale queued SHA is dropped, never retargeted; a current
   queue waits for capacity); even without a queue entry, locks held by other PRs can fill a seat;
8. is this exact head marked in flight (a run is already out for it);
9. a verdict at this head with no fix run out (retry the fixer gate event, not an absent fixer's push);
10. a non-verdict review at this head (a comment consumes no round and does not suppress a new
    review request in the reviewer gate);
11. nothing at this head: the fixer's request is what wakes the reviewer, and GitHub clears it when
    a verdict lands, so a missing request is the classic silent stall. A pending request *without*
    a run needs its gate event re-delivered, not a verdict from a reviewer who never started.

Every report ends in exactly one `next:` line: a reviewer verdict, a review request, a retry of the
fixer event, a fixer push plus request when a run exists, a released slot, an adjudication, a re-arm,
a read retry, or nothing at all. Timestamps
carry their source (the read itself, the verdict's `submitted_at`, or the state mark's own epoch),
and anything that could not be read is printed as unknown with the reason.

**Zero mutation is a property, not a promise.** `explain` does not call `st.active()` — which prunes
expired locks *and writes them back* — but `st.live_locks()`, its read-only twin; it reads inflight
marks with `inflight_at`, and never touches the queue except to count it. The suite runs it twice
with an expired lock, a queue entry and a breach marker on disk, and asserts every file (both loops'
configs, every state file, the route registry and the stub world) has the same SHA-256 hash and that
no webhook was fired.

## The observer feed (read-only, never a seat)

The loop is unattended, which is the point — but "nobody is watching" and "nothing is visible" are
different things. A loop may carry one **observer**: a chat destination that receives a short notice
each time the loop changes state, without joining the loop.

A notice is emitted *at* the transition, by whoever made it — the reviewer gate on a handoff, the
fixer gate on a verdict, `breach()` when the cap is spent, the reviewer gate on a close, and the
watchdog when it decides a stall is worth reporting. Nothing is inferred from an agent's summary,
and none of it can influence a seat: the observer path runs after the state change, never in front
of it; escalation is announced after the durable pending marker but before the adjudicator
POST, with delivery explicitly pending rather than reported as complete.

Delivery rides the same rails as a seat wake — a signed POST (`X-Hub-Signature-256`) at a route
bound to the observer's profile. The difference is that the route is `deliver_only` and its prompt
is the notice itself: the loop wrote the message before the route fired, so no agent is woken and no
turn is taken.

The delivery ledger (`observations.json`) is what makes the feed idempotent and honest:

```
entry key = loop : PR : head : event : verdict-or-round identity
status    = delivered | queued | pending | failed | uncertain
```

- A second webhook about the same transition finds the entry and stops, however many times GitHub
  redelivers or the sweep runs — the key is the fact, not the delivery attempt.
- A definite pre-POST failure is retried by the watchdog, up to `MAX_ATTEMPTS`.
  A timeout, 5xx, or stale pending claim may already have reached the receiver: it becomes
  `uncertain` and is never automatically replayed. Older `failed` receipts without explicit
  pre-POST evidence are also quarantined. Reconcile them manually before changing routes.
- With `digest_min` above zero, new entries park as `queued` and the sweep sends one message listing
  them. The batch is itself an entry carrying its members, so a digest and a single notice can never
  both claim the same transition.
- Muting, filtering by event, or having no feed short-circuits before the ledger is touched: a
  disabled observer is indistinguishable from no observer.

The feed holds no lock and carries no secret: the payload is the loop id, PR number and URL, head,
event, outcome, next turn and a one-line summary. A private PR URL reaches only the profile the
operator configured — that is the one boundary the observer is allowed to cross. A loop without
an explicitly configured webhook host cannot send a private link by inheriting the mutable route
registry's host.

## Cleanup

A finished PR gives its disk back: worktrees, build directories, probe logs, plus the loop's own
state (locks, queue, in-flight marks, breach marker). Rails, because this deletes real directories:

- only paths inside non-symlink configured `roots` (or the loop's artifacts directory) are
  considered; a root's PR-like name does not attribute every child to that PR, and a plain root
  child must name this loop's repository as well as the PR (`widgets-pr7-target`), because roots
  are shared between loops. `/`, the home directory and its ancestors are refused as roots;
- detached worktrees must be registered to this clone and inside an allowed root. Other Git
  checkouts, nested repositories, the clone and its contents are protected even if PR-named. The
  one exception is the loop's own `artifacts/<PR>/` under its real (non-symlink) state dir: the
  isolation clones inside it were made by the loop, so their `.git` does not protect them;
- a worktree with a **branch** checked out is never touched — that is somebody's working tree, not
  a review artifact (only detached checkouts are cleaned);
- evidence patterns (`phase3`, `evidence`, `soak`, `release-verification`) are skipped: regenerable
  build output is not the same thing as a receipt;
- cleanup requires a fresh GitHub lookup confirming the matching PR is closed; open, failed, and
  malformed lookups are refused. The standalone cleanup script's explicit `--force` is the
  operator-only override (the webhook and plugin CLI never pass it);
- the clone itself is out of scope by construction.

## Preflight: can this installation run?

`init` writes the config, the routes, the hooks and the cron job; it cannot check itself. So
`hermes review-loop doctor --loop <id>` walks the installation read-only and answers one question:
**can this loop wake a seat and post a verdict?** It is the installation-level counterpart of the
watchdog — the watchdog asks "is this PR stalled?", the preflight asks "is this loop wired at all?"

| check | what it proves |
|---|---|
| `config` | the loop file is there and parses |
| `profile:reviewer` / `profile:fixer` | each seat's Hermes profile home exists (`~/.hermes/profiles/<name>`, or `~/.hermes` itself for `default`) |
| `credential:<seat>` | a nonempty token file is mapped for that seat's login through `gh.token_path`; profile `GH_TOKEN` alone is not used by the gates |
| `token:<login>` | every credential file named in the config exists, is non-empty, and is not readable by group or other users |
| `read_token` | the login the gates read GitHub as is one of those mappings |
| `route:<name>` | the gateway's registry holds the route, it wakes *this* seat's profile, it carries a secret and a prompt, it runs the right gate script for the right event, and it resolves to this loop's own gateway origin |
| `scripts` | the plugin's `watchdog.py`, both gates and `cleanup.py` are on disk |
| `cron:shim` | `~/.hermes/scripts/review-loop-watchdog.py` exists **and is pinned to the plugin install that is here now** — an upgrade that moves the directory leaves the scheduler running an old path |
| `cron:job` | the scheduler's own store holds this loop's watchdog job and it is not paused |
| `clone` | the clone exists, is a git checkout, and is not inside the loop's artifacts root — the cleanup deletes that whole tree |
| `state_dir` / `roots` | the loop can write its locks and queue there; every cleanup root is a directory |
| `gateway` | a TCP connect to the loop's webhook origin is accepted |
| `hook:<route>` | the repo hook posts at the route's URL, subscribes to that seat's event, and is active |

Four states, and the difference between the last two is the point:

* ✅ **verified** — checked, and correct;
* ❌ **absent** — not there at all;
* ❌ **mismatch** — there, but not what this loop needs: a route waking another profile, a hook on
  another gateway, a shim pinned to a stale plugin path, a world-readable PAT;
* ⚠️ **unknown** — could not be decided from here: a hooks read the token was not allowed to make
  (reading a repo's hooks needs `admin:repo_hook`), or a probe skipped with `--offline`.

**Unknown is never folded into absent.** "The API refused to tell me" and "there are no hooks" are
different claims, and printing the second when the first is true sends the operator hunting for a
hook that exists. Failures exit 1 and each one carries the single command that fixes it;
`--strict` makes an `unknown` a failure too, for installs that require a fully proved preflight.

Read-only is a hard rule here, twice over. The preflight writes no config, route, state or hook —
a check that repairs what it looks at cannot be trusted to describe what is wrong — and it never
fires a route, because a synthetic POST at a seat's route **is** a real agent run with a real
budget. The entire network side of the preflight is a TCP connect to the gateway and, when the
token is permitted, a read of the repo's hooks. There is deliberately no test-fire mode: the way
to test a route is to hand GitHub a real event.

## What a plugin can and cannot own

This ships as a general Hermes plugin, which means it can register a CLI command, tools, hooks,
middleware and skills — and it **cannot** own webhook routes, GitHub hooks or cron jobs. That is
why `init` writes those through the operator-visible config surfaces instead of inventing a second
registry. The consequence is good: nothing about the install is hidden, `uninstall` is the inverse
of `init`, and a broken loop can always be inspected with the tools the gateway already has.
