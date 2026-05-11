#!/usr/bin/env python3
"""ACP probe: initialize → session/new → session/prompt, dump all events."""
import json, subprocess, threading, time, sys, os

events = []
proc = subprocess.Popen(
    ["kimi", "acp"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    bufsize=0, env={**os.environ},
)

def reader(stream, label):
    for line in iter(stream.readline, b""):
        s = line.decode(errors="replace").rstrip()
        if not s:
            continue
        if label == "stdout":
            try:
                ev = json.loads(s)
                events.append(ev)
                print(f"<< {json.dumps(ev)[:600]}", flush=True)
            except Exception:
                print(f"<<RAW {s[:300]}", flush=True)
        else:
            print(f"!! {s[:300]}", flush=True)

threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True).start()
threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True).start()

def send(req):
    s = json.dumps(req) + "\n"
    print(f">> {s.strip()[:400]}", flush=True)
    proc.stdin.write(s.encode())
    proc.stdin.flush()

def wait_for_response(req_id, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        for e in events:
            if e.get("id") == req_id and ("result" in e or "error" in e):
                return e
        time.sleep(0.1)
    return None

# 1. initialize
send({"jsonrpc":"2.0","id":1,"method":"initialize",
      "params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":True,"writeTextFile":True}}}})
init_resp = wait_for_response(1, 5)
print(f"\n=== initialize: {'OK' if init_resp and 'result' in init_resp else 'FAIL'} ===\n", flush=True)

# 2. session/new
send({"jsonrpc":"2.0","id":2,"method":"session/new",
      "params":{"cwd":"/tmp/kimi-acp-probe","mcpServers":[]}})
new_resp = wait_for_response(2, 10)
print(f"\n=== session/new resp: {json.dumps(new_resp)[:800] if new_resp else 'TIMEOUT'} ===\n", flush=True)

session_id = None
if new_resp and "result" in new_resp:
    session_id = new_resp["result"].get("sessionId")
    print(f"=== sessionId = {session_id} ===\n", flush=True)

# 3. session/prompt (only if we got a session)
if session_id:
    send({"jsonrpc":"2.0","id":3,"method":"session/prompt",
          "params":{"sessionId":session_id,
                    "prompt":[{"type":"text","text":"Reply with exactly: PONG"}]}})
    # collect events for 30s
    print("\n=== streaming events (30s) ===\n", flush=True)
    t0 = time.time()
    seen = len(events)
    while time.time() - t0 < 30:
        time.sleep(0.5)
        if any(e.get("id") == 3 and ("result" in e or "error" in e) for e in events):
            break

# cleanup
proc.terminate()
try: proc.wait(timeout=3)
except: proc.kill()

print(f"\n=== TOTAL events: {len(events)} ===")
print("\n=== unique methods seen ===")
methods = set()
for e in events:
    if "method" in e: methods.add(e["method"])
for m in sorted(methods): print(f"  - {m}")
