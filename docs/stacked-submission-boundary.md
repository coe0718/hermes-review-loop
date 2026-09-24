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
stacked reviewer/fixer gates remain disabled. The trusted host broker from #16
must supply the run attestation and receipt path before this boundary can ship.

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
