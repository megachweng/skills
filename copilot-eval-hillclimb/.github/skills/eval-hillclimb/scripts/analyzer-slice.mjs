#!/usr/bin/env node
// Prepare the analyzer's train-only inputs for one hillclimb round, without the
// outer agent reading any of them (Python twin: analyzer_slice.py).
//
//   node analyzer-slice.mjs .hillclimb/<flow> <variant> [--all]
//
// Writes <flow>/<variant>/_analyzer/:
//   results.train.jsonl   the variant's results.jsonl rows whose case is in _state.json.train_ids
//   scores.train.tsv      trajectory/scores.tsv header + train rows (run the report builder first)
//   traces.txt            absolute paths of the variant's train trace files, one per line
// and prints only counts and paths - never row or transcript content. Hand those
// three files to the eval-analyzer subagent. --all includes every case (only for
// a flow with no held-out split, whose numbers are directional anyway).
// Exit 2 when there is no train split (and no --all) or the variant has no rows.

import { lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

const [flowArg, variant, ...rest] = process.argv.slice(2);
const all = rest.includes('--all');
if (!flowArg || !/^(baseline|v[1-9]\d*)$/.test(variant ?? '') || rest.some(a => a !== '--all')) {
  console.error('usage: node analyzer-slice.mjs <flow-dir> <baseline|vN> [--all]');
  process.exit(2);
}
const flow = resolve(flowArg);
// Regular files only: never follow a planted link out of the flow dir.
const isFile = p => { try { return lstatSync(p).isFile(); } catch { return false; } };
const isDir = p => { try { return lstatSync(p).isDirectory(); } catch { return false; } };
const read = p => (isFile(p) ? readFileSync(p, 'utf8') : '');
let state = {};
try { state = JSON.parse(read(join(flow, '_state.json')) || '{}') || {}; } catch { console.error('_state.json is not valid JSON'); process.exit(2); }
const train = new Set((Array.isArray(state.train_ids) ? state.train_ids : []).map(String));
if (!train.size && !all) { console.error('no _state.json.train_ids - draw the split first (or pass --all for a no-split flow)'); process.exit(2); }
const keep = id => all || train.has(id);
const vdir = join(flow, variant);
if (!isDir(vdir)) { console.error(`no variant directory ${variant}/`); process.exit(2); }
const out = join(vdir, '_analyzer');
if (lstatSync(vdir).isSymbolicLink() || (isDir(out) === false && (() => { try { return lstatSync(out).isSymbolicLink(); } catch { return false; } })())) {
  console.error('refusing: symlinked variant or _analyzer directory'); process.exit(2);
}
mkdirSync(out, { recursive: true });
if (!realpathSync(out).startsWith(realpathSync(flow))) { console.error('refusing: _analyzer resolves outside the flow'); process.exit(2); }
const writeOut = (name, text) => {
  const p = join(out, name);
  try { if (lstatSync(p).isSymbolicLink() || lstatSync(p).nlink > 1) { console.error(`refusing to overwrite ${p}`); process.exit(2); } } catch {}
  writeFileSync(p, text);
};

const rows = read(join(vdir, 'results.jsonl')).split('\n').filter(l => {
  if (!l.trim()) return false;
  try { const r = JSON.parse(l); return r && typeof r === 'object' && keep(String(r.prompt_id ?? r.id ?? r.case_id ?? '')); } catch { return false; }
});
if (!rows.length) { console.error(`${variant}/results.jsonl has no ${all ? '' : 'train '}rows`); process.exit(2); }
writeOut('results.train.jsonl', rows.join('\n') + '\n');

const tsv = read(join(flow, 'trajectory', 'scores.tsv')).split('\n').filter(Boolean);
const tsvRows = tsv.length ? [tsv[0], ...tsv.slice(1).filter(l => keep(l.split('\t')[0].replace(/^'/, '')))] : [];
writeOut('scores.train.tsv', tsvRows.length ? tsvRows.join('\n') + '\n' : '');

const tdir = join(vdir, 'traces');
const traces = (isDir(tdir) ? readdirSync(tdir) : []).filter(f => {
  const m = /^(.+?)(?:_rep\d+)?\.json$/.exec(f);
  return m && keep(m[1]) && isFile(join(tdir, f)) && lstatSync(join(tdir, f)).nlink === 1;
}).sort().map(f => join(realpathSync(tdir), f));
writeOut('traces.txt', traces.join('\n') + (traces.length ? '\n' : ''));

console.log(`${variant}: ${rows.length} ${all ? '' : 'train '}rows, ${Math.max(0, tsvRows.length - 1)} scores.tsv rows, ${traces.length} trace files`);
console.log(`hand the analyzer: ${join(out, 'results.train.jsonl')}, ${join(out, 'scores.train.tsv')}, and the paths listed in ${join(out, 'traces.txt')}`);
if (!tsvRows.length) console.log('note: no trajectory/scores.tsv yet - run the report builder first');
