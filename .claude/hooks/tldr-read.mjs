#!/usr/bin/env node
/**
 * PreToolUse hook — intercept Read for large code files.
 *
 * For code files >50 lines, runs `tldr extract` to build a structural
 * nav map (functions, classes, imports with line numbers), injects it
 * as additionalContext, and truncates the read for large files.
 *
 * Falls through silently if tldr is not installed.
 *
 * Cold-read fast path: if the persistent tldr-mcp shim (tldr-shim.mjs) is
 * running, cache-miss extracts go through it (~50ms) instead of spawnSync
 * tldr (~2s init per invocation). Fails open to spawnSync in all cases.
 * Env: TLDR_READ_SHIM=0 disables the shim path;
 *      TLDR_READ_SHIM_AUTOSTART=1 launches the shim on a miss (detached).
 *
 * Bypass rules (always pass through):
 *  - Non-code files (.json, .yaml, .md, etc.)
 *  - Small files (<1500 bytes, ~50 lines)
 *  - Test files
 *  - Files under .claude/hooks/ or .claude/skills/
 *  - Targeted reads (offset/limit already set)
 */
import { readFileSync, statSync, existsSync, mkdirSync, writeFileSync, renameSync } from 'fs';
import { spawnSync, spawn } from 'child_process';
import { extname, basename, dirname } from 'path';
import { homedir, tmpdir } from 'os';
import { join } from 'path';
import { createHash } from 'crypto';
import { connect } from 'net';
import { fileURLToPath } from 'url';

// Resolve tldr by absolute path first: a Claude Code process started before
// ~/.cargo/bin joined PATH can't find it by name, and the failure is silent.
const CARGO_TLDR = join(homedir(), '.cargo', 'bin', process.platform === 'win32' ? 'tldr.exe' : 'tldr');
const TLDR = existsSync(CARGO_TLDR) ? CARGO_TLDR : 'tldr';

const CODE_EXTENSIONS = new Set([
  '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs',
  '.go', '.rs', '.java', '.kt',
  '.c', '.cpp', '.cc', '.h', '.hpp',
  '.rb', '.php', '.swift', '.cs', '.scala',
  '.ex', '.exs', '.lua',
]);

const BYPASS_PATTERNS = [
  /\.json$/, /\.yaml$/, /\.yml$/, /\.toml$/, /\.md$/, /\.txt$/,
  /\.env/, /\.gitignore$/, /Makefile$/, /Dockerfile$/,
  // Test files — need full context for implementation
  /test_.*\.py$/, /.*_test\.py$/, /.*\.test\.[tj]sx?$/, /.*\.spec\.[tj]sx?$/,
  /.*_test\.go$/, /.*_test\.rs$/, /.*_spec\.rb$/, /.*Tests?\.kt$/,
  /.*Tests?\.swift$/, /.*Tests?\.cs$/, /.*_test\.exs?$/,
  // Own hooks and skills — we edit these
  /\.claude\/hooks\//, /\.claude\/skills\//,
];

const SIZE_THRESHOLD = 1500; // ~50 lines

// tldr pays a ~2s fixed init per invocation; cache the final hook output
// keyed on path+mtime+size so unchanged files skip the spawn entirely.
const CACHE_DIR = join(tmpdir(), 'tldr-read-cache');

// Persistent tldr-mcp shim (tldr-shim.mjs): serves extracts in ~50ms over a
// localhost TCP endpoint, avoiding the ~2s CLI init on cache-miss cold reads.
// Fails open: no port file / refused connect / slow response -> spawnSync path.
// TLDR_READ_SHIM=0 disables; TLDR_READ_SHIM_AUTOSTART=1 opt-in launches the
// shim (detached, unref'd) on a miss so the NEXT cold read is fast.
const SHIM_PORT_FILE = join(tmpdir(), 'tldr-shim.json');
const SHIM_CONNECT_TIMEOUT_MS = 150;  // worst-case added latency when shim is gone
const SHIM_RESPONSE_TIMEOUT_MS = 2000;

// Resolves raw extract-JSON string from the shim, or null on any failure.
function tryShimExtract(filePath) {
  return new Promise(resolve => {
    let info;
    try { info = JSON.parse(readFileSync(SHIM_PORT_FILE, 'utf-8')); } catch { return resolve(null); }
    if (!info || !Number.isInteger(info.port) || info.port <= 0) return resolve(null);
    let settled = false;
    const sock = connect({ port: info.port, host: '127.0.0.1' });
    const done = v => {
      if (settled) return;
      settled = true;
      clearTimeout(connectTimer);
      clearTimeout(responseTimer);
      sock.destroy();
      resolve(v);
    };
    const connectTimer = setTimeout(() => done(null), SHIM_CONNECT_TIMEOUT_MS);
    const responseTimer = setTimeout(() => done(null), SHIM_RESPONSE_TIMEOUT_MS);
    sock.on('error', () => done(null));
    sock.on('close', () => done(null));
    sock.on('connect', () => {
      clearTimeout(connectTimer);
      sock.write(JSON.stringify({ op: 'extract', file: filePath }) + '\n');
    });
    let buf = '';
    sock.on('data', d => {
      buf += d.toString();
      const i = buf.indexOf('\n');
      if (i === -1) return;
      try {
        const r = JSON.parse(buf.slice(0, i));
        done(r && r.ok && typeof r.json === 'string' ? r.json : null);
      } catch { done(null); }
    });
  });
}

// Fire-and-forget detached shim launch (opt-in); current read still pays the
// spawnSync cost once, subsequent cold reads hit the shim.
function autostartShim() {
  try {
    const shimPath = join(dirname(fileURLToPath(import.meta.url)), 'tldr-shim.mjs');
    if (!existsSync(shimPath)) return;
    spawn(process.execPath, [shimPath, 'serve'], { detached: true, stdio: 'ignore', windowsHide: true }).unref();
  } catch { /* fail open */ }
}

function cachePathFor(normPath) {
  return join(CACHE_DIR, createHash('sha1').update(normPath).digest('hex') + '.json');
}

function formatNavMap(info, fileName) {
  const parts = [`# ${fileName}`];

  if (info.imports && info.imports.length > 0) {
    parts.push('## Imports');
    for (const imp of info.imports.slice(0, 15)) {
      const names = imp.names ? imp.names.join(', ') : imp.module;
      parts.push(`  from ${imp.module}: ${names}`);
    }
  }

  if (info.functions && info.functions.length > 0) {
    parts.push('## Functions');
    for (const fn of info.functions.slice(0, 30)) {
      const params = fn.params ? fn.params.join(', ') : '';
      const ret = fn.return_type ? ` -> ${fn.return_type}` : '';
      const async_ = fn.is_async ? 'async ' : '';
      parts.push(`  ${async_}${fn.name}(${params})${ret}  [L${fn.line_number || fn.line || '?'}]`);
      if (fn.docstring) {
        parts.push(`    # ${fn.docstring.split('\n')[0].trim().slice(0, 100)}`);
      }
    }
  }

  if (info.classes && info.classes.length > 0) {
    parts.push('## Classes');
    for (const cls of info.classes.slice(0, 20)) {
      const bases = cls.bases && cls.bases.length ? ` (${cls.bases.join(', ')})` : '';
      parts.push(`  ${cls.name}${bases}  [L${cls.line_number || cls.line || '?'}]`);
      if (cls.fields && cls.fields.length > 0) {
        for (const f of cls.fields.slice(0, 10)) {
          const ftype = f.field_type ? `: ${f.field_type}` : '';
          parts.push(`    .${f.name}${ftype}`);
        }
        if (cls.fields.length > 10) parts.push(`    ... +${cls.fields.length - 10} fields`);
      }
      if (cls.methods && cls.methods.length > 0) {
        for (const m of cls.methods.slice(0, 8)) {
          const mparams = m.params ? m.params.join(', ') : '';
          const mret = m.return_type ? ` -> ${m.return_type}` : '';
          parts.push(`    .${m.name}(${mparams})${mret}  [L${m.line_number || m.line || '?'}]`);
        }
        if (cls.methods.length > 8) parts.push(`    ... +${cls.methods.length - 8} methods`);
      }
    }
  }

  return parts;
}

async function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { console.log('{}'); return; }

  if (data.tool_name !== 'Read') { console.log('{}'); return; }

  const toolInput = data.tool_input || {};
  const filePath = toolInput.file_path || '';
  // Windows paths arrive with backslashes; patterns use forward slashes
  const normPath = filePath.replace(/\\/g, '/');
  const ext = extname(filePath);

  // Pass through: non-code files (.ipynb is handled by a pure-JS path, no tldr)
  if (ext !== '.ipynb' && !CODE_EXTENSIONS.has(ext)) { console.log('{}'); return; }

  // Pass through: bypassed patterns
  if (BYPASS_PATTERNS.some(p => p.test(normPath))) { console.log('{}'); return; }

  // Pass through: targeted reads (offset/limit already set)
  if (toolInput.offset || (toolInput.limit && toolInput.limit < 100)) { console.log('{}'); return; }

  // Pass through: small files
  let stat;
  try { stat = statSync(filePath); } catch { console.log('{}'); return; }
  const fileSize = stat.size;
  if (fileSize < SIZE_THRESHOLD) { console.log('{}'); return; }

  // Cache hit: unchanged file (same mtime+size) reuses the exact prior output
  const cacheFile = cachePathFor(normPath);
  try {
    const cached = JSON.parse(readFileSync(cacheFile, 'utf-8'));
    if (cached.mtimeMs === stat.mtimeMs && cached.size === fileSize && typeof cached.stdout === 'string') {
      console.log(cached.stdout);
      return;
    }
  } catch { /* missing/corrupt cache -> fall through to normal path */ }

  const output = await buildOutput(filePath, fileSize);

  // Write-through cache; temp+rename for atomicity; failures never block the hook.
  // '{}' can mean a transient failure (tldr timeout/spawn error), and caching it
  // keyed on mtime+size would suppress nav maps for this file version forever.
  try {
    if (output === '{}') throw new Error('skip-cache');
    mkdirSync(CACHE_DIR, { recursive: true });
    const tmpFile = cacheFile + '.' + process.pid + '.tmp';
    writeFileSync(tmpFile, JSON.stringify({ mtimeMs: stat.mtimeMs, size: fileSize, stdout: output }));
    renameSync(tmpFile, cacheFile);
  } catch { /* cache is best-effort */ }

  console.log(output);
}

// Notebook outputs embed base64 blobs (matplotlib PNGs etc.) that would dump
// megabytes into context. Detect payload-carrying lines to place the read cutoff.
const BASE64_RUN = /[A-Za-z0-9+/=]{100,}/;
const NOTEBOOK_MAX_CELLS = 50;   // nav map cap
const NOTEBOOK_MAX_LIMIT = 100;  // raw-read cap even when no base64 found early

// Pure-JS .ipynb path: tldr cannot parse notebooks, so build the cell nav map
// directly from the JSON. Any parse/shape failure falls through to '{}'.
function buildNotebookOutput(filePath, fileSize) {
  let text, nb;
  try {
    text = readFileSync(filePath, 'utf-8');
    nb = JSON.parse(text.replace(/^﻿/, ''));
  } catch { return '{}'; }
  const cells = nb && Array.isArray(nb.cells) ? nb.cells : null;
  if (!cells || cells.length === 0) return '{}';

  const fileName = basename(filePath);
  const parts = [`# ${fileName} — ${cells.length} cells`, '## Cells'];
  for (const [i, cell] of cells.slice(0, NOTEBOOK_MAX_CELLS).entries()) {
    const src = Array.isArray(cell.source) ? cell.source.join('') : (cell.source || '');
    const firstLine = (src.split('\n').find(l => l.trim()) || '').trim().slice(0, 80);
    let outKind = 'none';
    if (Array.isArray(cell.outputs) && cell.outputs.length > 0) {
      const hasImage = cell.outputs.some(o =>
        o && o.data && Object.keys(o.data).some(k => k.startsWith('image/')));
      outKind = hasImage ? 'image' : 'text';
    }
    parts.push(`  [${i}] ${cell.cell_type || '?'}: ${firstLine}  (output: ${outKind})`);
  }
  if (cells.length > NOTEBOOK_MAX_CELLS) {
    parts.push(`  ... +${cells.length - NOTEBOOK_MAX_CELLS} more cells`);
  }

  // Cut the raw read just before the first base64-carrying line. indent>=1
  // notebooks put each payload on its own line, so this strips all blobs;
  // minified (single-line) notebooks degrade to limit=1 — acceptable, the
  // nav map above carries the structure.
  const lines = text.split('\n');
  let truncateLimit = Math.min(lines.length, NOTEBOOK_MAX_LIMIT);
  for (let i = 0; i < truncateLimit; i++) {
    if (BASE64_RUN.test(lines[i])) { truncateLimit = Math.max(1, i); break; }
  }

  parts.push('', '---',
    `Notebook truncated to ${truncateLimit} lines (base64 outputs stripped).`,
    'Use NotebookEdit for cell edits; Read with offset/limit for raw JSON ranges.');

  return JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'allow',
      updatedInput: { file_path: filePath, limit: truncateLimit },
      additionalContext: `[Notebook Map: ${fileName}]\n\n${parts.join('\n')}`,
    },
  });
}

// Returns the final hook output string ('{}' or hookSpecificOutput JSON)
async function buildOutput(filePath, fileSize) {
  if (extname(filePath) === '.ipynb') return buildNotebookOutput(filePath, fileSize);
  // Prefer the persistent shim (~50ms); fall back to spawnSync tldr (~2s).
  let raw = null;
  if (process.env.TLDR_READ_SHIM !== '0') {
    raw = await tryShimExtract(filePath);
    if (raw === null && process.env.TLDR_READ_SHIM_AUTOSTART === '1') autostartShim();
  }
  if (raw === null) {
    // Run tldr extract — falls through if tldr not installed
    const proc = spawnSync(TLDR, ['extract', filePath, '--format', 'json'], {
      encoding: 'utf-8', timeout: 10000, windowsHide: true,
    });
    if (proc.error || !proc.stdout) { return '{}'; }
    raw = proc.stdout;
  }
  let info;
  try { info = JSON.parse(raw); } catch { return '{}'; }

  // Build nav map
  const fileName = basename(filePath);
  const parts = formatNavMap(info, fileName);

  // Only inject if we got meaningful content
  if (!parts.some(p => p.startsWith('## '))) { return '{}'; }

  parts.push('', '---', 'Read specific lines: offset=N limit=M');

  // Truncation limits based on file size
  let truncateLimit;
  if (fileSize < 3000) truncateLimit = undefined;      // ~50-100 lines: read full, just inject nav
  else if (fileSize < 10000) truncateLimit = 200;       // ~100-300 lines
  else truncateLimit = 150;                              // 300+ lines

  const updatedInput = { file_path: filePath };
  if (truncateLimit) updatedInput.limit = truncateLimit;

  let additionalContext = `[Nav Map: ${fileName}]\n\n${parts.join('\n')}`;
  if (truncateLimit) additionalContext += `\nFile truncated to ${truncateLimit} lines. Use offset/limit for more.`;

  return JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'allow',
      updatedInput,
      additionalContext,
    },
  });
}

main().catch(() => { console.log('{}'); });
