"""One-run, credential-owning Unix socket broker for review-loop REST writes.

The trusted launcher constructs RunScope from gate-verified data, starts this server
outside the agent's mount namespace, then bind-mounts ONLY its socket (or its private
socket directory) at /run/review-loop/broker.sock inside that namespace. Do not mount
this module's host config, state, token files or socket parent into the agent. The
socket is a bearer capability: namespace isolation and a per-run, unguessable path,
not SO_PEERCRED (the agent may share the host UID), provide authentication. Never
share this socket between runs; close it when the run ends.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import stat
import threading
from dataclasses import dataclass

from . import broker, config, safe_push

MAX_REQUEST = 16 * 1024
MAX_BODY = 12 * 1024
MAX_PUSH_REQUEST = 196 * 1024


@dataclass(frozen=True)
class RunScope:
    repo: str
    number: int
    head: str
    role: str
    branch: str
    run_id: str | None = None
    ledger_db: str | None = None
    generation: str | None = None


class ProtocolError(Exception):
    """Malformed or out-of-scope request, without leaking host details."""


def _read_line(conn: socket.socket, limit: int) -> bytes:
    data = bytearray()
    while len(data) <= limit:
        part = conn.recv(min(4096, limit + 1 - len(data)))
        if not part:
            raise ProtocolError("incomplete request")
        data.extend(part)
        if b"\n" in part:
            line, extra = bytes(data).split(b"\n", 1)
            if extra or len(line) > limit:
                raise ProtocolError("invalid frame")
            return line
    raise ProtocolError("request too large")


class RunBroker:
    """Single-threaded server: reviewer writes once; fixer may push then request review."""

    def __init__(self, loop: dict, scope: RunScope, directory: str | Path,
                 *, require_push: bool = False, require_receipt: bool = False):
        self._loop = loop
        self.require_push = require_push
        self.require_receipt = require_receipt
        self.scope = scope
        self.directory = Path(directory)
        self.socket_path: Path | None = None
        self._listener: socket.socket | None = None
        self._used = False
        self.completed = False
        self._pushed_head: str | None = None
        self._stop = threading.Event()

    def __enter__(self) -> "RunBroker":
        root = self.directory
        info = root.lstat()  # never follow a symlink in a caller-provided root
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ProtocolError("broker root must be a private owned directory")
        for _ in range(8):
            candidate = root / ("run-" + secrets.token_hex(16))
            try:
                candidate.mkdir(mode=0o700)
                break
            except FileExistsError:
                continue
        else:
            raise ProtocolError("cannot allocate run socket")
        self.socket_path = candidate / "broker.sock"
        try:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._listener = listener
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(4)
            listener.settimeout(0.2)
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        if self.socket_path is not None:
            self.socket_path.unlink(missing_ok=True)
            self.socket_path.parent.rmdir()
            self.socket_path = None

    def __exit__(self, *_: object) -> None:
        self.close()

    def serve(self) -> None:
        if self._listener is None:
            raise RuntimeError("broker not started")
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                raise
            with conn:
                conn.settimeout(5)
                try:
                    self._dispatch(_read_line(conn, MAX_PUSH_REQUEST if self.scope.role == "fixer" else MAX_REQUEST))
                    # Never relay arbitrary GitHub response fields into the namespace.
                    response = {"ok": True, "result": {"accepted": True}}
                except (ProtocolError, broker.BrokerDenied, ValueError, UnicodeError, TimeoutError) as exc:
                    response = {"ok": False, "error": str(exc) if isinstance(exc, ProtocolError) else "write denied"}
                except Exception:
                    # A GitHub, filesystem, or audit failure cannot expose paths or credentials.
                    response = {"ok": False, "error": "write failed"}
                try:
                    conn.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
                except OSError:
                    pass

    def _dispatch(self, raw: bytes) -> object:
        request = json.loads(raw)
        # The adjudicator has exactly one operation, and only the adjudicator has it. Decide
        # that before any other branch so neither side can reach the other's write paths.
        if self.scope.role == "adjudicator" or (isinstance(request, dict)
                                                and request.get("operation") == "ruling"):
            return self._ruling(raw, request)
        if isinstance(request, dict) and request.get("operation") == "push":
            if set(request) != {"operation", "manifest"} or self.scope.role != "fixer":
                raise ProtocolError("operation out of scope")
            if self._used:
                raise ProtocolError("run capability already used")
            # The socket request and launch-time loop snapshot are not policy sources.
            # Reload the host-owned repository configuration at the write boundary.
            current_loop = config.by_repo(self.scope.repo)
            if current_loop is None or not config.unattended_fixer_push_enabled(current_loop):
                raise ProtocolError("unattended fixer push is not enabled by the host operator")
            if (self._loop.get("repo") != current_loop.get("repo")
                    or self._loop.get("id") != current_loop.get("id")
                    or self._loop.get("state_dir") != current_loop.get("state_dir")):
                raise ProtocolError("run configuration changed")
            # Validation precedes consuming the capability, but no write can be replayed.
            safe_push._manifest(request["manifest"])
            self._used = True
            try:
                # Lock covers the final host policy read and the complete ref operation.
                # Disable cannot return while an authorized push is still in progress.
                with config.push_policy_lock():
                    current_loop = config.by_repo(self.scope.repo)
                    if (current_loop is None or
                            not config.unattended_fixer_push_enabled(current_loop) or
                            current_loop.get('id') != self._loop.get('id') or
                            current_loop.get('state_dir') != self._loop.get('state_dir')):
                        raise ProtocolError('unattended fixer push policy changed before write')
                    # A newly enabled config must not authorize a worker that
                    # was admitted while the policy was off (or a legacy row).
                    if not self.scope.run_id or not self.scope.ledger_db:
                        raise ProtocolError('host run admission unavailable')
                    from .run_supervisor import Supervisor
                    supervisor = Supervisor(self.scope.ledger_db)
                    if not supervisor.push_admitted(
                            self.scope.run_id, self.scope.repo, self.scope.number,
                            self.scope.head):
                        raise ProtocolError('fixer push not authorized at run admission')
                    # Durable write-ahead intent precedes the external Git operation.
                    # If this commit fails, safe_push (and Git) are never called.
                    supervisor.begin_push(self.scope.run_id, self.scope.repo,
                                          self.scope.number, self.scope.head)
                    try:
                        result = safe_push.push(current_loop, repo=self.scope.repo,
                                                number=self.scope.number, head=self.scope.head,
                                                role=self.scope.role, branch=self.scope.branch,
                                                manifest=request["manifest"])
                        # safe_push returns only after exact ref + PR readback and
                        # durable audit. Failure to commit completion leaves intent.
                        supervisor.confirm_push(self.scope.run_id, self.scope.repo,
                                                self.scope.number, self.scope.head)
                    except Exception as exc:
                        # An explicit attempt boundary, not exception text, determines
                        # whether a failed push must occupy the host-side hold.
                        if isinstance(exc, safe_push.PushFailure) or not isinstance(
                                exc, (broker.BrokerDenied, ProtocolError)):
                            if not self.scope.run_id or not self.scope.ledger_db:
                                raise ProtocolError('post-write hold has no host ledger') from exc
                            from .run_supervisor import Supervisor
                            outcome = (exc.outcome if isinstance(exc, safe_push.PushFailure)
                                       and exc.outcome == 'published_pr_unverified' else 'unknown')
                            try:
                                Supervisor(self.scope.ledger_db).quarantine_push(
                                    self.scope.run_id, self.scope.repo, self.scope.number,
                                    self.scope.head, outcome)
                            except Exception as persistence_error:
                                raise ProtocolError('post-write quarantine persistence failed') from persistence_error
                        raise
            except (broker.BrokerDenied, ProtocolError):
                raise
            except Exception:
                # Failure to obtain the policy lock or reload configuration is
                # pre-write; the capability was consumed but Git was not called.
                raise
            self._pushed_head = result["new_head"]
            return result
        if not isinstance(request, dict) or set(request) != {"operation", "verdict", "body"}:
            raise ProtocolError("unsupported request fields")
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("request too large")
        operation, verdict, body = (request[key] for key in ("operation", "verdict", "body"))
        expected = {"reviewer": "review", "fixer": "request_review"}.get(self.scope.role)
        if operation != expected or expected is None:
            raise ProtocolError("operation out of scope")
        if operation == "request_review" and self.require_push and not self._pushed_head:
            raise ProtocolError("fixer must publish a confirmed push first")
        if not isinstance(verdict, str) or not isinstance(body, str) or len(body.encode()) > MAX_BODY:
            raise ProtocolError("invalid review fields")
        if self._used and not (operation == "request_review" and self._pushed_head):
            raise ProtocolError("run capability already used")
        # Consume BEFORE an external write: a lost response cannot lead to a replay.
        self._used = True
        after_push = operation == "request_review" and bool(self._pushed_head)
        head = self._pushed_head if after_push else self.scope.head
        self._pushed_head = None
        if operation == 'review' and self.scope.run_id is not None:
            from .review_receipt import ReceiptLedger, submit
            ledger = ReceiptLedger(self.scope.ledger_db, self.scope.run_id,
                                   self.scope.generation)
            result = submit(self._loop, self.scope, ledger, verdict, body)
        else:
            if operation == 'review' and self.require_receipt:
                raise ProtocolError('host review claim required')
            result = broker.perform(self._loop, repo=self.scope.repo, number=self.scope.number,
                                    head=head, role=self.scope.role, branch=self.scope.branch,
                                    operation=operation, verdict=verdict, body=body,
                                    require_verdict=not after_push)
        self.completed = True
        return result


    def _ruling(self, raw: bytes, request: object) -> object:
        """Record the one ruling, then tell the operator, then (optionally) the PR.

        The response is ok as soon as the ruling is durable in the host run ledger; the notice
        and the comment are host deliveries of an already recorded fact, so their failures are
        recorded there too and never turned into a sandbox-visible error or retry.
        """
        if (self.scope.role != "adjudicator" or not isinstance(request, dict)
                or set(request) != {"operation", "verdict", "body"}
                or request["operation"] != "ruling"):
            raise ProtocolError("operation out of scope")
        if len(raw) > MAX_REQUEST:
            raise ProtocolError("request too large")
        verdict, body = request["verdict"], request["body"]
        from .run_supervisor import RULINGS, Supervisor
        if (verdict not in RULINGS or not isinstance(body, str) or not body.strip()
                or len(body.encode()) > MAX_BODY):
            raise ProtocolError("invalid ruling fields")
        if self._used:
            raise ProtocolError("run capability already used")
        if not self.scope.run_id or not self.scope.ledger_db:
            raise ProtocolError("host run ledger unavailable")
        # Consume BEFORE the ledger write: a lost response cannot lead to a second ruling.
        self._used = True
        supervisor = Supervisor(self.scope.ledger_db)
        recorded = supervisor.record_ruling(self.scope.run_id, self.scope.repo,
                                            self.scope.number, self.scope.head, verdict, body)
        self.completed = True
        try:
            _deliver_ruling(self._loop, self.scope, supervisor, recorded["turn_key"], verdict, body)
        except Exception:
            # Already durable, and the operator outbox reports it regardless.
            pass
        return {"accepted": True}


def _deliver_ruling(launch_loop: dict, scope: RunScope, supervisor, turn_key: str,
                    verdict: str, body: str) -> None:
    """Observer notice (best effort), then the PR comment only for a configured identity."""
    from . import observer, state as state_mod
    # Delivery follows the host's current configuration, not the launch snapshot: an identity
    # the operator removed since launch must not comment. A vanished or re-pointed loop gets
    # neither delivery; the outbox (run ledger) still carries the ruling.
    try:
        loop = config.by_repo(scope.repo)
    except Exception:
        loop = None
    if (loop is None or loop.get("id") != launch_loop.get("id")
            or loop.get("state_dir") != launch_loop.get("state_dir")):
        supervisor.ruling_status(scope.run_id, observer="skipped", comment="denied",
                                 comment_error="loop configuration changed")
        return
    rounds = turn_key.split(":", 1)[1] if turn_key.startswith("breach:") else "?"
    # The feed carries the fact (verdict, counts), never the model's reason text — that goes
    # to the operator outbox and, when configured, the PR. See observer.notify's contract.
    sent = observer.notify(loop, state_mod.state_for(loop), "ruling", scope.number, scope.head,
                           identity=turn_key, outcome=f"{verdict} · {rounds}/{loop['cap']} verdicts",
                           next_turn="you — the adjudicator never merges")
    supervisor.ruling_status(scope.run_id, observer="sent" if sent else "unsent")
    if not config.adjudicator_login(loop):
        supervisor.ruling_status(scope.run_id, comment="none")
        return
    try:
        login = broker.authorize_ruling_comment(loop, repo=scope.repo, number=scope.number,
                                                head=scope.head, branch=scope.branch)
    except broker.BrokerDenied as exc:
        supervisor.ruling_status(scope.run_id, comment="denied", comment_error=str(exc)[:200])
        return
    except Exception as exc:
        supervisor.ruling_status(scope.run_id, comment="denied",
                                 comment_error=f"authorization failed: {type(exc).__name__}")
        return
    # Durable intent before the POST: a crash after it is reported as uncertain, never replayed.
    supervisor.ruling_status(scope.run_id, comment="posting")
    try:
        comment_id = broker.post_ruling_comment(
            loop, repo=scope.repo, number=scope.number, head=scope.head, branch=scope.branch,
            login=login, text=broker.ruling_comment_body(verdict, body, head=scope.head,
                                                          turn_key=turn_key, run_id=scope.run_id,
                                                          cap=loop["cap"]))
    except Exception as exc:
        supervisor.ruling_status(scope.run_id, comment="uncertain",
                                 comment_error=f"POST outcome unknown: {type(exc).__name__}")
        return
    supervisor.ruling_status(scope.run_id, comment="posted", comment_id=comment_id)


def serve_in_thread(server: RunBroker) -> threading.Thread:
    """Start an entered server; caller owns its lifetime and must join on shutdown."""
    thread = threading.Thread(target=server.serve, daemon=True)
    thread.start()
    return thread


# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900


def request(operation: str, *, verdict: str = "", body: str = "",
            manifest: object = None,
            socket_path: str = "/run/review-loop/broker/broker.sock") -> dict:
    """Credentialless in-namespace caller; never accepts a target repo or token."""
    if operation == "push":
        payload = {"operation": operation, "manifest": manifest}
    elif operation in ("review", "request_review", "ruling"):
        payload = {"operation": operation, "verdict": verdict, "body": body}
    else:
        raise ProtocolError("unsupported operation")
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if len(raw) > MAX_PUSH_REQUEST:
        raise ProtocolError("request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(WRITE_TIMEOUT)
        conn.connect(socket_path)
        conn.sendall(raw + b"\n")
        answer = _read_line(conn, MAX_REQUEST)
    result = json.loads(answer)
    if not isinstance(result, dict) or set(result) not in ({"ok", "result"}, {"ok", "error"}):
        raise ProtocolError("invalid broker response")
    return result


def main() -> None:
    """Small CLI for a sandboxed Hermes terminal tool call."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("review", "request_review", "push", "ruling"))
    parser.add_argument("--verdict", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--manifest-file")
    args = parser.parse_args()
    if args.operation == "push":
        if args.body_file or not args.manifest_file:
            parser.error("push requires --manifest-file only")
        path = Path(args.manifest_file)
        if path.stat().st_size > MAX_PUSH_REQUEST:
            parser.error("manifest too large")
        result = request("push", manifest=json.loads(path.read_text()))
    else:
        if args.operation == "ruling" and (args.verdict not in ("ACCEPT", "REJECT", "RESPEC")
                                           or not args.body_file):
            parser.error("ruling requires --verdict ACCEPT|REJECT|RESPEC and --body-file")
        if args.manifest_file or (args.operation == "review" and not args.body_file):
            parser.error("invalid review arguments")
        body = Path(args.body_file).read_text() if args.body_file else ""
        if len(body.encode()) > MAX_BODY:
            parser.error("review body too large")
        result = request(args.operation, verdict=args.verdict, body=body)
    print(json.dumps(result))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
