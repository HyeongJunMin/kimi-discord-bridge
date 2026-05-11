# router — Discord ↔ kimi-cli ACP bridge MVP

## What this is
Spawns one `kimi acp` per Discord thread, multiplexed by a single bot.
Bypasses kimi-cli's OAuth gate via PYTHONPATH-injected sitecustomize patch
so authentication uses `MOONSHOT_API_KEY` only.

## Layout
- `acp_client.py`     — async JSON-RPC client over `kimi acp` stdio
- `auth_bypass/`      — PYTHONPATH'd sitecustomize patch (no global mutation)
- `cmux_client.py`    — `cmux rpc` wrapper for workspace.* / surface.*
- `registry.py`       — sqlite session table (thread_id ↔ acp session)
- `discord_relay.py`  — per-thread debounce + edit-roll-over output
- `bot.py`            — entry point: `/new`, message routing, permission buttons

## Run
```sh
pip install discord.py
export DISCORD_BOT_TOKEN=...
export MOONSHOT_API_KEY=sk-...   # kimi-cli reads this for model auth
export GUILD_ID=...              # optional: guild-only sync (faster)
python -m router.bot
```

## Out-of-scope (deferred)
- Multi-user ACL beyond owner-only
- Restart recovery (re-attach to live ACP processes by PID)
- Monitor surface in cmux (read-only viewer)
- File attachment forwarding (image input via promptCapabilities.image)
- /yolo, /afk, /compact slash commands surfaced from availableCommands
- ACP `session/load` to resume a saved session

## Known caveats observed during probe
- `--config '{"default_yolo": false}'` did not actually disable yolo in
  smoke tests; tools auto-approved despite the override. We ship with
  `yolo=False` configured but emit a warning if `session/request_permission`
  never fires for a destructive prompt — would need to investigate
  kimi-cli config merge order.
- ACP `tool_call_update` streams arg JSON one character at a time
  (~100 events per call). We suppress `in_progress` updates and only
  surface `tool_call` start + `completed`/`failed` end states.
