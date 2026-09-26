# Operating a loop

Installing, checking and running a review loop once it is configured: what `init` writes, the
day-to-day verbs, the `doctor` preflight, the `selftest` of the isolated turn path, `explain` for a
PR that is not moving, and how a burst of PRs is queued. The keys themselves are in the
[configuration reference](configuration.md); the design behind each tool is in
[architecture](architecture.md). The quick install is in the [README](../README.md#install).

## What `init` writes

When the plugin settings name the seats (`fixer_profile`, `reviewer_profile`, `reviewer_login`,
`fixer_login`, `adjudicator_profile`), the seat flags of the [`init` example](../README.md#install) become optional — the form supplies them
and an explicit flag still wins. `--dry-run` prints the whole plan (who serves each seat, the route
URLs, what would be written) and stops there, which is the way to check a form before it reaches a
running loop.

`init` writes exactly four things, all of them visible and reversible:

1. one loop config — `~/.hermes/review-loops.d/<id>.json`
2. three webhook routes — `<id>-review`, `<id>-fix`, `<id>-breach` — into the gateway's own
   `webhook_subscriptions.json` (generated prompts, generated secrets, file left at 0600)
3. two GitHub hooks, on `pull_request` and `pull_request_review`, pointing at those routes
4. one cron job plus a 5-line shim in `~/.hermes/scripts/` that forwards to the plugin's watchdog

Route edits are serialized only among cooperating review-loop plugin processes, using a sibling
lock file and atomic replacement. Native Hermes CLI and dashboard subscription edits do **not**
take that lock (issue #1; upstream fix pending in NousResearch/hermes-agent#120964), so the plugin
mitigates the race from its side — it does not close it:

* **Optimistic writes.** Each plugin edit records the registry's inode, mtime, size and content
  hash when it reads, re-checks them immediately before `os.replace`, and re-reads and re-applies
  its edit (up to 5 attempts, then `RegistryConflictError` with nothing published) if a native
  write landed in between — so the plugin no longer overwrites a concurrent native change.
* **Intent record + self-heal.** Every route the plugin installs or rebinds is also copied,
  secret included, to `<state_dir>/route-intent.json` (0600, atomic). Every armed watchdog sweep
  compares this loop's routes with it and restores any route a native writer erased or changed
  (secret, script, prompt, events, profile, `deliver_only`, host) with the **same secret**, so
  GitHub's hook keeps authenticating, and says what it restored in its cron output. Other routes
  are never touched; a name now held by a non-review-loop script is reported, not overwritten; a
  malformed registry is never overwritten. Change or remove routes through `set`/`apply`/
  `uninstall` — they update the record — or self-heal will put a native edit back.
* **What remains.** A few syscalls between the final identity check and the rename, and a native
  writer that read *before* a plugin publish and writes *after* it, can still drop a plugin edit
  (or a native one). The plugin's lost routes come back on the next armed sweep; a native edit
  the plugin overwrote in that window does not. Between sweeps a broken route can miss
  deliveries. See [docs/issue-1-route-self-heal.md](issue-1-route-self-heal.md).

If directory sync
fails after replacement, the plugin raises `RegistryDurabilityError(published=True)`: the new
registry is visible, but crash durability is unconfirmed; do not assume the operation rolled back.

## Everyday commands

```bash
hermes review-loop list                 # what is configured
hermes review-loop status --loop name   # seats, profiles, routes, live runs, queue, breaches
hermes review-loop explain --loop name --pr 123   # why that PR is not moving, and what is next
hermes review-loop doctor --loop name   # preflight the install: profiles, seat models, tokens, routes, hooks, cron
hermes review-loop models --seat reviewer --loop name   # what that seat's profile's provider offers (read-only)
hermes review-loop settings             # the plugin-level defaults, and where each came from
hermes review-loop init --repo owner/name --dry-run   # preview a loop: seats, routes, nothing written
hermes review-loop apply --loop name    # push those defaults onto an existing loop (--dry-run)
hermes review-loop apply --loop name --while-busy     # rebind even while a seat has a run out
hermes review-loop set --loop name --reviewer-concurrency 2   # two reviews at once, one fix at a time
hermes review-loop arm --loop name      # arm/pause by flipping the repo hooks
hermes review-loop pause --loop name
hermes review-loop drain --loop name --seat reviewer
hermes review-loop cleanup --loop name --sweep --dry-run
hermes review-loop uninstall --loop name
```

`set` is how you change the knobs after install — `--reviewer-concurrency`, `--fixer-concurrency`,
`--concurrency` (the default for both seats), `--cap`, `--clone`, `--base`, `--grace-min`,
`--ttl-min` — through the same validation `init` uses, so a capacity above 1 without a clone is
refused here exactly as it is at init. Prompts are rendered from the payload at fire time, so a
change takes effect on the next event with nothing to re-install. The observer feed is changed the
same way: `--observer-profile`, `--observer-route`, `--observer-deliver`, `--observer-events`,
`--observer-digest-min`, and `--observer-mute` / `--observer-unmute` / `--observer-disable`.

`set --adjudicator-login LOGIN --token LOGIN=/abs/path` names (or `--adjudicator-login ""`
clears) the optional account a ruling is also posted as; `set --token` maps only that login.

Who serves each seat, and how the plugin-level defaults reach a loop, is covered in
[Settings, in the desktop](settings.md).

## Token files: one PAT per account

Each GitHub account the loop uses gets its **own fine-grained PAT in its own file**, and the loop
config (or the settings form) holds only the path:

```bash
(umask 077; mkdir -p ~/.hermes/keys)   # paste each account's PAT into its own <login>-pat file
chmod 600 ~/.hermes/keys/*-pat

hermes review-loop init --repo owner/name \
  --fixer dev-account --reviewer rev-bot \
  --fixer-profile drey --reviewer-profile vex \
  --read-token reader-bot \
  --token reader-bot=~/.hermes/keys/reader-bot-pat \
  --token rev-bot=~/.hermes/keys/rev-bot-pat \
  --token dev-account=~/.hermes/keys/dev-account-pat \
  --adjudicator-route name-breach --adjudicator-profile tuck \
  --adjudicator-login rule-bot --token rule-bot=~/.hermes/keys/rule-bot-pat \
  --host https://your-gateway.example
```

* **Mode 600, owned by you, absolute path.** The settings form's `*_token_file` fields and the
  adjudicator's `--token` are refused unless the path is absolute after `~` expansion, exists, is a
  regular file you own, and is not group/world readable. The check reads metadata only.
* **The four-identity rule.** The reader, the reviewer, the fixer and (if set) the adjudicator
  comment login must be four different accounts with four different token files — the broker
  re-checks distinct `/user` principals before each write. A shared file is one account wearing
  two hats, and the review loop exists so a different account reviews the fixer's work.
* **Never a token value.** `status`, `settings`, `doctor` and every refusal print paths; `doctor`
  reports each seat's file as `path (exists: yes, private: yes)`.

## Preflight: `doctor`

`init` writes the config, the routes and (optionally) the hooks and the cron job — but a
syntactically valid file is not proof that any of it can run. The reviewer's profile may not
exist, the token file named for the fixer may have gone in a key rotation, the route in the
gateway registry may wake a *different* profile than the loop config says, the repo hook may
point at your previous gateway, or the cron shim may still be pinned to the plugin directory a
previous upgrade left behind. Every one of those is a loop that looks armed and cannot wake a
seat — so `doctor` checks the installation itself, read-only, before anyone arms it:

```bash
hermes review-loop doctor --loop attest             # one loop; without --loop it preflights them all
hermes review-loop doctor --loop attest --offline   # skip the gateway probe and the hooks read
hermes review-loop doctor --loop attest --strict    # an undecided check counts as a failure
```

One line per check, in one of four states:

| state | meaning |
|---|---|
| ✅ verified | checked, and correct |
| ❌ absent | the thing is not there — a missing profile, token file, route, hook, job or script |
| ❌ mismatch | present, but not what this loop needs — a route waking another profile, a hook on another gateway, a shim pinned to a stale plugin path, a world-readable PAT |
| ⚠️ unknown | could not be decided *from here* — a hooks read the token was not allowed to make, or a probe skipped with `--offline` |

Each failure is followed by the one command that fixes it, failures exit 1, and `unknown` is never
reported as `absent`: "the API refused to tell me" and "there are no hooks" are different claims,
and printing the second when the first is true sends you hunting for a hook that exists (reading
the repo's hooks needs `admin:repo_hook`, so a token without it shows ⚠️, not ❌).

It writes nothing — no config, no route registry, no state, no GitHub hook — unless you pass
`--repair`, whose one write is restoring this loop's own routes from the plugin's intent record
(same secret) before the read-only checks run. It never fires a
route, because a synthetic POST at a seat's route is a real agent run with a real budget. The
network side is a TCP connect to the gateway (is anything listening?) and, when the token is
allowed to, a read of the repo's hooks.

A correct installation:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → a nonempty token file (identity and API access not checked)
  ✅ profile:fixer        fixer-profile → doctor-demo/hermes-home/profiles/fixer-profile
  ✅ credential:fixer     dev-fixer → a tokens entry
  ✅ token:dev-fixer      doctor-demo/fix.pat (mode 600, non-empty)
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           rev-coach (mapped in tokens)
  ✅ route:widgets-review reviewer-profile · pull_request · http://127.0.0.1:43651/p/reviewer-profile/webhooks/widgets-review
  ✅ route:widgets-fix    fixer-profile · pull_request_review · http://127.0.0.1:43651/p/fixer-profile/webhooks/widgets-fix
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /home/jeremy/projects/rl-15-doctor/scripts (watchdog, both gates, cleanup)
  ✅ cron:shim            doctor-demo/hermes-home/scripts/review-loop-watchdog.py → /home/jeremy/projects/rl-15-doctor/scripts/watchdog.py
  ✅ cron:job             8f21c0 every 15m, next 2026-09-23T22:15:00Z
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                1 configured: doctor-demo/reviews
  ✅ gateway              127.0.0.1:43651 accepts a connection
  ✅ hook:widgets-review  hook 41 → http://127.0.0.1:43651/p/reviewer-profile/webhooks/widgets-review (pull_request, active)
  ✅ hook:widgets-fix     hook 42 → http://127.0.0.1:43651/p/fixer-profile/webhooks/widgets-fix (pull_request_review, active)

widgets: 20 verified, 0 failed, 0 unknown (of 20 checks)
  every check passed — this loop can wake a seat and post a verdict.
```

and the same loop with six of the ways it really breaks:

```
$ hermes review-loop doctor --loop widgets
[widgets] acme/widgets — preflight (read-only: it writes nothing and fires nothing)
  ✅ config               doctor-demo/loops/widgets.json (repo acme/widgets, cap 3, base main)
  ✅ profile:reviewer     reviewer-profile → doctor-demo/hermes-home/profiles/reviewer-profile
  ✅ credential:reviewer  rev-coach → a nonempty token file (identity and API access not checked)
  ❌ profile:fixer        no profile home at doctor-demo/hermes-home/profiles/fixer-profile
      fix: `hermes profile create fixer-profile`, or re-run init with --fixer-profile pointing at a profile that exists: the run happens as this profile
  ✅ credential:fixer     dev-fixer → a tokens entry
  ❌ token:dev-fixer      no file at doctor-demo/fix.pat
      fix: write the PAT for dev-fixer to doctor-demo/fix.pat (chmod 600), or re-run init with --token dev-fixer=<a path that exists>
  ✅ token:rev-coach      doctor-demo/rev.pat (mode 600, non-empty)
  ✅ read_token           rev-coach (mapped in tokens)
  ❌ route:widgets-review registered at https://old-gateway.example, but the loop is armed at http://127.0.0.1:43651
      fix: re-run init to rewrite the route for http://127.0.0.1:43651: a hook or a manual POST still goes to the recorded origin
  ❌ route:widgets-fix    wakes profile 'some-other-agent', but seats.fixer.profile is 'fixer-profile' — the wake would run the wrong agent
      fix: re-run init with --fixer-profile fixer-profile so the route and the loop config agree
  ✅ route:widgets-breach default · adjudication wake
  ✅ scripts              /home/jeremy/projects/rl-15-doctor/scripts (watchdog, both gates, cleanup)
  ❌ cron:shim            pinned to /opt/old/plugins/hermes-review-loop/scripts/watchdog.py, this install runs /home/jeremy/projects/rl-15-doctor/scripts/watchdog.py
      fix: re-run init --schedule 15m for this loop: the shim was written by a different plugin install, and the scheduler keeps running that path
  ❌ cron:job             8f21c0 (review loop watchdog (widgets)) is paused
      fix: `hermes cron resume 8f21c0`: a paused watchdog never reports a stall
  ✅ clone                doctor-demo/clone (git checkout)
  ✅ state_dir            doctor-demo/state (created under doctor-demo on the first run)
  ✅ roots                1 configured: doctor-demo/reviews
  ✅ gateway              127.0.0.1:43651 accepts a connection
  ⚠️ hooks                could not read /repos/acme/widgets/hooks — nothing was proved about 2 hook(s) (a token without admin:repo_hook reads as denied)

widgets: 12 verified, 6 failed, 1 unknown (of 19 checks)
  6 failed: profile:fixer, token:dev-fixer, route:widgets-review, route:widgets-fix, cron:shim, cron:job — fix the ❌ lines above before this loop is armed.
```

(Both transcripts are real output from the suite's isolated demo home — a loopback gateway
sink for the probe, a stubbed GitHub, short relative paths. A run against a live install
prints the same lines with absolute paths and the real hook list.)

Each seat needs its own GitHub token, and that is deliberate: the token that reviews, the token
that pushes and the token that reads are separate and revocable one at a time. A classic PAT with
`repo` is enough for the seats; creating hooks additionally needs `admin:repo_hook`. A loop that
names tokens must name one per seat, and the file has to be there — checked before `init` or `apply`
writes anything, because a missing PAT otherwise surfaces hours later as an unauthenticated read.

The checks and the reasoning behind them are in
[architecture: Preflight](architecture.md#preflight-can-this-installation-run).

## Verifying the isolated setup: `selftest`

`doctor` checks the installation; `selftest` checks the **isolated turn path** (issue #16) against
the real capabilities, one ✅/❌ line per check with the fix under each failure. It exits 1 on any
failure. **It never writes to GitHub**: every GitHub call it makes goes through a GET-only guard,
and the live turn's broker runs in a host-only no-write mode. No token or key is printed.

Run these in order (replace `ID` and `N`; `N` should be an open, non-draft, same-repository PR
targeting the loop's base):

```bash
# 0. the private runtime file the production worker reads (host paths; each seat's model comes
#    from its Hermes profile — see configuration.md#runtime-file-and-seat-models-review-loop-runtimejson)
(umask 077; touch ~/.hermes/review-loop-runtime.json); chmod 600 ~/.hermes/review-loop-runtime.json; $EDITOR ~/.hermes/review-loop-runtime.json
hermes review-loop doctor   --loop ID                       # installation preflight
hermes review-loop selftest --loop ID --no-model            # 1,2,4,6: runtime, bwrap, identities, ledger — free
hermes review-loop selftest --loop ID --pr N                # + one tiny completion per seat model + broker dry run
hermes review-loop selftest --loop ID --pr N --live-turn    # + one real isolated reviewer turn, NOT posted
python -m review_loop.run_supervisor status ~/.hermes/state/review-loop-runs.sqlite
```

| step | what it proves |
|---|---|
| 1 runtime | the runtime file is a regular 0600 file you own with `source venv runtime rust` (plus optional `seats.<seat>` overrides and the legacy `model upstream key_file`, warned about), the paths exist (the venv's interpreter link must stay inside `runtime`), and any override has an HTTPS `…/chat/completions` upstream and a private non-empty key file; then one `seat:<seat>` line per seat — reviewer, fixer, and the adjudicator when it has a route — with the profile → provider / model the worker will use and its `[api_mode, API key \| OAuth (host-refreshed)]` (never the key or token), or the reason that seat's turn would be held |
| 2 bubblewrap | unprivileged user namespaces work; a probe in the real sandbox layout (committed source snapshot, configured venv/runtime/Rust) cannot read a dummy host secret, any model key file, each seat profile's `.env`/`auth.json`/`config.yaml`, the PATs, the runtime file, `~/.hermes/.env` or the loop config, and has no network or credential-like env |
| 3 inference | one ~16-token request in the seat's own wire format (chat completion, Responses or Messages) through the host inference capability **per distinct seat resolution** (seats that share a profile's provider, model and credential share one call), each with that resolution's own credential; an OAuth seat's 401 is refreshed and retried once on the host before it is reported (`--no-model` skips it) |
| 4 identities | read, reviewer, fixer (and optional adjudicator) PATs resolve via `/user` to the expected logins and distinct principals; the repo is readable |
| 5 authorization | with `--pr N`: the broker's reviewer-write checks (`broker.authorize`, reads only) and the host receipt generation |
| 6 supervisor | the ledger migrates and `status` reads; the route would accept the runtime file; `doctor`'s state dir, cron shim/job and gateway checks; the observer route |
| 7 live turn | with `--live-turn --pr N`: a real isolated reviewer turn with the reviewer seat's resolved model (`--timeout`, default 600 s); the verdict and body the agent *would* submit are printed |

Example step-1/3 lines for a ChatGPT-subscription reviewer and an API-key fixer:

```text
✅ seat:reviewer    profile codex: openai-codex / gpt-5.3-codex via chatgpt.com [codex_responses, OAuth (host-refreshed)]
✅ seat:fixer       profile fix: custom:acme / fix-model via acme.example [chat_completions, API key]
✅ model:completion HTTP 200 — reviewer: profile codex: openai-codex / gpt-5.3-codex via chatgpt.com [codex_responses, OAuth (host-refreshed)], reply 'OK'
```

A subscription seat's step 3 spends a request from **your** plan's usage window (the seat shares
it with your own use of that account); a 429 there means that window is spent.

The live turn runs in the CLI process, not through the supervisor, so it adds no ledger row and
raises no operator notice; confirming alerts still needs a real enqueued turn and a watchdog sweep.

The no-write guarantees and what the selftest does not prove are in
[Issue #16: live verification](issue-16-boundary.md#live-verification-hermes-review-loop-selftest).

## Why isn't this PR moving?

`status` shows the loop's shape; `explain` answers the question you actually have at 2am, for one
PR: what GitHub says about the head, how much of the budget is spent *at that head*, who holds the
seat, what is queued or marked in flight, whether the loop is paused, and — last line, always — the
one event that has to happen next.

```bash
hermes review-loop explain --loop widgets --pr 7    # --loop may be omitted when it is the only loop
```

```
[widgets] acme/widgets#7 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/7
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head aaaaaaa
  budget:     1/3 verdicts spent · 1 at head aaaaaaa — changes requested 2026-01-01T00:00:00Z by rev-coach
  seat:       nobody holds it
  queue:      not queued
  in-flight:  none
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  blocked:    the changes-requested verdict at head aaaaaaa has no fix run out — the fixer gate did not start one for that delivery
  next:       re-deliver the changes-requested review event for head aaaaaaa to the fixer gate after checking why its run did not start — no fixer is running to push a fix
```

A PR that is waiting rather than broken says so, instead of looking like a failure:

```
[widgets] acme/widgets#9 — why this PR is not moving
  pr:         https://github.com/acme/widgets/pull/9
  read:       2026-09-24T01:58:53Z (GitHub pulls/reviews/hooks + local state; read once, nothing written)
  state:      open · base main · author dev-fixer · head bbbbbbb
  budget:     0/3 verdicts spent · nothing at head bbbbbbb
  seat:       nobody holds it
  queue:      reviewer 1 of 1 (waiting 9m) — reviewer at capacity 1/1: acme/widgets#7 (720s)
  in-flight:  none
  escalation: none
  hooks:      armed — both seat routes are active repo hooks
  sweep:      no watchdog sweep recorded — nothing has read this loop's PRs yet
  blocked:    no capacity: queued with the reviewer seat — reviewer at capacity 1/1: acme/widgets#7 (720s)
  next:       a reviewer slot frees — the queued run starts then (a verdict or a handoff ends the run holding it; the lock expiry at 45m is the backstop)
```

Three rules keep it honest:

* **No second engine.** The conclusions come from the same predicates the live gates run
  (`verdicts`, `reviewed_at_head`, `changes_at_head`, `approved_at_head`, the seat ledgers, the
  queue, the breach marker, the armed check), in the gates' own guard order. A gate stops at the
  first guard that silences it; `explain` reports every guard and names the one that is holding the
  PR. What it says cannot drift from what the loop would do, because it is the same code.
* **Unknown is not a guess.** A transient GitHub failure, an unreadable review list or hook list,
  or a malformed PR head is printed as unknown and needs a retry, not a guessed verdict or review
  request. HTTP 404 means missing *or inaccessible* and calls for checking the number and access,
  not treating it as a transient failure. Every timestamp is labelled with where it came from — the
  read itself, the verdict's `submitted_at`, or the state file's own mark.
* **Read-only, byte for byte.** No claim, no queue entry, no inflight mark, no drain, no webhook
  POST, no token printed, and it does not even prune an expired lock while looking at it. Run it
  twice and GitHub, the loop's state directory and your routes file are untouched. The suite asserts
  exactly that.

It needs to read the repo's hooks to tell "paused" from "armed", so the read token wants enough
scope to see them (`repo` is normally enough); if it cannot, the line says the hook state is unknown
rather than claiming the loop is parked. `explain` exits 2 only when the question cannot be asked at
all — an unknown loop, or several loops and no `--loop`.

The guard order `explain` walks is in
[architecture: Explain](architecture.md#explain--why-is-this-pr-not-moving).

## How it handles a burst

Fifty PRs arrive in an hour. Ten of them wake the reviewer, and forty-one queue — the queue costs
nothing but disk-less JSON. The moment a review ends, that slot is filled from the queue:

```
review #101 finishes (approve or changes-requested)
  → its slot is freed
  → the queue is drained immediately, up to the free slots
  → the next queued PR's review starts
```

A slot is not freed by a timer. The **verdict** frees it, either kind; the fixer's **request** frees
the fixer's. The watchdog sweep is only the backstop, and `ttl_min` is the last resort for a run that
died without a verdict. Capacity is per seat, so `reviewer 10 · fixer 2` is a legitimate shape —
reviews are cheap and parallel, fixes are not.

Do the arithmetic before setting it high: each in-flight run is a whole agent plus its own clone and
its own cold build. On a big Rust repo, ten at once is ten parallel builds — the machine, not GitHub,
is what decides how high this number can go.