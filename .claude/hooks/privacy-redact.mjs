/**
 * Shared privacy rules for the hooks (library module, not an event hook).
 *
 * 1. Rule sources ported from tools/privacy_guard.py, used by fleet-audit.mjs:
 *    PLACEHOLDER_NAMES, SERVICE_ACCOUNTS, HOME_PATTERNS (ASCII), UUID_RE, escapeRe,
 *    loadTerms(cwd) (env CCV_PRIVACY_TERMS, ~/.claude/privacy-terms, the repo's
 *    .git/info/privacy-terms; worktrees: the common dir), usernames().
 * 2. A byte-for-byte port of privacy_guard.redact(), used by pre-compact.mjs:
 *      redact(text, { users, terms, key, homes }) -> home prefix (every HOME_PATTERNS
 *        spelling; the known home names, homes = homeNames() plus the users, are
 *        consumed whole even with spaces, hyphens or dots) -> `~`; usernames
 *        (whole word) -> `<user>`; each term
 *        (case-insensitive, longest first, one pass) -> placeholderToken(term, key).
 *        Placeholder home names and existing `<private:..>` / `<user>` tokens stay;
 *        UUIDs stay. The key (loadOrCreateKey()) is read only when a term matches.
 *      redactHome(text, { homes, users }) -> the home step alone (fail-open fallback).
 *      homeNames() -> basenames of os.homedir(), $HOME, $USERPROFILE.
 *      placeholderToken(term, key) -> `<private:XXXXXXXX>`, first 8 hex of
 *        HMAC-SHA256(key, casefold(term)).
 *      loadOrCreateKey(path = defaultKeyFile()) -> env CCV_PRIVACY_KEY_FILE or
 *        ~/.claude/privacy-key; when missing, 32 random bytes are written to a temp
 *        file beside it and linked into place without replacing (concurrent
 *        creators converge on one key, never a partial one). A 0-byte file (creator
 *        killed before writing; it never made a token) is waited on, then replaced
 *        under `<key>.lock` once older than KEY_STALE_S; 1-31 bytes throws.
 *    Python semantics are emulated: \w / \s are Python's Unicode classes, re.I's
 *    extra i/İ/ı (and a few Greek) equivalences, str.casefold(). Not ported: the
 *    repo allowlist `.privacy-allow` (repo-specific; holds only UUID entries, which
 *    redact never touches). Known gap: characters newer than Python's Unicode
 *    database (case/word data from Node's ICU) can match differently.
 * 3. Credential redaction (moved from fleet-audit.mjs), used by fleet-audit.mjs and
 *    pre-compact.mjs: secretSpans(text) -> [start, end) of every credential shape
 *    (SECRET_PATTERNS, attached mysql -p<pass>, ConvertTo-SecureString plaintext);
 *    applySpans(text, spans, limit) -> text[0, limit) with spans replaced by REDACTED;
 *    redactSecrets(text, { limit }) -> both. Every regex is bounded and linear.
 *    fleet-audit.mjs keeps a built-in copy for partial installs; its test checks parity.
 */
import {
  readFileSync, statSync, mkdirSync, openSync, writeSync, closeSync, fsyncSync, linkSync,
  renameSync, unlinkSync, copyFileSync, constants as fsConstants,
} from 'fs';
import { homedir, userInfo } from 'os';
import { join, dirname, resolve, basename } from 'path';
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
// a 0-byte key (or its lock) older than this, or dated this far in the future
// (clock moved back), is an abandoned create
const KEY_STALE_S = 3;
const KEY_WAIT_S = 10; // overall bound on waiting for / repairing the key file
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

const NAME_CHAR = '[^/\\\\' + PY_S + '"\'`<>:;,|*?()\\[\\]{}]'; // one char of a generic home-dir name
const DIR_NAME_CHAR = `[${AZ}0-9_.]`; // ... in the transcript-dir form
const HOME_PRE = [
  String.raw`(?<![${AZ}])(?<pre>[${AZ}]:(?:\/|\\+)Users(?:\/|\\+))`,
  String.raw`(?<![${PY_W}.])(?<pre>\/[${AZ}]\/Users\/)`,
  String.raw`(?<![${AZ}])(?<pre>[${AZ}]--Users-)`,
];
// privacy_guard._home_regexes: known / knownDir alternations are tried before the generic
// name, each only as a whole name (no generic name char may follow).
function homeRegexes(known = '', knownDir = '') {
  const k1 = known ? `(?:${known})(?!${NAME_CHAR})|` : '';
  const k3 = knownDir ? `(?:${knownDir})(?!${DIR_NAME_CHAR})|` : '';
  const names = [
    `(?<name><[^>${PY_S}]*>|${k1}${NAME_CHAR}+)`,
    `(?<name><[^>${PY_S}]*>|${k1}${NAME_CHAR}+)`,
    `(?<name><[^>${PY_S}]*>|${k3}${DIR_NAME_CHAR}+)`,
  ];
  return HOME_PRE.map((pre, i) => new RegExp(pre + names[i], 'giu'));
}
const PY_HOME_PATTERNS = homeRegexes();
const KEEP = `(?<keep>${pyLiteral('<private:')}[0-9a-f]{8}>|${pyLiteral(USER_TOKEN)})`;

const isPlaceholder = (name) => name.startsWith('<') || PLACEHOLDER_NAMES.has(name.toLowerCase());
const pyLen = (s) => [...s].length;
const byLenDesc = (xs) => [...xs].sort((a, b) => pyLen(b) - pyLen(a));

// privacy_guard.home_names(): basenames of this machine's home dir.
export function homeNames() {
  const paths = [];
  try { paths.push(homedir()); } catch {}
  paths.push(process.env.HOME || '', process.env.USERPROFILE || '');
  const out = [];
  for (const p of paths) {
    const name = p.replace(/[\\/]+$/, '').split(/[\\/]/).pop();
    if (name && !name.endsWith(':') && !out.includes(name)) out.push(name);
  }
  return out;
}

// Guard.usernames: length >= 3, first spelling of each lowercased name.
function guardUsers(users) {
  const seen = new Map();
  for (const n of users) if (n && pyLen(n) >= 3 && !seen.has(n.toLowerCase())) seen.set(n.toLowerCase(), n);
  return [...seen.values()];
}

// privacy_guard.home_regexes(): HOME_PATTERNS consuming each known home name whole.
function knownHomeRegexes(names) {
  const known = [...new Set(names)].filter((n) => n && !isPlaceholder(n));
  if (!known.length) return PY_HOME_PATTERNS;
  const dirNames = [...new Set([...known, ...known.map((n) => n.replace(/[^A-Za-z0-9]/gu, '-'))])];
  const alt = (xs) => byLenDesc(xs).map(pyLiteral).join('|');
  return homeRegexes(alt(known), alt(dirNames));
}

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

const sleepMs = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
const BUSY = new Set(['EPERM', 'EACCES', 'EBUSY']); // Windows: a racer holds or replaces the file
const isBusy = (e) => process.platform === 'win32' && e && BUSY.has(e.code);

// src visible as dst in one step, never replacing an existing dst.
function linkNoReplace(src, dst) {
  try { linkSync(src, dst); } catch (e) {
    if (e && e.code === 'EEXIST') return;
    if (isBusy(e)) return; // the caller's loop retries
    if (process.platform !== 'win32') throw e;
    try { copyFileSync(src, dst, fsConstants.COPYFILE_EXCL); } // no hard links (e.g. FAT)
    catch (e2) { if (!e2 || (e2.code !== 'EEXIST' && !isBusy(e2))) throw e2; }
  }
}

// Temp file beside path holding 32 fresh random bytes (mode 0600, synced).
function newKeyFile(path) {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = join(dirname(path), `.${basename(path)}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`);
  const fd = openSync(tmp, 'wx', 0o600);
  try { writeSync(fd, randomBytes(KEY_BYTES)); fsyncSync(fd); } finally { closeSync(fd); }
  return tmp;
}

const unlinkQuiet = (p) => { try { unlinkSync(p); } catch {} };

function publishKey(path) {
  const tmp = newKeyFile(path);
  try { linkNoReplace(tmp, path); } finally { unlinkQuiet(tmp); }
}

// An abandoned 0-byte key replaced in one step (rename over it, so the path never goes
// missing) under an exclusive `<key>.lock`, emptiness re-checked under the lock.
function replaceEmptyKey(path) {
  const lock = `${path}.lock`;
  try { closeSync(openSync(lock, 'wx', 0o600)); } catch (e) {
    if (!e || (e.code !== 'EEXIST' && !isBusy(e))) throw e;
    try { if (Math.abs(Date.now() - statSync(lock).mtimeMs) >= KEY_STALE_S * 1000) unlinkSync(lock); } catch {}
    sleepMs(20);
    return;
  }
  let tmp = null;
  try {
    if (statSync(path).size === 0) {
      tmp = newKeyFile(path);
      renameSync(tmp, path);
    }
  } catch (e) {
    if (!e || (e.code !== 'ENOENT' && !isBusy(e))) throw e;
  } finally {
    if (tmp) unlinkQuiet(tmp);
    unlinkQuiet(lock);
  }
}

// [bytes or null when missing, age in s of an empty file]
function readKey(path, deadline) {
  for (;;) {
    try {
      const data = readFileSync(path);
      return [data, data.length ? 0 : (Date.now() - statSync(path).mtimeMs) / 1000];
    } catch (e) {
      if (e && e.code === 'ENOENT') return [null, 0];
      if (!isBusy(e) || Date.now() > deadline) throw e;
      sleepMs(10);
    }
  }
}

export function loadOrCreateKey(path = defaultKeyFile()) {
  const deadline = Date.now() + KEY_WAIT_S * 1000;
  for (;;) {
    const [data, age] = readKey(path, deadline);
    if (data && data.length >= KEY_BYTES) return data;
    if (data && data.length) {
      throw new Error(`privacy key ${path} is corrupt (${data.length} bytes, expected ${KEY_BYTES}); `
        + 'not replaced because redacted files may use it: restore it from a backup, or move it aside to start a new key');
    }
    if (Date.now() > deadline) throw new Error(`privacy key ${path} could not be created`);
    if (data === null) publishKey(path);
    else if (Math.abs(age) >= KEY_STALE_S) replaceEmptyKey(path); // negative: mtime in the future
    else sleepMs(50);
  }
}

export function placeholderToken(term, key) {
  const hex = createHmac('sha256', key).update(casefold(term), 'utf8').digest('hex');
  return `<private:${hex.slice(0, TOKEN_HEX)}>`;
}

export function redactHome(text, { homes = homeNames(), users = usernames() } = {}) {
  for (const re of knownHomeRegexes([...homes, ...guardUsers(users)])) {
    text = text.replace(re, (...args) => {
      const groups = args[args.length - 1];
      return isPlaceholder(groups.name) ? args[0] : HOME_TOKEN;
    });
  }
  return text;
}

// privacy_guard.redact(text, Guard(users, terms, [], homes), key) in JS; users/terms/homes
// default to usernames() / loadTerms(cwd) / homeNames(), key to loadOrCreateKey() on the
// first term match.
export function redact(text, { users = usernames(), terms, key = null, cwd = process.cwd(), homes = homeNames() } = {}) {
  const guarded = guardUsers(users);
  text = redactHome(text, { homes, users: guarded });
  const termList = byLenDesc(terms ?? loadTerms(cwd));
  const userList = byLenDesc(guarded);
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

// --- credential redaction (moved from fleet-audit.mjs; used by fleet-audit and pre-compact) ---

export const REDACTED = '<redacted>';

// Credential shapes; group `s` (when present) is the secret, else the whole match.
// Linear by construction: every quantifier is bounded and none is nested.
const VAL = String.raw`(?<s>\\"[^"]{0,512}"|"[^"]{0,512}"|'[^']{0,512}'|[^\s;|&]{1,512})`;
const SECRET_NAME = String.raw`(?:token|password|passwd|secret|api[_-]?key)`;
export const SECRET_PATTERNS = [
  new RegExp(String.raw`[A-Za-z0-9_]{0,64}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Za-z0-9_]{0,64}[ \t]{0,8}=[ \t]{0,8}${VAL}`, 'dgi'),
  new RegExp(String.raw`(?<![\w-])--?[\w-]{0,32}${SECRET_NAME}[\w-]{0,32}(?:[=:]|[ \t]{1,8})${VAL}`, 'dgi'),
  // JSON / YAML key: value (optionally quoted or \"-escaped key)
  new RegExp(String.raw`(\\?["']?)[\w-]{0,32}${SECRET_NAME}[\w-]{0,32}\1[ \t]{0,8}:[ \t]{0,8}(?<s>\\"[^"]{0,512}"|"[^"]{0,512}"|'[^']{0,512}'|[^\s,;}'"|&]{1,512})`, 'dgi'),
  // headers: x-*-key/token/auth/secret, Authorization, Private-Token
  /(?<![\w-])(?:x-[\w-]{0,32}(?:key|token|auth|secret)[\w-]{0,32}|authorization|proxy-authorization|private-token)[ \t]{0,8}:[ \t]{0,8}(?:(?:Bearer|Basic|Token|Digest)[ \t]{1,8})?(?<s>[^\s"']{1,512})/dgi,
  /\b(?:Bearer|Basic)[ \t]{1,8}(?<s>[A-Za-z0-9._~+/=-]{6,512})/dgi,
  // URL userinfo (user:pass@ or a bare token@), except the conventional ssh `git@`
  /:\/\/(?!git@)(?<s>[^\s/@:]{1,256}(?::[^\s/@]{0,256})?)@/dg,
  // curl -u / --user user:pass (needs the colon, so `git push -u origin` is untouched)
  /(?<![\w-])(?:-u|--user)(?:[ \t]{1,8}|=)?(?<s>"[^"\s]{0,256}:[^"]{1,256}"|'[^'\s]{0,256}:[^']{1,256}'|[^\s"':;|&]{0,256}:[^\s"';|&]{1,256})/dg,
  /(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|glpat-[A-Za-z0-9_-]{16,255}|sk-[A-Za-z0-9_-]{16,255}|[sr]k_(?:live|test)_[A-Za-z0-9]{8,255}|npm_[A-Za-z0-9]{20,255}|hf_[A-Za-z0-9]{20,255}|xox[abprs]-[A-Za-z0-9-]{10,255}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,255})/dg,
];
const MYSQL_RE = /(?<![\w.-])(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlshow|mysqlcheck|mariadb(?:-dump|-admin)?)(?:\.exe)?(?![\w-])/i;
const MYSQL_PW_RE = /(?<!\S)-p(?<s>"[^"]{0,256}"|'[^']{0,256}'|[^\s;|&]{1,256})/dg;
const SECURE_RE = /ConvertTo-SecureString(?![\w-])/gi;
const PS_TOKEN_RE = /[ \t]{1,8}(?<t>-[A-Za-z]{1,32}(?::[^\s;|&]{0,64})?|"[^"]{0,512}"|'[^']{0,512}'|[^\s;|&()]{1,512})/y;

// [start, end) of the value piped into the command at `at` (`'x' | ConvertTo-...`), else null
function pipedValue(text, at) {
  const ws = (c) => c === ' ' || c === '\t';
  let j = at - 1;
  while (j >= 0 && ws(text[j])) j--;
  if (text[j] !== '|' || text[j - 1] === '|') return null;
  j--;
  while (j >= 0 && ws(text[j])) j--;
  if (j < 0) return null;
  const end = j + 1;
  if (text[j] === '"' || text[j] === "'") {
    const k = text.lastIndexOf(text[j], j - 1);
    return k >= 0 && end - k <= 514 ? [k, end] : null;
  }
  let k = j;
  while (k >= 0 && end - k <= 512 && !/[\s|;&]/.test(text[k])) k--;
  return [k + 1, end];
}

// ConvertTo-SecureString's plaintext: -String value, first positional, or the piped-in value.
function secureStringSpans(text, spans) {
  for (const m of text.matchAll(SECURE_RE)) {
    let pos = m.index + m[0].length;
    let found = false;
    let takeNext = false;
    let skipNext = false;
    for (let k = 0; k < 8; k++) {
      PS_TOKEN_RE.lastIndex = pos;
      const t = PS_TOKEN_RE.exec(text);
      if (!t) break;
      pos = PS_TOKEN_RE.lastIndex;
      const tok = t.groups.t;
      const start = pos - tok.length;
      if (skipNext) { skipNext = false; continue; }
      if (!takeNext && tok.startsWith('-')) {
        const name = tok.slice(1).split(':')[0].toLowerCase();
        const inline = tok.includes(':');
        if ('string'.startsWith(name) && name.length >= 1) {
          if (inline) { spans.push([start + name.length + 2, pos]); found = true; break; }
          takeNext = true;
        } else if (['key', 'securekey'].includes(name) && !inline) skipNext = true;
        continue;
      }
      spans.push([start, pos]);
      found = true;
      break;
    }
    if (!found) { const v = pipedValue(text, m.index); if (v) spans.push(v); }
  }
}

// Appends the [start, end) span of every credential in text to spans (returned):
// KEY=v, --token v, JSON/YAML "token"/"password"/"secret"/"api_key": v, x-*-key /
// Authorization / Private-Token headers, Bearer/Basic, URL userinfo, curl -u user:pass,
// gh*_/github_pat_/glpat-/sk-/sk_live_/sk_test_/rk_/npm_/hf_/xox*-/AKIA/AIza tokens,
// ConvertTo-SecureString plaintext, attached mysql -p<pass>.
export function secretSpans(text, spans = []) {
  const secret = (m) => (m.indices.groups && m.indices.groups.s) || m.indices[0];
  for (const re of SECRET_PATTERNS) {
    for (const m of text.matchAll(re)) spans.push(secret(m));
  }
  if (MYSQL_RE.test(text)) for (const m of text.matchAll(MYSQL_PW_RE)) spans.push(secret(m));
  secureStringSpans(text, spans);
  return spans;
}

// text[0, limit) with each span replaced by REDACTED (overlapping spans merge); spans may
// reach past limit, so a secret crossing the cut is still removed.
export function applySpans(text, spans, limit = text.length) {
  spans.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let out = '', pos = 0;
  for (const [s, e] of spans) {
    if (s >= limit) break;
    if (e <= pos) continue;
    if (s >= pos) { out += text.slice(pos, s) + REDACTED; pos = e; }
    else pos = e; // overlap: extend the redaction already emitted
  }
  return pos < limit ? out + text.slice(pos, limit) : out;
}

// Credentials alone -> REDACTED (no home/user/term rules); limit as in applySpans.
export function redactSecrets(text, { limit = text.length } = {}) {
  return applySpans(text, secretSpans(text), limit);
}
