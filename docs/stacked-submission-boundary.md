# Stacked reviewer submission boundary (not enabled)

`gate_reviewer.py` executes as a webhook filter **before** the gateway creates the
reviewer session. Its `_loop` prompt block is caller-visible and forgeable. The
existing seat lock and in-flight marker only hold PR/head and timestamps; neither
names an actual gateway run, proves its principal, or pins the PR's base/parents.
A CLI accepting a supplied PR number, generation key, environment variable,
workspace path, or that `_loop` block would therefore let another process mint a
`trusted-submission-receipt`. A local token file readable by the reviewer process
has the same defect. No such CLI or writer is enabled here.

No submission module or callable writer is provided: a read-only comparison of
head/base/parents cannot establish a dispatched run, and an always-rejecting
`submit` function would misleadingly resemble a working integration.
`associated_review` rejects disk records including ones with a claimed
`trusted-submission-receipt` source. Direct trunk review behavior is unchanged;
stacked reviewer/fixer gates remain disabled: there is **no unattended review while a
PR is stacked on an unmerged parent**.

## After the parent merges: a fresh review situation

When an observed stacked child is retargeted to trunk with the same head (typically
because its parent merged), the host records a separate quarantine ledger
(`review_loop/transition.py`). Old review IDs (the `old_review_ids` baseline), rounds,
in-flight marks, queued requests and cap markers cannot authorize a new run, and
**nothing from before the boundary carries over** — not an approval, not a rejection,
not a round.

The transition then starts a fresh review situation automatically: it enqueues **one**
isolated reviewer turn at that head (`gate.enqueue_isolated`) with the turn key
`retarget:<from_base>:<boundary ms>`, derived only from facts frozen in the hold, so the
armed watchdog sweep (`reconcile_stacked`, and `retry_fresh_reviews` on later sweeps),
the `edited` webhook and a queued drain all name the same turn and dedup on the run
ledger's unique repo/PR/head/seat/turn index. Each enqueue re-reads the live PR; a draft
waits until it is ready. A failed enqueue (no private runtime, ledger or spawn error) is
kept on the hold as `fresh_review.state = retry`, reported by the sweep and by `explain`,
and retried on the next sweep. A same-head review request re-drives only that same turn.

In the new situation a review counts for the held head only when
`transition.effective_reviews` accepts it: its ID is **not** in the baseline **and** a
confirmed host receipt in the run ledger (`review_receipts` joined to `runs`) binds that
exact review ID, principal and verdict to an isolated *reviewer* run for this repo/PR/head
whose pinned generation names this head and the configured root base. Every decision
uses that one helper: the reviewer gate (already reviewed, round count, cap), the fixer
gate (a receipted post-boundary `CHANGES_REQUESTED` is a work order; a receipted
post-boundary `APPROVED` can produce the `you merge` cue, with every other base check
unchanged), the watchdog (stalls, drain, breach retry), the supervisor's claim and
launch checks for fixer and adjudicator turns, and `explain`. A review GitHub merely
lists after the boundary — a human's, or one posted by hand — has no receipt and stays
diagnostic only. An unreadable receipt ledger is unknown, never "no review".

A hold whose baseline could not be read (`old_review_ids` is null) is permanent at that
head: old and new reviews cannot be separated, so no fresh turn is enqueued and no
review counts; `explain` names that reason. A **later** child head pushed while
targeting trunk is an ordinary trunk PR. An unobserved stacked push followed by a
retarget is quarantined at its newly seen head: there is no proof that the push
occurred after the retarget. Operators may inspect and resolve manually but must not
treat this gate as merge authorization. A first observation *after* an unobserved
retarget cannot prove the old base; it cannot retrospectively classify legacy same-head
reviews. Operators must not infer their approval from the lack of a quarantine receipt.
Activation on an existing PR with no earlier stacked observation is a provenance gap:
the system cannot distinguish an unobserved retarget from a direct-main PR. Do not call
such a PR proven safe; onboarding must baseline it explicitly or require a subsequent
new head. An already-running agent is not cancelled by this ledger. The fixer approval
gate rechecks the live base ref/SHA at its final PR read before saying `you merge`; this
blocks a retarget or base advancement observed between its PR reads. That read is not
atomic with the later notice: a subsequent retarget, or a retarget and rollback entirely
between reads, remains possible. GitHub has no atomic conditional review POST; the
receipt's pre- and post-POST generation reads narrow, but cannot close, that window.

## Stacked (pre-merge) submission — not enabled

To enable submission, the gateway must expose a trusted attestation of the **actual
created reviewer run** and its dispatch generation (repo, PR, head, base ref/SHA,
parent chain, reviewer principal, and expiry), inaccessible to arbitrary reviewers
and webhook payloads. A privileged submission service must atomically claim this
run once, verify the reviewer seat token's `/user` identity, read live PR and
parent generation, POST the verdict with that token, read back the **exact** review
ID and verdict/head/principal, re-resolve the generation, and only then durably
associate the receipt. Reject a failed POST or malformed response. A timeout may
mean the POST landed: do not blindly retry or infer a review ID from chronology;
park for explicit reconciliation. A delayed response after retarget/closure must
not create a receipt. GitHub provides no atomic conditional review POST against a
PR base generation: even pre- and post-reads cannot prevent a transient retarget
and rollback between them. This residual race must be documented and treated
conservatively before enabling unattended stacked handoff.
