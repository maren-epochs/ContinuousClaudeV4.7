#!/usr/bin/env node
/**
 * PreToolUse guard: installed harness files are generated from the ccv47 repo.
 * Requires Node 18+ (node: imports; proposal-id randomness loaded lazily from 'node:crypto').
 *
 * A Write/Edit/MultiEdit/NotebookEdit whose target, or a Bash/PowerShell command whose
 * write target (>, >>, N>, &>, tee, cp, mv, Copy-Item, Move-Item, Set-Content,
 * Add-Content, Out-File, Tee-Object), resolves to a file listed in the install manifest
 * (~/.claude/.ccv47-manifest.json, tools/fleet/schema.md) is DENIED. The deny names the
 * repo file to change and the inbox id of a Proposal (kind edit, the exact intended
 * change) saved to ~/.claude/harness-inbox/<id>.json. Kept (keep-list) entries are
 * user-owned and allowed. No manifest -> allow everything.
 *
 * Shell parsing: quotes, heredocs, PowerShell here-strings, `#` / `<# #>` comments,
 * `[[ ]]` / `(( ))` / `$(( ))` (no redirects inside), PowerShell common parameters,
 * aliases and unambiguous prefixes; `cd` / `pushd` / `popd` / Set-Location earlier in
 * the command move the base for relative targets (unknown dir -> relative targets skipped).
 *
 * Path matching: ~, $HOME, ${HOME}, $env:NAME, %NAME% expanded; Git Bash /c/ form and
 * relative paths resolved; case-insensitive on win32. A target is stat'ed (realpath.native
 * on the deepest existing parent: 8.3 short names, symlinks) only when its lexical form is
 * under ~/.claude or, on win32, carries an 8.3 `~N` component off UNC; UNC and
 * network-style targets are never touched on disk.
 *
 * Proposal: change strings share a 256 KiB (UTF-16 units) budget, `change.truncated: true`
 * when cut; the stored shell command has home paths -> ~ and username / privacy terms
 * redacted (file content is stored verbatim). If the proposal cannot be saved, the write
 * is still denied (message names the repo file) and the error is logged.
 *
 * Fails open only before a managed target is identified: malformed stdin or manifest,
 * parse/IO errors allow the call and append a line to ~/.claude/fleet/guard-errors.log.
 * Only a deny prints output.
 */
import { readFileSync, existsSync, realpathSync, statSync, writeFileSync, renameSync, mkdirSync, appendFileSync } from 'node:fs';
import { join, resolve, dirname, basename, isAbsolute } from 'node:path';
import { homedir, userInfo } from 'node:os';
import { createRequire } from 'node:module';

const WIN = process.platform === 'win32';
const FILE_TOOLS = { Write: 'file_path', Edit: 'file_path', MultiEdit: 'file_path', NotebookEdit: 'notebook_path' };
const HOME = homedir();
const CLAUDE = join(HOME, '.claude');
const CHANGE_BUDGET = 256 * 1024;

const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function logError(msg) {
  try {
    const dir = join(CLAUDE, 'fleet');
    mkdirSync(dir, { recursive: true });
    appendFileSync(join(dir, 'guard-errors.log'), `${nowIso()} ${String(msg).replace(/\s+/g, ' ').slice(0, 500)}\n`);
  } catch {}
}

// --- paths ---

function envVar(name, ps) {
  const up = name.toUpperCase();
  if (ps && up === 'HOME') return HOME; // PowerShell $HOME is the profile dir, not env HOME
  const v = process.env[name] ?? (WIN ? process.env[up] : undefined);
  if (v) return v;
  return up === 'HOME' || up === 'USERPROFILE' ? HOME : null;
}

function expand(p, ps) {
  let s = p.replace(/^~(?=$|[\\/])/, () => HOME);
  s = s.replace(/\$env:([A-Za-z_][A-Za-z0-9_]*)/gi, (m, n) => envVar(n, false) ?? m);
  s = s.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g, (m, a, b) => envVar(a || b, ps) ?? m);
  s = s.replace(/%([A-Za-z_][A-Za-z0-9_]*)%/g, (m, n) => envVar(n, false) ?? m);
  if (WIN) {
    s = s.replace(/^[\\/]{2}\?[\\/]UNC[\\/]/i, '\\\\').replace(/^[\\/]{2}[?.][\\/](?=[A-Za-z]:)/, '');
    s = s.replace(/^\/(?:cygdrive\/)?([A-Za-z])(?=$|\/)/, '$1:');
  } else if (ps) s = s.replace(/\\/g, '/'); // pwsh accepts \ as a separator on every OS
  return s;
}

// realpath of the deepest existing ancestor + the not-yet-existing tail
function real(abs) {
  const tail = [];
  let cur = abs;
  for (;;) {
    try {
      return join(realpathSync.native(cur), ...tail.reverse());
    } catch {
      const parent = dirname(cur);
      if (parent === cur) return abs;
      tail.push(basename(cur));
      cur = parent;
    }
  }
}

const keyOf = (p) => {
  const s = p.replace(/\\/g, '/').replace(/\/+$/, '');
  return WIN ? s.toLowerCase() : s;
};
const under = (k, root) => k === root || k.startsWith(root + '/');

// Lexical absolute path, no fs access; null when relative against an unknown cwd.
function lexical(p, cwd, ps) {
  const e = expand(p, ps);
  if (cwd === null && !isAbsolute(e)) return null;
  return resolve(cwd ?? process.cwd(), e);
}

let claudeKeys = null;
function plausible(abs) {
  const k = keyOf(abs);
  claudeKeys ??= [...new Set([keyOf(CLAUDE), keyOf(real(CLAUDE))])];
  if (claudeKeys.some((c) => under(k, c))) return true;
  if (k.startsWith('//')) return false;
  return WIN && /~\d/.test(k);
}

// --- shell command write targets ---

function matchingClose(cmd, i) { // index after the `)` closing the `(` at i
  let depth = 0;
  for (let j = i; j < cmd.length; j++) {
    if (cmd[j] === '(') depth++;
    else if (cmd[j] === ')' && --depth === 0) return j + 1;
  }
  return cmd.length;
}

function tokenize(cmd, ps) {
  const toks = [];
  const heredocs = [];
  let cur = null;
  let dbl = false; // inside bash [[ ]]
  let i = 0;
  const n = cmd.length;
  const hereStart = /[ \t]*\r?\n/y;
  const atCmdStart = () => { const t = toks[toks.length - 1]; return !t || t.op === ';'; };
  const push = () => {
    if (cur === null) return;
    const prev = toks[toks.length - 1];
    if (prev && prev.op === '<<') heredocs.push(cur);
    if (!ps && cur === '[[' && atCmdStart()) dbl = true;
    else if (cur === ']]') dbl = false;
    toks.push({ w: cur });
    cur = null;
  };
  const op = (v) => { push(); toks.push({ op: v }); };
  while (i < n) {
    const c = cmd[i];
    if (c === "'" ) {
      const j = cmd.indexOf("'", i + 1);
      const end = j < 0 ? n : j;
      cur = (cur ?? '') + cmd.slice(i + 1, end);
      i = end + 1;
      continue;
    }
    if (c === '"') {
      let s = '';
      let j = i + 1;
      while (j < n && cmd[j] !== '"') {
        if (!ps && cmd[j] === '\\' && '"\\$`'.includes(cmd[j + 1] ?? '')) { s += cmd[j + 1]; j += 2; continue; }
        if (ps && cmd[j] === '`' && j + 1 < n) { s += cmd[j + 1]; j += 2; continue; }
        s += cmd[j++];
      }
      cur = (cur ?? '') + s;
      i = j + 1;
      continue;
    }
    if (ps && c === '@' && cur === null && (cmd[i + 1] === "'" || cmd[i + 1] === '"')) {
      hereStart.lastIndex = i + 2;
      if (hereStart.test(cmd)) {
        const close = cmd.indexOf(`\n${cmd[i + 1]}@`, i + 2);
        const end = close < 0 ? n : close;
        cur = cmd.slice(cmd.indexOf('\n', i) + 1, end);
        i = close < 0 ? n : close + 3;
        continue;
      }
    }
    if (ps && c === '<' && cmd[i + 1] === '#') {
      push();
      const j = cmd.indexOf('#>', i + 2);
      i = j < 0 ? n : j + 2;
      continue;
    }
    if (c === '#' && cur === null) {
      const j = cmd.indexOf('\n', i);
      i = j < 0 ? n : j;
      continue;
    }
    if (!ps && c === '$' && cmd[i + 1] === '(' && cmd[i + 2] === '(') {
      const end = matchingClose(cmd, i + 1);
      cur = (cur ?? '') + cmd.slice(i, end);
      i = end;
      continue;
    }
    if (!ps && c === '(' && cmd[i + 1] === '(' && cur === null) {
      op(';');
      i = matchingClose(cmd, i);
      continue;
    }
    if (c === '\n') {
      op(';');
      i++;
      while (heredocs.length) {
        const delim = heredocs.shift();
        for (;;) {
          if (i >= n) break;
          const nl = cmd.indexOf('\n', i);
          const end = nl < 0 ? n : nl;
          const line = cmd.slice(i, end).replace(/\r$/, '').replace(/^\t+/, '');
          i = end + 1;
          if (line === delim) break;
        }
      }
      continue;
    }
    if (c === ' ' || c === '\t' || c === '\r') { push(); i++; continue; }
    if (!ps && c === '\\') {
      if (cmd[i + 1] === '\n') { i += 2; continue; }
      if (i + 1 < n) { cur = (cur ?? '') + cmd[i + 1]; i += 2; continue; }
    }
    if (dbl && (c === '>' || c === '<')) { cur = (cur ?? '') + c; i++; continue; }
    if (c === '&' && cmd[i + 1] === '>') {
      i += cmd[i + 2] === '>' ? 3 : 2;
      op('>');
      continue;
    }
    if (c === '|' || c === ';' || c === '&') {
      i += cmd[i + 1] === c ? 2 : 1;
      op(';');
      continue;
    }
    if (c === '>') {
      if (cur !== null && /^(\d+|\*)$/.test(cur)) cur = null;
      i++;
      if (cmd[i] === '>' || cmd[i] === '|') i++;
      if (cmd[i] === '&') { i++; op('dup'); continue; }
      op('>');
      continue;
    }
    if (c === '<') {
      i++;
      if (cmd[i] === '<') {
        i++;
        if (cmd[i] === '<') { i++; op('<'); continue; }
        if (cmd[i] === '-') i++;
        op('<<');
        continue;
      }
      op('<');
      continue;
    }
    if (cur === null && '(){}'.includes(c)) { op(';'); i++; continue; }
    cur = (cur ?? '') + c;
    i++;
  }
  push();
  return toks;
}

const PREFIXES = new Set(['sudo', 'command', 'builtin', 'env', 'nohup', 'time', 'exec', '&', '.']);
const PS_COPY = new Set(['copy-item', 'cpi', 'copy', 'cp', 'move-item', 'mi', 'move', 'mv']);
const PS_CONTENT = new Set(['set-content', 'sc', 'add-content', 'ac']);
const PS_OUTFILE = new Set(['out-file']);
const PS_TEE = new Set(['tee-object', 'tee']);
const PS_LOCATION = new Set(['set-location', 'sl', 'cd', 'chdir', 'push-location', 'pushd']);
const PATH_ALIASES = { pspath: 'literalpath', lp: 'literalpath' };
const PS_COMMON = {
  valued: ['erroraction', 'warningaction', 'informationaction', 'errorvariable', 'warningvariable',
    'informationvariable', 'outvariable', 'outbuffer', 'pipelinevariable', 'progressaction'],
  switches: ['verbose', 'debug', 'whatif', 'confirm', 'usetransaction'],
  aliases: { ea: 'erroraction', wa: 'warningaction', infa: 'informationaction', ev: 'errorvariable',
    wv: 'warningvariable', iv: 'informationvariable', ov: 'outvariable', ob: 'outbuffer', pv: 'pipelinevariable',
    proga: 'progressaction', vb: 'verbose', db: 'debug', wi: 'whatif', cf: 'confirm', usetx: 'usetransaction' },
};
const spec = (valued, switches, aliases = {}) => ({
  valued: new Set([...valued, ...PS_COMMON.valued]),
  switches: new Set([...switches, ...PS_COMMON.switches]),
  aliases: { ...PATH_ALIASES, ...PS_COMMON.aliases, ...aliases },
});
const PS_SPECS = {
  copy: spec(['path', 'literalpath', 'destination', 'filter', 'include', 'exclude', 'credential', 'tosession', 'fromsession'],
    ['container', 'force', 'recurse', 'passthru']),
  content: spec(['path', 'literalpath', 'value', 'filter', 'include', 'exclude', 'credential', 'encoding', 'stream'],
    ['passthru', 'force', 'nonewline', 'asbytestream']),
  outfile: spec(['filepath', 'literalpath', 'encoding', 'width', 'inputobject'], ['append', 'force', 'noclobber', 'nonewline'],
    { path: 'filepath', nooverwrite: 'noclobber' }),
  tee: spec(['filepath', 'literalpath', 'variable', 'inputobject', 'encoding'], ['append'], { path: 'filepath' }),
  location: spec(['path', 'literalpath', 'stackname'], ['passthru']),
};

function psParamName(name, sp) {
  if (sp.valued.has(name) || sp.switches.has(name)) return name;
  if (sp.aliases[name]) return sp.aliases[name];
  const hits = [...sp.valued, ...sp.switches].filter((v) => v.startsWith(name));
  return hits.length === 1 ? hits[0] : null;
}

// Unknown / ambiguous parameters are taken as valued: a consumed positional can only
// remove a write target, never invent one.
function psParams(args, sp) {
  const named = {};
  const pos = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    const m = /^-([A-Za-z][\w-]*)(?::(.*))?$/.exec(a);
    if (!m) { pos.push(a); continue; }
    const full = psParamName(m[1].toLowerCase(), sp);
    if (full && sp.switches.has(full)) continue;
    const value = m[2] !== undefined ? m[2] : args[++i];
    if (full && value !== undefined) named[full] = value;
  }
  return { named, pos };
}

// -> { targets: [{ p, src? }], cd?: { kind: 'cd'|'push'|'pop', dir: string|null|undefined } }
function segmentInfo(words, ps) {
  let k = 0;
  while (k < words.length && (PREFIXES.has(words[k].toLowerCase()) || /^[A-Za-z_]\w*=/.test(words[k]))) k++;
  if (k >= words.length) return { targets: [] };
  const name = basename(words[k].replace(/\\/g, '/')).toLowerCase().replace(/\.exe$/, '');
  const args = words.slice(k + 1);
  if (ps) {
    if (name === 'pop-location' || name === 'popd') return { targets: [], cd: { kind: 'pop' } };
    if (PS_LOCATION.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.location);
      const dir = named.path ?? named.literalpath ?? pos[0];
      return { targets: [], cd: { kind: name.startsWith('push') ? 'push' : 'cd', dir: dir ?? '~' } };
    }
    if (PS_COPY.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.copy);
      const path = named.path ?? named.literalpath;
      const src = path ?? pos[0];
      const dest = named.destination ?? (path !== undefined ? pos[0] : pos[1]);
      return { targets: dest === undefined ? [] : [{ p: dest, src }] };
    }
    if (PS_CONTENT.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.content);
      const t = named.path ?? named.literalpath ?? pos[0];
      return { targets: t === undefined ? [] : [{ p: t }] };
    }
    if (PS_OUTFILE.has(name) || PS_TEE.has(name)) {
      const { named, pos } = psParams(args, PS_OUTFILE.has(name) ? PS_SPECS.outfile : PS_SPECS.tee);
      const t = named.filepath ?? named.literalpath ?? pos[0];
      return { targets: t === undefined ? [] : [{ p: t }] };
    }
    return { targets: [] };
  }
  if (name === 'cd' || name === 'pushd') {
    const dir = args.filter((a) => a === '-' || !a.startsWith('-'))[0];
    return { targets: [], cd: { kind: name === 'pushd' ? 'push' : 'cd', dir: dir ?? '~' } };
  }
  if (name === 'popd') return { targets: [], cd: { kind: 'pop' } };
  if (name === 'tee') return { targets: args.filter((a) => !a.startsWith('-')).map((p) => ({ p })) };
  if (name !== 'cp' && name !== 'mv') return { targets: [] };
  let dir = null;
  const rest = [];
  let opts = true;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (opts && a === '--') { opts = false; continue; }
    if (opts && (a === '-t' || a === '--target-directory')) { dir = args[++i] ?? null; continue; }
    if (opts && a.startsWith('--target-directory=')) { dir = a.slice(19); continue; }
    if (opts && a.startsWith('-') && a.length > 1) continue;
    rest.push(a);
  }
  if (dir !== null) return { targets: rest.map((s) => ({ p: join(dir, basename(s.replace(/[\\/]+$/, ''))) })) };
  if (rest.length < 2) return { targets: [] };
  const dest = rest.pop();
  return { targets: rest.map((s) => ({ p: dest, src: s })) };
}

function changeDir(cwd, dir, ps) {
  if (dir === '-' || /[`*?]|\$\(/.test(dir)) return null;
  const e = expand(dir, ps);
  if (/[$%]/.test(e)) return null;
  if (cwd === null && !isAbsolute(e)) return null;
  return resolve(cwd ?? process.cwd(), e);
}

// -> [{ p, src?, cwd }]; no fs access
function shellTargets(cmd, cwd0, ps) {
  const toks = tokenize(cmd, ps);
  const out = [];
  const stack = [];
  let cwd = cwd0;
  let words = [];
  const flush = () => {
    const info = segmentInfo(words, ps);
    for (const t of info.targets) out.push({ ...t, cwd });
    if (info.cd) {
      if (info.cd.kind === 'pop') cwd = stack.length ? stack.pop() : null;
      else {
        if (info.cd.kind === 'push') stack.push(cwd);
        cwd = changeDir(cwd, info.cd.dir, ps);
      }
    }
    words = [];
  };
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (t.w !== undefined) { words.push(t.w); continue; }
    if (t.op === ';') { flush(); continue; }
    const next = toks[i + 1];
    if (next && next.w !== undefined) {
      i++;
      if (t.op === '>') out.push({ p: next.w, cwd });
    }
  }
  flush();
  return out.filter((t) => t.p && !/^(\/dev\/(null|stdout|stderr)|\$null|nul)$/i.test(t.p));
}

// --- manifest + proposal ---

function loadManifest() {
  const file = join(CLAUDE, '.ccv47-manifest.json');
  if (!existsSync(file)) return null;
  const data = JSON.parse(readFileSync(file, 'utf-8'));
  const files = data && data.files;
  if (!files || typeof files !== 'object' || Array.isArray(files)) throw new Error('manifest has no files object');
  const map = new Map();
  for (const [rel, entry] of Object.entries(files)) {
    if (entry && typeof entry === 'object') map.set(keyOf(rel), { rel, ...entry });
  }
  return { repo: typeof data.repo === 'string' ? data.repo : null, map };
}

const newId = () => `${nowIso().replace(/[-:]/g, '')}-${createRequire(import.meta.url)('node:crypto').randomBytes(4).toString('hex')}`;

function fileLines(p) {
  try {
    return readFileSync(p, 'utf-8').replace(/^﻿/, '').split(/\r?\n/)
      .map((l) => l.trim()).filter((l) => l && !l.startsWith('#'));
  } catch { return []; }
}

// .git/info/privacy-terms of the repo containing cwd (worktrees: the common dir); local dirs only
function cloneTermsFile(cwd) {
  if (!cwd || keyOf(cwd).startsWith('//')) return null;
  let dir = cwd;
  for (let i = 0; i < 40; i++) {
    const dotgit = join(dir, '.git');
    try {
      const st = statSync(dotgit);
      if (st.isDirectory()) return join(dotgit, 'info', 'privacy-terms');
      const m = readFileSync(dotgit, 'utf-8').match(/^gitdir:\s*(.+)$/m);
      if (!m) return null;
      const gitdir = resolve(dir, m[1].trim());
      let common = gitdir;
      try { common = resolve(gitdir, readFileSync(join(gitdir, 'commondir'), 'utf-8').trim()); } catch {}
      return join(common, 'info', 'privacy-terms');
    } catch {}
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

function redactCommand(text, cwd) {
  let out = text;
  const homes = new Set();
  for (const h of new Set([HOME, real(HOME)])) {
    const fwd = h.replace(/\\/g, '/');
    homes.add(h).add(fwd).add(h.replace(/\\/g, '\\\\'));
    if (WIN && /^[A-Za-z]:/.test(fwd)) homes.add(`/${fwd[0].toLowerCase()}${fwd.slice(2)}`);
  }
  for (const h of [...homes].filter((x) => x.length > 3).sort((a, b) => b.length - a.length)) {
    out = out.replace(new RegExp(`${escapeRe(h)}(?=$|[\\\\/"'\\s;|&)])`, WIN ? 'gi' : 'g'), '~');
  }
  const users = [];
  try { users.push(userInfo().username); } catch {}
  users.push(process.env.USERNAME || '', process.env.USER || '');
  for (const u of new Set(users.filter((u) => u.length >= 3 && !['runner', 'runneradmin', 'root'].includes(u.toLowerCase())))) {
    out = out.replace(new RegExp(`(?<!\\w)${escapeRe(u)}(?!\\w)`, 'gi'), '<redacted>');
  }
  const env = (process.env.CCV_PRIVACY_TERMS || '').split(/[,\r\n]/).map((s) => s.trim()).filter((s) => s && !s.startsWith('#'));
  const clone = cloneTermsFile(cwd);
  for (const t of new Set([...env, ...fileLines(join(CLAUDE, 'privacy-terms')), ...(clone ? fileLines(clone) : [])])) {
    out = out.replace(new RegExp(escapeRe(t), 'gi'), '<redacted>');
  }
  return out;
}

function changeOf(tool, input, cwd) {
  let budget = CHANGE_BUDGET;
  let truncated = false;
  const take = (v) => {
    if (typeof v !== 'string') return null;
    if (v.length <= budget) { budget -= v.length; return v; }
    truncated = true;
    const cut = v.slice(0, budget);
    budget = 0;
    return cut;
  };
  const change = { tool, content: null, old_string: null, new_string: null, command: null };
  if (tool === 'Write') change.content = take(input.content);
  else if (tool === 'Edit') {
    change.old_string = take(input.old_string);
    change.new_string = take(input.new_string);
    if (typeof input.replace_all === 'boolean') change.replace_all = input.replace_all;
  } else if (tool === 'MultiEdit') {
    change.edits = [];
    for (const e of Array.isArray(input.edits) ? input.edits : []) {
      if (budget <= 0) { truncated = true; break; }
      change.edits.push(e && typeof e === 'object' ? { ...e, old_string: take(e.old_string), new_string: take(e.new_string) } : e);
    }
  } else if (tool === 'NotebookEdit') {
    change.content = take(input.new_source);
    change.notebook = { cell_id: input.cell_id ?? null, cell_type: input.cell_type ?? null, edit_mode: input.edit_mode ?? null };
    for (const k of Object.keys(change.notebook)) if (typeof change.notebook[k] !== 'string') change.notebook[k] = null;
  } else change.command = take(typeof input.command === 'string' ? redactCommand(input.command, cwd) : null);
  if (truncated) change.truncated = true;
  return change;
}

function saveProposal(proposal) {
  const dir = join(CLAUDE, 'harness-inbox');
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `${proposal.id}.json`);
  const tmp = join(dir, `.${proposal.id}.json.${process.pid}.tmp`);
  writeFileSync(tmp, JSON.stringify(proposal, null, 2) + '\n');
  renameSync(tmp, file);
}

function managedHits(data) {
  const tool = data.tool_name;
  const input = data.tool_input;
  if (!input || typeof input !== 'object') return null;
  const ps = tool === 'PowerShell';
  const cwd = typeof data.cwd === 'string' && data.cwd ? expand(data.cwd, false) : process.cwd();

  let raw;
  if (FILE_TOOLS[tool]) {
    const p = input[FILE_TOOLS[tool]];
    if (typeof p !== 'string' || !p) return null;
    raw = [{ p, cwd }];
  } else if (tool === 'Bash' || ps) {
    if (typeof input.command !== 'string') return null;
    raw = shellTargets(input.command, cwd, ps);
    if (!raw.length) return null;
  } else return null;

  const manifest = loadManifest();
  if (!manifest) return null;
  const claudeKey = keyOf(real(CLAUDE));
  const hits = [];
  const seen = new Set();
  for (const t of raw) {
    const lex = lexical(t.p, t.cwd, ps);
    if (lex === null || !plausible(lex)) continue;
    let abs = real(lex);
    if (t.src !== undefined) {
      let isDir = /[\\/]$/.test(t.p);
      if (!isDir) try { isDir = statSync(abs).isDirectory(); } catch {}
      if (isDir) abs = real(join(abs, basename(t.src.replace(/[\\/]+$/, ''))));
    }
    const k = keyOf(abs);
    if (!k.startsWith(claudeKey + '/') || seen.has(k)) continue;
    seen.add(k);
    const entry = manifest.map.get(k.slice(claudeKey.length + 1));
    if (entry && entry.kept !== true && typeof entry.repo_path === 'string') hits.push({ abs, entry });
  }
  return hits.length ? { tool, input, cwd, manifest, hits } : null;
}

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { return logError('unparseable hook input'); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return logError('hook input is not an object');
  let found;
  try { found = managedHits(data); } catch (e) { return logError(e && e.stack ? e.stack : e); }
  if (!found) return;

  // A managed target is identified: from here on the call is denied whatever fails.
  const { tool, input, cwd, manifest, hits } = found;
  const lines = [];
  for (const { abs, entry } of hits) {
    const repoFile = manifest.repo ? join(manifest.repo, entry.repo_path) : entry.repo_path;
    const reason = `${abs} is installed from the ccv47 harness repo; change the repo file ${repoFile} (${entry.repo_path}) and re-run install/sync_global.py --apply.`;
    let id = null;
    try {
      id = newId();
      saveProposal({
        schema_version: 1,
        id,
        created_at: nowIso(),
        kind: 'edit',
        source: { project: basename(cwd) || null, session_id: typeof data.session_id === 'string' ? data.session_id : null, cwd },
        target: { installed_path: abs, repo_path: entry.repo_path, repo: manifest.repo },
        change: changeOf(tool, input, cwd),
        reason,
        status: 'pending',
      });
    } catch (e) {
      logError(`proposal save failed for ${entry.repo_path}: ${e && e.stack ? e.stack : e}`);
      id = null;
    }
    lines.push(id
      ? `${reason} The intended change is saved as inbox id ${id} (~/.claude/harness-inbox/${id}.json).`
      : `${reason} The intended change could not be saved to the inbox (~/.claude/harness-inbox; see ~/.claude/fleet/guard-errors.log); make it in the repo file ${entry.repo_path} instead.`);
  }
  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'deny',
      permissionDecisionReason: `harness-guard: do not edit installed harness files. ${lines.join(' ')}`,
    },
  }));
}

try {
  main();
} catch (e) {
  logError(e && e.stack ? e.stack : e);
}
process.exitCode = 0;
