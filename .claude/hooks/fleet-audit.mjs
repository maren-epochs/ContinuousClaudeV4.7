#!/usr/bin/env node
/**
 * Fleet audit hook (PostToolUse, matcher Bash|PowerShell).
 *
 * Risky shell commands append one AuditEvent line per category to
 * ~/.claude/fleet/audit.jsonl (shape: tools/fleet/schema.md, AuditEvent):
 *   force-push        git push --force / --force-with-lease / -f / +refspec
 *   history-rewrite   git filter-repo, git filter-branch
 *   hard-reset        git reset --hard
 *   recursive-delete  rm -rf, Remove-Item -Recurse -Force, rd /s /q with a target outside temp
 *   settings-edit     a write to a settings*.json file (redirect, tee, sed -i, cp/mv dest,
 *                     Set-Content/Out-File, scripted writeFileSync/WriteAllText/open(..,'w'))
 *   global-install    pip install outside a venv, npm/pnpm -g, yarn global, cargo install,
 *                     winget, choco
 * The command is cut to 4000 chars (600-char lookahead so a secret crossing the cut is
 * still recognized) and then stored with secrets redacted: credential shapes (KEY=v,
 * --token v, JSON/YAML "token"/"password"/"secret"/"api_key": v, x-*-key / Authorization /
 * Private-Token headers, Bearer/Basic, URL userinfo, curl -u user:pass, gh*_/github_pat_/
 * glpat-/sk-/sk_live_/sk_test_/rk_/npm_/hf_/xox*-/AKIA/AIza tokens, ConvertTo-SecureString
 * plaintext, attached mysql -p<pass>) plus the rules ported from tools/privacy_guard.py
 * (user-home paths, OS username, UUID-shaped ids, private terms from CCV_PRIVACY_TERMS,
 * ~/.claude/privacy-terms and .git/info/privacy-terms; an unreadable .git is skipped).
 * Every regex is bounded (no nested or unbounded quantifiers) and the scanners are linear:
 * a 100k-char command costs well under 50 ms. Heuristic, quote-aware tokenizing; nested
 * `bash -c` / `cmd /c` / `pwsh -Command` are analyzed. Never blocks: always exit 0, no
 * stdout; any failure is a note on stderr. Imported as `fleet-audit.mjs?lib` it only
 * exports (auditCommand, categorize, redact) for tests.
 */
import { readFileSync, appendFileSync, mkdirSync, statSync } from 'fs';
import { homedir, tmpdir, userInfo } from 'os';
import { join, dirname, resolve, posix } from 'path';

const SHELL_TOOLS = new Set(['Bash', 'PowerShell']);
const MAX_COMMAND = 4000;
const REDACT_MARGIN = 600;
const REDACTED = '<redacted>';

// --- redaction (ported from tools/privacy_guard.py) ---

const PLACEHOLDER_NAMES = new Set(['x', 'user', 'name', 'you']);
const SERVICE_ACCOUNTS = new Set(['runner', 'runneradmin', 'root']);
const HOME_PATTERNS = [
  /(?<![A-Za-z])(?<pre>[A-Za-z]:(?:\/|\\+)Users(?:\/|\\+))(?<name><[^>\s]*>|[^/\\\s"'`<>:;,|*?()[\]{}]+)/gi,
  /(?<![\w.])(?<pre>\/[A-Za-z]\/Users\/)(?<name><[^>\s]*>|[^/\\\s"'`<>:;,|*?()[\]{}]+)/gi,
  /(?<![A-Za-z])(?<pre>[A-Za-z]--Users-)(?<name><[^>\s]*>|[A-Za-z0-9_.]+)/gi,
];
const UUID_RE = /(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])/gi;
// Credential shapes; group `s` (when present) is the secret, else the whole match.
// Linear by construction: every quantifier is bounded and none is nested.
const VAL = String.raw`(?<s>\\"[^"]{0,512}"|"[^"]{0,512}"|'[^']{0,512}'|[^\s;|&]{1,512})`;
const SECRET_NAME = String.raw`(?:token|password|passwd|secret|api[_-]?key)`;
const SECRET_PATTERNS = [
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

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function fileLines(p) {
  try {
    return readFileSync(p, 'utf-8').replace(/^\uFEFF/, '').split(/\r?\n/)
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
  } catch { return []; }
}

// .git/info/privacy-terms of the repo containing cwd (worktrees: the common dir).
function cloneTermsFile(cwd) {
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

function loadTerms(cwd) {
  const env = (process.env.CCV_PRIVACY_TERMS || '').split(/[,\r\n]/)
    .map((s) => s.trim()).filter((s) => s && !s.startsWith('#'));
  const clone = cwd ? cloneTermsFile(cwd) : null;
  const all = [...env, ...fileLines(join(homedir(), '.claude', 'privacy-terms')), ...(clone ? fileLines(clone) : [])];
  return [...new Map(all.map((t) => [t.toLowerCase(), t])).values()];
}

function usernames() {
  const names = [];
  try { names.push(userInfo().username); } catch {}
  names.push(process.env.USERNAME || '', process.env.USER || '');
  const seen = new Map();
  for (const n of names) {
    if (n && n.length >= 3 && !SERVICE_ACCOUNTS.has(n.toLowerCase())) seen.set(n.toLowerCase(), n);
  }
  return [...seen.values()];
}

// Redacted text[0, limit); spans are found over all of text so a secret crossing the
// limit is still recognized (callers pass a lookahead margin past the limit).
export function redact(text, { users = usernames(), terms = [], limit = text.length } = {}) {
  const spans = [];
  const secret = (m) => (m.indices.groups && m.indices.groups.s) || m.indices[0];
  for (const re of SECRET_PATTERNS) {
    for (const m of text.matchAll(re)) spans.push(secret(m));
  }
  if (MYSQL_RE.test(text)) for (const m of text.matchAll(MYSQL_PW_RE)) spans.push(secret(m));
  secureStringSpans(text, spans);
  for (const re of HOME_PATTERNS) {
    for (const m of text.matchAll(re)) {
      const { pre, name } = m.groups;
      if (name.startsWith('<') || PLACEHOLDER_NAMES.has(name.toLowerCase())) continue;
      const s = m.index + pre.length;
      spans.push([s, s + name.length]);
    }
  }
  const word = users.map((n) => new RegExp(`(?<!\\w)${escapeRe(n)}(?!\\w)`, 'gi'));
  const sub = terms.map((t) => new RegExp(escapeRe(t), 'gi'));
  for (const re of [UUID_RE, ...word, ...sub]) {
    for (const m of text.matchAll(re)) spans.push([m.index, m.index + m[0].length]);
  }
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

// --- command analysis ---

// Split on unquoted ; && || | & and newlines (not the & of 2>&1 / &>).
// Index after the quote closing the one at i (`\"` escapes inside double quotes);
// length + 1 when unclosed, so slice(i + 1, end - 1) is always the quoted body.
function quoteEnd(s, i) {
  const q = s[i];
  for (let j = i + 1; ;) {
    const k = s.indexOf(q, j);
    if (k < 0) return s.length + 1;
    if (q === '"' && s[k - 1] === '\\') { j = k + 1; continue; }
    return k + 1;
  }
}

// Scans jump between special characters (no per-character string building): linear.
function segments(cmd) {
  const out = [];
  const re = /[;\n\r|&"']/g;
  let start = 0, m;
  while ((m = re.exec(cmd))) {
    let i = m.index;
    const c = cmd[i];
    if (c === '"' || c === "'") { re.lastIndex = quoteEnd(cmd, i); continue; }
    if (c === '&' && (cmd[i - 1] === '>' || cmd[i + 1] === '>')) continue;
    out.push(cmd.slice(start, i));
    if ((c === '|' || c === '&') && cmd[i + 1] === c) i++;
    start = re.lastIndex = i + 1;
  }
  out.push(cmd.slice(start));
  return out.map((s) => s.trim()).filter(Boolean);
}

function tokens(seg) {
  const out = [];
  const re = /["'\s]/g;
  let cur = '', has = false, pos = 0, m;
  while ((m = re.exec(seg))) {
    const i = m.index;
    const c = seg[i];
    if (i > pos) { cur += seg.slice(pos, i); has = true; }
    if (c === '"' || c === "'") {
      const end = quoteEnd(seg, i);
      const body = seg.slice(i + 1, end - 1);
      cur += c === '"' ? body.replace(/\\"/g, '"') : body;
      has = true;
      pos = re.lastIndex = end;
      continue;
    }
    if (has) out.push(cur);
    cur = ''; has = false;
    pos = re.lastIndex = i + 1;
  }
  if (pos < seg.length) { cur += seg.slice(pos); has = true; }
  if (has) out.push(cur);
  return out;
}

const PREFIXES = new Set(['sudo', 'env', 'command', 'exec', 'nohup', 'time', 'xargs', 'builtin',
  '&', '.', '!', 'then', 'do', 'else', 'elif']);
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'cmd', 'pwsh', 'powershell']);
const RM = new Set(['rm', 'remove-item', 'ri', 'del', 'erase', 'rd', 'rmdir']);
const SETTINGS_RE = /settings[\w.-]{0,64}\.json/i;
const WRITE_ANY_ARG = new Set(['rm', 'del', 'erase', 'remove-item', 'ri', 'truncate', 'set-content', 'sc',
  'add-content', 'ac', 'out-file', 'clear-content', 'clc', 'new-item', 'ni', 'touch', 'ln', 'tee', 'tee-object']);
const COPY_MOVE = new Set(['cp', 'mv', 'copy', 'move', 'copy-item', 'cpi', 'move-item', 'mi', 'install', 'rsync', 'xcopy']);
const SCRIPT_WRITE_RE = /writeFileSync|writeFile\(|appendFileSync|WriteAllText|WriteAllLines|WriteAllBytes|write_text|write_bytes|json\.dump|open\([^)]{0,256}['"][wax]\+?['"]/;
const REDIRECT_RE = /(?:^|[^<>=-])(?:\d|&)?>>?\s{0,8}(?:"([^"]{1,512})"|'([^']{1,512})'|([^\s;|&<>"']{1,512}))/g;
const NPM_SUBS = new Set(['install', 'i', 'in', 'isntall', 'add', 'update', 'up', 'upgrade', 'uninstall', 'un',
  'remove', 'rm', 'r', 'unlink', 'link', 'ln']);
const VENV_PATH_RE = /(^|[/\\])\.?venv[/\\]|[/\\]env[/\\](Scripts|bin)[/\\]|virtualenv/i;

const cmdName = (t) => t.replace(/\\/g, '/').split('/').pop().toLowerCase().replace(/\.(exe|cmd|bat|ps1)$/, '');
const positional = (args) => args.filter((t) => !t.startsWith('-') && !t.startsWith('+'));
const norm = (p) => {
  let s = String(p).replace(/\\/g, '/');
  if (/^\/[A-Za-z](\/|$)/.test(s)) s = `${s[1]}:${s.slice(2) || '/'}`;
  return posix.normalize(s).replace(/(.)\/+$/, '$1').toLowerCase();
};

function tempRoots() {
  const env = process.env;
  return [tmpdir(), env.TEMP, env.TMP, env.TMPDIR, '/tmp', '/var/tmp', 'C:/tmp', 'C:/Windows/Temp']
    .filter(Boolean).map(norm);
}

function isTemp(target, cwd) {
  let p = target.replace(/\\/g, '/');
  if (/^(\$\{?(TMPDIR|TEMP|TMP)\}?|\$env:(TEMP|TMP)|%(TEMP|TMP)%)\/./i.test(p)) return true;
  const home = homedir().replace(/\\/g, '/');
  p = p.replace(/^(~|\$\{?HOME\}?|\$env:USERPROFILE|%USERPROFILE%)(?=\/|$)/i, home);
  if (/^[$%]/.test(p)) return false;
  if (!/^([A-Za-z]:\/|\/)/.test(p)) p = `${(cwd || process.cwd()).replace(/\\/g, '/')}/${p}`;
  p = norm(p);
  if (/\/appdata\/local\/temp\/./.test(p)) return true;
  return tempRoots().some((r) => p.startsWith(`${r}/`));
}

function gitCategories(argv, add) {
  if (cmdName(argv[0]) === 'git-filter-repo') return add('history-rewrite');
  let i = 1;
  while (i < argv.length && argv[i].startsWith('-')) {
    i += ['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--config-env'].includes(argv[i]) ? 2 : 1;
  }
  const sub = argv[i], rest = argv.slice(i + 1);
  if (sub === 'push' && rest.some((t) => t === '--force' || t.startsWith('--force-with-lease')
    || (t.length <= 64 && /^-[a-zA-Z]*f[a-zA-Z]*$/.test(t)) || (t.length > 1 && t.startsWith('+')))) add('force-push');
  if (sub === 'filter-branch' || sub === 'filter-repo') add('history-rewrite');
  if (sub === 'reset' && rest.includes('--hard')) add('hard-reset');
}

function isRecursiveDelete(argv, cwd) {
  let rec = false, force = false;
  const targets = [];
  for (let j = 1; j < argv.length; j++) {
    const t = argv[j];
    if (/^\/[sqf]$/i.test(t)) { if (/s/i.test(t[1])) rec = true; else force = true; continue; }
    if (/^--recursive$/i.test(t) || /^-r(e(c(u(r(s(e)?)?)?)?)?)?(:\$true)?$/i.test(t)) { rec = true; continue; }
    if (/^--force$/i.test(t) || /^-f(o(r(c(e)?)?)?)?(:\$true)?$/i.test(t)) { force = true; continue; }
    if (/^-[a-zA-Z]{1,4}$/.test(t)) { if (/r/i.test(t)) rec = true; if (/f/i.test(t)) force = true; continue; }
    if (t.startsWith('-')) continue;
    if (/^\d?>>?$/.test(t)) { j++; continue; }
    if (/^\d?>/.test(t)) continue;
    targets.push(t);
  }
  return rec && force && targets.some((t) => !isTemp(t, cwd));
}

function isGlobalInstall(argv, ctx) {
  const name = cmdName(argv[0]);
  if (/^pip(\d+(\.\d+)*)?$/.test(name) || /^(python(\d+(\.\d+)*)?|py)$/.test(name)) {
    let args = argv.slice(1);
    if (!name.startsWith('pip')) {
      const m = args.findIndex((t, k) => t === '-m' && /^pip\d*$/.test(args[k + 1] || ''));
      if (m < 0) return false;
      args = args.slice(m + 2);
    }
    if (positional(args)[0] !== 'install') return false;
    if (ctx.venv || VENV_PATH_RE.test(argv[0])) return false;
    return !args.some((t) => /^(-t|--target|--prefix|--root|--dry-run)(=|$)/.test(t));
  }
  const pos = positional(argv.slice(1));
  if (name === 'uv') return pos[0] === 'pip' && pos[1] === 'install' && argv.includes('--system');
  if (name === 'npm' || name === 'pnpm') {
    const global = argv.some((t, k) => t === '-g' || t === '--global' || t === '--location=global'
      || (t === '--location' && argv[k + 1] === 'global'));
    return global && pos.some((t) => NPM_SUBS.has(t));
  }
  if (name === 'yarn') return pos[0] === 'global' && ['add', 'remove', 'upgrade'].includes(pos[1]);
  if (name === 'cargo') return pos[0] === 'install';
  if (name === 'winget') return ['install', 'add', 'upgrade', 'update', 'uninstall', 'remove', 'rm'].includes(pos[0]);
  if (name === 'choco' || name === 'chocolatey') return ['install', 'upgrade', 'uninstall'].includes(pos[0]);
  return ['cinst', 'cup', 'cuninst'].includes(name);
}

function isSettingsEdit(seg, argv) {
  if (!SETTINGS_RE.test(seg)) return false;
  for (const m of seg.matchAll(REDIRECT_RE)) {
    if (SETTINGS_RE.test(m[1] || m[2] || m[3] || '')) return true;
  }
  const name = cmdName(argv[0]);
  const args = argv.slice(1);
  if (WRITE_ANY_ARG.has(name)) return args.some((t) => SETTINGS_RE.test(t));
  if (['sed', 'perl', 'ruby'].includes(name) && args.some((t) => /^-[a-zA-Z]*i/.test(t) || t.startsWith('--in-place'))) {
    return args.some((t) => SETTINGS_RE.test(t));
  }
  if (COPY_MOVE.has(name)) {
    const d = args.findIndex((t) => /^-dest(ination)?$/i.test(t));
    const dest = d >= 0 ? args[d + 1] : positional(args).pop();
    return SETTINGS_RE.test(dest || '');
  }
  return SCRIPT_WRITE_RE.test(seg);
}

export function categorize(cmd, cwd, depth = 0) {
  const found = [];
  const add = (c) => { if (!found.includes(c)) found.push(c); };
  const ctx = { venv: Boolean(process.env.VIRTUAL_ENV) };
  for (const seg of segments(cmd)) {
    const words = tokens(seg);
    let k = 0;
    while (k < words.length && (PREFIXES.has(words[k]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[k]))) k++;
    const argv = k ? words.slice(k) : words;
    if (argv.length) argv[0] = argv[0].replace(/^[({]+/, '');
    if (!argv.length || !argv[0]) continue;
    const name = cmdName(argv[0]);
    if (/(^|[/\\\s])activate(\.ps1|\.bat)?(\s|$)/i.test(seg) || name === 'workon') ctx.venv = true;
    if (SHELLS.has(name) && depth < 3) {
      const k = argv.findIndex((t, n) => n > 0 && (/^-c(ommand)?$/i.test(t) || /^\/[ck]$/i.test(t)));
      if (k > 0) { categorize(argv.slice(k + 1).join(' '), cwd, depth + 1).forEach(add); continue; }
    }
    if (name === 'git' || name === 'git-filter-repo') gitCategories(argv, add);
    if (RM.has(name) && isRecursiveDelete(argv, cwd)) add('recursive-delete');
    if (isSettingsEdit(seg, argv)) add('settings-edit');
    if (isGlobalInstall(argv, ctx)) add('global-install');
  }
  return found;
}

// --- hook ---

const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
const baseName = (p) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || null;

// Categories + the stored command: cut to MAX_COMMAND before redaction (with a lookahead
// margin so a secret crossing the cut is still recognized), so redaction cost is bounded.
export function auditCommand(cmd, cwd, opts = {}) {
  const cats = categorize(cmd, cwd);
  if (!cats.length) return { cats, command: null, opts: null };
  const o = { users: opts.users ?? usernames(), terms: opts.terms ?? loadTerms(cwd) };
  let command = redact(cmd.slice(0, MAX_COMMAND + REDACT_MARGIN), { ...o, limit: MAX_COMMAND });
  if (cmd.length > MAX_COMMAND || command.length > MAX_COMMAND) command = `${command.slice(0, MAX_COMMAND)}...`;
  return { cats, command, opts: o };
}

function main() {
  let raw = '';
  try { raw = readFileSync(0, 'utf-8'); } catch { return; }
  let data;
  try { data = JSON.parse(raw); } catch { return; }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return;
  if (!SHELL_TOOLS.has(data.tool_name)) return;
  const cmd = data.tool_input && data.tool_input.command;
  if (typeof cmd !== 'string' || !cmd.trim()) return;
  const cwd = typeof data.cwd === 'string' && data.cwd ? data.cwd : null;
  const { cats, command, opts } = auditCommand(cmd, cwd);
  if (!cats.length) return;
  const base = {
    ts: nowIso(),
    project: cwd ? redact(baseName(cwd) || '', opts) || null : null,
    session_id: typeof data.session_id === 'string' ? data.session_id : null,
  };
  const lines = cats.map((category) => `${JSON.stringify({ ...base, category, command, tool: data.tool_name })}\n`);
  const dir = join(homedir(), '.claude', 'fleet');
  mkdirSync(dir, { recursive: true });
  appendFileSync(join(dir, 'audit.jsonl'), lines.join(''), 'utf-8');
}

// Imported as `fleet-audit.mjs?lib` (tests): exports only, no stdin read.
if (new URL(import.meta.url).search !== '?lib') {
  try { main(); } catch (e) { process.stderr.write(`fleet-audit: ${e && e.message ? e.message : e}\n`); }
}
process.exitCode = 0;
