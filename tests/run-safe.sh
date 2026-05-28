#!/usr/bin/env bash
# Run the full test suite WITHOUT blowing up the sandbox.
#
# Why this exists: running `pytest` across every file in one process piles up
# real subprocesses + per-file asyncio loops and has SIGKILLed the whole
# sandbox (exit 137), taking the Claude Code / kimi session down with it.
# Running one file per pytest process keeps peak memory flat — each file
# passed individually even when the combined run did not.
#
# Usage:  bash tests/run-safe.sh            # includes slow tests, one file at a time
#         PYTEST="python -m pytest" bash tests/run-safe.sh
#
# Plain `pytest` (no slow tests, 60s/test cap) is already safe for the inner
# loop; use this script when you want full coverage including slow tests.
set -u

cd "$(dirname "$0")/.." || exit 1

# Prefer the project venv if present, else fall back to the PYTEST override
# or `python3 -m pytest`.
if [ -z "${PYTEST:-}" ]; then
    if [ -x ".venv/bin/python" ]; then
        PYTEST=".venv/bin/python -m pytest"
    else
        PYTEST="python3 -m pytest"
    fi
fi

fail=0
for f in tests/test_*.py; do
    printf '\n=== %s ===\n' "$f"
    # -m "" clears the default "not slow" filter so slow tests run too, but
    # only ever within a single file's process.
    $PYTEST "$f" -q -m "" -p no:cacheprovider
    rc=$?
    if [ "$rc" -ne 0 ]; then
        echo "!! FAILED ($f) rc=$rc"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    echo; echo "Some test files failed."
    exit 1
fi
echo; echo "All test files passed."
