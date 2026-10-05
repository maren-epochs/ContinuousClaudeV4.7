#!/usr/bin/env node
/**
 * Stop hook — block when context is too high and suggest handoff.
 *
 * Reads context percentage from the temp file written by status.mjs.
 * This ensures 1:1 match with status line display.
 */
import { readFileSync, existsSync, statSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

const CONTEXT_THRESHOLD = 85;
const MAX_PCT_AGE_MS = 2 * 60 * 60 * 1000; // ignore pct files older than 2h (crashed/stale sessions)

function getSessionId(data) {
  const sid = data.session_id || '';
  if (sid) return sid.slice(0, 8);
  return process.env.CLAUDE_SESSION_ID || String(process.ppid);
}

function main() {
  // Fail OPEN: this guard must never block a stop on missing/corrupt data.
  let data = {};
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch {}

  if (data.stop_hook_active) { console.log('{}'); return; }

  const sid = getSessionId(data);
  const file = join(tmpdir(), `claude-context-pct-${sid}.txt`);

  let pct = null;
  try {
    if (!existsSync(file)) {
      // status.mjs never wrote a pct for this session (headless/-p run or
      // unconfigured statusline). Degrade silently but leave one trace in logs.
      process.stderr.write(`auto-handoff-stop: pct file missing for session ${sid} (statusline hook not running?); context guard inactive, allowing stop\n`);
    } else if (Date.now() - statSync(file).mtimeMs <= MAX_PCT_AGE_MS) {
      const parsed = parseInt(readFileSync(file, 'utf-8').trim(), 10);
      if (Number.isFinite(parsed)) pct = parsed;
    }
    // Stale file (mtime > 2h): ignore — likely a crashed session reusing the key.
  } catch {}

  if (pct == null || pct < CONTEXT_THRESHOLD) {
    console.log('{}');
  } else {
    console.log(JSON.stringify({
      decision: 'block',
      reason: `Context at ${pct}%. Run: /create-handoff`,
    }));
  }
}

try { main(); } catch { console.log('{}'); }
