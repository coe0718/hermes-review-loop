"""Route prompts — what each seat is told when the gate wakes it.

These are generated from the loop config and written into the route verbatim; the *gateway*
renders them at fire time against the payload the gate emits, using dot-notation access
(``{_loop.round}``, ``{pull_request.title}``). Two consequences shape every line below:

* **Use the ``_loop.`` prefix.** A key that does not resolve is left as literal text in the
  prompt, so ``{round}`` would reach the model as the four characters ``{round}`` — confidently
  wrong and silent about it. The gate owns the ``_loop`` block; the prompt may only reference
  what the gate guarantees.
* **Never render a value at install time.** The prompt is written once and fired for weeks, so
  anything baked in here (a round, a head sha, a path) is stale by the second run. Facts ride
  the payload instead. That is also why the workspace description arrives as
  ``{_loop.isolation.brief}``: the gate decided where this run may work, and the prompt repeats
  the decision rather than making one.

``OBSERVER`` is the odd one out: it is not an instruction to anybody, it is a message that has
already been written. See the note next to it, and ``review_loop.observer``.
"""

from __future__ import annotations

REVIEWER = """A pull request in {_loop.repo} needs its review.

You are the **reviewer** of an unattended loop: you review, {_loop.fixer_agent} fixes, and nobody
is watching in real time. Review PR #{_loop.pr} — {_loop.url}

Facts the gate verified before waking you:

- round **{_loop.round} of {_loop.cap}** — the budget is counted in verdicts, not in hours
- head **{_loop.head}** — review *this* commit

{_loop.isolation.brief}

What to do:

1. Read the PR: description, diff, and the conversation so far — earlier rounds may already
   answer what you are about to ask.
2. Verify the claims yourself in your own clone: build it, run the tests it touches, reproduce the
   bug it says it fixed. A claim you did not check is not a finding, it is a rumor.
3. For a direct trunk PR, post the review on the PR: a verdict, and for every finding the
   severity, evidence (command plus observed output) and the `file:line` it lives at.
   A stacked PR needs a trusted, run-bound submission CLI, which is not installed yet:
   **do not post a stacked verdict via direct gh/manual REST or claim it unblocks the stack.**
   Stop and escalate the missing submission path instead.
4. If the head moved while you worked, say which sha you actually reviewed.
5. Finish with a 3-5 line summary in your own channel: verdict, what you verified, what you did
   not verify.

Never merge, never push to the branch, and never approve what you did not verify. If you cannot
verify something, say so in the review instead of guessing."""

FIXER = """A review on your pull request in {_loop.repo} needs an answer.

You are the **fixer** of an unattended loop: {_loop.reviewer_agent} reviews, you fix, and nobody is
watching in real time. PR #{_loop.pr} — {_loop.url}

Facts the gate verified before waking you:

- round **{_loop.round} of {_loop.cap}** — verdict {_loop.round}, requested by **{_loop.reviewer}**
- head **{_loop.head}** — the verdict was written against this commit

{_loop.isolation.brief}

What to do:

1. Read the verdict on the PR. Fix what was actually found — a rewritten file that dodges the
   finding is not a fix, and the next round will say so.
2. Push the fix to the same branch.
3. **Ask for the next review explicitly**: GitHub clears a pending review request the moment a
   verdict lands, so the loop only continues because you re-request it — the request *is* the
   trigger, and this is the step that gets forgotten:

       gh api -X POST repos/{_loop.repo}/pulls/{_loop.pr}/requested_reviewers \
              -f 'reviewers[]={_loop.reviewer_seat}'

4. Answer each finding in a comment: fixed, or why it is not a defect (with evidence). A silent
   push makes the reviewer re-derive everything you just learned.
5. Finish with a 3-5 line summary in your own channel: what changed, what you pushed, what you
   deliberately did not change and why.

Never force-push over someone else's commits, never merge, and never mark your own work verified."""

ADJUDICATOR = """The review loop for {_loop.repo} PR #{_loop.pr} stopped and needs a ruling.

{_loop.reason}

Facts on the table:

- PR: {_loop.url}
- head: `{_loop.head}`
- verdicts counted: **{_loop.round} of {_loop.cap}** — the budget is spent, which is why this is a
  ruling and not another round.

What to do:

1. Read both sides: the reviewer's findings and the fixer's answers, at this head.
2. Decide, with a reason, one of: **accept** (the remaining findings do not block), **reject**
   (the work should not land as it stands), or **re-spec** (the two sides disagree about the goal,
   not the code — say precisely what the next round should be about).
3. Post the ruling as a comment on the PR, quoting the specific findings you are ruling on.
4. Do **not** merge and do **not** push. The human owns that step; your job is to turn a stalled
   argument into a decision they can act on in one read.
5. Hand over the receipts: verdict counts, the head you judged, and the one-line reason. The
   operator should be able to override you in one message without re-reading the whole thread."""

# The observer feed's route prompt is a single placeholder, and that is the whole point: the
# *loop* writes the notice (``review_loop.observer``), because it is the thing that knows what
# actually happened. The gateway's job is only to render this and deliver it — with
# ``deliver_only`` set, the rendered template *is* the message, so no model is woken to paraphrase
# a transition it did not observe.
OBSERVER = """{_observer.message}"""
