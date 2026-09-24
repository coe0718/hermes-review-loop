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
stacked reviewer/fixer gates remain disabled. After an observed stacked child is
retargeted to trunk with the same head, the host records a separate quarantine
ledger. Old review IDs, rounds, in-flight marks, queued requests and cap markers
cannot authorize a new run. An edited event observes the live PR without waking
the reviewer; a missed event is recovered by the armed watchdog sweep. An
unobserved stacked push followed by a retarget is also quarantined at its newly
seen head: there is no proof that the push occurred after the retarget. A **later**
child head pushed while targeting trunk plus explicit review request can use the
ordinary trunk path. A same-head request and a post-boundary review cannot prove
base generation or dispatch from GitHub's request/review metadata; they remain
held even when their review ID and timestamp look fresh. Operators may inspect
and resolve manually but must not treat this gate as merge authorization. A first
observation *after* an unobserved retarget cannot prove the old base; it cannot
retrospectively classify legacy same-head reviews. Operators must not infer their
approval from the lack of a quarantine receipt. Activation on an existing PR with
no earlier stacked observation is a provenance gap: the system cannot distinguish
an unobserved retarget from a direct-main PR. Do not call such a PR proven safe;
onboarding must baseline it explicitly or require a subsequent new head. An
already-running agent is not cancelled by this ledger. The fixer approval gate
rechecks the live base ref/SHA at its final PR read before saying `you merge`;
this blocks a retarget or base advancement observed between its PR reads. That
read is not atomic with the later notice: a subsequent retarget, or a retarget
and rollback entirely between reads, remains possible. GitHub has no atomic
conditional review POST. The trusted host broker from #16 must supply the run
attestation and receipt path before this boundary can ship.

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
