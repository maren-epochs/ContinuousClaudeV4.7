#!/usr/bin/env node
/**
 * Stop hook - blocks a reply that hands the user a task Claude could do itself.
 *
 * The rule "never hand the user a task Claude can do" lives in CLAUDE.md and memory,
 * and still fails when Claude does not see a task as its own (2026-11-19: "if you send
 * a screenshot", when a screenshot was one PowerShell call away). This turns the rule
 * into a check: the final reply is scanned for hand-over phrasing ("if you send",
 * "can you run", "send me a screenshot", "you'll need to run", ...) and the stop is
 * blocked ONCE with the matched phrase, telling Claude to try it first.
 *
 * Reply text: `last_assistant_message` from the hook input when Claude Code sends it,
 * else the assistant text after the last user prompt in transcript_path. Quoted text
 * ("...", `...`, code blocks, > lines) is ignored, so quoting the user does not trip it.
 * A reply that names an actual denial ("denied") together with a `! ` line passes: that
 * is the allowed hand-over. stop_hook_active (the retry after a block) always passes,
 * so it never loops. Fails open on any error. HANDOVER_CHECK=0 disables.
 */
import { readFileSync, existsSync, statSync, openSync, readSync, closeSync } from 'fs';

const TAIL_BYTES = 512 * 1024;

// Each pattern is a hand-over aimed at the user. Kept narrow: phrasing Claude uses for
// its own actions ("I'll run", "I took a screenshot") must not match.
export const PATTERNS = [
  /\bif you (?:can )?(?:send|paste|share|run|take|open|try|check|restart|click|type)\b/i,
  /\b(?:can|could|would) you (?:please )?(?:send|paste|share|run|take|open|try|check|restart|click|type|confirm what)\b/i,
  /\b(?:send|paste|share|show) me (?:a |the |your )?(?:screenshot|output|error|log|result)/i,
  /\bplease (?:run|send|paste|take|open|restart|click|type)\b/i,
  /\byou(?:'ll| will)? (?:need|have) to (?:run|open|restart|click|install|type|paste|send|take)\b/i,
  /\b(?:run|try) (?:this|that|it|these) yourself\b/i,
  /\b(?:take|grab|send) (?:me )?a screenshot\b(?! (?:of|myself))/i,
  /\blet me know what (?:it|you) (?:says|see|shows)\b/i,
  /\b(?:and|then) (?:tell|show) me what (?:it|you|they) (?:says?|sees?|shows?|gets?)\b/i,
  /\btell me what (?:it|the \w+) (?:says|shows)\b/i,
];

// Text without quoted material: fenced/inline code, quote lines, "..." and “...”.
export function unquoted(text) {
  return String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`[^`\n]*`/g, ' ')
    .replace(/^\s*>.*$/gm, ' ')
    .replace(/"[^"\n]*"/g, ' ')
    .replace(/“[^”\n]*”/g, ' ');
}

// The first hand-over phrase in the reply, or null.
export function handover(text) {
  const raw = String(text || '');
  if (/\bdenied\b/i.test(raw) && /^\s*! \S/m.test(raw)) return null;
  const body = unquoted(raw);
  for (const p of PATTERNS) {
    const g = new RegExp(p.source, p.flags.includes('g') ? p.flags : p.flags + 'g');
    for (const m of body.matchAll(g)) {
      if (!selfAction(body.slice(Math.max(0, m.index - 40), m.index))) return m[0];
    }
  }
  return null;
}

// Text right before a match that makes it Claude's own step: "I'll take a screenshot",
// "we will run", "let me open". Only a first-person subject plus an optional modal and
// one adverb; "I need you to take" still counts as a hand-over.
const SELF_PREFIX = /(?:\b(?:I|we)(?:'ll|'d|'m going to| will| would| can| could| shall| am going to| are going to)?|\blet me)(?: (?:then|also|now|first|next|just))? $/i;
export function selfAction(before) {
  return SELF_PREFIX.test(before);
}

// Assistant text after the last real user prompt (tool results are user entries too).
function replyFromTranscript(path) {
  if (!path || !existsSync(path)) return '';
  const size = statSync(path).size;
  const len = Math.min(size, TAIL_BYTES);
  const buf = Buffer.alloc(len);
  const fd = openSync(path, 'r');
  try { readSync(fd, buf, 0, len, size - len); } finally { closeSync(fd); }
  const parts = [];
  for (const line of buf.toString('utf-8').split('\n')) {
    let e;
    try { e = JSON.parse(line); } catch { continue; }
    if (!e || e.isSidechain || !e.message) continue;
    const c = e.message.content;
    if (e.type === 'user') {
      const prompt = typeof c === 'string' || (Array.isArray(c) && c.some(b => b && b.type === 'text'));
      if (prompt) parts.length = 0;
    } else if (e.type === 'assistant' && Array.isArray(c)) {
      for (const b of c) if (b && b.type === 'text' && b.text) parts.push(b.text);
    }
  }
  return parts.length ? parts[parts.length - 1] : '';
}

function main() {
  let data = {};
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch {}
  if (process.env.HANDOVER_CHECK === '0' || data.stop_hook_active) { console.log('{}'); return; }
  const text = typeof data.last_assistant_message === 'string' && data.last_assistant_message
    ? data.last_assistant_message
    : replyFromTranscript(data.transcript_path);
  const hit = handover(text);
  if (!hit) { console.log('{}'); return; }
  console.log(JSON.stringify({
    decision: 'block',
    reason: `Your reply hands the user a task ("${hit}"). Do it yourself first: run the command, `
      + 'look at the screen with `py -3.13 ~/.claude/tools/fleet/fleet.py screenshot` and Read the PNGs, '
      + 'drive the app or tab yourself. Then rewrite the reply with the result. Hand over only what truly '
      + 'needs the user (credentials, the physical console, a denial in this session) and say why.',
  }));
}

if (process.argv[1] && process.argv[1].endsWith('handover-check.mjs')) {
  try { main(); } catch { console.log('{}'); }
}
