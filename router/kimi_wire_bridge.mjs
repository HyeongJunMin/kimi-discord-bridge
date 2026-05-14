#!/usr/bin/env node
import readline from 'node:readline';
import { createSession } from '@moonshot-ai/kimi-agent-sdk';

const sessions = new Map();
const activeTurns = new Map();

function write(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

async function handle(cmd) {
  const id = cmd.id;
  try {
    if (cmd.op === 'start') {
      if (sessions.has(cmd.name)) {
        const existing = sessions.get(cmd.name);
        write({ id, ok: true, sessionId: existing.session.sessionId });
        return;
      }
      const session = createSession({
        workDir: cmd.workDir,
        sessionId: cmd.sessionId || undefined,
        model: cmd.model || undefined,
        yoloMode: cmd.yolo === undefined ? undefined : Boolean(cmd.yolo),
      });
      sessions.set(cmd.name, { session });
      write({ id, ok: true, sessionId: session.sessionId });
      return;
    }

    if (cmd.op === 'prompt') {
      const entry = sessions.get(cmd.name);
      if (!entry) throw new Error(`session not started: ${cmd.name}`);
      if (activeTurns.has(cmd.name)) {
        throw new Error(`session already has an active turn: ${cmd.name}`);
      }
      const turn = entry.session.prompt(cmd.content || '');
      activeTurns.set(cmd.name, turn);
      try {
        for await (const event of turn) {
          write({ id, event });
        }
      } finally {
        activeTurns.delete(cmd.name);
      }
      write({ id, ok: true, done: true });
      return;
    }

    if (cmd.op === 'interrupt') {
      const turn = activeTurns.get(cmd.name);
      if (!turn) throw new Error(`no active turn: ${cmd.name}`);
      turn.interrupt();
      write({ id, ok: true });
      return;
    }

    if (cmd.op === 'close') {
      const entry = sessions.get(cmd.name);
      if (entry) {
        await entry.session.close();
        sessions.delete(cmd.name);
      }
      write({ id, ok: true });
      return;
    }

    throw new Error(`unknown op: ${cmd.op}`);
  } catch (err) {
    write({ id, error: err?.message || String(err) });
  }
}

const rl = readline.createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});

rl.on('line', (line) => {
  if (!line.trim()) return;
  let cmd;
  try {
    cmd = JSON.parse(line);
  } catch (err) {
    write({ id: null, error: `invalid json: ${err?.message || err}` });
    return;
  }
  handle(cmd);
});

