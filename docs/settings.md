# Settings, in the desktop

The plugin-level settings form, and how its defaults reach a loop. The key-by-key mapping onto the
loop file is in the configuration reference under
[Plugin settings](configuration.md#plugin-settings-the-desktop-form) and
[Seat identity](configuration.md#seat-identity-who-serves-each-seat).

*Who* serves each seat is the settings form's business, not `set`'s: a per-profile form holds
the defaults, and `apply --loop` pushes them onto exactly one loop — with the seat diff, the route
profiles it rebinds and the credentials it checked. For one repository that needs a shape no form
should own (a different allowlist, its own route names), the loop file is still plain JSON you can
read and diff; `init` is the only verb that writes routes from scratch.

The plugin declares a `config_schema`, so it has a settings form at
**Capabilities → Plugins → review loop** — no hand-edited JSON required:

| setting | default | what it does |
|---|---|---|
| `cap` | 3 | verdicts before the loop stops and hands the PR to an adjudicator |
| `reviewer_concurrency` | 1 | reviews at once; the rest queue |
| `fixer_concurrency` | 1 | fixes at once; the rest queue |
| `reviewer_profile` | — | Hermes profile the reviewer seat runs as |
| `fixer_profile` | — | Hermes profile the fixer seat runs as |
| `reviewer_login` | — | GitHub login the reviewer acts as (and the login the review route serves) |
| `fixer_login` | — | GitHub login the fixer acts as |
| `adjudicator_profile` | — | Hermes profile that rules when the budget is spent (optional) |
| `clone` | — | the local clone runs isolate from (required above 1) |
| `base` | main | base branch the loop watches |
| `grace_min` | 25 | quiet minutes before the watchdog speaks |
| `ttl_min` | 45 | how long a seat slot survives a run that died without a verdict |
| `inflight_ttl_min` | 10 | how long a mark blocks a second run at the same head |
| `host` | unset | your gateway's webhook origin; required for `init`, or supply `--host` |

The form shows friendly labels (`Reviews at once`, `Reviewer's Hermes profile`, `Clone path
(required above 1)`); the keys in the table are what `hermes review-loop settings` prints and what
the loop file holds.

**Blank means *not set here*, never "unset what the loop has".** A blank profile or login leaves
that seat exactly as the loop file has it, which is what lets one profile-level form hold defaults
without silently rewriting the seats a repository already answered for itself. Profiles and logins
are validated before anything is written — the profile must exist on this machine, the login must be
in that loop's allowlist, the two seats must not share a profile, a login or a token file, and every
token the loop names must be a file that is there. A form that names a seat is a promise that the
seat can run, so `init` and `apply` refuse rather than write a loop that fails at its first event.

Two rules, because a settings form that quietly renumbers a running loop is a miserable thing to
debug at 2am:

* settings are **defaults for a new loop**, and
* they reach an existing loop only when you push them: `hermes review-loop apply --loop <id>`,
  which prints the diff first (`--dry-run` to stop there).

Changing *who* serves a seat is staged rather than half-applied: the routes whose URLs carry the old
profile are rebound first, the loop config is committed second, and every rebind is verified by
reading the route registry back. A registry that refuses the rebind leaves the loop exactly as it
was — there is no config to put back. A seat with a run in flight is refused rather than rewritten
underneath itself — the live run holds its old profile, login and credential until it ends — and
`--while-busy` is the explicit override that says "rebind now, I know that run finishes under the
identity it started with". `apply --dry-run` shows all of it without writing: the seat diff, the
route profiles it would rebind, and nothing else.

`apply` is also the upgrade path for a loop installed before the dedicated adjudicator gate: its
breach route still runs `gate_reviewer.py`, `doctor` flags it, and `apply --loop <id>` rebinds that
route to `gate_adjudicator.py` in place, secret kept. Only a script this plugin itself once
installed for the route (with its own prompt) is rewritten; anything else is left alone.

Preview a loop before installing it with `hermes review-loop init ... --dry-run`: it prints the
effective seat mapping (profile, login, route, and the URL the profile is part of) and stops
without writing config, routes, hooks or cron.

```bash
hermes review-loop settings                      # the form's seat mapping, and what each loop runs as today
hermes review-loop init --repo owner/name --dry-run   # preview: seats, routes, nothing written
hermes review-loop apply --loop name --dry-run   # what a push would change, including routes
hermes review-loop status --loop name            # each seat, its profile, and whether its route agrees
```

`status` is the honest surface: it prints what the *installed* route serves next to what the config
claims, and says `MISMATCH` with the command to fix it when a seat moved but its route did not.
Token *references* are shown (which login reads which file); token values never are — they live in
the per-profile 0600 file the seats read at use time.

The form stays a per-profile default on purpose. Fixer/reviewer **allowlists**, route names, the
adjudicator route and everything else that is per repository stay in the loop config, because one
form cannot honestly claim to own every repository. Pushing the form onto one loop with
`apply --loop` is the loop-specific editor: it goes through the same validation and the same staged
apply, and it never touches a loop you did not name.

`hermes review-loop settings` prints the same table from the CLI with `[set]` / `[default]` beside
each value — and the seat mapping underneath it — so you can tell what the form actually holds,
and what each configured loop runs as today, without opening either. Settings follow the
**profile** they were saved in, and the rails still apply: a concurrency above 1 with no clone is
refused at `init`, at `set` and at `apply` alike.

The settings form and the loop config are different surfaces on purpose: the form is per profile and
holds defaults, the loop file is per repository and holds the truth. Point a seat at this plugin's
own skill by its qualified name — `--skill hermes-review-loop:review-loop` — since plugin skills are
never copied into `~/.hermes/skills/`.