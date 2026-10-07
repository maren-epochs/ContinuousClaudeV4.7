#!/usr/bin/env node
/**
 * PreToolUse guard: installed harness files are generated from the ccv47 repo.
 *
 * A Write/Edit/MultiEdit/NotebookEdit whose target, or a Bash/PowerShell command whose
 * write target (>, >>, N>, &>, tee, cp, mv, Copy-Item, Move-Item, Set-Content,
 * Add-Content, Out-File, Tee-Object), resolves to a file listed in the install manifest
 * (~/.claude/.ccv47-manifest.json, tools/fleet/schema.md) is DENIED. The deny names the
 * repo file to change and the inbox id of a Proposal (kind edit, the exact intended
 * change) saved to ~/.claude/harness-inbox/<id>.json. Kept (keep-list) entries are
 * user-owned and allowed. No manifest -> allow everything.
 *
 * Path matching: ~, $HOME, ${HOME}, $env:NAME, %NAME% expanded; Git Bash /c/ form and
 * relative paths (against the session cwd) resolved; realpath.native on the deepest
 * existing parent (8.3 short names, symlinks); case-insensitive on win32.
 *
 * Fails open: any internal error (malformed stdin or manifest, IO) allows the call and
 * appends a line to ~/.claude/fleet/guard-errors.log. Only a deny prints output.
 */
import { readFileSync, existsSync, realpathSync, statSync, writeFileSync, renameSync, mkdirSync, appendFileSync } from 'fs';
import { join, resolve, dirname, basename } from 'path';
import { homedir } from 'os';

const WIN = process.platform === 'win32';
const FILE_TOOLS = { Write: 'file_path', Edit: 'file_path', MultiEdit: 'file_path', NotebookEdit: 'notebook_path' };
const HOME = homedir();
const CLAUDE = join(HOME, '.claude');

const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

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
  if (WIN) s = s.replace(/^\/(?:cygdrive\/)?([A-Za-z])(?=$|\/)/, '$1:');
  else if (ps) s = s.replace(/\\/g, '/'); // pwsh accepts \ as a separator on every OS
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

function normalize(p, cwd, ps) {
  return real(resolve(cwd, expand(p, ps)));
}

// --- shell command write targets ---

function tokenize(cmd, ps) {
  const toks = [];
  const heredocs = [];
  let cur = null;
  let i = 0;
  const n = cmd.length;
  const push = () => {
    if (cur === null) return;
    const prev = toks[toks.length - 1];
    if (prev && prev.op === '<<') heredocs.push(cur);
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
    if (ps && c === '@' && cur === null && (cmd[i + 1] === "'" || cmd[i + 1] === '"') && /^[ \t]*\r?\n/.test(cmd.slice(i + 2))) {
      const close = cmd.indexOf(`\n${cmd[i + 1]}@`, i + 2);
      const end = close < 0 ? n : close;
      cur = cmd.slice(cmd.indexOf('\n', i) + 1, end);
      i = close < 0 ? n : close + 3;
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
const PS_FILE = new Set(['out-file', 'tee-object', 'tee']);
const PS_VALUED = ['path', 'literalpath', 'pspath', 'lp', 'destination', 'filepath', 'value', 'encoding', 'filter',
  'include', 'exclude', 'credential', 'stream', 'width', 'inputobject', 'delimiter', 'variable'];

function psParams(args) {
  const named = {};
  const pos = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    const m = /^-([A-Za-z][\w-]*)(?::(.*))?$/.exec(a);
    if (!m) { pos.push(a); continue; }
    const name = m[1].toLowerCase();
    const full = PS_VALUED.find((v) => v === name) || (name.length >= 3 ? PS_VALUED.find((v) => v.startsWith(name)) : undefined);
    if (!full) continue;
    const value = m[2] !== undefined ? m[2] : args[++i];
    if (value !== undefined) named[full] = value;
  }
  return { named, pos };
}

function isDirTarget(p, cwd, ps) {
  if (/[\\/]$/.test(p)) return true;
  try { return statSync(normalize(p, cwd, ps)).isDirectory(); } catch { return false; }
}

const intoDir = (dest, src, cwd, ps) => (isDirTarget(dest, cwd, ps) ? join(dest, basename(src.replace(/[\\/]+$/, ''))) : dest);

function segmentTargets(words, cwd, ps) {
  let k = 0;
  while (k < words.length && (PREFIXES.has(words[k].toLowerCase()) || /^[A-Za-z_]\w*=/.test(words[k]))) k++;
  if (k >= words.length) return [];
  const name = basename(words[k].replace(/\\/g, '/')).toLowerCase().replace(/\.exe$/, '');
  const args = words.slice(k + 1);
  if (ps) {
    const { named, pos } = psParams(args);
    const path = named.path ?? named.literalpath ?? named.pspath ?? named.lp;
    if (PS_COPY.has(name)) {
      const src = path ?? pos[0];
      const dest = named.destination ?? (path !== undefined ? pos[0] : pos[1]);
      return dest === undefined ? [] : [src === undefined ? dest : intoDir(dest, src, cwd, ps)];
    }
    if (PS_CONTENT.has(name)) { const t = path ?? pos[0]; return t === undefined ? [] : [t]; }
    if (PS_FILE.has(name)) { const t = named.filepath ?? path ?? pos[0]; return t === undefined ? [] : [t]; }
    return [];
  }
  if (name === 'tee') return args.filter((a) => !a.startsWith('-'));
  if (name !== 'cp' && name !== 'mv') return [];
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
  if (dir !== null) return rest.map((s) => join(dir, basename(s.replace(/[\\/]+$/, ''))));
  if (rest.length < 2) return [];
  const dest = rest.pop();
  return rest.map((s) => intoDir(dest, s, cwd, ps));
}

function shellTargets(cmd, cwd, ps) {
  const toks = tokenize(cmd, ps);
  const out = [];
  let words = [];
  for (let i = 0; i < toks.length; i++) {
    const t = toks[i];
    if (t.w !== undefined) { words.push(t.w); continue; }
    if (t.op === ';') { out.push(...segmentTargets(words, cwd, ps)); words = []; continue; }
    const next = toks[i + 1];
    if (next && next.w !== undefined) {
      i++;
      if (t.op === '>') out.push(next.w);
    }
  }
  out.push(...segmentTargets(words, cwd, ps));
  return out.filter((p) => p && !/^(\/dev\/(null|stdout|stderr)|\$null|nul)$/i.test(p));
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

function newId() {
  const stamp = nowIso().replace(/[-:]/g, '');
  const bytes = new Uint8Array(4);
  globalThis.crypto.getRandomValues(bytes);
  return `${stamp}-${Buffer.from(bytes).toString('hex')}`;
}

function changeOf(tool, input) {
  const s = (v) => (typeof v === 'string' ? v : null);
  const change = { tool, content: null, old_string: null, new_string: null, command: null };
  if (tool === 'Write') change.content = s(input.content);
  else if (tool === 'Edit') {
    change.old_string = s(input.old_string);
    change.new_string = s(input.new_string);
    if (typeof input.replace_all === 'boolean') change.replace_all = input.replace_all;
  } else if (tool === 'MultiEdit') change.edits = Array.isArray(input.edits) ? input.edits : [];
  else if (tool === 'NotebookEdit') {
    change.content = s(input.new_source);
    change.notebook = { cell_id: s(input.cell_id), cell_type: s(input.cell_type), edit_mode: s(input.edit_mode) };
  } else change.command = s(input.command);
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

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { return logError('unparseable hook input'); }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return logError('hook input is not an object');
  const tool = data.tool_name;
  const input = data.tool_input;
  if (!input || typeof input !== 'object') return;
  const cwd = typeof data.cwd === 'string' && data.cwd ? expand(data.cwd, false) : process.cwd();

  let raw;
  if (FILE_TOOLS[tool]) {
    const p = input[FILE_TOOLS[tool]];
    if (typeof p !== 'string' || !p) return;
    raw = [p];
  } else if (tool === 'Bash' || tool === 'PowerShell') {
    if (typeof input.command !== 'string') return;
    raw = shellTargets(input.command, cwd, tool === 'PowerShell');
    if (!raw.length) return;
  } else return;

  const manifest = loadManifest();
  if (!manifest) return;
  const claudeKey = keyOf(real(CLAUDE));
  const hits = [];
  const seen = new Set();
  for (const r of raw) {
    const abs = normalize(r, cwd, tool === 'PowerShell');
    const k = keyOf(abs);
    if (!k.startsWith(claudeKey + '/') || seen.has(k)) continue;
    seen.add(k);
    const entry = manifest.map.get(k.slice(claudeKey.length + 1));
    if (entry && entry.kept !== true && typeof entry.repo_path === 'string') hits.push({ abs, entry });
  }
  if (!hits.length) return;

  const lines = [];
  for (const { abs, entry } of hits) {
    const repoFile = manifest.repo ? join(manifest.repo, entry.repo_path) : entry.repo_path;
    const id = newId();
    const reason = `${abs} is installed from the ccv47 harness repo; change the repo file ${repoFile} (${entry.repo_path}) and re-run install/sync_global.py --apply.`;
    saveProposal({
      schema_version: 1,
      id,
      created_at: nowIso(),
      kind: 'edit',
      source: { project: basename(cwd) || null, session_id: typeof data.session_id === 'string' ? data.session_id : null, cwd },
      target: { installed_path: abs, repo_path: entry.repo_path, repo: manifest.repo },
      change: changeOf(tool, input),
      reason,
      status: 'pending',
    });
    lines.push(`${reason} The intended change is saved as inbox id ${id} (~/.claude/harness-inbox/${id}.json).`);
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
