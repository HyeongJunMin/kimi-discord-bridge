#!/usr/bin/env python3
"""Probe ACP permission flow with a non-fs tool (Bash)."""
import json, subprocess, threading, time, os

events = []
proc = subprocess.Popen(
    ["kimi", "--config", '{"default_yolo": false}', "acp"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    bufsize=0, env={**os.environ, "PYTHONPATH": "/tmp/kimi-acp-probe/pythonpath"},
)

def reader(stream, label):
    for line in iter(stream.readline, b""):
        s = line.decode(errors="replace").rstrip()
        if not s: continue
        if label == "stdout":
            try:
                ev = json.loads(s)
                events.append(ev)
                # Highlight only interesting events
                m = ev.get("method", "")
                sub = ev.get("params", {}).get("update", {}).get("sessionUpdate", "")
                if m in ("session/request_permission",):
                    print(f"\n*** PERMISSION REQUEST: {json.dumps(ev, indent=2)[:1500]}", flush=True)
                elif "id" in ev and m and "result" not in ev and "error" not in ev:
                    print(f"\n*** SERVER REQ method={m} id={ev['id']}: {json.dumps(ev)[:800]}", flush=True)
                elif sub in ("tool_call",):
                    print(f"<< tool_call start: {json.dumps(ev.get('params', {}).get('update'))[:300]}", flush=True)
                elif sub == "tool_call_update":
                    status = ev.get("params", {}).get("update", {}).get("status", "")
                    if status in ("completed", "failed", "pending"):
                        print(f"<< tool_call_update [{status}]: {json.dumps(ev.get('params', {}).get('update'))[:400]}", flush=True)
                elif sub == "agent_message_chunk":
                    pass  # noisy
                elif "id" in ev and ("result" in ev or "error" in ev):
                    print(f"<< RESP id={ev['id']}: {json.dumps(ev.get('result') or ev.get('error'))[:200]}", flush=True)
            except: pass

threading.Thread(target=reader, args=(proc.stdout, "stdout"), daemon=True).start()
threading.Thread(target=reader, args=(proc.stderr, "stderr"), daemon=True).start()

def send(req):
    proc.stdin.write((json.dumps(req)+"\n").encode())
    proc.stdin.flush()

def wait(req_id, timeout=15):
    start = time.time()
    while time.time() - start < timeout:
        for e in events:
            if e.get("id") == req_id and ("result" in e or "error" in e):
                return e
        time.sleep(0.1)
    return None

send({"jsonrpc":"2.0","id":1,"method":"initialize",
      "params":{"protocolVersion":1,
                "clientCapabilities":{"fs":{"readTextFile":True,"writeTextFile":True}}}})
wait(1, 5)

send({"jsonrpc":"2.0","id":2,"method":"session/new",
      "params":{"cwd":"/tmp/kimi-acp-probe","mcpServers":[]}})
new_resp = wait(2, 10)
sid = new_resp["result"]["sessionId"]
print(f"sessionId={sid}\n", flush=True)

send({"jsonrpc":"2.0","id":3,"method":"session/prompt",
      "params":{"sessionId":sid,
                "prompt":[{"type":"text","text":"Run the bash command 'echo HELLO_FROM_BASH' and show me the output."}]}})

# Auto-handle server requests
deadline = time.time() + 60
handled = set()
done = False
while time.time() < deadline and not done:
    for i, e in enumerate(events):
        if i in handled: continue
        if "id" in e and "method" in e and "result" not in e and "error" not in e:
            handled.add(i)
            req_id, method = e["id"], e["method"]
            params = e.get("params", {})
            if method == "session/request_permission":
                opts = params.get("options", [])
                # Approve first option
                opt_id = opts[0].get("optionId") if opts else "allow"
                send({"jsonrpc":"2.0","id":req_id,
                      "result":{"outcome":{"outcome":"selected","optionId":opt_id}}})
                print(f">> approved permission with optionId={opt_id}", flush=True)
            elif method == "fs/write_text_file":
                with open(params["path"],"w") as f: f.write(params.get("content",""))
                send({"jsonrpc":"2.0","id":req_id,"result":{}})
            elif method == "fs/read_text_file":
                try:
                    with open(params["path"]) as f: content = f.read()
                    send({"jsonrpc":"2.0","id":req_id,"result":{"content":content}})
                except Exception as ex:
                    send({"jsonrpc":"2.0","id":req_id,"error":{"code":-32000,"message":str(ex)}})
            else:
                send({"jsonrpc":"2.0","id":req_id,"result":{}})
    for e in events:
        if e.get("id") == 3 and ("result" in e or "error" in e):
            done = True
            print(f"\n=== PROMPT DONE: {json.dumps(e.get('result') or e.get('error'))[:200]}", flush=True)
            break
    time.sleep(0.2)

proc.terminate()
try: proc.wait(timeout=3)
except: proc.kill()

print(f"\n=== Total events: {len(events)}")
perms = [e for e in events if e.get("method") == "session/request_permission"]
print(f"=== session/request_permission count: {len(perms)}")
if perms:
    print(f"=== first permission request schema:")
    print(json.dumps(perms[0], indent=2))
