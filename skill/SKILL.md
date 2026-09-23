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
| `artifacts` | the only directory you may create files in |

## If you are the reviewer

1. Read the PR first: description, diff, and what earlier rounds already settled. Repeating a
   finding that was answered last round wastes the whole budget.
2. Check the head out **under `{artifacts}`** and verify the claims yourself — build it, run the
   tests it touches, reproduce what it says it fixed. A claim you did not check is not a finding,
   it is a rumor. Never work inside the main clone: another seat may be using it.
3. Post a verdict on the PR. For each finding: severity, evidence (the command and what it
   printed), and `file:line`. Findings without evidence get argued about instead of fixed.
4. If the head moved while you worked, say which sha you actually reviewed.
5. Finish with a 3-5 line summary in your channel: verdict, what you verified, what you did not.

**Never merge, never push, never approve what you did not verify.**

## If you are the fixer

1. Read the verdict. Fix what was found — a rewrite that dodges the finding is not a fix, and the
   next round will say so.
2. Push to the same branch.
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

The gate, not you, keeps the books: seat locks, the queue, in-flight marks and breach markers
live under the loop's state directory, and the watchdog reads GitHub state directly rather than
trusting anyone's summary. That means two things for how you work:

* your review's verdict count comes from the reviews on the PR, so a verdict you post is the
  round — commenting without a verdict does not consume one;
* a run that died mid-way leaves a lock that expires, so you never need to clean up after
  yourself for the loop to keep moving.

## When something is wrong with the loop itself

Say it plainly in your summary — a gate that fired when it should not have, a stale head, a
missing queue entry, work you were asked to do twice. The loop's whole value is that a failure is
visible instead of silent, and the operator's next action depends on your report being accurate
rather than flattering.
