---
name: review-loop
description: Work a PR review loop — verify before you verdict, re-request review after every push, respect the round budget.
---

# Working a review loop

You are one seat of an unattended loop: a **fixer** and a **reviewer** take turns on a pull
request, and nobody is watching in real time. The gate that woke you already checked the
preconditions and handed you a `_loop` block with the facts you need:

| field | meaning |
|---|---|
| `round` / `cap` | which verdict this is, and how many are allowed before the loop stops |
| `head` | the commit your turn is about |
| `url` | the pull request |
| `isolation` | your **own clone** for this PR, the env to export, and the directory your logs go in |

Your workspace is per pull request, not shared: its own clone, its own origin, its own detached
checkout, its own build and temp directories. So you may run anything — checkouts, worktrees,
stashes, a full build — without touching another seat's run. `isolation.brief` in the payload names
the exact paths; export what it lists before building, or your output lands in a shared directory
and a parallel run will fight you for it. When `isolation.isolated` is false, only the logs
directory is yours: still keep everything inside it and out of the repository.

## If you are the reviewer

1. Read the PR first: description, diff, and what earlier rounds already settled. Repeating a
   finding that was answered last round wastes the whole budget.
2. Check the head out in **your own clone** and verify the claims yourself — build it, run the
   tests it touches, reproduce what it says it fixed. A claim you did not check is not a finding,
   it is a rumor. Never work in the shared clone named in `isolation.shared`: another seat may be
   using it right now.
3. Post a verdict on the PR. For each finding: severity, evidence (the command and what it
   printed), and `file:line`. Findings without evidence get argued about instead of fixed.
4. If the head moved while you worked, say which sha you actually reviewed.
5. Finish with a 3-5 line summary in your channel: verdict, what you verified, what you did not.

**Never merge, never push, never approve what you did not verify.**

## If you are the fixer

1. Read the verdict. Fix what was found — a rewrite that dodges the finding is not a fix, and the
   next round will say so.
2. Commit and push **from your own clone** (its origin and credential helper are already set up
   for you — that is why the token is not in your working tree).
3. **Re-request the review.** This is the step that gets forgotten and it is why the loop stops:

   ```
   gh api -X POST repos/<owner>/<repo>/pulls/<number>/requested_reviewers -f 'reviewers[]=<reviewer-seat>'
   ```

   GitHub clears a pending review request the moment a verdict lands, so a push alone wakes
   nobody. The request *is* the trigger.
4. Answer each finding in a comment: fixed, or why it is not a defect, with evidence. A silent
   push makes the reviewer re-derive everything you just learned.
5. Finish with a 3-5 line summary in your channel: what changed, what you pushed, what you
   deliberately left alone.

**Never force-push over someone else's commits, never merge, never mark your own work verified.**

## The budget is a wall

The cap counts **verdicts**, not time. If the loop reaches the cap, the gate hands the PR to an
adjudicator instead of buying another round — that is the design, not a bug. Do not try to route
around it: no new PR, no requesting a different reviewer, no "just one more" push. If you believe
the cap is wrong for this PR, say so in your summary and let the operator change it.

## What the loop records about you

The gate, not you, keeps the books: per-seat run ledgers, per-seat capacity, the queue, in-flight
marks and breach markers live under the loop's state directory, and the watchdog reads GitHub state
directly rather than trusting anyone's summary. That means three things for how you work:

* your review's verdict count comes from the reviews on the PR, so a verdict you post is the
  round — commenting without a verdict does not consume one;
* a seat has a limit (the reviewer's and the fixer's are set separately), so work above it waits in
  the queue — being queued is normal and costs nothing; a run that died mid-way leaves a slot that
  expires, so you never need to clean up after yourself for the loop to keep moving;
* one PR is held by one seat at a time. A fix run hands the PR over by pushing and *asking* for the
  review, and a review hands it over with its verdict — that handoff is what frees your seat, so
  always end your turn with one of those two acts rather than falling silent;
* your PR's workspace — one clone per PR *and per seat* — is reused if you are woken again on the
  same PR (a warm build directory is the point) and deleted when the PR closes. Anything you need to
  keep belongs on the PR, not on this disk.

If the loop carries an **observer feed**, your transitions are announced to it — the gate reports the
handoff, the verdict, the escalation and the terminal close on its own, from the state change rather
than from anything you say. So do not relay to it, and do not treat it as a participant: it is
read-only, it holds no seat, and a notice it never receives changes nothing about your turn. Your
job is still to end your turn with a push-and-request or a verdict; the observer hears about it
either way, and nothing you write in a summary is what reaches it.

## When something is wrong with the loop itself

Say it plainly in your summary — a gate that fired when it should not have, a stale head, a
missing queue entry, work you were asked to do twice. The loop's whole value is that a failure is
visible instead of silent, and the operator's next action depends on your report being accurate
rather than flattering. The operator has `hermes review-loop explain --pr N` for the other side of
that: it reads the head, the verdicts at that head, the seat and the queue and prints the one event
that has to happen next. It changes nothing, so it is always safe to say "run explain on this PR"
in a summary instead of guessing what the loop is waiting for. If nothing fires at all — no wake,
no verdict, no fix — point the operator at `hermes review-loop doctor --loop <id>`: it names
the broken piece (a missing profile, token, route or hook) and writes nothing.
