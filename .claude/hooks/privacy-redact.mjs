/**
 * Shared privacy rules for the hooks (library module, not an event hook).
 *
 * 1. Rule sources ported from tools/privacy_guard.py, used by fleet-audit.mjs:
 *    PLACEHOLDER_NAMES, SERVICE_ACCOUNTS, HOME_PATTERNS (ASCII), UUID_RE, escapeRe,
 *    loadTerms(cwd) (env CCV_PRIVACY_TERMS, ~/.claude/privacy-terms, the repo's
 *    .git/info/privacy-terms; worktrees: the common dir), usernames().
 * 2. A byte-for-byte port of privacy_guard.redact(), used by pre-compact.mjs:
 *      redact(text, { users, terms, key }) -> home prefix (every HOME_PATTERNS
 *        spelling) -> `~`; usernames (whole word) -> `<user>`; each term
 *        (case-insensitive, longest first, one pass) -> placeholderToken(term, key).
 *        Placeholder home names and existing `<private:..>` / `<user>` tokens stay;
 *        UUIDs stay. The key (loadOrCreateKey()) is read only when a term matches.
 *      redactHome(text) -> the home step alone (fail-open fallback).
 *      placeholderToken(term, key) -> `<private:XXXXXXXX>`, first 8 hex of
 *        HMAC-SHA256(key, casefold(term)).
 *      loadOrCreateKey(path = defaultKeyFile()) -> env CCV_PRIVACY_KEY_FILE or
 *        ~/.claude/privacy-key; created (parents too, mode 0600, exclusive) with
 *        32 random bytes when missing; under 32 bytes throws.
 *    Python semantics are emulated: \w / \s are Python's Unicode classes, re.I's
 *    extra i/İ/ı (and a few Greek) equivalences, str.casefold(). Not ported: the
 *    repo allowlist `.privacy-allow` (repo-specific; holds only UUID entries, which
 *    redact never touches). Known gap: characters newer than Python's Unicode
 *    database (case/word data from Node's ICU) can match differently.
 */
import { readFileSync, statSync, mkdirSync, openSync, writeSync, closeSync } from 'fs';
import { homedir, userInfo } from 'os';
import { join, dirname, resolve } from 'path';
import { createHmac, randomBytes } from 'crypto';

// --- rule sources (shared with fleet-audit.mjs; behaviour unchanged there) ---

export const PLACEHOLDER_NAMES = new Set(['x', 'user', 'name', 'you']);
export const SERVICE_ACCOUNTS = new Set(['runner', 'runneradmin', 'root']);
export const HOME_PATTERNS = [
  /(?<![A-Za-z])(?<pre>[A-Za-z]:(?:\/|\\+)Users(?:\/|\\+))(?<name><[^>\s]*>|[^/\\\s"'`<>:;,|*?()[\]{}]+)/gi,
  /(?<![\w.])(?<pre>\/[A-Za-z]\/Users\/)(?<name><[^>\s]*>|[^/\\\s"'`<>:;,|*?()[\]{}]+)/gi,
  /(?<![A-Za-z])(?<pre>[A-Za-z]--Users-)(?<name><[^>\s]*>|[A-Za-z0-9_.]+)/gi,
];
export const UUID_RE = /(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])/gi;

export const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export function fileLines(p) {
  try {
    return readFileSync(p, 'utf-8').replace(/^﻿/, '').split(/\r?\n/)
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
  } catch { return []; }
}

// .git/info/privacy-terms of the repo containing cwd (worktrees: the common dir).
export function cloneTermsFile(cwd) {
  let dir = cwd;
  for (let i = 0; dir && i < 40; i++) {
    const dotgit = join(dir, '.git');
    let st = null;
    try { st = statSync(dotgit); } catch {}
    if (st && st.isDirectory()) return join(dotgit, 'info', 'privacy-terms');
    if (st && st.isFile()) {
      let body = '';
      try { body = readFileSync(dotgit, 'utf-8'); } catch { return null; }
      const m = body.match(/^gitdir:\s*(.+)$/m);
      if (!m) return null;
      const gitdir = resolve(dir, m[1].trim());
      let common = gitdir;
      try { common = resolve(gitdir, readFileSync(join(gitdir, 'commondir'), 'utf-8').trim()); } catch {}
      return join(common, 'info', 'privacy-terms');
    }
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

export function loadTerms(cwd) {
  const env = (process.env.CCV_PRIVACY_TERMS || '').split(/[,\r\n]/)
    .map((s) => s.trim()).filter((s) => s && !s.startsWith('#'));
  const clone = cwd ? cloneTermsFile(cwd) : null;
  const all = [...env, ...fileLines(join(homedir(), '.claude', 'privacy-terms')), ...(clone ? fileLines(clone) : [])];
  const seen = new Map(); // first spelling wins, like privacy_guard.load_terms
  for (const t of all) if (!seen.has(t.toLowerCase())) seen.set(t.toLowerCase(), t);
  return [...seen.values()];
}

export function usernames() {
  const names = [];
  try { names.push(userInfo().username); } catch {}
  names.push(process.env.USERNAME || '', process.env.USER || '');
  const seen = new Map();
  for (const n of names) {
    if (n && n.length >= 3 && !SERVICE_ACCOUNTS.has(n.toLowerCase())) seen.set(n.toLowerCase(), n);
  }
  return [...seen.values()];
}

// --- reversible redaction: port of privacy_guard.redact() ---

const KEY_BYTES = 32;
const TOKEN_HEX = 8;
const USER_TOKEN = '<user>';
const HOME_TOKEN = '~';
// Python str regex classes: \s (str.isspace) and \w (letters, numbers, `_`)
const PY_S = String.raw`\t\n\v\f\r\x1c-\x20\x85\xa0  -     　`;
const PY_W = String.raw`\p{L}\p{N}_`;
// re.IGNORECASE equivalences that JS `iu` case folding lacks
const PY_CI_EXTRA = [
  ['i', 'I', 'İ', 'ı'],
  ['ΐ', 'ΐ'],
  ['ΰ', 'ΰ'],
  ['θ', 'ϑ', 'ϴ', 'Θ'],
  ['ﬅ', 'ﬆ'],
];
const CI_CLASS = new Map();
for (const group of PY_CI_EXTRA) {
  const cls = `[${group.join('')}]`;
  for (const ch of group) CI_CLASS.set(ch, cls);
}
const AZ = 'A-Za-zİı'; // [A-Za-z] under Python re.I also matches İ and ı

// Literal string -> `iu` pattern matching what Python re.escape(s) matches under re.I.
function pyLiteral(s) {
  let out = '';
  for (const ch of s) out += CI_CLASS.get(ch) || escapeRe(ch);
  return out;
}

const NAME_CLASS = '[^/\\\\' + PY_S + '"\'`<>:;,|*?()\\[\\]{}]+';
const PY_HOME_PATTERNS = [
  new RegExp(String.raw`(?<![${AZ}])(?<pre>[${AZ}]:(?:\/|\\+)Users(?:\/|\\+))(?<name><[^>${PY_S}]*>|${NAME_CLASS})`, 'giu'),
  new RegExp(String.raw`(?<![${PY_W}.])(?<pre>\/[${AZ}]\/Users\/)(?<name><[^>${PY_S}]*>|${NAME_CLASS})`, 'giu'),
  new RegExp(String.raw`(?<![${AZ}])(?<pre>[${AZ}]--Users-)(?<name><[^>${PY_S}]*>|[${AZ}0-9_.]+)`, 'giu'),
];
const KEEP = `(?<keep>${pyLiteral('<private:')}[0-9a-f]{8}>|${pyLiteral(USER_TOKEN)})`;

const isPlaceholder = (name) => name.startsWith('<') || PLACEHOLDER_NAMES.has(name.toLowerCase());
const pyLen = (s) => [...s].length;

// Python str.casefold() for one code point.
function foldChar(ch) {
  const c = ch.codePointAt(0);
  if (c === 0x131) return ch; // dotless i has no case fold
  if ((c >= 0x13a0 && c <= 0x13fd) || (c >= 0xab70 && c <= 0xabbf)) return ch.toUpperCase(); // Cherokee folds up
  return ch.toLowerCase().toUpperCase().toLowerCase(); // full folding (ß -> ss), per char: no final-sigma context
}

export function casefold(s) {
  let out = '';
  for (const ch of s) out += foldChar(ch);
  return out;
}

export function defaultKeyFile() {
  return process.env.CCV_PRIVACY_KEY_FILE || join(homedir(), '.claude', 'privacy-key');
}

export function loadOrCreateKey(path = defaultKeyFile()) {
  let data;
  try {
    data = readFileSync(path);
  } catch (err) {
    if (!err || err.code !== 'ENOENT') throw err;
    mkdirSync(dirname(path), { recursive: true });
    let fd;
    try {
      fd = openSync(path, 'wx', 0o600);
    } catch (e) {
      if (!e || e.code !== 'EEXIST') throw e;
      fd = null; // another process created it first
    }
    if (fd === null) data = readFileSync(path);
    else {
      data = randomBytes(KEY_BYTES);
      try { writeSync(fd, data); } finally { closeSync(fd); }
    }
  }
  if (data.length < KEY_BYTES) throw new Error(`privacy key ${path} is shorter than ${KEY_BYTES} bytes`);
  return data;
}

export function placeholderToken(term, key) {
  const hex = createHmac('sha256', key).update(casefold(term), 'utf8').digest('hex');
  return `<private:${hex.slice(0, TOKEN_HEX)}>`;
}

export function redactHome(text) {
  for (const re of PY_HOME_PATTERNS) {
    text = text.replace(re, (...args) => {
      const groups = args[args.length - 1];
      return isPlaceholder(groups.name) ? args[0] : HOME_TOKEN;
    });
  }
  return text;
}

// privacy_guard.redact(text, Guard(users, terms, []), key) in JS; users/terms default to
// usernames() / loadTerms(cwd), key to loadOrCreateKey() on the first term match.
export function redact(text, { users = usernames(), terms, key = null, cwd = process.cwd() } = {}) {
  text = redactHome(text);
  const termList = [...(terms ?? loadTerms(cwd))].sort((a, b) => pyLen(b) - pyLen(a));
  const seen = new Map();
  for (const n of users) if (n && pyLen(n) >= 3 && !seen.has(n.toLowerCase())) seen.set(n.toLowerCase(), n);
  const userList = [...seen.values()].sort((a, b) => pyLen(b) - pyLen(a));
  const alts = [KEEP, ...termList.map((t, i) => `(?<t${i}>${pyLiteral(t)})`)];
  if (userList.length) {
    alts.push(`(?<user>(?<![${PY_W}])(?:${userList.map(pyLiteral).join('|')})(?![${PY_W}]))`);
  }
  if (alts.length === 1) return text;
  let k = key;
  return text.replace(new RegExp(alts.join('|'), 'giu'), (...args) => {
    const groups = args[args.length - 1];
    if (groups.keep !== undefined) return args[0];
    if (groups.user !== undefined) return USER_TOKEN;
    if (k === null) k = loadOrCreateKey();
    for (let i = 0; i < termList.length; i++) {
      if (groups[`t${i}`] !== undefined) return placeholderToken(termList[i], k);
    }
    return args[0];
  });
}
