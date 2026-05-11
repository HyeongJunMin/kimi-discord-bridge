#!/usr/bin/env python3
"""Probe ACP tool/permission events: prompt that requires file write,
yolo disabled so server emits session/request_permission."""
import json, subprocess, threading, time, sys, os

events = []
PYTHONPATH_DIR = "/tmp/kimi-acp-probe/pythonpath"

env = {**os.environ, "PYTHONPATH": PYTHONPATH_DIR}
proc = subprocess.Popen(
    ["kimi", "--config", '{"default_yolo": false}', "acp"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    bufsize=0, env=env,
)

def reader(stream, label):
    for line in iter(stream.readline, b""):
        s = line.decode(errors="replace").rstrip()
        if not s: continue
        if label == "stdout":
            try:
                ev = json.loads(s)
                events.append(ev)
                # Pretty print key info
                if "method" in ev:
                    sub = ev.get("params", {}).get("update", {}).get("sessionUpdate", "")
                    method = ev["method"]
                    if method == "session/update" and sub in ("agent_thought_chunk", "agent_message_chunk"):
                        # skip noisy text deltas in output
                        pass
                    else:
                        print(f"<< NOTIF {method} {sub}: {json.dumps(ev)[:600]}", flush=True)
                elif "id" in ev:
                    if "result" in ev:
                        print(f"<< RESP id={ev['id']}: {json.dumps(ev['result'])[:600]}", flush=True)
                    elif "error" in ev:
                        print(f"<< ERROR id={ev['id']}: {json.dumps(ev['error'])[:400]}", flush=True)
                    else:
                        # Server-initiated request (e.g. permission, fs/write)
                        print(f"<< REQ id={ev['id']} method={ev.get('method')}: {json.dumps(ev)[:800]}", flush=True)
            except Exception as e:
                print(f"<<RAW {s[:300]}", flush=True)
        else:
            if "Warning" not in s and "Deprecation" not in s:
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

def find_server_request(timeout=30):
    """Find a server-initiated request (has id but no result/error and is unhandled)."""
    start = time.time()
    handled = set()
    while time.time() - start < timeout:
        for i, e in enumerate(events):
            if i in handled: continue
            if "id" in e and "method" in e and "result" not in e and "error" not in e:
                handled.add(i)
                return e
        time.sleep(0.1)
    return None

# 1. initialize
send({"jsonrpc":"2.0","id":1,"method":"initialize",
      "params":{"protocolVersion":1,
                "clientCapabilities":{"fs":{"readTextFile":True,"writeTextFile":True}}}})
wait_for_response(1, 5)

# 2. session/new
send({"jsonrpc":"2.0","id":2,"method":"session/new",
      "params":{"cwd":"/tmp/kimi-acp-probe","mcpServers":[]}})
new_resp = wait_for_response(2, 10)
session_id = new_resp["result"]["sessionId"] if new_resp and "result" in new_resp else None
print(f"\n=== sessionId = {session_id} ===\n", flush=True)

# 3. prompt that requires a file write tool — should trigger permission request
prompt_text = ("Create a file at /tmp/kimi-acp-probe/hello.txt with content 'hello from kimi'. "
               "Use the write tool. Do not ask for confirmation, just attempt.")
send({"jsonrpc":"2.0","id":3,"method":"session/prompt",
      "params":{"sessionId":session_id,
                "prompt":[{"type":"text","text":prompt_text}]}})

# 4. wait for server-initiated request_permission (or fs/write_text_file)
print("\n=== waiting for server request (permission / fs)... ===\n", flush=True)
server_req = None
deadline = time.time() + 60
last_handled = set()
prompt_done = False
while time.time() < deadline and not prompt_done:
    # Find unhandled server-initiated requests
    for i, e in enumerate(events):
        if i in last_handled: continue
        if "id" in e and "method" in e and "result" not in e and "error" not in e:
            last_handled.add(i)
            method = e["method"]
            req_id = e["id"]
            print(f"\n*** SERVER REQUEST: method={method} id={req_id}", flush=True)
            print(f"    full: {json.dumps(e)[:1500]}", flush=True)
            # Auto-respond
            if method == "session/request_permission":
                # Approve the first option
                opts = e.get("params", {}).get("options", [])
                outcome_id = opts[0].get("optionId") if opts else "allow"
                resp = {"jsonrpc":"2.0","id":req_id,
                        "result":{"outcome":{"outcome":"selected","optionId":outcome_id}}}
                print(f"    >> approving with optionId={outcome_id}", flush=True)
                send(resp)
            elif method == "fs/write_text_file":
                params = e.get("params", {})
                path = params.get("path")
                content = params.get("content", "")
                try:
                    with open(path, "w") as f: f.write(content)
                    send({"jsonrpc":"2.0","id":req_id,"result":{}})
                    print(f"    >> wrote {len(content)} bytes to {path}", flush=True)
                except Exception as ex:
                    send({"jsonrpc":"2.0","id":req_id,
                          "error":{"code":-32000,"message":str(ex)}})
            elif method == "fs/read_text_file":
                params = e.get("params", {})
                path = params.get("path")
                try:
                    with open(path) as f: content = f.read()
                    send({"jsonrpc":"2.0","id":req_id,"result":{"content":content}})
                    print(f"    >> read {len(content)} bytes from {path}", flush=True)
                except Exception as ex:
                    send({"jsonrpc":"2.0","id":req_id,
                          "error":{"code":-32000,"message":str(ex)}})
            else:
                # Unknown server method — reply with empty result to keep going
                send({"jsonrpc":"2.0","id":req_id,"result":{}})
                print(f"    >> empty reply (unknown method)", flush=True)
    # check prompt completion
    for e in events:
        if e.get("id") == 3 and ("result" in e or "error" in e):
            prompt_done = True
            print(f"\n=== PROMPT DONE: {json.dumps(e)[:300]}", flush=True)
            break
    time.sleep(0.3)

# Cleanup
proc.terminate()
try: proc.wait(timeout=3)
except: proc.kill()

# Summary
print(f"\n\n=== SUMMARY ===")
print(f"Total events: {len(events)}")
methods_notif = {}
methods_req = {}
sub_types = {}
for e in events:
    if "method" in e:
        if "result" in e or "error" in e:
            continue  # not used
        if "id" in e:
            methods_req[e["method"]] = methods_req.get(e["method"], 0) + 1
        else:
            methods_notif[e["method"]] = methods_notif.get(e["method"], 0) + 1
        sub = e.get("params", {}).get("update", {}).get("sessionUpdate")
        if sub:
            sub_types[sub] = sub_types.get(sub, 0) + 1

print(f"\nNotifications (no id):")
for m, c in sorted(methods_notif.items()): print(f"  {m}: {c}")
print(f"\nServer requests (with id, awaiting client reply):")
for m, c in sorted(methods_req.items()): print(f"  {m}: {c}")
print(f"\nsession/update sub-types:")
for s, c in sorted(sub_types.items()): print(f"  {s}: {c}")

# Check file actually got written
import os
hello_path = "/tmp/kimi-acp-probe/hello.txt"
if os.path.exists(hello_path):
    with open(hello_path) as f:
        print(f"\n✓ {hello_path} exists: {f.read()!r}")
else:
    print(f"\n✗ {hello_path} not created")
