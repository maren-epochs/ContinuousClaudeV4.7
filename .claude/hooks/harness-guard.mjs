#!/usr/bin/env node
/**
 * PreToolUse guard: installed harness files are generated from the ccv47 repo.
 * Requires Node 18+ (node: imports; proposal-id randomness loaded lazily from 'node:crypto').
 *
 * A Write/Edit/MultiEdit/NotebookEdit whose target, or a Bash/PowerShell command whose
 * write target (>, >>, N>, &>, >&file, tee, cp, mv, install, ln, rsync, dd of=, sed -i,
 * perl -i, truncate, curl -o/-O/-D/-c, wget -O/-o/-P/URL name, tar -x [-C] / -c -f, unzip [-d],
 * 7z x|e [-o], Expand-Archive, [IO.StreamWriter]/FileStream ::new / New-Object, cmd /c|/k and
 * env -S bodies,
 * Copy-Item, Move-Item, Set-Content, Add-Content, Out-File, Tee-Object, New-Item,
 * [IO.File]::WriteAll.. / AppendAll.. / Copy / Move; bodies of bash/sh -c, eval, pwsh -Command /
 * -EncodedCommand and Invoke-Expression re-parsed, depth 3; wrapper options of sudo, env,
 * timeout, nice, stdbuf, xargs, ... skipped), resolves to a file listed in the install manifest
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
 * network-style targets are never touched on disk. A target counts as managed when its
 * lexical OR resolved path is a manifest entry (a symlink/junction inside ~/.claude that
 * points outside still guards the installed path). Glob targets (* ? [] {a,b}) match
 * manifest keys; a directory, glob or recursive copy (cp -r, mv, rsync, Copy-Item -Recurse,
 * dest an ancestor of ~/.claude) hits each manifest entry under the destination whose
 * source file exists (unknown / UNC sources: assumed to exist). An archive extraction
 * (tar -x, unzip, wget -r) into a directory inside ~/.claude hits every entry under it. At most MAX_PROPOSALS
 * proposals per call; further hits are listed in the deny message.
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
const MAX_PROPOSALS = 5;

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
    // bash `{` / `}` are reserved words only standalone (`{ cmd; }`); `{}` and `{a,b}` are words
    const group = ps || c === '(' || c === ')' || (c === '{' ? /^[\s;]?$/.test(cmd[i + 1] ?? '') : atCmdStart());
    if (cur === null && '(){}'.includes(c) && group) { op(';'); i++; continue; }
    cur = (cur ?? '') + c;
    i++;
  }
  push();
  return toks;
}

// Command wrappers: short options taking a value (v), short options whose value is the rest
// of the cluster (o), long options taking a separate value (l), leading operands (pos).
const WRAPPERS = {
  sudo: { v: 'CDgpRrtTUu', l: ['close-from', 'chdir', 'group', 'prompt', 'chroot', 'role', 'type', 'command-timeout', 'other-user', 'user', 'host'] },
  doas: { v: 'uC' },
  env: { v: 'uCS', l: ['unset', 'chdir', 'split-string'] },
  timeout: { v: 'sk', l: ['signal', 'kill-after'], pos: 1 },
  nice: { v: 'n', l: ['adjustment'] },
  ionice: { v: 'cnp', l: ['class', 'classdata', 'pid'] },
  stdbuf: { v: 'ioe', l: ['input', 'output', 'error'] },
  xargs: { v: 'aEdILnPs', o: 'eil', l: ['arg-file', 'delimiter', 'max-args', 'max-procs', 'max-chars', 'process-slot-var'] },
  time: { v: 'fo', l: ['format', 'output'] },
  exec: { v: 'a' },
  nohup: {}, setsid: {}, command: {}, builtin: {},
};
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash', 'ksh', 'ash', 'mksh']);
const PWSH = new Set(['pwsh', 'powershell']);
const TARGET_DIR = { v: 'St', l: ['suffix', 'target-directory'], tdir: true };
const COPY_LIKE = {
  cp: { ...TARGET_DIR, rec: 'rRa', lrec: ['recursive', 'archive'] },
  mv: { ...TARGET_DIR, always: true },
  ln: TARGET_DIR,
  install: { v: 'mogSt', l: ['mode', 'owner', 'group', 'suffix', 'target-directory', 'strip-program'], tdir: true },
  rsync: { v: 'efBTM', rec: 'ra', lrec: ['recursive', 'archive'], l: ['exclude', 'include', 'filter', 'exclude-from',
    'include-from', 'files-from', 'rsh', 'rsync-path', 'temp-dir', 'partial-dir', 'backup-dir', 'suffix', 'chmod',
    'chown', 'log-file', 'password-file', 'compare-dest', 'copy-dest', 'link-dest', 'max-size', 'min-size', 'bwlimit',
    'timeout', 'port', 'out-format', 'usermap', 'groupmap', 'info', 'debug', 'block-size', 'remote-option'] },
};
const PS_CLI_VALUED = new Set(['executionpolicy', 'ex', 'ep', 'windowstyle', 'w', 'win', 'workingdirectory', 'wd',
  'configurationname', 'config', 'custompipename', 'inputformat', 'inp', 'if', 'outputformat', 'o', 'of', 'version', 'v',
  'psconsolefile', 'settingsfile', 'settings', 'configurationfile', 'encodedarguments', 'ea']);
const IO_FILE = /^\[(?:system\.)?io\.file\]::(\w+)\((.*)$/is;
const IO_STREAM = /^\[(?:system\.)?io\.(?:streamwriter|filestream)\]::new\((.*)$/is;
const NEW_STREAM = /(?:system\.)?io\.(?:streamwriter|filestream)['"]?(?:\s*\(\s*|\s+-argumentlist\s+|\s*,?\s+)['"]?([^'",()\s]+)/i;
const PS_COPY = new Set(['copy-item', 'cpi', 'copy', 'cp', 'move-item', 'mi', 'move', 'mv']);
const PS_CONTENT = new Set(['set-content', 'sc', 'add-content', 'ac']);
const PS_OUTFILE = new Set(['out-file']);
const PS_TEE = new Set(['tee-object', 'tee']);
const PS_LOCATION = new Set(['set-location', 'sl', 'cd', 'chdir', 'push-location', 'pushd']);
const PS_NEWITEM = new Set(['new-item', 'ni']);
const PS_IEX = new Set(['invoke-expression', 'iex']);
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
  newitem: spec(['path', 'name', 'itemtype', 'value', 'credential'], ['force'], { type: 'itemtype', target: 'value' }),
  iex: spec(['command'], []),
  expand: spec(['path', 'literalpath', 'destinationpath'], ['force', 'passthru']),
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
    if (full && sp.switches.has(full)) { named[full] = true; continue; }
    const value = m[2] !== undefined ? m[2] : args[++i];
    if (full && value !== undefined) named[full] = value;
  }
  return { named, pos };
}

const cmdName = (w) => basename(w.replace(/\\/g, '/')).toLowerCase().replace(/\.exe$/, '');

// GNU-style options (permuted; `--` ends them) -> { flags: Set, vals: {name: value}, pos: [] }
function gnuParse(args, { v = '', o = '', l = [] }) {
  const flags = new Set();
  const vals = {};
  const pos = [];
  let opts = true;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!opts || a.length < 2 || a[0] !== '-') { pos.push(a); continue; }
    if (a === '--') { opts = false; continue; }
    if (a.startsWith('--')) {
      const eq = a.indexOf('=');
      const name = eq < 0 ? a.slice(2) : a.slice(2, eq);
      if (eq >= 0) vals[name] = a.slice(eq + 1);
      else if (l.includes(name)) vals[name] = args[++i] ?? '';
      else flags.add(name);
      continue;
    }
    for (let j = 1; j < a.length; j++) {
      const ch = a[j];
      if (o.includes(ch)) { vals[ch] = a.slice(j + 1); break; }
      if (v.includes(ch)) { vals[ch] = j + 1 < a.length ? a.slice(j + 1) : (args[++i] ?? ''); break; }
      flags.add(ch);
    }
  }
  return { flags, vals, pos };
}

// index of the wrapped command after a wrapper's options and leading operands
function skipWrapper(words, i, sp, name) {
  while (i < words.length) {
    const a = words[i];
    if (a === '--') { i++; break; }
    if (a === '-' && name === 'env') { i++; continue; }
    if (a.length < 2 || a[0] !== '-') break;
    i++;
    if (a.startsWith('--')) { if (!a.includes('=') && sp.l?.includes(a.slice(2))) i++; continue; }
    for (let j = 1; j < a.length; j++) {
      if (sp.o?.includes(a[j])) break;
      if (sp.v?.includes(a[j])) { if (j === a.length - 1) i++; break; }
    }
  }
  return i + (sp.pos ?? 0);
}

// bash/sh -c BODY -> BODY | null
function shellBody(args) {
  let c = false;
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a === '--' || a === '-') return c ? args[i + 1] ?? null : null;
    if (a.length > 1 && (a[0] === '-' || a[0] === '+')) {
      if (a.startsWith('--')) { if (a === '--rcfile' || a === '--init-file') i++; continue; }
      if (a.includes('c', 1)) c = true;
      if (/[oO]/.test(a.slice(1))) i++;
      continue;
    }
    return c ? a : null;
  }
  return null;
}

// pwsh / powershell -Command ... | -EncodedCommand B64 -> script text | null
function pwshBody(args) {
  for (let i = 0; i < args.length; i++) {
    const m = /^[-/]([A-Za-z]+)(:.*)?$/.exec(args[i]);
    if (!m) return null;
    const n = m[1].toLowerCase();
    if ('command'.startsWith(n)) return args.slice(i + 1).join(' ') || null;
    if ('encodedcommand'.startsWith(n) || n === 'ec') {
      const b = args[i + 1];
      return b ? Buffer.from(b, 'base64').toString('utf16le') : null;
    }
    if ('file'.startsWith(n)) return null;
    if (!m[2] && (PS_CLI_VALUED.has(n) || (n.length >= 3 && [...PS_CLI_VALUED].some((x) => x.startsWith(n))))) i++;
  }
  return null;
}

// [IO.File]::Method(path, ...) anywhere in a PowerShell segment
function ioFileTargets(words) {
  const out = [];
  for (let i = 0; i < words.length; i++) {
    const m = IO_FILE.exec(words[i]);
    if (!m) continue;
    const meth = m[1].toLowerCase();
    const idx = /^(copy|move|replace)$/.test(meth) ? 1
      : /^(write|append)all|^(create|createtext|appendtext|openwrite)$/.test(meth) ? 0 : -1;
    if (idx < 0) continue;
    const arg = [m[2], ...words.slice(i + 1)].join(' ').split(',')[idx];
    const p = arg === undefined ? '' : arg.trim().replace(/\)+$/, '').trim();
    if (p) out.push({ p });
  }
  for (let i = 0; i < words.length; i++) { // [IO.StreamWriter]::new(path, ...) / FileStream
    const m = IO_STREAM.exec(words[i]);
    if (!m) continue;
    const p = [m[1], ...words.slice(i + 1)].join(' ').split(',')[0].trim().replace(/\)+$/, '').trim();
    if (p) out.push({ p });
  }
  const n = words.findIndex((w) => /^new-object$/i.test(w)); // $w = New-Object IO.StreamWriter(path)
  const m = n < 0 ? null : NEW_STREAM.exec(words.slice(n + 1).join(' '));
  if (m) out.push({ p: m[1] });
  return out;
}

// env -S / --split-string 'CMD ARGS' [MORE]: the string is the command line
function envSplit(words, i) {
  for (; i < words.length; i++) {
    const a = words[i];
    if (a === '--' || a.length < 2 || a[0] !== '-') return null;
    if (a === '--split-string' || a === '-S') return words.slice(i + 1).join(' ') || null;
    if (a.startsWith('--split-string=')) return [a.slice(15), ...words.slice(i + 1)].join(' ');
    const s = /^-[^-]*?S(.+)$/.exec(a);
    if (s && !/[uC]/.test(a.slice(1, a.indexOf('S')))) return [s[1], ...words.slice(i + 1)].join(' ');
    if (/^-[uC]$/.test(a)) i++;
  }
  return null;
}

// file name curl -O / wget give a download: last path segment, no query or fragment
function urlName(u) {
  if (typeof u !== 'string' || !/^[a-z][\w+.-]*:\/\//i.test(u)) return '';
  const path = u.replace(/[?#].*$/, '').replace(/^[a-z][\w+.-]*:\/\/[^/]*/i, '');
  return path.split('/').pop() ?? '';
}

// cp / mv / ln / install / rsync -> [{ p, src?, into?, noDir?, rec?, contents? }]
function copyTargets(name, args) {
  const sp = COPY_LIKE[name];
  const { flags, vals, pos } = gnuParse(args, sp);
  if (name === 'install' && (flags.has('d') || flags.has('directory'))) return [];
  const rec = sp.always === true || [...flags].some((f) => (f.length === 1 ? (sp.rec ?? '').includes(f) : (sp.lrec ?? []).includes(f)));
  const dir = sp.tdir ? (vals.t ?? vals['target-directory']) : undefined;
  if (dir !== undefined) return pos.map((s) => ({ p: dir, src: s, into: true, rec }));
  if (name === 'ln' && pos.length === 1) return [{ p: basename(pos[0].replace(/[\\/]+$/, '')) }];
  if (pos.length < 2) return [];
  const dest = pos.pop();
  if (name === 'rsync' && /^[^/\\]*:/.test(dest) && !/^[A-Za-z]:/.test(dest)) return []; // remote
  const noDir = sp.tdir === true && (flags.has('T') || flags.has('no-target-directory'));
  return pos.map((s) => ({ p: dest, src: s, rec, noDir, contents: name === 'rsync' && /[\\/]\.?$/.test(s) }));
}

// -> { targets: [{ p, src?, ... }], nested?: [{ cmd, ps }], cd?: { kind: 'cd'|'push'|'pop', dir } }
function segmentInfo(words, ps) {
  let k = 0;
  for (let n = 0; k < words.length && n < 16; n++) {
    const w = words[k];
    if (w === '&' || w === '.' || /^[A-Za-z_]\w*=/.test(w)) { k++; continue; }
    const wn = cmdName(w);
    if (!Object.hasOwn(WRAPPERS, wn)) break;
    if (wn === 'env') {
      const s = envSplit(words, k + 1);
      if (s !== null) return { targets: ps ? ioFileTargets(words) : [], nested: [{ cmd: s, ps: false }] };
    }
    k = skipWrapper(words, k + 1, WRAPPERS[wn], wn);
  }
  const io = ps ? ioFileTargets(words) : [];
  if (k >= words.length) return { targets: io };
  const name = cmdName(words[k]);
  const args = words.slice(k + 1);
  if (SHELLS.has(name)) {
    const b = shellBody(args);
    return { targets: io, nested: b ? [{ cmd: b, ps: false }] : [] };
  }
  if (PWSH.has(name)) {
    const b = pwshBody(args);
    return { targets: io, nested: b ? [{ cmd: b, ps: true }] : [] };
  }
  if (name === 'cmd') { // cmd /c|/k BODY: PowerShell tokenizing keeps `\` paths; copy/move/type are its aliases
    const i = args.findIndex((a) => /^\/[ck]/i.test(a));
    if (i < 0) return { targets: io };
    const first = args[i].slice(2);
    const b = [first, ...args.slice(i + 1)].filter(Boolean).join(' ');
    return { targets: io, nested: b ? [{ cmd: b, ps: true }] : [] };
  }
  if (/^7z[ar]?$/.test(name) && /^[ex]$/i.test(args[0] ?? '')) { // 7z x|e ARCHIVE [-oDIR]
    const o = args.find((a) => /^-o./i.test(a));
    return { targets: [...io, { p: o !== undefined ? o.slice(2) : '.', tree: true }] };
  }
  if (ps) {
    if (name === 'pop-location' || name === 'popd') return { targets: io, cd: { kind: 'pop' } };
    if (PS_LOCATION.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.location);
      const dir = named.path ?? named.literalpath ?? pos[0];
      return { targets: io, cd: { kind: name.startsWith('push') ? 'push' : 'cd', dir: dir ?? '~' } };
    }
    if (PS_COPY.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.copy);
      const path = named.path ?? named.literalpath;
      const src = path ?? pos[0];
      const dest = named.destination ?? (path !== undefined ? pos[0] : pos[1]);
      const rec = named.recurse === true || /^(move-item|mi|move|mv)$/.test(name);
      return { targets: dest === undefined ? io : [...io, src === undefined ? { p: dest } : { p: dest, src, rec }] };
    }
    if (PS_CONTENT.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.content);
      const t = named.path ?? named.literalpath ?? pos[0];
      return { targets: t === undefined ? io : [...io, { p: t }] };
    }
    if (PS_OUTFILE.has(name) || PS_TEE.has(name)) {
      const { named, pos } = psParams(args, PS_OUTFILE.has(name) ? PS_SPECS.outfile : PS_SPECS.tee);
      const t = named.filepath ?? named.literalpath ?? pos[0];
      return { targets: t === undefined ? io : [...io, { p: t }] };
    }
    if (PS_NEWITEM.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.newitem);
      if (typeof named.itemtype === 'string' && /^d/i.test(named.itemtype)) return { targets: io };
      const base = named.path ?? pos[0];
      const t = typeof named.name === 'string' ? (base !== undefined ? `${base}/${named.name}` : named.name) : base;
      return { targets: t === undefined ? io : [...io, { p: t }] };
    }
    if (name === 'expand-archive') {
      const { named, pos } = psParams(args, PS_SPECS.expand);
      return { targets: [...io, { p: named.destinationpath ?? pos[1] ?? '.', tree: true }] };
    }
    if (PS_IEX.has(name)) {
      const { named, pos } = psParams(args, PS_SPECS.iex);
      const b = typeof named.command === 'string' ? named.command : pos.join(' ');
      return { targets: io, nested: b ? [{ cmd: b, ps: true }] : [] };
    }
    return { targets: io };
  }
  if (name === 'cd' || name === 'pushd') {
    const dir = args.filter((a) => a === '-' || !a.startsWith('-'))[0];
    return { targets: [], cd: { kind: name === 'pushd' ? 'push' : 'cd', dir: dir ?? '~' } };
  }
  if (name === 'popd') return { targets: [], cd: { kind: 'pop' } };
  if (name === 'find') { // find ... -exec CMD ARGS {} \; | +  -> CMD parsed as its own command
    const targets = [];
    const nested = [];
    for (let i = 0; i < args.length; i++) {
      if (!/^-(exec|execdir|ok|okdir)$/.test(args[i])) continue;
      let j = i + 1;
      while (j < args.length && args[j] !== ';' && args[j] !== '+') j++;
      const sub = segmentInfo(args.slice(i + 1, j), false);
      targets.push(...sub.targets);
      if (sub.nested) nested.push(...sub.nested);
      i = j;
    }
    return { targets, nested };
  }
  if (name === 'eval') return { targets: [], nested: args.length ? [{ cmd: args.join(' '), ps: false }] : [] };
  if (name === 'tee') return { targets: args.filter((a) => !a.startsWith('-')).map((p) => ({ p })) };
  if (name === 'dd') return { targets: args.filter((a) => a.startsWith('of=')).map((a) => ({ p: a.slice(3) })) };
  if (name === 'sed' || name === 'perl') {
    const sed = name === 'sed';
    const { flags, vals, pos } = gnuParse(args, sed
      ? { v: 'efl', o: 'i', l: ['expression', 'file', 'line-length'] }
      : { v: 'eE', o: 'iIMmxdD' });
    if (!('i' in vals) && !flags.has('in-place') && !('in-place' in vals)) return { targets: [] };
    const script = sed ? ['e', 'f', 'expression', 'file'].some((x) => x in vals) : 'e' in vals || 'E' in vals;
    return { targets: (script ? pos : pos.slice(1)).map((p) => ({ p })) };
  }
  if (name === 'truncate') return { targets: gnuParse(args, { v: 'sr', l: ['size', 'reference'] }).pos.map((p) => ({ p })) };
  if (name === 'tar' || name === 'bsdtar') { // old-style first word `xzf` = `-xzf`
    const a = args.length && /^[A-Za-z]+$/.test(args[0]) ? ['-' + args[0], ...args.slice(1)] : args;
    const { flags, vals } = gnuParse(a, { v: 'fCbHIKNTVX', l: ['file', 'directory', 'blocking-factor', 'format',
      'use-compress-program', 'starting-file', 'newer', 'files-from', 'label', 'exclude-from', 'exclude', 'transform',
      'xform', 'owner', 'group', 'mode', 'mtime'] });
    const has = (s, l) => [...s].some((c) => flags.has(c)) || l.some((x) => flags.has(x));
    if (has('x', ['extract', 'get'])) return { targets: [{ p: vals.C ?? vals.directory ?? '.', tree: true }] };
    const f = vals.f ?? vals.file;
    const create = has('cruA', ['create', 'append', 'update', 'catenate', 'concatenate']);
    return { targets: create && f !== undefined && f !== '-' ? [{ p: f }] : [] };
  }
  if (name === 'unzip') {
    if (args.some((a) => /^-[lvtZpcz]$/.test(a))) return { targets: [] }; // list / test / to stdout
    const i = args.indexOf('-d');
    const glued = args.find((a) => /^-d./.test(a));
    const dir = i >= 0 ? args[i + 1] : glued !== undefined ? glued.slice(2) : '.';
    return { targets: dir === undefined ? [] : [{ p: dir, tree: true }] };
  }
  if (name === 'curl') {
    const { flags, vals, pos } = gnuParse(args, { v: 'oDcdHuXAeFTxbKmwErYyzCUQt', l: ['output', 'dump-header',
      'cookie-jar', 'data', 'data-raw', 'data-binary', 'data-urlencode', 'header', 'user', 'request', 'user-agent',
      'referer', 'form', 'upload-file', 'proxy', 'cookie', 'config', 'max-time', 'write-out', 'cert', 'range',
      'output-dir', 'trace', 'trace-ascii', 'stderr', 'libcurl', 'url', 'connect-timeout', 'retry', 'resolve',
      'cacert', 'key', 'pass', 'json'] });
    const dir = vals['output-dir'];
    const pre = (f) => (dir !== undefined && !/^([A-Za-z]:)?[\/~$%]/.test(f) ? `${dir}/${f}` : f);
    const targets = [];
    for (const k of ['o', 'output']) if (vals[k] && vals[k] !== '-') targets.push({ p: pre(vals[k]) });
    for (const k of ['D', 'dump-header', 'c', 'cookie-jar', 'trace', 'trace-ascii', 'stderr', 'libcurl']) {
      if (vals[k] && vals[k] !== '-') targets.push({ p: vals[k] });
    }
    if (['O', 'remote-name', 'remote-name-all'].some((f) => flags.has(f))) {
      for (const u of [...pos, vals.url]) { const n = urlName(u); if (n) targets.push({ p: pre(n) }); }
    }
    return { targets };
  }
  if (name === 'wget') {
    const { flags, vals, pos } = gnuParse(args, { v: 'OPaoiDeUTtwlAR', l: ['output-document', 'directory-prefix',
      'append-output', 'output-file', 'input-file', 'domains', 'execute', 'user-agent', 'timeout', 'tries', 'wait',
      'level', 'accept', 'reject', 'user', 'password', 'header'] });
    const targets = [];
    for (const k of ['o', 'output-file', 'a', 'append-output']) if (vals[k]) targets.push({ p: vals[k] });
    const doc = vals.O ?? vals['output-document'];
    const dir = vals.P ?? vals['directory-prefix'];
    if (doc !== undefined) { if (doc !== '-') targets.push({ p: doc }); }
    else if (['r', 'recursive', 'm', 'mirror'].some((f) => flags.has(f))) targets.push({ p: dir ?? '.', tree: true });
    else for (const u of pos) { const n = urlName(u); if (n) targets.push({ p: dir !== undefined ? `${dir}/${n}` : n }); }
    return { targets };
  }
  if (Object.hasOwn(COPY_LIKE, name)) return { targets: copyTargets(name, args) };
  return { targets: [] };
}

function changeDir(cwd, dir, ps) {
  if (dir === '-' || /[`*?]|\$\(/.test(dir)) return null;
  const e = expand(dir, ps);
  if (/[$%]/.test(e)) return null;
  if (cwd === null && !isAbsolute(e)) return null;
  return resolve(cwd ?? process.cwd(), e);
}

// -> [{ p, src?, into?, noDir?, rec?, contents?, cwd }]; no fs access
function shellTargets(cmd, cwd0, ps, depth = 0) {
  const toks = tokenize(cmd, ps);
  const out = [];
  const stack = [];
  let cwd = cwd0;
  let words = [];
  const flush = () => {
    const info = segmentInfo(words, ps);
    for (const t of info.targets) out.push({ ...t, cwd });
    if (info.nested && depth < 3) for (const nb of info.nested) out.push(...shellTargets(nb.cmd, cwd, nb.ps, depth + 1));
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
      // `>&WORD` / `N>&WORD` duplicates a fd only for digits (`1`, `1-`) or `-`; else WORD is a file
      if (t.op === '>' || (t.op === 'dup' && !/^(\d+-?|-)$/.test(next.w))) out.push({ p: next.w, cwd });
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
  // One alternation, longest first: a term inside a longer term (or username) never leaves
  // the longer one's remainder in clear. Terms override a same-spelled username (broader).
  const alts = new Map();
  for (const u of users) {
    if (u.length >= 3 && !['runner', 'runneradmin', 'root'].includes(u.toLowerCase())) alts.set(u.toLowerCase(), `(?<!\\w)${escapeRe(u)}(?!\\w)`);
  }
  const env = (process.env.CCV_PRIVACY_TERMS || '').split(/[,\r\n]/).map((s) => s.trim()).filter((s) => s && !s.startsWith('#'));
  const clone = cloneTermsFile(cwd);
  for (const t of [...env, ...fileLines(join(CLAUDE, 'privacy-terms')), ...(clone ? fileLines(clone) : [])]) alts.set(t.toLowerCase(), escapeRe(t));
  if (!alts.size) return out;
  const re = [...alts].sort((a, b) => b[0].length - a[0].length).map((x) => x[1]).join('|');
  return out.replace(new RegExp(re, 'gi'), '<redacted>');
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

// --- target -> manifest keys ---

const claudeRootKeys = () => (claudeKeys ??= [...new Set([keyOf(CLAUDE), keyOf(real(CLAUDE))])]);

// Manifest-relative keys of an absolute path, from its lexical form (a symlink / junction
// inside ~/.claude pointing outside still names the installed path) and its resolved form.
function relKeys(abs) {
  const out = new Set();
  for (const k of new Set([keyOf(abs), keyOf(real(abs))])) {
    for (const c of claudeRootKeys()) if (under(k, c)) out.add(k.slice(c.length + 1));
  }
  return out;
}

const GLOB = /[*?[]|\{[^{}]*,[^{}]*\}/;
function globSrc(pat) {
  let re = '';
  for (let i = 0; i < pat.length; i++) {
    const c = pat[i];
    if (c === '*') re += '[^/]*';
    else if (c === '?') re += '[^/]';
    else if (c === '[') {
      const j = pat.indexOf(']', i + 2);
      if (j < 0) { re += '\\['; continue; }
      const body = pat.slice(i + 1, j);
      const neg = body[0] === '!' || body[0] === '^';
      re += `[${neg ? '^' : ''}${(neg ? body.slice(1) : body).replace(/[\\\]^]/g, '\\$&')}]`;
      i = j;
    } else if (c === '{') {
      const j = pat.indexOf('}', i);
      const body = j < 0 ? '' : pat.slice(i + 1, j);
      if (!body.includes(',') || body.includes('{')) { re += '\\{'; continue; }
      re += `(?:${body.split(',').map(globSrc).join('|')})`;
      i = j;
    } else re += escapeRe(c);
  }
  return re;
}
const segMatch = (pat, seg) => (GLOB.test(pat) ? new RegExp(`^${globSrc(pat)}$`, WIN ? 'i' : '').test(seg) : pat === seg);
function globRel(pat, key) {
  const p = pat.split('/');
  const k = key.split('/');
  return p.length === k.length && p.every((s, i) => segMatch(s, k[i]));
}

// Does the copy source hold `segs`? Unknown (unexpanded, glob, unresolvable cwd) and UNC
// sources are never touched on disk and count as holding it.
function srcHas(base, segs) {
  if (base === null || /[*?[$%]/.test(base) || keyOf(base).startsWith('//')) return true;
  try { statSync(join(base, ...segs)); return true; } catch { return false; }
}

// Manifest keys (not yet filtered for kept) that target t writes.
function writtenKeys(t, lex, map, ps) {
  const out = [];
  if (t.tree === true) { // archive extraction: unknown members, every entry under the dir
    const dests = [...new Set([keyOf(lex), keyOf(real(lex))])];
    for (const k of map.keys()) {
      if (claudeRootKeys().some((c) => dests.some((d) => under(`${c}/${k}`, d)))) out.push(k);
    }
    return out;
  }
  if (t.src === undefined) {
    for (const r of relKeys(lex)) {
      if (!r) continue;
      if (GLOB.test(r)) { for (const k of map.keys()) if (globRel(r, k)) out.push(k); } else out.push(r);
    }
    return out;
  }
  let isDir = t.into === true || (!t.noDir && /[\\/]$/.test(t.p));
  if (!isDir && !t.noDir) try { isDir = statSync(real(lex)).isDirectory(); } catch {}
  const name = basename(t.src.replace(/[\\/]+$/, ''));
  const src = lexical(t.src, t.cwd, ps);
  const into = isDir && !t.contents; // dest/<name>[/...]; else dest itself is the copy of src
  if (into && !t.rec && !GLOB.test(name)) {
    for (const r of relKeys(join(lex, name))) if (r) out.push(r);
    return out;
  }
  if (!isDir) for (const r of relKeys(lex)) if (r) out.push(r);
  if (!isDir && !t.rec) return out;
  const first = into ? keyOf(name) : null;
  const base = src === null ? null : into ? dirname(src) : src;
  const dests = [...new Set([keyOf(lex), keyOf(real(lex))])];
  for (const k of map.keys()) {
    let rest = null;
    for (const c of claudeRootKeys()) {
      const full = `${c}/${k}`;
      const d = dests.find((x) => under(full, x) && full !== x);
      if (d !== undefined) { rest = full.slice(d.length + 1).split('/'); break; }
    }
    if (rest === null || (rest.length > 1 && !t.rec)) continue;
    if (first !== null && !segMatch(first, rest[0])) continue;
    if (srcHas(base, rest)) out.push(k);
  }
  return out;
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
  const realClaude = real(CLAUDE);
  const hits = [];
  const seen = new Set();
  for (const t of raw) {
    const lex = lexical(t.p, t.cwd, ps);
    if (lex === null) continue;
    // a copy into an ancestor of ~/.claude (cp -r repo/.claude ~/) can still land on managed files
    if (!plausible(lex) && !(t.src !== undefined && claudeRootKeys().some((c) => under(c, keyOf(lex))))) continue;
    for (const rel of writtenKeys(t, lex, manifest.map, ps)) {
      if (seen.has(rel)) continue;
      seen.add(rel);
      const entry = manifest.map.get(rel);
      if (entry && entry.kept !== true && typeof entry.repo_path === 'string') hits.push({ abs: join(realClaude, entry.rel), entry });
    }
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
  for (const { abs, entry } of hits.slice(0, MAX_PROPOSALS)) {
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
  if (hits.length > MAX_PROPOSALS) {
    const more = hits.slice(MAX_PROPOSALS).map((h) => h.entry.repo_path);
    lines.push(`${more.length} more installed harness files are targeted (no proposal saved): ${more.slice(0, 20).join(', ')}${more.length > 20 ? ', ...' : ''}.`);
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
