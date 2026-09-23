"""Hermes review loop — deterministic control plane for unattended agent PR review.

Two agents, one repository: a **fixer** pushes work and asks for review, a **reviewer**
returns a verdict, the fixer answers it, and the budget is counted in verdicts rather
than in wall-clock time. Everything in between — who runs, how many rounds are left,
whether a run is already out, whether the loop has gone quiet — is decided by small
deterministic scripts, never by a model.

Nothing here bakes in a repository, an account or a budget: those live in one JSON file
per loop (see `review_loop.config`), so a single install can drive several repositories
with different seats, caps and credentials.

Why it exists: an unattended loop fails *quietly*. A dropped event, a stalled run, a
rate limit or a review that keeps flipping between two states all look like "no news",
and a loop that stopped being driven looks exactly like a loop with nothing to do. So
every step is a script that either fires or says why not, and a watchdog reads GitHub
state directly rather than trusting any announcement.
"""

__version__ = "0.1.0"
