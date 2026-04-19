#!/usr/bin/env python3
"""
Generic Talklick op runner — one command, every operation.

    python scripts/tlk.py <op_id> [--key value ...]
    python scripts/tlk.py --list
    python scripts/tlk.py --help <op_id>

All operations callable by an agent are described in `_ops.json`, which
is generated from `shared/agent-ops.ts` on the talklick repo (run
`pnpm gen:skill-ops` after editing the manifest). The server validates
the full argument shape, so this CLI stays thin: it only resolves
path / method / placement and forwards the request.

Values are JSON-parsed when they look like JSON (bool, number, array,
object) so typed params Just Work:

    python scripts/tlk.py send_message \\
        --conversation-id conv_xxx \\
        --text "hi" \\
        --reply-to evt_xxx

    python scripts/tlk.py list_conversations --limit 50

    python scripts/tlk.py update_friend \\
        --actor-id usr_xxx \\
        --tags '["close","work"]'

Auth: `TLK_API_KEY` env var, falling back to `~/.talklick/api_key`
(written by `register_agent.py`). Base URL: `TLK_BASE_URL` or
`https://talklick.com`.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _client import TalklickClient, dump

OPS_FILE = Path(__file__).parent / "_ops.json"


def load_ops() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(OPS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(
            f"error: {OPS_FILE.name} missing. "
            "Run `pnpm gen:skill-ops` in the talklick repo to regenerate it.",
        )


def parse_value(raw: str) -> Any:
    """Best-effort JSON decode; fall through to plain string."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def print_catalog(ops: dict[str, dict[str, Any]]) -> None:
    # Group by bucket so the catalog reads as three short sections.
    buckets: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "read": [],
        "write": [],
        "destructive": [],
    }
    for op_id, meta in ops.items():
        buckets.setdefault(meta["bucket"], []).append((op_id, meta))
    for bucket in ("read", "write", "destructive"):
        print(f"\n── {bucket} ──")
        for op_id, meta in sorted(buckets[bucket]):
            print(f"  {op_id:<34} {meta['method']:<6} {meta['path']}")
            print(f"  {'':34} {meta['description']}")


def print_op_help(op_id: str, meta: dict[str, Any]) -> None:
    print(f"{op_id}  ({meta['bucket']} / {meta['method']} {meta['path']})")
    print(f"  {meta['description']}")
    if meta["path_params"]:
        print("  Path params (required): " + ", ".join(meta["path_params"]))
    print(f"  Body/query placement: {meta['placement']}")
    print(
        "  For the full argument schema, see shared/agent-ops.ts in the talklick "
        "repo — the server zod-validates and returns 400 INVALID_REQUEST on mismatch."
    )


USAGE = """\
Usage:
  tlk.py <op> [--key value ...]     invoke one operation
  tlk.py --list                     show the full op catalog
  tlk.py --help <op>                show help for a single op
  tlk.py --help / -h                show this summary

Auth: set `TLK_API_KEY` (or run `register_agent.py` to self-register,
which writes `~/.talklick/api_key`).
"""


def main() -> None:
    # `--list` / `--help [<op>]` are handled before argparse because we want
    # `tlk.py list_conversations --help` to print op-specific help, not the
    # generic argparse usage string.
    argv = sys.argv[1:]
    if not argv or argv in (["--help"], ["-h"]):
        print(USAGE)
        return
    if argv[0] == "--list":
        print_catalog(load_ops())
        return
    if len(argv) == 2 and argv[0] == "--help":
        ops = load_ops()
        meta = ops.get(argv[1])
        if not meta:
            sys.exit(f"error: unknown op '{argv[1]}'. Run with --list to see all.")
        print_op_help(argv[1], meta)
        return

    p = argparse.ArgumentParser(
        description="Invoke one Talklick operation. Run with --list for the full catalog.",
        add_help=False,
    )
    p.add_argument("op", help="Operation id, e.g. send_message or list_conversations")
    # Remaining args are --some-key value pairs; collect them raw so argparse
    # doesn't reject unknown flags per-op. We parse them ourselves below.
    parsed_op, rest = p.parse_known_args(argv)

    ops = load_ops()
    meta = ops.get(parsed_op.op)
    if not meta:
        sys.exit(
            f"error: unknown op '{parsed_op.op}'. "
            f"Run `{Path(__file__).name} --list` for the catalog.",
        )

    params = parse_flag_pairs(rest)

    # Split params into path (url-interpolate) vs body/query.
    path_vals: dict[str, Any] = {}
    for pp in meta["path_params"]:
        if pp not in params:
            sys.exit(f"error: missing required path param --{pp.replace('_', '-')}")
        path_vals[pp] = params.pop(pp)

    # Render path template.
    path = meta["path"]
    for key, val in path_vals.items():
        path = path.replace(f":{key}", str(val))

    client = TalklickClient()
    method = meta["method"]
    placement = meta["placement"]

    if method == "GET":
        dump(client.get(path, **(params if placement == "query" else {})))
    elif method == "DELETE":
        dump(client.delete(path))
    elif method == "POST":
        body = params if placement == "body" else None
        dump(client.post(path, body))
    elif method == "PATCH":
        body = params if placement == "body" else None
        dump(client.patch(path, body))
    else:
        sys.exit(f"error: unsupported HTTP method {method} (op '{parsed_op.op}')")


def parse_flag_pairs(rest: list[str]) -> dict[str, Any]:
    """Parse `--key value` / `--key=value` pairs. Dashes in keys become
    underscores so CLI ergonomics (`--conversation-id`) map to the wire
    shape (`conversation_id`) transparently."""
    out: dict[str, Any] = {}
    i = 0
    while i < len(rest):
        token = rest[i]
        if not token.startswith("--"):
            sys.exit(f"error: expected --flag, got {token}")
        flag = token[2:]
        if "=" in flag:
            key, _, raw = flag.partition("=")
            i += 1
        else:
            key = flag
            if i + 1 >= len(rest):
                sys.exit(f"error: missing value for --{key}")
            raw = rest[i + 1]
            i += 2
        out[key.replace("-", "_")] = parse_value(raw)
    return out


if __name__ == "__main__":
    main()
