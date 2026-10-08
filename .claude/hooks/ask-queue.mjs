#!/usr/bin/env node
/**
 * PreToolUse:AskUserQuestion -> fleet question queue while the user is away.
 *
 * Away mode is the flag file ~/.claude/fleet/away (`fleet.py away on|off`). Without it
 * the hook does nothing and the clickable box opens as usual. A question box stops the
 * session until it is answered in that window, and a stopped session cannot read
 * cross-session messages; so while away every AskUserQuestion is DENIED and its
 * questions are appended to the session's own queue, ~/.claude/fleet/questions/<session_id>.json
 * (shape: tools/fleet/schema.md). The deny reason tells the model to list the questions to
 * the user in the same A-D format at the end of its reply, keep working on whatever does
 * not depend on the answers, and record an in-session answer with `fleet.py answer`.
 * The user can also answer from any session with /fleet questions; that answer arrives
 * as a message, which the still-running session can read.
 *
 * Allowed through (the box opens): calls whose every question carries the relay marker
 * `[fleet q-...]` (the /fleet questions box, where the user is present by definition),
 * and any call while FLEET_QUESTIONS=0.
 *
 * Fails open: malformed stdin, missing session_id or an IO error allows the box and
 * appends a line to ~/.claude/fleet/guard-errors.log. Only a deny prints output.
 */
import { readFileSync, writeFileSync, renameSync, mkdirSync, appendFileSync, existsSync } from 'node:fs';
import { join, basename } from 'node:path';
import { homedir } from 'node:os';
import { randomBytes } from 'node:crypto';

const FLEET = join(homedir(), '.claude', 'fleet');
const QDIR = join(FLEET, 'questions');
const AWAY = join(FLEET, 'away');
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const RELAY = /\[fleet q-[0-9a-f]{8}\]/;
const FLEET_PY = '~/.claude/tools/fleet/fleet.py';

const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

function logError(msg) {
  try {
    mkdirSync(FLEET, { recursive: true });
    appendFileSync(join(FLEET, 'guard-errors.log'), `${nowIso()} ask-queue ${String(msg).replace(/\s+/g, ' ').slice(0, 500)}\n`);
  } catch {}
}

const str = (v, n) => (typeof v === 'string' ? v.slice(0, n) : '');

function normalize(q) {
  const options = Array.isArray(q.options) ? q.options : [];
  return {
    id: `q-${randomBytes(4).toString('hex')}`,
    created_at: nowIso(),
    header: str(q.header, 60),
    question: str(q.question, 2000),
    options: options
      .filter((o) => o && typeof o === 'object')
      .map((o) => ({ label: str(o.label, 200), description: str(o.description, 1000) })),
    multi_select: q.multiSelect === true,
    status: 'pending',
    answer: null,
    answered_at: null,
    answered_via: null,
  };
}

function enqueue(input, questions) {
  const sid = input.session_id;
  const path = join(QDIR, `${sid}.json`);
  let queue;
  try {
    queue = JSON.parse(readFileSync(path, 'utf-8'));
  } catch {
    queue = null;
  }
  if (!queue || typeof queue !== 'object' || !Array.isArray(queue.questions)) {
    queue = { schema_version: 1, session_id: sid, questions: [] };
  }
  const cwd = typeof input.cwd === 'string' ? input.cwd : null;
  if (cwd) {
    queue.cwd = cwd;
    queue.project = basename(cwd);
  }
  const added = questions.map(normalize);
  queue.questions.push(...added);
  queue.updated_at = nowIso();
  mkdirSync(QDIR, { recursive: true });
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, JSON.stringify(queue, null, 2));
  renameSync(tmp, path);
  return added;
}

function reason(added) {
  const ids = added.map((q) => q.id).join(', ');
  return [
    `fleet question queue: the user is away, so no question box opens (a box would stop this session from reading messages).`,
    `Queued as ${ids}.`,
    `1) End your reply by listing these questions for the user in the same format: each question with its id, lettered A-D options, recommended option first with a one-line reason.`,
    `2) Continue with any work that does not depend on the answers; stop only if nothing is left.`,
    `3) When the user answers in this session, record each one: py -3.13 ${FLEET_PY} answer <id> "<answer>" (exit 1 = already answered elsewhere; use that answer).`,
    `Answers given from /fleet questions arrive as a message. Put questions still open into your handoff.`,
  ].join(' ');
}

function main() {
  if (process.env.FLEET_QUESTIONS === '0' || !existsSync(AWAY)) return;
  let input;
  try {
    input = JSON.parse(readFileSync(0, 'utf-8'));
  } catch (err) {
    logError(`bad stdin: ${err && err.message}`);
    return;
  }
  if (!input || input.tool_name !== 'AskUserQuestion') return;
  const questions = input.tool_input && Array.isArray(input.tool_input.questions) ? input.tool_input.questions : [];
  const objs = questions.filter((q) => q && typeof q === 'object');
  if (!objs.length) return;
  if (objs.every((q) => RELAY.test(String(q.question || '')))) return; // /fleet questions relay box
  if (typeof input.session_id !== 'string' || !SAFE_ID.test(input.session_id)) {
    logError('missing or unsafe session_id; box allowed');
    return;
  }
  let added;
  try {
    added = enqueue(input, objs);
  } catch (err) {
    logError(`enqueue failed: ${err && err.message}`);
    return;
  }
  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: reason(added),
    },
  }));
}

try { main(); } catch (err) { logError(`unexpected: ${err && err.message}`); }
