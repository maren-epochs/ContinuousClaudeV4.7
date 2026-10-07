#!/usr/bin/env node
/**
 * Stop hook — block when context is too high and suggest handoff.
 *
 * Reads context percentage from the temp file written by status.mjs.
 * This ensures 1:1 match with status line display. When that file is missing
 * or stale (headless -p runs, agent_call: no statusline), falls back to the
 * last main-thread usage in transcript_path. Window: CLAUDE_CONTEXT_WINDOW env,
 * else 200K (1M once usage exceeds 200K, which proves the larger window).
 *
 * Also starts a background `fleet.py collect` (refreshes ~/.claude/fleet/state.json
 * for the statusline fleet segment): detached, cwd ~/.claude/fleet, at most once per
 * 2 min via collect.lock (exclusive create; mtime = last start; a lock older than
 * 2 min or dated in the future is stale and reclaimed). collect.py aborts itself
 * after 20 s (watchdog hard exit 5 s later), so reclaimed locks never pile up hung
 * collectors. FLEET_COLLECT=0 disables; FLEET_PYTHON / FLEET_PY override the
 * interpreter / script (tests).
 */
import { readFileSync, existsSync, statSync, openSync, readSync, closeSync, writeSync, mkdirSync, renameSync, unlinkSync } from 'fs';
import { spawn } from 'child_process';
import { join, dirname } from 'path';
import { tmpdir, homedir } from 'os';
import { fileURLToPath } from 'url';

const CONTEXT_THRESHOLD = 85;
const MAX_PCT_AGE_MS = 2 * 60 * 60 * 1000; // ignore pct files older than 2h (crashed/stale sessions)

const TAIL_BYTES = 512 * 1024;

// Context % from the newest non-sidechain assistant usage in the transcript
// tail; null when unavailable. Input-only, same formula as used_percentage.
function pctFromTranscript(path) {
  if (!path || !existsSync(path)) return null;
  const size = statSync(path).size;
  const len = Math.min(size, TAIL_BYTES);
  const buf = Buffer.alloc(len);
  const fd = openSync(path, 'r');
  try { readSync(fd, buf, 0, len, size - len); } finally { closeSync(fd); }
  const lines = buf.toString('utf-8').split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    let e;
    try { e = JSON.parse(lines[i]); } catch { continue; }
    const u = e && e.type === 'assistant' && !e.isSidechain && e.message && e.message.usage;
    if (!u) continue;
    const tokens = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    const window = Number(process.env.CLAUDE_CONTEXT_WINDOW) || (tokens > 200000 ? 1000000 : 200000);
    return Math.min(100, Math.floor(tokens * 100 / window));
  }
  return null;
}

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
      pct = pctFromTranscript(data.transcript_path);
      process.stderr.write(pct == null
        ? `auto-handoff-stop: pct file missing for session ${sid} (statusline hook not running?) and no transcript usage; context guard inactive, allowing stop\n`
        : `auto-handoff-stop: pct file missing for session ${sid}; using transcript usage (${pct}%)\n`);
    } else if (Date.now() - statSync(file).mtimeMs <= MAX_PCT_AGE_MS) {
      const parsed = parseInt(readFileSync(file, 'utf-8').trim(), 10);
      if (Number.isFinite(parsed)) pct = parsed;
    }
    // Stale file (mtime > 2h): ignore — likely a crashed session reusing the key.
    if (pct == null && existsSync(file)) pct = pctFromTranscript(data.transcript_path);
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

const FLEET_MIN_INTERVAL_MS = 2 * 60 * 1000;

// Never blocks or fails the hook: every error is swallowed, the child is unref'd.
function refreshFleet() {
  if (process.env.FLEET_COLLECT === '0') return;
  const here = dirname(fileURLToPath(import.meta.url));
  // Installed: ~/.claude/hooks -> ~/.claude/tools; repo: .claude/hooks -> tools.
  const script = process.env.FLEET_PY
    || [join(here, '..', 'tools', 'fleet', 'fleet.py'), join(here, '..', '..', 'tools', 'fleet', 'fleet.py')].find(p => existsSync(p));
  if (!script || !existsSync(script)) return;
  const dir = join(homedir(), '.claude', 'fleet');
  const now = Date.now();
  // A future mtime (clock skew, restored backup) is stale, not recent.
  const recent = (p) => { try { const age = now - statSync(p).mtimeMs; return age >= 0 && age < FLEET_MIN_INTERVAL_MS; } catch { return false; } };
  if (recent(join(dir, 'state.json'))) return;
  mkdirSync(dir, { recursive: true });
  const lock = join(dir, 'collect.lock');
  let fd;
  try { fd = openSync(lock, 'wx'); } catch (e) {
    if (e.code !== 'EEXIST' || recent(lock)) return;
    try { renameSync(lock, `${lock}.${process.pid}.stale`); unlinkSync(`${lock}.${process.pid}.stale`); } catch { return; }
    fd = openSync(lock, 'wx');
  }
  try {
    const [cmd, pre] = process.env.FLEET_PYTHON ? [process.env.FLEET_PYTHON, []]
      : process.platform === 'win32' ? ['py', ['-3.13']] : ['python3', []];
    const child = spawn(cmd, [...pre, script, 'collect'], {
      cwd: dir, detached: true, stdio: 'ignore', windowsHide: true,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    });
    child.on('error', () => {});
    child.unref();
    writeSync(fd, `${child.pid || ''}
`);
  } finally { closeSync(fd); }
}

try { main(); } catch { console.log('{}'); }
try { refreshFleet(); } catch {}
