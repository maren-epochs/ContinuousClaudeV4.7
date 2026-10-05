#!/usr/bin/env node
/**
 * PostToolUse hook — run diagnostics after file edits.
 *
 * Python (.py/.pyi) goes straight to ruff (~70ms) instead of through
 * tldr diagnostics (~2s) — same findings, 28x faster. Everything else
 * (and Python when ruff is missing) runs `tldr diagnostics`.
 *
 * Falls through silently if no backend is available.
 */
import { readFileSync, existsSync, readdirSync } from 'fs';
import { spawnSync } from 'child_process';
import { extname, basename, join } from 'path';
import { homedir } from 'os';

const WIN = process.platform === 'win32';

// Resolve tldr by absolute path first: a Claude Code process started before
// ~/.cargo/bin joined PATH can't find it by name, and the failure is silent.
const CARGO_TLDR = join(homedir(), '.cargo', 'bin', WIN ? 'tldr.exe' : 'tldr');
const TLDR = existsSync(CARGO_TLDR) ? CARGO_TLDR : 'tldr';

// Same pattern for ruff: scan install dirs (newest Python first) so a Python
// upgrade doesn't silently drop the fast path; bare name as fallback.
function pythonScriptDirs(root) {
  try {
    return readdirSync(root)
      .filter((d) => /^Python3\d+$/.test(d))
      .sort((a, b) => Number(b.slice(7)) - Number(a.slice(7)))
      .map((d) => join(root, d, 'Scripts'));
  } catch { return []; }
}
const RUFF_EXE = WIN ? 'ruff.exe' : 'ruff';
const RUFF_CANDIDATES = [
  ...(WIN ? [
    ...pythonScriptDirs(join(homedir(), 'AppData', 'Local', 'Programs', 'Python')),
    ...pythonScriptDirs(join(homedir(), 'AppData', 'Roaming', 'Python')),
  ] : []),
  join(homedir(), '.local', 'bin'),  // pipx / uv tool
].map((d) => join(d, RUFF_EXE));
const RUFF = RUFF_CANDIDATES.find((c) => existsSync(c)) || 'ruff';

const ENABLED_EXTENSIONS = new Set([
  '.py', '.pyx', '.pyi',                    // Python: ruff + pyright
  '.ts', '.tsx', '.js', '.jsx', '.mjs',      // JS/TS: eslint + tsc
  '.rs',                                      // Rust: cargo check + clippy
]);

// Fast path: ruff invoked directly, skipping tldr's startup cost.
const RUFF_EXTENSIONS = new Set(['.py', '.pyi']);

// Errors first, then warnings — the listing is capped, so rank by urgency
const SEVERITY_RANK = { error: 0, warning: 1, info: 2, hint: 3 };

// Ruff codes that mean the code is broken, not just unidiomatic — flake8's
// standard "select=E9,F63,F7,F82" set, plus syntax errors (`invalid-syntax`, or no code).
const RUFF_ERROR = /^(E9|F63|F7|F82|invalid-syntax)/;

// Run ruff directly on a Python file. Returns the normalized diagnostics
// shape, or null when ruff is unavailable/failed (caller falls back to tldr).
function ruffDiagnostics(filePath) {
  if (!RUFF) return null;
  // spawnSync, not execSync: ruff exits 1 whenever it has findings,
  // which is exactly when we want its stdout.
  const proc = spawnSync(RUFF, ['check', filePath, '--output-format', 'json', '--quiet'], {
    encoding: 'utf-8', timeout: 15000, windowsHide: true,
  });
  if (proc.error || typeof proc.stdout !== 'string' || proc.stdout.trim() === '') return null;

  let arr;
  try { arr = JSON.parse(proc.stdout); } catch { return null; }
  if (!Array.isArray(arr)) return null;

  const findings = arr.map((f) => ({
    severity: !f.code || RUFF_ERROR.test(f.code) ? 'error' : 'warning',
    file: f.filename,
    line: f.location && f.location.row,
    column: f.location && f.location.column,
    code: f.code,
    message: f.message,
  }));
  const errors = findings.filter((f) => f.severity === 'error').length;
  return { findings, errors, warnings: findings.length - errors, info: 0, hints: 0, total: findings.length };
}

// Run `tldr diagnostics` on a file. Returns the normalized diagnostics
// shape, or null on any failure.
function tldrDiagnostics(filePath) {
  // spawnSync, not execSync: tldr exits 1 whenever it has findings,
  // which is exactly when we want its stdout.
  const proc = spawnSync(TLDR, ['diagnostics', filePath, '--format', 'json'], {
    encoding: 'utf-8', timeout: 15000, windowsHide: true,
  });
  if (proc.error || !proc.stdout) return null;

  let diag;
  try { diag = JSON.parse(proc.stdout); } catch { return null; }

  // tldr emits { diagnostics: [...], summary: { errors, warnings, info, hints, total } }
  const findings = Array.isArray(diag.diagnostics) ? diag.diagnostics : [];
  const summary = diag.summary || {};
  return {
    findings,
    errors: summary.errors || 0,
    warnings: summary.warnings || 0,
    info: summary.info || 0,
    hints: summary.hints || 0,
    total: summary.total || findings.length,
  };
}

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { console.log('{}'); return; }

  const editTools = new Set(['Edit', 'Write', 'MultiEdit', 'Update']);
  if (!editTools.has(data.tool_name)) { console.log('{}'); return; }

  const filePath = (data.tool_input || {}).file_path || '';
  if (!filePath) { console.log('{}'); return; }

  const ext = extname(filePath);
  if (!ENABLED_EXTENSIONS.has(ext)) { console.log('{}'); return; }

  // Python fast path first; null (ruff missing/broken) falls back to tldr.
  let result = null;
  if (RUFF_EXTENSIONS.has(ext)) result = ruffDiagnostics(filePath);
  if (!result) result = tldrDiagnostics(filePath);
  if (!result) { console.log('{}'); return; }

  const { findings, errors, warnings, info, hints, total } = result;
  if (total === 0) { console.log('{}'); return; }

  // Build diagnostics summary — errors and warnings always, the quieter two only when present
  let counts = `${errors} errors, ${warnings} warnings`;
  if (info) counts += `, ${info} info`;
  if (hints) counts += `, ${hints} hints`;
  const lines = [`Diagnostics: ${counts}`];

  const ranked = [...findings].sort(
    (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9)
  );
  for (const f of ranked.slice(0, 5)) {
    const loc = f.column
      ? `${basename(f.file || filePath)}:${f.line}:${f.column}`
      : `${basename(f.file || filePath)}:${f.line}`;
    const code = f.code ? ` (${f.code})` : '';
    // pyright wraps long messages across lines — keep one finding per line
    const message = String(f.message).replace(/\s+/g, ' ').trim();
    lines.push(`   - ${loc}: ${message}${code}`);
  }
  if (ranked.length > 5) lines.push(`   ... and ${ranked.length - 5} more`);

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: lines.join('\n'),
    },
  }));
}

main();
