# Issue #1: route registry self-heal (a mitigation, not a fix)

**Problem.** The gateway's `webhook_subscriptions.json` is written by this plugin (under its own
`flock`, atomically, fail-closed on malformed JSON) *and* by Hermes's CLI and dashboard, which do
not take that lock. A native read-modify-write racing a plugin one can erase or rewrite the
loop's routes — including their HMAC secrets, after which GitHub's hook can no longer
authenticate — and a plugin write could overwrite a concurrent native change. The real fix is
upstream (NousResearch/hermes-agent#120964: every writer shares the lock for the whole
transaction). This issue stays open until that ships and the installed Hermes is verified.

**What the plugin now does.**

| mechanism | where | effect |
|---|---|---|
| optimistic write | `review_loop/routes.py` (`_transact`) | identity (inode, mtime_ns, size, sha256) recorded at read and re-checked just before `os.replace`; a change means re-read and re-apply (5 attempts), so a native write that lands during a plugin edit is preserved |
| intent record | `<state_dir>/route-intent.json` (`review_loop/route_intent.py`) | the plugin's private 0600 copy of every route it owns, secret included; updated by `init`/`apply`/`set`, forgotten by `uninstall` and observer renames |
| self-heal | every armed watchdog sweep; `doctor --repair` | missing or drifted routes (secret, script, prompt, events, profile, `deliver_only`, host) restored with the same secret, and the cron output says what was restored |
| report | `doctor` (read-only) | `route:<name>` turns ❌ when the live route differs from the record |

Never touched: routes not in this loop's config, routes whose name another writer now uses for a
non-review-loop script (reported instead), and a malformed registry (alert only — fail closed).

**What remains (honestly).**

* *The last-check-to-rename gap.* Between the final identity check and `os.replace` there are a
  few syscalls. A native write landing exactly there is overwritten by the plugin's publish.
* *Native writers that read early.* A native writer that read the registry before a plugin
  publish and writes after it overwrites the plugin's edit — optimistic checks on our side cannot
  see its stale read. The plugin's routes come back on the next armed sweep; the native writer's
  own edit is whatever it wrote.
* *Between sweeps.* Until the next armed watchdog sweep (or `doctor --repair`), a damaged route
  can miss deliveries. A paused loop (hooks inactive) does not heal.
* *Intentional native edits are reverted.* Changing this loop's routes in the Hermes dashboard
  is indistinguishable from the race; make route changes through `hermes review-loop set/apply`
  or remove them with `uninstall`.
