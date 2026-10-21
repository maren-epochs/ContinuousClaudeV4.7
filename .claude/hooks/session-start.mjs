#!/usr/bin/env node
/**
 * SessionStart hook — inject knowledge once per session.
 *
 *   startup | clear → `bloks context <cwd>` (scored, nack-filtered rule/taste cards)
 *   compact         → latest handoff from the handoff root, so work resumes from the
 *                     auto-handoff pre-compact.mjs just wrote instead of a lossy summary
 *   resume | fork   → nothing (the transcript already carries the context)
 *
 * Handoff root: project thoughts/shared/handoffs/ if it exists, else
 * ~/.claude/handoffs/<project-dir-basename>/ (same rule as pre-compact/status).
 *
 * Env: SESSION_START_CAP (chars per injection, default 6000), BLOKS_BIN (tests).
 * Fail-open: any error → '{}'. Output via hookSpecificOutput.additionalContext.
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'fs';
import { spawnSync } from 'child_process';
import { join, basename, resolve } from 'path';
import { homedir } from 'os';

const WIN = process.platform === 'win32';
const CAP = Number(process.env.SESSION_START_CAP) || 6000;
const CARGO_BLOKS = join(homedir(), '.cargo', 'bin', WIN ? 'bloks.exe' : 'bloks');
const BLOKS = process.env.BLOKS_BIN || (existsSync(CARGO_BLOKS) ? CARGO_BLOKS : 'bloks');

function cap(text) {
  return text.length > CAP ? `${text.slice(0, CAP)}\n[... truncated at ${CAP} chars]` : text;
}

function bloksContext(cwd) {
  // A .mjs/.js BLOKS_BIN runs under node (tests: Windows can't spawn shebang scripts).
  const [cmd, pre] = /\.m?js$/.test(BLOKS) ? [process.execPath, [BLOKS]] : [BLOKS, []];
  const proc = spawnSync(cmd, [...pre, 'context', cwd], { encoding: 'utf-8', timeout: 5000, windowsHide: true });
  if (proc.error || proc.status !== 0 || !proc.stdout || !proc.stdout.trim()) return null;
  return cap(proc.stdout.trim());
}

function handoffRoot(cwd) {
  const local = join(cwd, 'thoughts', 'shared', 'handoffs');
  return existsSync(local) ? local : join(homedir(), '.claude', 'handoffs', basename(resolve(cwd)));
}

function latestHandoff(cwd) {
  const root = handoffRoot(cwd);
  if (!existsSync(root)) return null;
  let best = null;
  (function walk(d) {
    for (const e of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(ya?ml|md)$/.test(e.name)) {
        const m = statSync(p).mtimeMs;
        if (!best || m > best.m) best = { p, m };
      }
    }
  })(root);
  return best && best.p;
}

function main() {
  let data = {};
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch {}
  if (!data || typeof data !== 'object') data = {};
  // Launch dir first, matching pre-compact.mjs (CLAUDE_PROJECT_DIR), so a cd'd subdir
  // still finds the handoff written just before compaction.
  const cwd = process.env.CLAUDE_PROJECT_DIR
    || (typeof data.cwd === 'string' && data.cwd ? data.cwd : process.cwd());
  const source = data.source || 'startup';

  let context = null;
  if (source === 'startup' || source === 'clear') {
    const cards = bloksContext(cwd);
    if (cards) context = `bloks knowledge cards for this project (rules ranked by feedback; act on them):\n${cards}`;
  } else if (source === 'compact') {
    const f = latestHandoff(cwd);
    if (f) context = `Context was just compacted. Latest handoff (${f.replace(/\\/g, '/')}):\n${cap(readFileSync(f, 'utf-8'))}`;
  }

  if (!context) { console.log('{}'); return; }
  console.log(JSON.stringify({ hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: context } }));
}

try { main(); } catch { console.log('{}'); }
