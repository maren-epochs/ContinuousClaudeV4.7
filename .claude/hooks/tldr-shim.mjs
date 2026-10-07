#!/usr/bin/env node
/**
 * tldr-shim.mjs — persistent tldr-mcp bridge for tldr-read.mjs.
 *
 * Why: every `tldr extract` CLI invocation pays ~2s fixed init. tldr-mcp
 * (stdio JSON-RPC MCP server) inits once (~10ms RPC after process start) and
 * serves extracts in ~25-55ms. This shim owns one tldr-mcp child and exposes
 * extract over a localhost TCP endpoint with a port file under tmpdir, so the
 * synchronous-per-invocation hook can get sub-100ms cold extracts.
 *
 * Usage:
 *   node tldr-shim.mjs start    # spawn detached server, wait until ready
 *   node tldr-shim.mjs serve    # run server in foreground (internal)
 *   node tldr-shim.mjs stop     # shut down a running shim
 *   node tldr-shim.mjs status   # print whether a shim is reachable
 *
 * Wire protocol (newline-delimited JSON over TCP, one request per line):
 *   {"op":"ping"}                      -> {"ok":true,"pong":true}
 *   {"op":"extract","file":"<path>"}   -> {"ok":true,"json":"<raw extract JSON>"}
 *                                         or {"ok":false,"error":"..."}
 *   {"op":"shutdown"}                  -> {"ok":true} then exit
 *
 * Lifecycle: singleton via port file ping; idle-exit after 10 minutes with no
 * requests; exits (removing its port file) if the tldr-mcp child dies.
 * Everything fails open — tldr-read.mjs falls back to spawnSync `tldr extract`
 * whenever the shim is unreachable or errors.
 */
import { spawn } from 'child_process';
import { readFileSync, writeFileSync, renameSync, unlinkSync, existsSync } from 'fs';
import { createServer, connect } from 'net';
import { homedir, tmpdir } from 'os';
import { join } from 'path';
import { fileURLToPath } from 'url';

const PORT_FILE = join(tmpdir(), 'tldr-shim.json');
const IDLE_EXIT_MS = 10 * 60 * 1000;
const MCP_CALL_TIMEOUT_MS = 10000;
const CARGO_MCP = join(homedir(), '.cargo', 'bin', process.platform === 'win32' ? 'tldr-mcp.exe' : 'tldr-mcp');
const TLDR_MCP = existsSync(CARGO_MCP) ? CARGO_MCP : 'tldr-mcp';

function readPortFile() {
  try {
    const info = JSON.parse(readFileSync(PORT_FILE, 'utf-8'));
    if (info && Number.isInteger(info.port) && info.port > 0) return info;
  } catch { /* missing/corrupt */ }
  return null;
}

function removePortFile() {
  try { unlinkSync(PORT_FILE); } catch { /* best-effort */ }
}

// Send one request to a running shim; resolves response object or null.
function request(port, payload, timeoutMs = 2000) {
  return new Promise(resolve => {
    let settled = false;
    const sock = connect({ port, host: '127.0.0.1' });
    const done = v => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      sock.destroy();
      resolve(v);
    };
    const timer = setTimeout(() => done(null), timeoutMs);
    sock.on('error', () => done(null));
    sock.on('close', () => done(null));
    sock.on('connect', () => sock.write(JSON.stringify(payload) + '\n'));
    let buf = '';
    sock.on('data', d => {
      buf += d.toString();
      const i = buf.indexOf('\n');
      if (i === -1) return;
      try { done(JSON.parse(buf.slice(0, i))); } catch { done(null); }
    });
  });
}

async function shimAlive() {
  const info = readPortFile();
  if (!info) return null;
  const r = await request(info.port, { op: 'ping' }, 1000);
  return r && r.ok ? info : null;
}

// ---------------------------------------------------------------- serve ----

async function serve() {
  // Singleton: another live shim wins.
  if (await shimAlive()) return;

  // Own the tldr-mcp child over stdio (newline-delimited JSON-RPC).
  const child = spawn(TLDR_MCP, [], { stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true });
  let childDead = false;
  child.on('error', () => { childDead = true; shutdown(1); });
  child.on('exit', () => { childDead = true; shutdown(1); });

  let stdoutBuf = '';
  const pending = new Map();
  let nextId = 1;
  child.stdout.on('data', d => {
    stdoutBuf += d.toString();
    let i;
    while ((i = stdoutBuf.indexOf('\n')) !== -1) {
      const line = stdoutBuf.slice(0, i).trim();
      stdoutBuf = stdoutBuf.slice(i + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch { continue; }
      if (msg.id != null && pending.has(msg.id)) {
        pending.get(msg.id)(msg);
        pending.delete(msg.id);
      }
    }
  });

  function rpc(method, params) {
    return new Promise((resolve, reject) => {
      if (childDead) return reject(new Error('tldr-mcp child dead'));
      const id = nextId++;
      pending.set(id, resolve);
      try { child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); }
      catch (e) { pending.delete(id); return reject(e); }
      setTimeout(() => {
        if (pending.has(id)) { pending.delete(id); reject(new Error('mcp timeout: ' + method)); }
      }, MCP_CALL_TIMEOUT_MS);
    });
  }

  await rpc('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'tldr-shim', version: '1.0.0' },
  });
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');

  // Idle-exit timer: reset on every request.
  let idleTimer = null;
  function resetIdle() {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => shutdown(0), IDLE_EXIT_MS);
  }

  let server;
  function shutdown(code) {
    const mine = readPortFile();
    if (!mine || mine.pid === process.pid) removePortFile();
    try { server && server.close(); } catch { /* ignore */ }
    try { child.kill(); } catch { /* ignore */ }
    process.exit(code);
  }

  server = createServer(sock => {
    resetIdle();
    let buf = '';
    const reply = obj => { try { sock.write(JSON.stringify(obj) + '\n'); } catch { /* peer gone */ } };
    sock.on('error', () => { /* peer reset: ignore */ });
    sock.on('data', async d => {
      buf += d.toString();
      const i = buf.indexOf('\n');
      if (i === -1) return;
      const line = buf.slice(0, i);
      buf = buf.slice(i + 1);
      let req;
      try { req = JSON.parse(line); } catch { return reply({ ok: false, error: 'bad request' }); }
      if (req.op === 'ping') return reply({ ok: true, pong: true });
      if (req.op === 'shutdown') { reply({ ok: true }); return shutdown(0); }
      if (req.op === 'extract' && typeof req.file === 'string') {
        try {
          const r = await rpc('tools/call', { name: 'tldr_extract', arguments: { file: req.file } });
          const text = r.result && r.result.content && r.result.content[0] && r.result.content[0].text;
          if (r.result && !r.result.isError && typeof text === 'string') {
            return reply({ ok: true, json: text });
          }
          return reply({ ok: false, error: 'extract failed' });
        } catch (e) {
          return reply({ ok: false, error: String(e && e.message || e) });
        }
      }
      return reply({ ok: false, error: 'unknown op' });
    });
  });

  server.listen(0, '127.0.0.1', () => {
    const port = server.address().port;
    // Atomic write so a hook never reads a torn port file.
    const tmp = PORT_FILE + '.' + process.pid + '.tmp';
    try {
      writeFileSync(tmp, JSON.stringify({ port, pid: process.pid, started: Date.now() }));
      renameSync(tmp, PORT_FILE);
    } catch {
      shutdown(1);
    }
    resetIdle();
  });
}

// ----------------------------------------------------------------- cli -----

async function start() {
  if (await shimAlive()) { console.log('tldr-shim already running'); return; }
  const childProc = spawn(process.execPath, [fileURLToPath(import.meta.url), 'serve'], {
    detached: true, stdio: 'ignore', windowsHide: true,
  });
  childProc.unref();
  // Wait for readiness so callers (tests, users) can rely on the shim.
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    const info = await shimAlive();
    if (info) { console.log(`tldr-shim ready on 127.0.0.1:${info.port} (pid ${info.pid})`); return; }
    await new Promise(r => setTimeout(r, 100));
  }
  console.error('tldr-shim failed to start within 10s');
  process.exitCode = 1;
}

async function stop() {
  const info = readPortFile();
  if (info) await request(info.port, { op: 'shutdown' }, 1000);
  removePortFile();
  console.log('tldr-shim stopped');
}

async function status() {
  const info = await shimAlive();
  if (info) console.log(`running on 127.0.0.1:${info.port} (pid ${info.pid})`);
  else { console.log('not running'); process.exitCode = 1; }
}

const cmd = process.argv[2] || 'start';
if (cmd === 'serve') serve().catch(() => { removePortFile(); process.exit(1); });
else if (cmd === 'start') start();
else if (cmd === 'stop') stop();
else if (cmd === 'status') status();
else { console.error('usage: tldr-shim.mjs [start|serve|stop|status]'); process.exitCode = 2; }
