"""GitHub REST access — stdlib only, token read from a file the operator controls.

Two deliberate choices:

* **No ``gh`` CLI dependency.** The gates run inside the gateway, where a PATH or a
  keyring is not guaranteed. A token file plus ``urllib`` works everywhere and has no
  login state to expire.
* **One token per seat.** Each seat's token lives in its own file (mode 600) and is named
  in the loop config, so the credential that pushes, the credential that reviews and the
  credential that reads are separate and revocable one at a time.

Test hook: set ``REVIEW_LOOP_GH_STUB`` to an executable that takes the API path as argv[1]
and prints a JSON response. That is how the test suite exercises the gates without a
network or a real repository.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request

from .util import log

API = "https://api.github.com"


class GitHubError(Exception):
    pass


def token_path(loop: dict, login: str | None = None) -> pathlib.Path | None:
    name = login or loop.get("read_token")
    raw = (loop.get("tokens") or {}).get(name)
    return pathlib.Path(str(raw)).expanduser() if raw else None


def token(loop: dict, login: str | None = None) -> str:
    path = token_path(loop, login)
    if not path or not path.exists():
        raise GitHubError(f"no token file for {login or loop.get('read_token')!r} "
                          f"(checked {path})")
    return path.read_text().strip()


def _stub(path: str, method: str, body) -> object | None:
    stub = os.environ.get("REVIEW_LOOP_GH_STUB")
    if not stub:
        return None
    argv = [stub, path] if not body else [stub, path, json.dumps(body)]
    env = {**os.environ, "GH_METHOD": method}
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    except Exception as exc:
        log(f"gh stub failed: {exc}")
        return None
    if proc.returncode != 0:
        log(f"gh stub rc={proc.returncode}: {proc.stderr.strip()[:120]}")
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def api(loop: dict, path: str, method: str = "GET", body=None, login: str | None = None):
    """One REST call. Returns parsed JSON, or None when the call did not succeed.

    Callers are expected to treat None as "unknown" and stay quiet: a loop that cannot
    read the review list must not guess how many rounds are left.
    """
    stub = _stub(path, method, body)
    if stub is not None or os.environ.get("REVIEW_LOOP_GH_STUB"):
        return stub
    try:
        tok = token(loop, login)
    except GitHubError as exc:
        log(str(exc))
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Accept": "application/vnd.github+json", "Authorization": f"token {tok}",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hermes-review-loop"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode() or "null"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:120]
        log(f"gh {method} {path} -> HTTP {exc.code}: {detail}")
    except Exception as exc:
        log(f"gh {method} {path} failed: {exc}")
    return None


# -- convenience --------------------------------------------------------------


def pr(loop: dict, number: int):
    return api(loop, f"/repos/{loop['repo']}/pulls/{number}")


def reviews(loop: dict, number: int):
    return api(loop, f"/repos/{loop['repo']}/pulls/{number}/reviews?per_page=100")


def open_prs(loop: dict):
    return api(loop, f"/repos/{loop['repo']}/pulls?state=open&per_page=100")



def request_review(loop: dict, number: int, login: str | None = None, as_login: str | None = None):
    """Ask for a review explicitly.

    GitHub clears a pending review request the moment a review is submitted, so the fixer
    must re-ask after every push — this call is what keeps the loop turning.
    """
    seat = login or loop["reviewer_seat"]
    return api(loop, f"/repos/{loop['repo']}/pulls/{number}/requested_reviewers",
               method="POST", body={"reviewers": [seat]}, login=as_login)


def review_state(review: dict) -> str:
    """Webhook payloads spell review states lowercase; the REST API shouts them.

    Comparing case-insensitively on both sides of that boundary is not paranoia: the first
    version of this loop had a fixer leg that was dead on arrival because of exactly this.
    """
    return str((review or {}).get("state", "")).upper()


def gh_cli_available() -> bool:
    return shutil.which("gh") is not None
