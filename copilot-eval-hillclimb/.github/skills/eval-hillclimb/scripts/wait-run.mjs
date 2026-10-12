#!/usr/bin/env node
// Block until an eval pass started in the background has finished, or until
// --max-wait-s passes - whichever is first (Python twin: wait_run.py).
//
//   node wait-run.mjs .hillclimb/<flow>/<variant> [--max-wait-s 540] [--expect N]
//
// For VS Code agent mode: run it as a sync terminal call with a tool timeout a
// little above --max-wait-s. It prints one status line every --interval-s
// (default 30 s; the steady output also keeps the terminal tool from moving a
// silent command to the background) and reads only <variant>/progress.txt and
// the line count of <variant>/results.jsonl - never transcripts.
//
// Exit 0: progress.txt ends with the runner's 'finished' line (the pass is over;
//         read its exit code on that line and check the row count).
// Exit 3: still running when --max-wait-s ran out - call again.
// Exit 2: bad arguments, or no progress.txt at all (nothing was started there).

import { lstatSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const argv = process.argv.slice(2);
let dir = null, maxWait = 540, interval = 30, expect = null;
for (let i = 0; i < argv.length; i++) {
  const k = argv[i];
  if (k === '--max-wait-s') maxWait = +argv[++i];
  else if (k === '--interval-s') interval = +argv[++i];
  else if (k === '--expect') expect = +argv[++i];
  else if (!k.startsWith('-') && dir == null) dir = k;
  else { dir = null; break; }
}
if (dir == null || !(maxWait >= 0) || !(interval > 0) || (expect != null && !(expect >= 0))) {
  console.error('usage: node wait-run.mjs <flow>/<variant> [--max-wait-s 540] [--interval-s 30] [--expect N]');
  process.exit(2);
}
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f-\x9f]|\x1b\[[0-?]*[ -\/]*[@-~]/g;
// Regular files only - never follow a planted link out of the flow dir.
const read = p => { try { return lstatSync(p).isFile() ? readFileSync(p, 'utf8') : null; } catch { return null; } };
const rows = () => (read(join(dir, 'results.jsonl')) ?? '').split('\n').filter(l => l.trim()).length;
const t0 = Date.now();
for (;;) {
  const prog = read(join(dir, 'progress.txt'));
  if (prog == null) { console.error(`no progress.txt in ${dir} - was the pass started there?`); process.exit(2); }
  const lines = prog.replace(CONTROL, '').trim().split('\n');
  const last = lines.pop() ?? '';
  const rowInfo = `rows ${rows()}${expect != null ? '/' + expect : ''}`;
  if (last.startsWith('finished')) {
    console.log(`${lines.pop() ?? ''} | ${last} | ${rowInfo}`);
    process.exit(0);
  }
  const waited = (Date.now() - t0) / 1000;
  console.log(`${last} | ${rowInfo} | waited ${Math.round(waited)}s`);
  if (waited + interval > maxWait) { console.log('still running - call wait-run again'); process.exit(3); }
  await new Promise(r => setTimeout(r, interval * 1000));
}
