#!/usr/bin/env python3
"""Hybrid PoC hook: communicates via JSONL files for permission round-trip."""
from __future__ import annotations
import json, os, sys, time

IN_FILE = "/tmp/poc_hook_in.jsonl"
OUT_FILE = "/tmp/poc_hook_out.jsonl"


def main():
    data = json.load(sys.stdin)
    tool_name = data.get("tool_name", "")
    session_id = data.get("session_id", "")

    # Append request to in-file
    with open(IN_FILE, "a") as f:
        f.write(json.dumps({"tool_name": tool_name, "session_id": session_id,
                            "timestamp": time.time()}, ensure_ascii=False) + "\n")

    # Poll out-file for a decision (max 10s)
    deadline = time.time() + 10
    while time.time() < deadline:
        if os.path.exists(OUT_FILE):
            with open(OUT_FILE, "r") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
            # Check last lines in reverse
            for line in reversed(lines):
                if line == "block":
                    print(json.dumps({"action": "block", "reason": "blocked by hybrid poc"}))
                    sys.exit(2)
                elif line == "allow":
                    print(json.dumps({"action": "allow"}))
                    sys.exit(0)
        time.sleep(0.1)

    # Default allow on timeout
    print(json.dumps({"action": "allow", "reason": "timeout default"}))
    sys.exit(0)


if __name__ == "__main__":
    main()
