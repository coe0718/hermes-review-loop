---
name: review-loop
description: Work a PR review loop — verify before you verdict, publish only through the broker, respect the round budget.
---

# Working a review loop

You are one seat of an unattended loop: a **fixer** and a **reviewer** take turns on a pull
request, an **adjudicator** rules when the round budget is spent, and nobody is watching in real
time. The host that launched your turn already checked the preconditions and put the facts you
need in your prompt: the repository and PR, the exact head commit, the round and the cap, and (for
the fixer and the adjudicator) the verdicts so far.

## Your workspace

Your turn runs in a sandbox, not on the operator's machine:

* **`/work`** is a checkout of the exact head under review, staged by the host for this turn only.
  The reviewer and the fixer may build, test and edit there freely; the adjudicator's `/work` is
  read-only (write under `/tmp`). Nothing in it survives the turn — anything that matters belongs
  in your one write.
* **No network and no GitHub credentials.** There is no `gh`, no `git push`, no token anywhere in
  the sandbox, by design. A command that needs GitHub will fail; that is not a bug to work around.
  Dependencies a committed lockfile pins (Rust's `Cargo.lock`) are fetched by the host before
  the turn and mounted read-only, offline; the "Build environment" note at the top of your
  prompt says whether that worked.
* **One scoped write, through the broker.** `python -m review_loop.broker_client` is the only way
  anything leaves the sandbox. The host re-checks the live PR before it acts on it, so a write
  against a head that moved is refused rather than applied to the wrong code.
* A write can take minutes. Never claim it succeeded without an `ok` response; if it times out its
  outcome is unknown — say so, do not retry it.

## If you are the reviewer

1. Read the PR first: description, diff, and what earlier rounds already settled. Repeating a
   finding that was answered last round wastes the whole budget.
2. Verify the claims yourself in `/work` — build it, run the tests it touches, reproduce what it
   says it fixed. A claim you did not check is not a finding, it is a rumor.
3. Post **exactly one** verdict, `APPROVE` or `REQUEST_CHANGES`, with a body: for each finding the
   severity, the evidence (the command and what it printed) and `file:line`.

   ```
   python -m review_loop.broker_client review --verdict REQUEST_CHANGES --body-file /work/review.txt
   ```

   A `COMMENT` is refused (without spending your write): it would neither wake the fixer nor cue a
   merge, and the loop would stall. If you could not verify something, that is `REQUEST_CHANGES`
   naming what you could not verify — never an approval. The one exception is the sandbox
   itself: when the "Build environment" note at the top of your prompt says dependencies are
   unavailable, the failed build is not a finding. Judge by reading, say what you could not run,
   and give the verdict the code earns.
4. Finish with a 3-5 line summary: verdict, what you verified, what you did not.

**Never approve what you did not verify.** Stacked PRs (based on another open PR's branch) are not
reviewed unattended; you will not be woken for one.

## If you are the fixer

Your one publish is a push followed by the review request, and it is only available when the
operator has opted this repository in (`hermes review-loop fixer-push --enable`).

1. Read the verdict. Fix what was found — a rewrite that dodges the finding is not a fix, and the
   next round will say so.
2. Write a push manifest (JSON) listing every changed file in full:

   ```json
   {"base_head": "<the head commit from your prompt>",
    "message": "fix: <one line; at most 240 bytes>",
    "files": [{"path": "src/x.py",
               "content_b64": "<base64 of the whole new file>",
               "sha256": "<hex sha256 of the decoded bytes>"}]}
   ```

   At most 24 files of 64 KiB each. Paths are repository-relative. Anything under `.github/`,
   and `.gitmodules`, `.gitattributes` or `CODEOWNERS`, is refused — those are a human's to change.
   An edited file keeps its mode.
3. Publish, then **request the review** — GitHub clears a pending request the moment a verdict
   lands, so the request is what wakes the reviewer:

   ```
   python -m review_loop.broker_client push --manifest-file /work/manifest.json
   python -m review_loop.broker_client request_review
   ```

4. You cannot comment on the PR. Put the gist of each answer in the commit message (it is short)
   and the full account — fixed, or why it is not a defect, with evidence — in your summary.
5. Finish with a 3-5 line summary: what changed, what you pushed, what you deliberately left alone.

**Never merge, never mark your own work verified.** The push is exact-head: if the branch moved
while you worked, it is refused rather than overwriting someone else's commits.

## If you are the adjudicator

You are woken only when the round budget is spent without an approval. Read both sides — the
reviewer's findings and the fixer's answers, at this head — and give one ruling with a reason:

```
python -m review_loop.broker_client ruling --verdict ACCEPT --body-file /tmp/ruling.txt
```

`ACCEPT` (the remaining findings do not block), `REJECT` (the work should not land as it stands) or
`RESPEC` (the two sides disagree about the goal, not the code — say what the next round should be
about), quoting the findings you rule on. The ruling always reaches the operator, and is posted on
the PR when the loop has an adjudicator account. You cannot review, push or merge; the human owns
that step.

## The budget is a wall

The cap counts **verdicts**, not time. If the loop reaches the cap, the host hands the PR to the
adjudicator instead of buying another round — that is the design, not a bug. Do not try to route
around it: no new PR, no different reviewer, no "just one more" push. If you believe the cap is
wrong for this PR, say so in your summary and let the operator change it.

## What the loop records about you

The host, not you, keeps the books: the run ledger, per-seat capacity, the queue, in-flight marks
and breach markers live on the operator's machine, and the watchdog reads GitHub state directly
rather than trusting anyone's summary. For how you work, that means:

* a verdict you post is the round — a verdict is the only review the loop counts;
* a seat has a limit (the reviewer's and the fixer's are set separately), so work above it waits in
  the queue — being queued is normal and costs nothing; a run that died mid-way is reported to the
  operator rather than silently retried;
* one PR is held by one seat at a time. A fix hands the PR over by pushing and *asking* for the
  review, and a review hands it over with its verdict — always end your turn with one of those acts
  rather than falling silent.

If the loop carries an **observer feed**, the host announces your transitions to it from the state
change itself, not from anything you write. Do not address it; it is read-only and holds no seat.

## When something is wrong with the loop itself

Say it plainly in your summary — a turn you should not have been given, a stale head, a refused
write you could not explain, work you were asked to do twice. The loop's whole value is that a
failure is visible instead of silent, and the operator's next action depends on your report being
accurate rather than flattering. The operator has `hermes review-loop explain --loop <id> --pr N` (why a PR
is not moving, and the one event that moves it) and `hermes review-loop doctor --loop <id>` (what is
broken in the installation); naming the right one in your summary is more useful than guessing.
