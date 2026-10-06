#!/usr/bin/env node
/**
 * Worker Stop hook (frontmatter `Stop` -> SubagentStop at runtime).
 *
 * Validates the worker's report with tools/validate_report.py before the
 * worker may finish. ERROR lines -> {"decision":"block"} so the worker keeps
 * going and fixes the report. Everything else allows the stop.
 *
 * Report path: the `output` field of the worker's own prompt (first user
 * message in agent_transcript_path), else the last Write/Edit to a
 * continuum/autonomous/*\/reports/*.json path. Never newest-by-mtime:
 * parallel workers write sibling reports and would cross-wire.
 *
 * Loop guard: stop_hook_active -> allow; at most MAX_BLOCKS blocks per agent_id.
 * Fail-open: missing transcript/python/validator -> allow, note on stderr.
 */
import { readFileSync, writeFileSync, existsSync } from 'fs';
import { spawnSync } from 'child_process';
import { join, dirname, isAbsolute, resolve } from 'path';
import { tmpdir } from 'os';
import { fileURLToPath } from 'url';

const WIN = process.platform === 'win32';
const MAX_BLOCKS = 2;
const HERE = dirname(fileURLToPath(import.meta.url));
const REPORT_RE = /continuum[\\/]autonomous[\\/][^\\/]+[\\/]reports[\\/][^\\/]+\.json$/;

const allow = (note) => {
  if (note) process.stderr.write(`worker-report-check: ${note}\n`);
  console.log('{}');
};

// Installed layout: ~/.claude/hooks + ~/.claude/tools. Repo layout: .claude/hooks + tools/.
function findValidator() {
  if (process.env.WORKER_REPORT_VALIDATOR) return process.env.WORKER_REPORT_VALIDATOR;
  return [join(HERE, '..', 'tools', 'validate_report.py'), join(HERE, '..', '..', 'tools', 'validate_report.py')]
    .find((p) => existsSync(p)) || null;
}

function textOf(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.map((b) => (b && b.type === 'text' ? b.text : '')).join('\n');
  return '';
}

function findReportPath(transcriptPath) {
  let lines;
  try { lines = readFileSync(transcriptPath, 'utf-8').split('\n'); } catch { return null; }
  let output = null, lastWrite = null;
  for (const line of lines) {
    if (!line.trim()) continue;
    let ev; try { ev = JSON.parse(line); } catch { continue; }
    const msg = ev.message || {};
    if (output === null && (ev.type === 'user' || msg.role === 'user')) {
      const m = textOf(msg.content).match(/"output"\s*:\s*"([^"]+\.json)"/);
      if (m) output = m[1];
    }
    if (Array.isArray(msg.content)) {
      for (const b of msg.content) {
        const fp = b && b.type === 'tool_use' && b.input && b.input.file_path;
        if (fp && REPORT_RE.test(fp)) lastWrite = fp;
      }
    }
  }
  return output || lastWrite;
}

function bumpBlocks(agentId) {
  const f = join(tmpdir(), `worker-report-check-${String(agentId).replace(/[^\w-]/g, '_')}.txt`);
  let n = 0;
  try { n = parseInt(readFileSync(f, 'utf-8'), 10) || 0; } catch {}
  try { writeFileSync(f, String(n + 1)); } catch {}
  return n + 1;
}

// ERROR lines from validate_report.py, [] when valid, null when the validator can't run.
function validate(report) {
  const validator = findValidator();
  if (!validator) return null;
  const [cmd, args] = WIN ? ['py', ['-3.13', validator, report]] : ['python3', [validator, report]];
  const proc = spawnSync(process.env.WORKER_REPORT_PYTHON || cmd,
    process.env.WORKER_REPORT_PYTHON ? [validator, report] : args,
    { encoding: 'utf-8', timeout: 20000, windowsHide: true });
  if (proc.error || proc.status === null || (proc.status !== 0 && proc.status !== 1)) return null;
  return `${proc.stdout || ''}\n${proc.stderr || ''}`.split('\n').filter((l) => /\bERROR\b/.test(l));
}

const REPORT_PATH = /continuum[\\/]autonomous[\\/][^\\/]+[\\/]reports[\\/][^\\/]+\.json$/;

// PostToolUse on the worker's own Write/Edit: the worker is still running, so a block
// here reaches it — unlike Stop, which a background worker may already have passed.
function onReportWrite(data) {
  const fp = (data.tool_input || {}).file_path;
  if (typeof fp !== 'string' || !REPORT_PATH.test(fp)) return console.log('{}');
  const errors = validate(isAbsolute(fp) ? fp : resolve(data.cwd || process.cwd(), fp));
  if (!errors || !errors.length) return console.log('{}');
  console.log(JSON.stringify({
    decision: 'block',
    reason: `Report ${fp} fails schema validation. Fix every ERROR in the report now:\n`
      + errors.slice(0, 20).join('\n') + (errors.length > 20 ? `\n... and ${errors.length - 20} more` : ''),
  }));
}

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { return allow('unparseable hook input'); }
  if (!data || typeof data !== 'object') return allow('unparseable hook input');
  if (data.hook_event_name === 'PostToolUse') return onReportWrite(data);
  if (data.stop_hook_active) return allow();

  const transcript = data.agent_transcript_path || data.transcript_path;
  if (!transcript) return allow('no transcript path in hook input');
  let report = findReportPath(transcript);
  if (!report) return allow('no report path found in worker transcript');
  if (!isAbsolute(report)) report = resolve(data.cwd || process.cwd(), report);

  const blocks = () => bumpBlocks(data.agent_id || data.session_id || 'unknown');
  if (!existsSync(report)) {
    if (blocks() > MAX_BLOCKS) return allow(`report still missing after ${MAX_BLOCKS} blocks: ${report}`);
    return console.log(JSON.stringify({
      decision: 'block',
      reason: `No report at ${report}. Write the report JSON to your output path before finishing.`,
    }));
  }

  const validator = findValidator();
  if (!validator) return allow('validate_report.py not found');
  const [cmd, args] = WIN ? ['py', ['-3.13', validator, report]] : ['python3', [validator, report]];
  const proc = spawnSync(process.env.WORKER_REPORT_PYTHON || cmd,
    process.env.WORKER_REPORT_PYTHON ? [validator, report] : args,
    { encoding: 'utf-8', timeout: 20000, windowsHide: true });
  if (proc.error || proc.status === null) return allow(`validator did not run: ${proc.error ? proc.error.message : 'timeout'}`);
  if (proc.status === 0) return allow();
  if (proc.status !== 1) return allow(`validator exit ${proc.status}: ${(proc.stderr || '').trim().slice(0, 200)}`);

  const errors = `${proc.stdout || ''}\n${proc.stderr || ''}`.split('\n').filter((l) => /\bERROR\b/.test(l));
  if (!errors.length) return allow('validator exit 1 without ERROR lines');
  if (blocks() > MAX_BLOCKS) return allow(`report still invalid after ${MAX_BLOCKS} blocks; VALIDATE will normalize`);
  console.log(JSON.stringify({
    decision: 'block',
    reason: `Report ${report} fails schema validation. Fix every ERROR (report only, no code changes), then finish:\n`
      + errors.slice(0, 20).join('\n') + (errors.length > 20 ? `\n... and ${errors.length - 20} more` : ''),
  }));
}

main();
