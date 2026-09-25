# Watching from your phone (the observer feed)

The operator-facing guide to the observer feed. Every `observer` key is in the
[configuration reference](configuration.md#the-observer-feed); how the feed stays out of the loop is
in [architecture](architecture.md#the-observer-feed-read-only-never-a-seat).

The seats drive each other in the historical design; on this branch, PR-facing turns are held
for isolated workers or queued fail-closed, and no gateway agent is dispatched. An observer
can still report a verified transition, but an `opened`, `handoff`, or `verdict` notice says the
next turn is **queued**, not that a reviewer or fixer has started. Give a loop an **observer** and
it sends one short notice per transition to a chat you choose — Telegram, Discord, wherever
that Hermes profile already talks:

```
🔧 [widgets] #7 `aaaaaaa` fix pushed · review requested (dev-fixer) · round 1/3 · next: reviewer queued
https://github.com/acme/widgets/pull/7
```

That is the whole payload: the loop, the PR, the head at the recorded transition, the seat and
event, the outcome, an optional next-turn hint, and a direct link to the PR. Delayed retries and
digests omit next-turn hints because the PR or review may have changed since the transition.
Never a token, an HMAC secret, a private diff, or a review body. Escalation reaches the observer
after the cap marker is durable and before the adjudicator turn is enqueued; it explicitly says
delivery is pending, not that the adjudicator received it. A `ruling` notice carries the verdict
and counts, never the adjudicator's reason text (that goes to the watchdog outbox and, when an
adjudicator identity is configured, the PR).

Turn it on at init, or add it to a loop that is already running:

```bash
hermes review-loop init --repo owner/name ... --observer-profile tuck   # one flag turns it on
hermes review-loop set --loop widgets --observer-profile tuck           # or add it later
hermes review-loop set --loop widgets --observer-events verdict,escalation,closed
hermes review-loop set --loop widgets --observer-digest-min 30          # batch instead of pinging
hermes review-loop set --loop widgets --observer-mute                   # quiet, config kept
hermes review-loop set --loop widgets --observer-disable                # stop/remove route; retain owed ledger and old destination binding
```

The flags write this block into the loop file, the only place the feed is configured:

```json
"observer": {
  "route": "widgets-observe",
  "profile": "tuck",
  "deliver": "telegram",
  "events": ["opened", "handoff", "verdict", "approved", "escalation", "ruling", "stall", "closed"],
  "digest_min": 30
}
```

`init` and `set` write the keys you asked for and nothing else: `mute: true` for a muted feed,
`digest_min` above zero to batch, `events` to narrow the feed (leave it out for all of them).

`init` also installs the route (`<id>-observe`) through the seats' own mechanism — the same signed
POST at the same gateway — but with `deliver_only: true` and a two-line prompt, because the notice
is *already written*: nothing wakes an agent, and there is no third seat to hold a lock or take a
turn. The eight transitions are `opened` (a new PR needs its first look), `handoff` (a fix was
pushed and review requested), `verdict` (a changes-requested verdict started a fix), `approved`
(the reviewer approved), `escalation` (the cap is spent and adjudicator delivery is pending),
`ruling` (an isolated adjudicator's ruling was recorded), `stall` (the watchdog decided a quiet PR is worth reporting) and `closed` (merged or abandoned;
cleanup was attempted, but disk reclamation is not confirmed by this notice).

Four rules keep the feed from becoming a gate:

* **Emitted from state, not from prose.** A notice is written by the gate, by the seat scripts and
  by the watchdog, at the transition they just made — never parsed out of an agent's summary, and
  never on the strength of an agent's claim.
* **One transition, one notice.** The ledger key is loop + PR + head + event + verdict/round
  identity, so a redelivered webhook, a re-run gate or a retried sweep cannot produce a duplicate.
* **Ambiguous delivery is not replayed.** A missing route or secret is a definite pre-POST failure
  and the watchdog retries it (up to three attempts). A timeout or 5xx after posting may have sent
  the notice; it remains `uncertain` in `status` for manual reconciliation, never automatically
  retried. Legacy `failed` receipts without proof of a pre-POST failure are quarantined the same
  way. Neither outcome consumes a seat or blocks the queue.
* **Off means off.** No observer, a muted feed, an event filtered out: the loop behaves exactly as
  it would with no observer at all. A misconfigured feed (a route that was never installed, or a
  bare profile with no route) never refuses a loop — the seats keep running and `status` says what
  is wrong with the feed.

Private PRs are safe to watch this way: the link goes to the chat the operator configured for that
profile, and nowhere else.