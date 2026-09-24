"""Webhook routes: read the gateway's subscription file, and fire a signed POST at it.

The loop does not own the gateway, so it does not invent a second way to reach agents. It
writes routes through the same config file the gateway already reads (``new_route``) and
wakes a seat by POSTing a GitHub-shaped payload with a valid signature (``fire``).

A route's URL is derived from its ``profile``: the gateway serves the launch profile at
``/webhooks/<name>`` and every other profile at ``/p/<profile>/webhooks/<name>``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import time
import urllib.request

from . import config
from .util import log


def subs_path() -> pathlib.Path:
    override = os.environ.get("REVIEW_LOOP_SUBS")
    return pathlib.Path(override).expanduser() if override else config.home() / "webhook_subscriptions.json"


def all_routes() -> dict:
    try:
        return json.loads(subs_path().read_text())
    except Exception:
        return {}


def route(name: str) -> dict | None:
    entry = all_routes().get(name)
    return entry if isinstance(entry, dict) else None


def url_for(name: str, host: str | None = None) -> str | None:
    entry = route(name)
    if not entry:
        return None
    # Never invent a relative webhook URL when neither the caller nor the route
    # names an operator-owned gateway. Reject malformed origins at this boundary.
    base = config.webhook_host(host or entry.get("host"))
    if not base:
        return None
    profile = entry.get("profile", "default")
    if profile == "default":
        return f"{base}/webhooks/{name}"
    return f"{base}/p/{profile}/webhooks/{name}"


def target(name: str, host: str | None = None):
    """(url, secret_bytes) for a route, or None when it is missing or has no secret."""
    entry = route(name)
    if not entry:
        log(f"route {name!r} not found in {subs_path().name}")
        return None
    secret = entry.get("secret") or ""
    try:
        url = url_for(name, host)
    except config.ConfigError as exc:
        log(f"route {name!r} has invalid webhook host: {exc}")
        return None
    if not secret or not url:
        log(f"route {name!r} has no secret/url")
        return None
    return url, secret.encode()


def fire(name: str, event: str, payload: dict, tag: str, host: str | None = None) -> bool:
    """POST a signed payload at a route. Returns True only on an HTTP 2xx."""
    target_ = target(name, host)
    if not target_:
        return False
    url, secret = target_
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest(),
        "X-GitHub-Delivery": f"{tag}-{int(time.time())}",
        "User-Agent": "hermes-review-loop",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            log(f"fired {name} for {tag} (HTTP {resp.status})")
            return 200 <= resp.status < 300
    except Exception as exc:
        log(f"could not fire {name} for {tag}: {exc}")
        return False


def new_route(name: str, *, profile: str, prompt: str, events: list[str], script: str,
               deliver: str, description: str = "", skills: list[str] | None = None,
               host: str | None = None) -> dict:
    """Create (or update) a route entry and write it back to the gateway's file.

    The secret is generated here, not asked for. 0600, same file the gateway reads.
    """
    import secrets as _secrets

    path = subs_path()
    data = all_routes()
    prior = data.get(name) or {}
    entry = {
        "description": description or prior.get("description", ""),
        "events": list(events),
        "secret": prior.get("secret") or _secrets.token_hex(32),
        "prompt": prompt,
        "skills": list(skills or prior.get("skills") or []),
        "deliver": deliver,
        "profile": profile,
        "created_at": prior.get("created_at") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "script": script,
    }
    if host:
        entry["host"] = host
    data[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    os.chmod(path, 0o600)
    return entry


def remove_route(name: str) -> bool:
    data = all_routes()
    if data.pop(name, None) is None:
        return False
    subs_path().write_text(json.dumps(data, indent=2))
    os.chmod(subs_path(), 0o600)
    return True
