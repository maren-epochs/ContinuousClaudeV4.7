#!/usr/bin/env node
/**
 * PreCompact hook — auto-handoff before context compaction.
 *
 * Transcript schema (real Claude Code JSONL): each line is an entry;
 * conversational entries have type 'user'|'assistant' and entry.message.
 * Assistant content is an array of blocks {type:'text'|'thinking'|'tool_use'};
 * tool_use blocks carry {id, name, input}. User content is either a plain
 * string (actual user text) or an array containing tool_result blocks
 * {tool_use_id, content, is_error}. Sidechain (subagent) user text is not
 * the session goal.
 *
 * Constraints: stdout must always be a valid hook response; all IO guarded;
 * never throw or block. Scan cost capped: one bounded read, extraction
 * limited to the last MAX_ENTRIES conversational entries (lines parsed from
 * the end, so a multi-MB transcript does not pay full JSON.parse cost).
 *
 * Input: JSON on stdin with trigger, session_id, transcript_path
 * Output: JSON with continue: true, systemMessage
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'fs';
import { join } from 'path';

const MAX_ENTRIES = 400;

function flattenResultContent(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .filter((b) => b && typeof b === 'object' && b.type === 'text' && typeof b.text === 'string')
      .map((b) => b.text)
      .join('\n');
  }
  return '';
}

function parseTranscript(transcriptPath) {
  const summary = {
    goal: '',
    lastTodos: [],
    recentToolCalls: [],
    lastAssistantMessage: '',
    filesModified: [],
    recentCommands: [],
    errors: [],
    lastExitCode: null,
  };

  let content;
  try { content = readFileSync(transcriptPath, 'utf-8'); } catch { return summary; }

  // Collect the last MAX_ENTRIES conversational entries, parsing from the end.
  const lines = content.split('\n');
  const entries = [];
  for (let i = lines.length - 1; i >= 0 && entries.length < MAX_ENTRIES; i--) {
    const line = lines[i].trim();
    if (!line) continue;
    let entry;
    try { entry = JSON.parse(line); } catch { continue; }
    if (!entry || typeof entry !== 'object') continue;
    if ((entry.type === 'user' || entry.type === 'assistant') && entry.message && typeof entry.message === 'object') {
      entries.push(entry);
    }
  }
  entries.reverse();

  const callsById = new Map();
  const allToolCalls = [];
  const filesModified = new Set();

  const isGoalText = (t) =>
    t && !t.startsWith('<') && !t.startsWith('Caveat:') && !t.startsWith('[Request interrupted');

  for (const entry of entries) {
    const msg = entry.message;
    const blocks = Array.isArray(msg.content) ? msg.content : null;

    if (entry.type === 'assistant' && blocks) {
      for (const b of blocks) {
        if (!b || typeof b !== 'object') continue;

        if (b.type === 'text' && typeof b.text === 'string' && b.text.trim()) {
          summary.lastAssistantMessage = b.text;
        }

        if (b.type === 'tool_use') {
          const name = typeof b.name === 'string' ? b.name : 'unknown';
          const input = (b.input && typeof b.input === 'object') ? b.input : {};
          const lname = name.toLowerCase();
          const tc = { name, input: null, success: true };

          if (lname === 'todowrite' && Array.isArray(input.todos)) {
            summary.lastTodos = input.todos.map((t, i) => ({
              id: (t && t.id) || `todo-${i}`,
              content: (t && t.content) || '',
              status: (t && t.status) || 'pending',
            }));
          }

          if (['edit', 'write', 'multiedit', 'notebookedit', 'update'].includes(lname)) {
            const fp = input.file_path || input.path || input.notebook_path;
            if (fp) { filesModified.add(fp); tc.input = { file_path: fp }; }
          }

          if ((lname === 'bash' || lname === 'powershell') && typeof input.command === 'string') {
            summary.recentCommands.push(input.command);
            tc.input = { command: input.command };
          }

          if (b.id) callsById.set(b.id, tc);
          allToolCalls.push(tc);
        }
      }
    }

    if (entry.type === 'user') {
      if (typeof msg.content === 'string') {
        if (entry.isSidechain !== true && isGoalText(msg.content.trim())) {
          summary.goal = msg.content.trim().slice(0, 300);
        }
      } else if (blocks) {
        for (const b of blocks) {
          if (!b || typeof b !== 'object') continue;

          if (b.type === 'text' && typeof b.text === 'string' &&
              entry.isSidechain !== true && isGoalText(b.text.trim())) {
            summary.goal = b.text.trim().slice(0, 300);
          }

          if (b.type === 'tool_result') {
            const tc = callsById.get(b.tool_use_id);
            const text = flattenResultContent(b.content);
            const exitMatch = /exit code[:\s]*(\d+)/i.exec(text);
            if (exitMatch) summary.lastExitCode = Number(exitMatch[1]);
            if (b.is_error === true) {
              if (tc) tc.success = false;
              const label = tc
                ? (tc.input && tc.input.command) || (tc.input && tc.input.file_path) || tc.name
                : 'tool';
              summary.errors.push(`${label}: ${(text || 'failed').slice(0, 200)}`);
            }
          }
        }
      }
    }
  }

  summary.recentToolCalls = allToolCalls.slice(-8);
  summary.filesModified = [...filesModified].slice(-20);
  summary.recentCommands = summary.recentCommands.slice(-8);
  summary.lastAssistantMessage = summary.lastAssistantMessage.slice(0, 500);
  summary.errors = summary.errors.slice(-5);
  return summary;
}

function generateHandoff(summary, sessionName) {
  const ts = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  const lines = [
    '---', `date: ${ts}`, 'type: auto-handoff', 'trigger: pre-compact-auto',
    `session: ${sessionName}`, '---', '',
    '# Auto-Handoff (PreCompact)', '',
    'This handoff was automatically generated before context compaction.', '',
  ];

  lines.push('## Goal', '');
  lines.push(summary.goal || 'No user goal captured.', '');

  lines.push('## In Progress', '');
  if (summary.lastTodos.length) {
    const inProgress = summary.lastTodos.filter(t => t.status === 'in_progress');
    const pending = summary.lastTodos.filter(t => t.status === 'pending');
    const completed = summary.lastTodos.filter(t => t.status === 'completed');
    if (inProgress.length) { lines.push('**Active:**'); inProgress.forEach(t => lines.push(`- [>] ${t.content}`)); lines.push(''); }
    if (pending.length) { lines.push('**Pending:**'); pending.forEach(t => lines.push(`- [ ] ${t.content}`)); lines.push(''); }
    if (completed.length) { lines.push('**Completed this session:**'); completed.forEach(t => lines.push(`- [x] ${t.content}`)); lines.push(''); }
  } else {
    lines.push('No TodoWrite state captured.', '');
  }

  lines.push('## Recent Actions', '');
  if (summary.recentToolCalls.length) {
    for (const tc of summary.recentToolCalls) {
      const status = tc.success ? 'OK' : 'FAILED';
      const inp = tc.input ? ` - ${JSON.stringify(tc.input).slice(0, 100)}` : '';
      lines.push(`- ${tc.name} [${status}]${inp}`);
    }
  } else lines.push('No tool calls recorded.');
  lines.push('');

  lines.push('## Files Modified', '');
  if (summary.filesModified.length) summary.filesModified.forEach(f => lines.push(`- ${f}`));
  else lines.push('No files modified.');
  lines.push('');

  lines.push('## Recent Commands', '');
  if (summary.recentCommands.length) summary.recentCommands.forEach(c => lines.push(`- \`${c.slice(0, 160)}\``));
  else lines.push('No commands recorded.');
  lines.push('');

  if (summary.lastExitCode != null) {
    lines.push('## Test Status', '',
      `Last visible exit code in tool results: ${summary.lastExitCode}`, '');
  }

  if (summary.errors.length) {
    lines.push('## Errors Encountered', '');
    summary.errors.forEach(e => lines.push('```', e, '```'));
    lines.push('');
  }

  lines.push('## Last Context', '');
  if (summary.lastAssistantMessage) {
    lines.push('```', summary.lastAssistantMessage);
    if (summary.lastAssistantMessage.length >= 500) lines.push('[... truncated]');
    lines.push('```');
  } else lines.push('No assistant message captured.');
  lines.push('');

  lines.push('## Suggested Next Steps', '',
    '1. Review "Goal" and "In Progress" for current task state',
    '2. Check "Errors Encountered" and "Test Status" if debugging',
    '3. Read modified files to understand recent changes',
    '4. Continue from where session left off', '');

  return lines.join('\n');
}

function main() {
  let input = {};
  try { input = JSON.parse(readFileSync(0, 'utf-8')); } catch { input = {}; }
  if (!input || typeof input !== 'object') input = {};

  const projectDir = process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const trigger = input.trigger || 'auto';
  const sessionId = input.session_id || 'unknown';
  const sessionName = String(sessionId).slice(0, 8) || 'unknown';
  const transcriptPath = input.transcript_path || '';

  let message;
  let transcriptExists = false;
  try { transcriptExists = Boolean(transcriptPath) && existsSync(transcriptPath); } catch { transcriptExists = false; }

  if (trigger === 'auto' && transcriptExists) {
    try {
      const summary = parseTranscript(transcriptPath);
      const content = generateHandoff(summary, sessionName);

      const handoffDir = join(projectDir, 'thoughts', 'shared', 'handoffs', sessionName);
      mkdirSync(handoffDir, { recursive: true });

      const ts = new Date().toISOString().replace(/:/g, '-').replace(/\.\d+Z$/, '');
      const filename = `auto-handoff-${ts}.md`;
      writeFileSync(join(handoffDir, filename), content);

      message = `[PreCompact:auto] Created ${filename} in thoughts/shared/handoffs/${sessionName}/`;
    } catch (err) {
      message = `[PreCompact:auto] Handoff generation failed (${err && err.message ? String(err.message).slice(0, 120) : 'unknown error'}). Consider running /create-handoff manually.`;
    }
  } else if (trigger === 'auto') {
    message = '[PreCompact:auto] No transcript available. Consider running /create-handoff manually.';
  } else {
    message = '[PreCompact] Consider running /create-handoff before compacting.';
  }

  console.log(JSON.stringify({ continue: true, systemMessage: message }));
}

try { main(); } catch { console.log(JSON.stringify({ continue: true })); }
