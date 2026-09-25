#!/usr/bin/env python3
"""Credentialless agent-side client. Launcher mounts only this run's UDS at SOCKET.

Examples: broker_client.py review APPROVE "Reviewed exact head";
          broker_client.py request_review
          broker_client.py ruling ACCEPT "Remaining findings do not block"
No repo, PR, head, branch, URL, identity, token or socket path CLI options exist.
"""
import argparse
import json
import socket
import sys

SOCKET = "/run/review-loop/broker.sock"
MAX_RESPONSE = 16 * 1024
# A push or review is several GitHub calls plus git fetch/push (each up to 90s), all
# host-side. Wait for the answer instead of timing out mid-write; the turn deadline is
# the real bound.
WRITE_TIMEOUT = 900


def request(operation: str, verdict: str = "", body: str = "") -> dict:
    payload = json.dumps({"operation": operation, "verdict": verdict, "body": body},
                         separators=(",", ":")).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(WRITE_TIMEOUT)
        conn.connect(SOCKET)
        conn.sendall(payload)
        chunks = bytearray()
        while len(chunks) <= MAX_RESPONSE:
            part = conn.recv(min(4096, MAX_RESPONSE + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
            if b"\n" in part:
                break
    if len(chunks) > MAX_RESPONSE or not chunks.endswith(b"\n") or chunks.count(b"\n") != 1:
        raise ValueError("invalid broker response")
    result = json.loads(chunks)
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("invalid broker response")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("review", "request_review", "ruling"))
    parser.add_argument("verdict", nargs="?", default="")
    parser.add_argument("body", nargs="?", default="")
    args = parser.parse_args()
    if args.operation == "review" and (not args.verdict or not args.body):
        parser.error("review requires verdict and body")
    if args.operation == "ruling" and (args.verdict not in ("ACCEPT", "REJECT", "RESPEC")
                                       or not args.body):
        parser.error("ruling requires ACCEPT|REJECT|RESPEC and a reason")
    if args.operation == "request_review" and (args.verdict or args.body):
        parser.error("request_review takes no extra fields")
    try:
        response = request(args.operation, args.verdict, args.body)
    except TimeoutError:
        print("broker response timed out: write outcome unknown; do not retry or report success", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"broker unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(response, sort_keys=True))
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
