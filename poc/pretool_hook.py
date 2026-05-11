#!/usr/bin/env python3
"""PoC PreToolUse hook: block WriteFile, allow everything else.

Usage: registered in ~/.kimi/config.toml [[hooks]] section.
Expected input (stdin): JSON with tool_name, tool_input, tool_call_id, session_id, cwd.
Output (stdout): JSON.
Exit code: 0 = allow, 2 = block.
"""
from __future__ import annotations
import json, sys


def main():
    data = json.load(sys.stdin)
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})
    session_id = data.get("session_id", "")

    # Log to a file so we can observe that the hook fired
    with open("/tmp/poc_pretool_hook.log", "a") as f:
        f.write(json.dumps({"tool_name": tool_name, "input_keys": list(tool_input.keys()),
                            "session_id": session_id}, ensure_ascii=False) + "\n")

    if tool_name == "WriteFile":
        result = {"action": "block", "reason": "PoC hook blocked WriteFile"}
        print(json.dumps(result))
        sys.exit(2)
    else:
        result = {"action": "allow"}
        print(json.dumps(result))
        sys.exit(0)


if __name__ == "__main__":
    main()
