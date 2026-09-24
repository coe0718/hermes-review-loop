"""Hermes plugin entry point.

Registers two things and nothing else:

* the ``hermes review-loop`` CLI (``init`` / ``list`` / ``status`` / ``explain`` / ``doctor`` / ``arm`` /
  ``pause`` / ``drain`` / ``cleanup`` / ``uninstall``);
* the loop's skill, so the reviewer and fixer agents can load the protocol the prompts refer
  to and know what "round 2 of 3" obliges them to do.

No hooks, no middleware, no tools. Everything else this plugin needs — the webhook routes, the
GitHub hooks, the cron watchdog — is written through the operator-visible config surfaces by
the CLI, so there is nothing hidden and nothing to unwind by hand.
"""

from __future__ import annotations

import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:          # the gates import this package from scripts/
    sys.path.insert(0, str(_HERE))

from review_loop import cli, config  # noqa: E402


def _settings(ctx) -> dict:
    """What the operator set in the plugin's settings form (Capabilities → Plugins).

    Read through the documented context accessor, never the config file directly, and never
    fatally: a plugin that fails to register because one setting was misspelled is a plugin the
    operator cannot reach to fix it.
    """
    out: dict = {}
    for key in config.SETTINGS_SCHEMA:
        try:
            value = ctx.get_config(key, None)
        except Exception:
            continue
        if value is not None and value != "":
            out[key] = value
    return out


def register(ctx) -> None:  # noqa: ANN001 - PluginContext, untyped by design here
    cli.register_cli(ctx, settings=_settings(ctx))
    skill_md = _HERE / "skill" / "SKILL.md"
    if skill_md.exists():
        ctx.register_skill(
            "review-loop",
            skill_md,
            description="Protocol for an agent working a review loop: verify before you verdict, "
                        "re-request review after every push, and respect the round budget.",
        )
