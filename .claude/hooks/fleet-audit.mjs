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
 * The command is stored with secrets redacted: credential shapes plus the rules ported
 * from tools/privacy_guard.py (user-home paths, OS username, UUID-shaped ids, private
 * terms from CCV_PRIVACY_TERMS, ~/.claude/privacy-terms and .git/info/privacy-terms).
 * Heuristic, quote-aware tokenizing; nested `bash -c` / `cmd /c` / `pwsh -Command` are
 * analyzed. Never blocks: always exit 0, no stdout; any failure is a note on stderr.
 */
import { readFileSync, appendFileSync, mkdirSync, statSync } from 'fs';
import { homedir, tmpdir, userInfo } from 'os';
import { join, dirname, resolve, posix } from 'path';

const SHELL_TOOLS = new Set(['Bash', 'PowerShell']);
const MAX_COMMAND = 4000;
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
// Credential shapes; group 1 (when present) is the secret, else the whole match.
const SECRET_PATTERNS = [
  /\b[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Za-z0-9_]*\s*=\s*("[^"]*"|'[^']*'|[^\s;|&]+)/dgi,
  /--?[\w-]*(?:token|password|passwd|secret|api-?key)[\w-]*(?:=|\s+)("[^"]*"|'[^']*'|[^\s;|&]+)/dgi,
  /\b(?:Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{6,})/dgi,
  /:\/\/[^\s/:@]+:([^\s/@]+)@/dg,
  /\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})\b/dg,
];

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
      const m = readFileSync(dotgit, 'utf-8').match(/^gitdir:\s*(.+)$/m);
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

function redact(text, { users = usernames(), terms = [] } = {}) {
  const spans = [];
  for (const re of SECRET_PATTERNS) {
    for (const m of text.matchAll(re)) {
      const [s, e] = m.indices[1] || m.indices[0];
      spans.push([s, e]);
    }
  }
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
    if (e <= pos) continue;
    if (s >= pos) { out += text.slice(pos, s) + REDACTED; pos = e; }
    else pos = e; // overlap: extend the redaction already emitted
  }
  return out + text.slice(pos);
}

// --- command analysis ---

// Split on unquoted ; && || | & and newlines (not the & of 2>&1 / &>).
function segments(cmd) {
  const out = [];
  let cur = '', q = null;
  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i];
    if (q) {
      cur += c;
      if (c === q) q = null;
      else if (c === '\\' && q === '"' && cmd[i + 1] === '"') cur += cmd[++i];
      continue;
    }
    if (c === '"' || c === "'") { q = c; cur += c; continue; }
    const amp = c === '&' && cmd[i - 1] !== '>' && cmd[i + 1] !== '>';
    if (c === ';' || c === '\n' || c === '\r' || c === '|' || amp) {
      if ((c === '|' || c === '&') && cmd[i + 1] === c) i++;
      out.push(cur); cur = '';
      continue;
    }
    cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function tokens(seg) {
  const out = [];
  let cur = '', q = null, has = false;
  for (let i = 0; i < seg.length; i++) {
    const c = seg[i];
    if (q) {
      if (c === q) q = null;
      else if (c === '\\' && q === '"' && seg[i + 1] === '"') cur += seg[++i];
      else cur += c;
      continue;
    }
    if (c === '"' || c === "'") { q = c; has = true; continue; }
    if (/\s/.test(c)) { if (has) out.push(cur); cur = ''; has = false; continue; }
    cur += c; has = true;
  }
  if (has) out.push(cur);
  return out;
}

const PREFIXES = new Set(['sudo', 'env', 'command', 'exec', 'nohup', 'time', 'xargs', 'builtin',
  '&', '.', '!', 'then', 'do', 'else', 'elif']);
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'cmd', 'pwsh', 'powershell']);
const RM = new Set(['rm', 'remove-item', 'ri', 'del', 'erase', 'rd', 'rmdir']);
const SETTINGS_RE = /settings[\w.-]*\.json/i;
const WRITE_ANY_ARG = new Set(['rm', 'del', 'erase', 'remove-item', 'ri', 'truncate', 'set-content', 'sc',
  'add-content', 'ac', 'out-file', 'clear-content', 'clc', 'new-item', 'ni', 'touch', 'ln', 'tee', 'tee-object']);
const COPY_MOVE = new Set(['cp', 'mv', 'copy', 'move', 'copy-item', 'cpi', 'move-item', 'mi', 'install', 'rsync', 'xcopy']);
const SCRIPT_WRITE_RE = /writeFileSync|writeFile\(|appendFileSync|WriteAllText|WriteAllLines|WriteAllBytes|write_text|write_bytes|json\.dump|open\([^)]*['"][wax]\+?['"]/;
const REDIRECT_RE = /(?:^|[^<>=-])(?:\d|&)?>>?\s*(?:"([^"]+)"|'([^']+)'|([^\s;|&<>"']+))/g;
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
    || /^-[a-zA-Z]*f[a-zA-Z]*$/.test(t) || (t.length > 1 && t.startsWith('+')))) add('force-push');
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

function categorize(cmd, cwd, depth = 0) {
  const found = [];
  const add = (c) => { if (!found.includes(c)) found.push(c); };
  const ctx = { venv: Boolean(process.env.VIRTUAL_ENV) };
  for (const seg of segments(cmd)) {
    const argv = tokens(seg);
    while (argv.length && (PREFIXES.has(argv[0]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(argv[0]))) argv.shift();
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
  const cats = categorize(cmd, cwd);
  if (!cats.length) return;
  const opts = { users: usernames(), terms: loadTerms(cwd) };
  let command = redact(cmd, opts);
  if (command.length > MAX_COMMAND) command = `${command.slice(0, MAX_COMMAND)}...`;
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

try { main(); } catch (e) { process.stderr.write(`fleet-audit: ${e && e.message ? e.message : e}\n`); }
process.exitCode = 0;
