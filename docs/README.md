# Documentation

Start with the [project README](../README.md) for the pitch, the install and the first-run steps.

| page | what it covers |
|---|---|
| [operations.md](operations.md) | what `init` writes, everyday commands, the `doctor` preflight, `selftest`, `explain` ("why isn't this PR moving?"), burst handling |
| [settings.md](settings.md) | the desktop settings form, seat identity defaults, `settings` / `apply` |
| [observer.md](observer.md) | the observer feed: notices to your phone, how to turn it on, its rules |
| [configuration.md](configuration.md) | reference for every loop-config key, the observer block, adjudication, plugin settings, seat identity, state files, environment overrides |
| [architecture.md](architecture.md) | design: the seats, isolation, escalation, the watchdog, `explain`, the observer feed, preflight |
| [issue-16-boundary.md](issue-16-boundary.md) | the isolated route-to-agent boundary (issue #16), selftest no-write guarantees, remaining blockers |
| [issue-1-route-self-heal.md](issue-1-route-self-heal.md) | the webhook-registry race (issue #1): intent record, self-heal, conflict-checked writes, the remaining window |
| [stacked-submission-boundary.md](stacked-submission-boundary.md) | the stacked reviewer submission boundary (not enabled) |
