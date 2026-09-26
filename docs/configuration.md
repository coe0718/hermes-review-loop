# Configuration reference

One JSON file per loop under `~/.hermes/review-loops.d/` (override the directory with
`REVIEW_LOOP_CONFIG_DIR`). Everything the loop knows about a repository is here, which is what lets
one install serve several repositories with different seats, budgets and credentials.

`hermes review-loop init` writes this file for you; hand-editing is fine, and the loader is strict
on purpose — a loop that cannot be resolved to a repository, a base branch and two seats is a
configuration error, not a run that guesses.

Before arming a loop, `hermes review-loop doctor --loop <id>` checks that everything this file
*names* actually exists — the seat profiles, the token files, the routes and their secrets, the
GitHub hooks, the cron shim and job, the clone and the gateway — and writes nothing while doing it.
See [Preflight](architecture.md#preflight-can-this-installation-run) and, for example output,
[Operating a loop](operations.md#preflight-doctor).

## Required

| key | meaning |
|---|---|
| `repo` | `owner/name`. The gate selects the loop from the webhook payload's `repository.full_name`. |
| `fixers` | GitHub logins allowed to open/push the PRs this loop works on. |
| `reviewers` | GitHub logins whose verdicts count toward the budget. |
| `seats.reviewer.route` / `seats.fixer.route` | the webhook routes that wake each seat. |
| `seats.reviewer.profile` / `seats.fixer.profile` | the Hermes profile each run happens as; it must exist on this machine, and the two must differ. |
| `reviewer_seat` | which reviewer login the reviewer route serves (a request aimed at anyone else is not this loop's business). Validated against `reviewers`: a route that serves a login this loop does not count is a loop that never wakes. |

## Everything else

| key | default | meaning |
|---|---|---|
| `id` | repo name | loop id; also the prefix of the generated routes |
| `base` | `main` | only PRs targeting this branch are in the loop |
| `cap` | `3` | verdicts allowed before escalation (`cap - 1` fix turns) |
| `seats.<seat>.login` | first fixer/reviewer | the GitHub login that seat acts as; `init`/`apply` refuse one outside that seat's allowlist, and two seats may not share one |
| `seats.<seat>.agent` | profile name | display name used in start-pings and prompts |
| `seats.<seat>.channel` | profile's `DISCORD_HOME_CHANNEL` | where the start-ping goes |
| `seats.<seat>.emoji` | 🔍 / 🔧 | cosmetic, for the ping |
| `adjudicator.route` | — | enables adjudication: when the cap is spent an isolated adjudicator turn is enqueued in the host run ledger; omit to only write the marker. The legacy gateway route itself stays silent |
| `adjudicator.profile` | `default` | profile of the legacy gateway route (validated against the seats; the isolated turn does not run as it) |
| `seats.adjudicator.login` | unset | **optional** GitHub identity the ruling is *also* posted as, as a PR comment. Set it with `init`/`set --adjudicator-login LOGIN --token LOGIN=/abs/path` (`set --adjudicator-login ""` clears it) or the `adjudicator_login` setting. It needs its own `tokens` entry (an absolute, private 0600 file) and must be a fourth account: not the `read_token`, not either seat, not in `fixers`/`reviewers`, and not sharing a token file with any of them. The broker re-checks all of it (plus distinct `/user` principals and the live PR) before each comment. Without it rulings go to the operator only — not an error |
| `seats.adjudicator.concurrency` | `1` | isolated adjudicator turns at once. Does not inherit the loop-level `concurrency` |
| `skill` | — | skill the seats are told to load |
| `tokens` | `{}` | `login → path of a file containing that seat's PAT (mode 600)` |
| `read_token` | first token | login whose token performs reads |
| `clone` | — | the local clone reviews may use; cleanup prunes its worktrees |
| `roots` | `[]` | directories the cleanup may ever touch. Anything outside them is out of scope. A root may be shared between loops: a child is only this loop's when its name carries both the PR (`pr7`) and the repository name (`widgets-pr7-target`). `/`, the home directory and its ancestors are refused. |
| `concurrency` | `1` | default runs at once *per seat*. `1` = serialized; above 1 requires `clone`, because every run then gets its own isolated clone. |
| `seats.<seat>.concurrency` | loop default | this seat's own limit, overriding the default. Set with `hermes review-loop set --reviewer-concurrency N` / `--fixer-concurrency N`. |
| `state_dir` | `~/.hermes/state/review-loops/<id>` | locks, queue, in-flight marks, breach markers, artifacts, watchdog memory |
| `host` | unset | your gateway's HTTP(S) webhook origin; `init` requires `--host` or an explicit plugin setting before it writes config/routes/hooks |
| `grace_min` | `25` | how long a quiet head is allowed to sit before the watchdog speaks |
| `marker_grace_min` | `60` | how long a breach marker may sit unpicked-up |
| `cooldown_h` | `6` | repeat suppression per stall |
| `ttl_min` | `45` | seat-lock lifetime; past this a crashed run has lost its seat |
| `inflight_ttl_min` | `10` | how long a same-head burst is considered already handled |
| `observer` | `{}` | the read-only observer feed. `{}` means no feed, and the loop is untouched by its absence — see [The observer feed](#the-observer-feed) |

## The observer feed

An **observer** is a destination that hears about transitions without being part of them: a short
notice per event, delivered to a chat, with no agent on the route and no seat to hold. It is opt-in
per loop — no `observer` block, no feed — and it is the one block in this file the loader is
*lenient* about, because a feed that cannot deliver must never refuse a loop that can run.
The operator walkthrough (what a notice looks like, turning it on) is [the observer guide](observer.md).

| key | default | meaning |
|---|---|---|
| `observer.route` | `<id>-observe` | the gateway route each notice is POSTed to. Without it there is nowhere to deliver, and `status` says so |
| `observer.profile` | `default` | the Hermes profile whose chat the route delivers into |
| `observer.deliver` | `telegram` | the route's delivery target. `log` is refused by `init` / `set`, since it would deliver to nobody |
| `observer.events` | all eight | any subset of `opened`, `handoff`, `verdict`, `approved`, `escalation`, `ruling`, `stall`, `closed`. An event left out is never sent, and never recorded as owed |
| `observer.digest_min` | `0` | `0` sends one message per transition; above `0` batches them into one compact message at the next watchdog sweep |
| `observer.mute` | `false` | stop delivering and keep the configuration (`--observer-mute` / `--observer-unmute`) |

```bash
hermes review-loop set --loop <id> --observer-profile tuck        # turn it on, or move it
hermes review-loop set --loop <id> --observer-route widgets-observe
hermes review-loop set --loop <id> --observer-events verdict,escalation,closed
hermes review-loop set --loop <id> --observer-digest-min 30
hermes review-loop set --loop <id> --observer-mute                # --observer-unmute, --observer-disable
```

`init --observer-profile <name>` writes the block and installs `<id>-observe` alongside the seats'
routes, with the same signed POST at the same gateway origin — but `deliver_only: true` and a prompt
that is just the notice, because by the time a route fires the message has already been written by
the loop. The transitions it can send:

| event | when it is sent |
|---|---|
| `opened` | a new PR needs its first look (also `ready_for_review` / `reopened`) |
| `handoff` | the fixer pushed and requested review — the fixer's turn ended |
| `verdict` | a changes-requested verdict landed and a fix run started |
| `approved` | the reviewer approved (nothing else would free that seat) |
| `escalation` | the cap is spent — sent after the durable marker, before adjudicator delivery; receipt remains pending |
| `ruling` | an isolated adjudicator recorded its ruling (ACCEPT / REJECT / RESPEC) in the host ledger. The notice carries the verdict and counts only; the reason reaches the operator through the watchdog's outbox, which delivers every ruling even with no, a muted, or a filtered feed |
| `stall` | the watchdog decided a quiet head is worth reporting |
| `closed` | the PR was merged or abandoned, and its disk was reclaimed |

Consequences worth knowing:

* **One notice per transition.** Each notice is keyed by loop + PR + head + event + verdict/round
  identity in `observations.json`, so a redelivered webhook, a re-run gate or a retried sweep cannot
  produce a second ping.
* **A failed delivery is recorded, not fatal.** A route that 500s, has no secret, or was never
  installed leaves an entry with its reason (visible in `status` as *owed*) and is retried by the
  next watchdog sweep, up to three attempts. The queue, the seats and the locks are never involved.
* **No secrets in a ping.** A notice carries the loop id, PR number and URL, head, event, outcome,
  next turn and a one-line summary — no PAT, no HMAC secret, no diff, no review body. The PR link is
  private to whatever chat the operator configured for `observer.profile`.
* **Disabling is safe at any time.** `--observer-mute` stops delivery; `--observer-disable` removes
  the active block and route even with owed notices. The ledger and original destination binding
  remain: queued notices stay owed but do not send while disabled, and re-enabling at a different
  host, route, profile or delivery target is refused until they are settled. `status` shows the
  outstanding count. A loop whose feed is broken logs the problem and runs its seats normally.

## Adjudication (the isolated ruling)

When the cap is spent without an approval, the gate writes the durable breach marker and — if the
loop has an `adjudicator.route` — enqueues an **isolated** adjudicator turn in the host run ledger
(`$HERMES_HOME/state/review-loop-runs.sqlite`, turn `breach:<rounds>` at that head). It runs exactly
like the other seats: a detached worker re-reads GitHub (PR open, not draft, same base, same head,
author a configured fixer, the cap still spent, no approval at the head, a matching marker),
exports the head read-only into bubblewrap, and gives the agent no credentials. Its only write is
`python -m review_loop.broker_client ruling --verdict ACCEPT|REJECT|RESPEC --body-file PATH`,
once. It never merges, pushes or reviews.

The host then, in order: records the ruling in the ledger (`rulings` table — this is the
acknowledgement), sends a `ruling` observer notice, and — only when `seats.adjudicator.login` is
configured — posts the ruling as a PR comment under that identity. Every ruling, with its reason,
is also delivered by the watchdog's operator outbox (cron stdout), so the operator always hears
of it. `python -m review_loop.run_supervisor rulings $HERMES_HOME/state/review-loop-runs.sqlite`
lists recent rulings with their comment state (`none`, `denied`, `posted`, or `uncertain` for a
POST whose outcome is unknown — never retried automatically).

If the enqueue fails (no private runtime file, ledger or spawn error), the marker stays
`delivery-pending` and the watchdog retries it; a duplicate delivery is deduplicated by the ledger.

## Plugin settings (the desktop form)

`plugin.yaml` declares a `config_schema`, so the desktop renders a form at **Capabilities → Plugins →
review loop**. Those values are **defaults for a new loop**; pushing them onto an existing loop is
explicit, because a form that quietly renumbers a running loop is a miserable thing to debug
(the operator walkthrough is [Settings, in the desktop](settings.md)):

```bash
hermes review-loop settings                      # what the form holds, [set] vs [default], per key
hermes review-loop apply --loop <id> --dry-run   # the diff
hermes review-loop apply --loop <id>             # write it
```

| setting | default | lands on |
|---|---|---|
| `cap` | 3 | `cap` |
| `reviewer_concurrency` / `fixer_concurrency` | 1 | `seats.<seat>.concurrency` |
| `reviewer_profile` / `fixer_profile` | — | `seats.<seat>.profile` (and `seats.<seat>.agent`, when the loop has not named one) |
| `reviewer_login` | — | `seats.reviewer.login` **and** `reviewer_seat` — the login the review route serves |
| `fixer_login` | — | `seats.fixer.login` |
| `adjudicator_profile` | — | `adjudicator.profile`, on a loop that already has an `adjudicator.route` |
| `reviewer_token_file` / `fixer_token_file` | — | `tokens[<that seat's login>]` — a **path** only; checked absolute, existing, yours, mode 600 before any write |
| `adjudicator_login` | — | `seats.adjudicator.login`, on a loop that already has an `adjudicator.route` |
| `adjudicator_token_file` | — | `tokens[<adjudicator login>]` — a **path** only, same checks, and not shared with any other login |
| `clone`, `base`, `host` | —, `main`, unset | the same loop keys; a blank host in the form preserves an existing loop's explicit host |
| `grace_min`, `ttl_min`, `inflight_ttl_min` | 25, 45, 10 | the same loop keys |

Settings are per profile (`plugins.entries.hermes-review-loop.settings`, written through Hermes'
single config writer), and `review_loop/config.py::SETTINGS_SCHEMA` mirrors the manifest — the suite
fails if the two drift, because a form that writes keys nothing reads is worse than no form.

Blank `clone` means *not set here*: it never erases the clone a loop already uses, since the cleanup
prunes worktrees through that path. The rails still apply — `reviewer_concurrency: 2` with no clone
is refused at `init`, at `set` and at `apply` alike.

There is no built-in webhook host. Set `host` to your own gateway origin (for example,
`https://your-gateway.example`) in the plugin settings, or pass `--host` to `init`; the CLI rejects
missing, relative, and malformed hosts before writing any config or routes, even without `--hooks`.
Existing loop files with an explicit `host` continue to load, and an unset form setting does not
erase one when you run `apply`. A public GitHub webhook should use HTTPS.

## Seat identity (who serves each seat)

A seat is a **Hermes profile** (the model, its budget, its credentials) plus a **GitHub login** (the
identity it acts as and the attribution its reviews carry). The form carries a per-profile default
for both, plus an optional adjudicator profile; the allowlists, the route names and the adjudicator
route stay per repository, because one form cannot honestly own every repository.

A blank profile or login means **not set here**. It never unsets what a loop file says, so a
repository that needs its own pair keeps it until somebody pushes the form onto that loop:

```bash
hermes review-loop settings                            # the form's mapping, and what each loop runs as now
hermes review-loop init --repo owner/name --dry-run    # preview seats + routes for a new loop
hermes review-loop apply --loop name --dry-run         # what a push would change, including routes
hermes review-loop apply --loop name                   # stage it: config and routes together
hermes review-loop apply --loop name --while-busy      # ...even while a seat has a run out
```

**Validation, before anything is written.** `init` and `apply` refuse a mapping that cannot drive a
run, and they refuse it *before* the loop config, the routes or the hooks are touched:

| check | why |
|---|---|
| the profile exists (`~/.hermes/profiles/<name>`, or `default` for the launch profile) | otherwise the first event wakes nobody |
| the login is in that loop's `fixers` / `reviewers` allowlist | a seat may only act as a login the repository already trusts |
| reviewer and fixer differ in profile, in login, and in token file | one seat reviewing its own work is not a review |
| an adjudicator differs from both seats | it is judging them |
| every `tokens` mapping points at a real, non-empty file, and `read_token` is one of them | a missing PAT reads as an unauthenticated call, hours later, in a log nobody reads |
| a token path from the settings form (and the adjudicator's) is absolute, yours, and mode 600 | a path is all the form may hold; a group-readable PAT is a shared credential |
| a loop that maps tokens maps one for each seat it is writing | otherwise that seat pushes as the read identity |
| a route is not claimed by another loop, and still runs this role's gate script | the registry is shared by every plugin on the host |

Existence and allowlist membership are checked for the roles an operation *writes*: a loop created
before this validation existed keeps loading when you tune its `cap`. Distinctness, credentials and
route ownership always apply, because the unsafe shape is the combination.

**Applying is staged, not half-applied.** A route URL carries the seat's profile
(`/p/<profile>/webhooks/<route>`), so changing who serves a seat must also move its installed
GitHub hook URL. `apply` first reads the repo hooks; if that listing is unavailable it refuses to
move the profile. It snapshots the affected routes, rebinds and reads them back, updates matching
hook URLs and reads each hook back, then writes the loop config. Any failed route write, readback,
or hook update returns failure and attempts to restore prior routes and hook URLs; an incomplete
rollback is reported loudly for manual repair. Other loops' routes and their secrets are untouched.
`init` likewise restores the previous config bytes and owned routes if a retry fails partway
through route installation. Installing or moving a seat requires token-file mappings for the
read login and both seat logins before any installation writes.

**A seat with a run in flight is not rewritten underneath itself.** `apply` refuses while the seat
it would move has a live run, and says who is running and for how long. `--while-busy` is the
explicit override: the change lands now, and that run finishes under the identity it started with.
A new token-file path for a seat's login counts as moving that seat. Numeric knobs (`cap`, concurrency, timers) are not gated this way — they take effect on the next
event and cannot strand a run.

`status` shows the mapping and checks it against the registry:

```
  seats:      reviewer=vex-coat (vex) · fixer=drey-coe (drey)
  adjudicator:tuck (route widgets-breach)
  routes:     reviewer widgets-review → vex (ok) · fixer widgets-fix → drey (ok) · adjudicator widgets-breach → tuck (ok)
  token refs: reviewer vex-coat → ~/.hermes/keys/vex-coat-pat · fixer drey-coe → ~/.hermes/keys/drey-coe-pat
```

`MISMATCH — hermes review-loop apply --loop <id>` in that line means the config moved and the route
did not. Token values are never printed: only which login reads which file.

## State files (per loop, under `state_dir`)

| file | what it holds |
|---|---|
| `locks.json` | `{seat: {"repo#PR": {at, head, why}}}` — live runs per seat; `concurrency` of them may be active at once |
| `pending.json` | `{seat: {"repo#PR": {at, head, url, reason}}}` — queued, not run |
| `inflight.json` | `{"review:PR:sha" / "fix:PR:sha": ts}` — this head is already being handled |
| `breach.json` | `{"repo#PR": {head, rounds, cap, reason, at, status}}` — `delivery-pending` retries on a current-head watchdog sweep; `awaiting-adjudication` means POST accepted; `adjudicating` means the ruling run was claimed |
| `watchdog.json` | `armed_since`, `{heads: {PR: {sha, observed_at, last_seen_at}}}` (null observation for baseline/invalid clocks; absent PR clocks retained 30 days since last seen). A missing, malformed, boolean, non-finite, or future `armed_since` re-arms at the first successful PR listing and baselines all current heads rather than trusting old observations; a failed listing leaves state and queue unchanged. Alert history and last run are also stored here. |
| `watchdog.log` | one line per sweep, and per breach |
| `observations.json` | the observer feed's delivery ledger: `{"entries": {"<loop>:<PR>:<head>:<event>:<identity>": {status, message, url, attempts, error, retryable, batch}}}`. `failed` is retried only with explicit `retryable: true` evidence of a pre-POST failure; legacy failures without that evidence and ambiguous/stale claims become `uncertain` for manual reconciliation. `queued` waits for a digest; `pending` is claimed mid-delivery; `delivered` has a receipt. |
| `artifacts/<PR>/<seat>/` | where a run must keep its worktrees, build dirs and logs — per PR *and* per seat, so the two never share a checkout |

`hermes review-loop status` prints the shape of these files, and `hermes review-loop explain --pr N`
reads them (with the same predicates the gates use) to say why one PR is not moving. `explain` is
read-only down to the byte: it uses the non-pruning readers, so asking twice leaves every file above
exactly as it was.

## Runtime file and seat models (`review-loop-runtime.json`)

The isolated worker reads one private (0600) host file, `$HERMES_HOME/review-loop-runtime.json`.
It names **host paths** — the model is not a runtime setting any more (issue #32):

```json
{"source": "/path/to/hermes-agent", "venv": "/path/to/hermes-agent/venv",
 "runtime": "/path/to/python-runtime", "rust": "~/.rustup/toolchains/stable-x86_64-unknown-linux-gnu"}
```

**The interpreter this names must be able to read YAML.** A seat's model is resolved by a child
process running the `venv` above, and Hermes reads its configs with `ruamel.yaml`. A packaged
install runs Hermes itself on a bundled python that ships **neither** `ruamel.yaml` nor PyYAML, so
on such a host — with no runtime file naming a venv that has them — every seat reports
`this interpreter (…) has no YAML library (looked for yaml and ruamel.yaml)` and is held before its
turn starts. The reader tries `yaml`, then `ruamel.yaml`, then JSON (a JSON config is valid YAML),
and names the interpreter it was when it has neither: a profile's `config.yaml` written as real
YAML needs one of the first two.

**Each seat runs its own Hermes profile's model.** Before a turn, the host resolves the seat's
profile (`seats.reviewer.profile`, `seats.fixer.profile`, `adjudicator.profile`) with Hermes's own
resolution — the profile's `config.yaml` `model` block, its `.env`/secret sources and `auth.json`
credential pool, exactly as `hermes -p <profile> chat` would — in a separate process per seat whose
environment is built from scratch. The key it returns lives only in that turn's host inference
proxy; the sandbox's `config.yaml` names the seat's model and points at the local bridge with a
dummy key. Changing a seat's model = `hermes -p <profile> model`.
`hermes review-loop models --profile NAME` (or `--seat reviewer --loop ID`) lists, read-only, what
that profile's provider offers in the Hermes model catalog.

**Supported providers, by wire format.** What decides support is the `api_mode` Hermes resolves
the profile to; the inference proxy has one fixed contract per mode (sandbox path, host-chosen
upstream path and headers, forced model, output-token cap in that mode's own field, quota,
streamed answers relayed as they arrive — see `review_loop/inference_proxy.py`):

| `api_mode` | providers (examples) | credential | upstream (host-fixed) | sandbox speaks |
|---|---|---|---|---|
| `chat_completions` | `custom:<name>`, `openrouter`, `deepseek`, other API-key providers | API key (`Bearer`) | `…/chat/completions` | `provider: custom` → `/v1/chat/completions` |
| `chat_completions` | `qwen-oauth`, `nous` (non-`anthropic/*` models) | OAuth (host-refreshed) | `…/chat/completions` | same |
| `codex_responses` | `openai-codex` (ChatGPT subscription), `xai-oauth` | OAuth (host-refreshed) | `…/backend-api/codex/responses`, `…/v1/responses` | named provider `review-loop-seat`, `api_mode: codex_responses` → `/v1/responses` |
| `codex_responses` | API-key providers Hermes routes to Responses (OpenAI, xAI keys) | API key | `…/responses` | same |
| `anthropic_messages` | `anthropic` with a Claude Pro/Max **subscription** token | OAuth (host-refreshed), `Bearer` + Claude Code identity | `https://api.anthropic.com/v1/messages` | `provider: anthropic` at the bridge with a dummy OAuth-shaped token, so Hermes applies the Claude Code system prefix and `mcp__` tool names the subscription requires |
| `anthropic_messages` | `anthropic` with a Console key, `minimax-oauth`, other Messages endpoints | API key (`x-api-key`) or `Bearer`, as Hermes decides | `…/v1/messages` | named provider `review-loop-seat`, `api_mode: anthropic_messages` → `/anthropic/v1/messages` |

Host-chosen headers come from Hermes's own client code, resolved on the host: the Codex
`ChatGPT-Account-ID`/`originator`/`User-Agent` set, Anthropic `anthropic-version`,
`anthropic-beta` (plus `oauth-2025-04-20`, `claude-code/<version>` user agent and `x-app` for a
subscription), the Qwen portal and OpenRouter headers. The sandbox's own `Authorization`,
`x-api-key`, beta, account and user-agent headers are always dropped; only the Responses
session-affinity headers `session_id`/`x-client-request-id` pass through. Caps: 4096 output tokens
per chat completion (over-cap requests refused, as before); 16384 for Responses (refused over
cap; on the ChatGPT Codex backend the field is validated and then **dropped**, because that backend
rejects it — Hermes's own Codex client omits it too — so there the per-turn call quota and the
subscription's own limits bound output); 16384 for Messages, **clamped** (Hermes always asks for the
model's native ceiling, e.g. 64000) with any extended-thinking budget kept below it.

**Not supported**, refused before any credential is read or refreshed: Copilot (token exchange
with its own client headers), Bedrock, Vertex, Azure Foundry and MoA (by name); a profile with
`model.openai_runtime: codex_app_server` (the turn would be a codex subprocess with its own login)
or `model.api_mode: bedrock_converse`; any other provider whose Hermes `auth_type` is not an API
key; and a profile with no `model.provider` (auto-detect). Any other `api_mode` Hermes resolves to
is refused after resolution, before the turn.

**OAuth seats are refreshed on the host, never in the sandbox.** Hermes keeps the refresh token in
the profile's `auth.json` (or Claude Code's / the Codex CLI's own store) under its own `auth.lock`;
review-loop only ever receives the short-lived access token, over the resolver process's pipe,
into the turn's proxy. The proxy re-runs the same isolated resolution when the token is within
60 s of the expiry Hermes states (or, when Hermes states none, the token's own JWT `exp`), and once
after an upstream 401 — asking Hermes to rotate exactly that rejected token — then retries the
request once. Resolutions of one profile are serialized (thread lock + `flock` under
`$HERMES_HOME/state/review-loop-seat-locks/`), so two seats sharing a profile never refresh in
parallel; the second simply reads the token the first refreshed. The sandbox sees only a dummy key.

> **A subscription seat shares its rate limits with you.** A seat on `openai-codex`, a Claude
> subscription, `xai-oauth`, `qwen-oauth` or `nous` draws on the same plan and usage window as your
> own use of that account (and any other seat on the same profile). A busy loop can exhaust it — and
> your own session can starve the loop. A 429 in `selftest` says so.

**Precedence, per seat:**

1. `seats.<seat>` in the runtime file — `{"model", "upstream", "key_file"}` — an explicit
   per-seat override (testing, or a profile whose provider the proxy cannot speak). `upstream` is the
   full HTTPS `…/chat/completions` URL; `key_file` is a private one-line key file. Overrides are
   always chat-completions with a static key; OAuth seats come from profiles.
2. the seat's Hermes profile — the default.
3. the legacy top-level `model` / `upstream` / `key_file` (the pre-#32 seven-key file) — used
   **only** for a seat whose profile cannot be resolved, so existing runtime files keep working.
   `doctor` and `selftest` warn whenever it is present or in effect: every seat that falls back to
   it shares one model and one key.

Otherwise the turn is **held**: the run is marked `failed` before any GitHub read, with the reason
in the ledger (`seat model unresolved: profile default (bedrock): …`), and it never falls back
to another seat's model or key. `doctor` shows each seat's profile → provider / model with the
expected `[api_mode, API key | OAuth (host-refreshed)]` (read-only, no credential lookup);
`selftest` resolves the credential, shows the resolved `[api_mode, auth]`, and makes one tiny
request in that wire format per distinct seat resolution.

## Environment overrides

| variable | effect |
|---|---|
| `HERMES_HOME` | where `.hermes` lives (default `~/.hermes`) |
| `REVIEW_LOOP_CONFIG_DIR` | where loop configs live |
| `REVIEW_LOOP_SUBS` | the gateway subscription file to read/write routes from |
| `REVIEW_LOOP_GH_STUB` | test hook: an executable that answers API paths from argv |
| `REVIEW_LOOP_TEST` | ignore the paused check and apply zero grace (real data, for a manual probe) |

## Deliberate non-features

- **No config_schema for *loop* config.** The interesting configuration is per repository, so a
  per-profile form cannot own it — the form carries the plugin-level defaults (including the seat
  profiles/logins above), and the loop file stays plain JSON you can read and diff. Pushing those
  defaults onto a loop is explicit (`apply --loop <id>`), never a subscription; blank fields never
  erase a loop's own answer.
- **No loop-specific editor in the plugin.** The form is a per-profile default, not a per-repository
  editor: it cannot claim to live-edit every loop. The one-loop-at-a-time path is
  `apply --loop <id>` (same validation, same staged route rebind), and a dashboard editor for a
  single loop would have to be built on top of that path, not beside it.
- **No auto-update, no telemetry, no network beyond GitHub, Discord and your own gateway.**
- **No agent on the observer route, and no observer seat.** The feed's route is `deliver_only` with a
  pre-written prompt — a notice is not an instruction to anybody, it is a message the loop already
  wrote — so it never consumes a seat, holds a lock, or takes a queue slot. The route's job is to
  republish that message; what it cannot do is decide anything.
- **No deploy/hosting integration and no model provider assumptions.** The seats are Hermes profiles;
  what model each profile runs is the operator's business.
