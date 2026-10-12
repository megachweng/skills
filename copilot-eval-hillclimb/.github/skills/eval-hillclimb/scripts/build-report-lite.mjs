#!/usr/bin/env node
// Lite report builder for the build-eval / hillclimb loop (VS Code Copilot port
// of the claude-api skill's evals; Python twin: build_report_lite.py - both
// write byte-identical trajectory/scores.tsv and trajectory/status.md): a
// single static report.html with no vendored runtime, fonts, or markdown engine.
//
//   node build-report-lite.mjs .hillclimb/<flow>/
//
// Reads the on-disk layout the runner writes (baseline/, v<N>/, results.jsonl,
// errors.jsonl, change.md, summary.json, traces/, _state.json) and derives:
// per-variant mean of the primary metric over status-ok rows, per-case means,
// split (train/val/test) from _state.json, failed-attempt counts from
// errors.jsonl. It writes trajectory/scores.tsv (one row per case, one column
// per variant) and trajectory/status.md - the hillclimb per-round status table:
// held-out and train means with 95% CIs, the paired per-case delta vs baseline
// (* = CI excludes zero), the other metrics, perf means with ratios vs
// baseline, and $/run + cumulative spend priced from _state.json.prices
// (USD per million tokens; cache reads default to 0.1x input, cache writes to
// 1.25x input; judge usage and failed attempts' billed usage included). A cost
// it cannot price is written 'not measured', never $0.
//
// What it deliberately does not do: render markdown, inline transcripts or
// attachments, or execute anything from the data. Every string from disk is
// HTML-escaped; traces are linked by relative path, not embedded. Sorting is
// a few lines of inline script over the rendered table only.
//
// Runs under node or bun. Node builtins only.

import { closeSync, constants as FS, fstatSync, ftruncateSync, lstatSync, mkdirSync, openSync, readdirSync, readFileSync, realpathSync, statSync, writeFileSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';

const isNum = x => typeof x === 'number' && Number.isFinite(x);
// readText/readJSON refuse symlinks, like the full builder's lib/adapter.mjs:
// the flow dir is model-influenced, and a prompt-injected agent could plant
// `summary.json -> ~/.ssh/id_rsa`; the user later runs this builder (outside
// any sandbox) and the target lands in the shareable report.html. Open
// O_NOFOLLOW and fstat the fd (not lstat-then-read) so a concurrent writer
// can't swap in a symlink between the check and the read.
// O_NOFOLLOW is only trustworthy on POSIX: Node leaves it undefined on
// Windows and Bun defines it there with a meaningless nonzero value that
// makes every open fail, so the flag is applied by platform, and
// Windows falls back to an lstat refusal - a check-then-use window, but the
// alternative is no symlink check at all.
const WIN = process.platform === 'win32';
const NOFOLLOW = WIN ? 0 : FS.O_NOFOLLOW;
// A guard that cannot tell must refuse: only "no such entry" reads as absent;
// any other lstat failure (EACCES, ENAMETOOLONG, ...) is rethrown, never "no".
const isSymlink = p => {
  try { return lstatSync(p).isSymbolicLink(); } catch (e) { if (e?.code === 'ENOENT') return false; throw e; }
};
// Files skipped for having a second hard link, so build() can say so as well
// as reporting them as missing or empty.
const skippedHardLinks = [];
const readText = p => {
  let fd;
  try {
    if (WIN && isSymlink(p)) return '';
    fd = openSync(p, FS.O_RDONLY | NOFOLLOW);
    const st = fstatSync(fd);
    // O_NOFOLLOW and lstat cannot see a hard link (a second name for a file
    // outside the flow dir); nothing in a flow dir needs one, so read nothing.
    if (!st.isFile()) return '';
    if (st.nlink > 1) { skippedHardLinks.push(p); return ''; }
    return readFileSync(fd, 'utf8');
  } catch { return ''; }
  finally { if (fd !== undefined) closeSync(fd); }
};
// Output writes get the same discipline as reads: the flow dir is
// model-influenced, so `report.html -> ~/.bashrc` planted there must not be
// followed. POSIX opens O_NOFOLLOW (a symlink fails with ELOOP); Windows
// lstat-refuses first. A symlinked parent dir is refused the same way.
// The immediate-parent lstat can't see a symlink higher up the path, so
// writes are also bound to the flow root resolved ONCE at build entry
// (resolveOutputRoot below).
// Re-resolving the caller-named path here would self-anchor: root and
// target would resolve through the same planted link and the check would
// pass vacuously.
function assertContained(dir, root) {
  if (root == null) throw new Error('refusing to write: output root not resolved');
  const dirReal = realpathSync(dir);
  if (dirReal !== root && !dirReal.startsWith(root + (WIN ? '\\' : '/')))
    throw new Error('refusing to write outside ' + root + ': ' + dir);
}
// Refuse a symlink at the flow path itself, then pin its resolution for the
// whole build. Takes the path AS THE CALLER NAMED IT: a RELATIVE root is
// lstat-walked component by component from the cwd first (a pre-planted
// ancestor link would otherwise relocate the anchor itself), and a trailing
// separator is stripped (lstat("link/") follows the final symlink).
// Residual: an ancestor link above an ABSOLUTE root stays the caller's own
// trust decision (it can be legitimate - macOS /tmp); a swap after entry
// fails assertContained when the check sees it. Residual, all platforms: the
// check and the open are separate path lookups (Node's sync fs has no
// openat-style call), so a directory swapped for a symlink in between is
// still followed. This stops a planted link, not a writer racing the build.
function resolveOutputRoot(root) {
  // Only Windows treats `\` as a separator; on POSIX it is a filename byte, so
  // splitting on it would walk prefixes that are not real path components.
  root = String(root).replace(WIN ? /(.)[\\/]+$/ : /(.)\/+$/, '$1');
  const segments = root.split(WIN ? /[\\/]/ : '/');
  // A `.`/`..` segment (e.g. a trailing `/.`) makes the leaf lstat below
  // follow a planted link at the flow root, and the absolute branch skips the
  // ancestor walk. Refuse dot segments outright.
  if (segments.some(seg => seg === '.' || seg === '..'))
    throw new Error('refusing to write into ' + root + ": '.'/'..' path segment");
  if (!isAbsolute(root)) {
    let walk = '';
    for (const part of segments.filter(Boolean).slice(0, -1)) {
      walk = walk ? join(walk, part) : part;
      if (isSymlink(walk)) throw new Error('refusing to write into ' + root + ': ancestor ' + walk + ' is a symlink');
    }
  }
  if (isSymlink(root)) throw new Error('refusing to write into ' + root + ': it is a symlink');
  return realpathSync(root);
}
function writeNoFollow(p, data, root) {
  if (isSymlink(dirname(p))) throw new Error('refusing to write through symlinked directory: ' + dirname(p));
  assertContained(dirname(p), root);
  if (WIN && isSymlink(p)) throw new Error('refusing to write through symlink: ' + p);
  // Opened without O_TRUNC and truncated only after the checks, so a refused
  // file keeps its bytes.
  const fd = openSync(p, FS.O_WRONLY | FS.O_CREAT | NOFOLLOW, 0o644);
  try {
    const st = fstatSync(fd);
    if (!st.isFile()) throw new Error('refusing to write to non-regular file: ' + p);
    if (st.nlink > 1) throw new Error('refusing to write to ' + p + ': it has a second hard link (another name for the same file); replace it with a plain copy if it is yours');
    ftruncateSync(fd, 0);
    // writeFileSync on the fd loops until every byte lands (a bare writeSync
    // is one write(2) that may return short on ENOSPC and silently truncate).
    writeFileSync(fd, data);
  } finally { closeSync(fd); }
}
function mkdirNoFollow(dir, root) {
  if (isSymlink(dir)) throw new Error('refusing to use symlinked directory: ' + dir);
  mkdirSync(dir, { recursive: true });
  // Check after creating: mkdirSync(recursive) follows symlinked ancestors.
  assertContained(dir, root);
}
const readJSON = (p, dflt) => { try { return JSON.parse(readText(p)); } catch { return dflt; } };
// Warnings/errors interpolate model-influenced bytes (directory names,
// JSON.parse messages). Strip escape sequences and control characters before
// they reach the terminal, so a planted
// OSC/CSI can't retitle the terminal or forge output lines.
const ESC_SEQ = /\x1b\[[0-?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]/g;
const CONTROL = /[\x00-\x1f\x7f-\x9f]/g;
const termSafe = s => String(s).replace(ESC_SEQ, '').replace(CONTROL, '');
const eprint = (...a) => console.error(...a.map(termSafe));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = x => (x == null ? '' : x.toFixed(3));
// TSV id cells come from untrusted results.jsonl: strip the separators that
// would forge rows/columns in trajectory/scores.tsv, and neutralize a leading
// formula trigger so spreadsheet apps don't execute '=WEBSERVICE(...)' on
// open. Mirrored in the full builder's lib/adapter.mjs so both emit
// byte-identical scores.tsv.
const tsvCell = s => {
  const flat = String(s).replace(/[\t\n\r]/g, ' ');
  return /^[=+\-@]/.test(flat) ? "'" + flat : flat;
};

// Variant directories: exactly `baseline` or `v<N>`, numeric order.
function discoverVariants(flow) {
  const names = readdirSync(flow, { withFileTypes: true })
    .filter(e => e.isDirectory() && /^(baseline|v\d+)$/.test(e.name))
    .map(e => e.name);
  const rank = n => (n === 'baseline' ? -1 : +n.slice(1));
  return names.sort((a, b) => rank(a) - rank(b));
}

// Mirrors the full adapter: an object of numeric/boolean grades, a bare
// boolean, or a bare number (-> {score}).
function coerceScores(g) {
  if (g && typeof g === 'object' && !Array.isArray(g)) {
    const out = {};
    for (const [k, v] of Object.entries(g)) if (isNum(v) || typeof v === 'boolean') out[k] = +v;
    return out;
  }
  if (typeof g === 'boolean') return { score: g ? 1 : 0 };
  if (isNum(g)) return { score: g };
  return {};
}

function loadRows(p, warnings, dir) {
  // Keyed by data-supplied ids: null-prototype so '__proto__' is a key, not a crash.
  const byId = Object.create(null);
  readText(p).split('\n').forEach((line, i) => {
    line = line.trim();
    if (!line) return;
    let r;
    try { r = JSON.parse(line); } catch { warnings.push(dir + '/results.jsonl:' + (i + 1) + ': malformed JSON, skipped'); return; }
    if (!r || typeof r !== 'object' || Array.isArray(r)) { warnings.push(dir + '/results.jsonl:' + (i + 1) + ': not a JSON object, skipped'); return; }
    const pid = String(r.prompt_id ?? r.id ?? r.case_id ?? '');
    if (!pid) { warnings.push(dir + '/results.jsonl:' + (i + 1) + ': no prompt_id, skipped'); return; }
    (byId[pid] ||= []).push(r);
  });
  return byId;
}

function countErrors(vdir) {
  let total = 0;
  const byClass = Object.create(null);
  const rows = [];
  for (const line of readText(join(vdir, 'errors.jsonl')).split('\n')) {
    if (!line.trim()) continue;
    let e; try { e = JSON.parse(line); } catch { continue; }
    total++;
    const c = String(e?.failure_class ?? 'error');
    byClass[c] = (byClass[c] || 0) + 1;
    if (e && typeof e === 'object' && !Array.isArray(e)) rows.push(e);
  }
  return { total, byClass, rows };
}

// Inferred metrics, mirroring the full builder's inferMetrics: any
// explanation for a key -> judge, all-0/1 values -> binary, else float;
// first-seen order.
function inferMetrics(rows, variants) {
  const seen = new Map();
  for (const v of variants) for (const reps of Object.values(rows[v])) for (const r of reps) {
    const expl = r.explanation && typeof r.explanation === 'object' ? r.explanation : {};
    for (const [k, val] of Object.entries(coerceScores(r.grade))) {
      const m = seen.get(k) || { binary: true, judge: false };
      if (val !== 0 && val !== 1) m.binary = false;
      if (k in expl) m.judge = true;
      seen.set(k, m);
    }
  }
  return [...seen.entries()].map(([id, s]) => ({ id, kind: s.judge ? 'judge' : s.binary ? 'binary' : 'float' }));
}

function build(flowArg) {
  const flow = resolve(flowArg);
  if (!statSync(flow, { throwIfNoEntry: false })?.isDirectory()) throw new Error('not a directory: ' + flow);
  // Pinned from the path as the caller named it, so the relative-ancestor
  // walk in resolveOutputRoot applies (resolve() would skip it).
  const flowRoot = resolveOutputRoot(flowArg);
  const variants = discoverVariants(flow);
  if (!variants.length) throw new Error('no variant directories (baseline/, v1/, ...) under ' + flow);
  const warnings = [];
  // Same warning the full builder gives: a mis-named variant dir is the usual
  // reason the header shows one variant fewer than expected.
  const nonVariant = new Set(['trajectory', 'attachments', 'refs', 'ref', 'inputs', 'out', 'target', '__pycache__']);
  for (const e of readdirSync(flow, { withFileTypes: true }))
    if (e.isDirectory() && !variants.includes(e.name) && !nonVariant.has(e.name)
        && !e.name.startsWith('.') && !e.name.startsWith('_'))
      warnings.push("ignored directory '" + e.name + "/' - variant dirs must be named 'baseline' or 'v<N>'; put the descriptive name in change.md's first line instead");
  const state = readJSON(join(flow, '_state.json'), {}) || {};

  const splitOf = Object.create(null);
  for (const sp of ['train', 'val', 'test']) {
    const ids = state[sp + '_ids'];
    if (ids == null) continue;
    if (!Array.isArray(ids)) { warnings.push('_state.json ' + sp + '_ids is not a list - ignored'); continue; }
    for (const pid of ids) splitOf[String(pid)] = sp;
  }

  const rows = Object.create(null);
  for (const v of variants) rows[v] = loadRows(join(flow, v, 'results.jsonl'), warnings, v);

  // Trace links are emitted only for a real traces/ directory that resolves
  // inside the pinned flow root: the per-file lstat below refuses a symlinked
  // trace but silently resolves a symlinked traces/ (or variant) directory,
  // which would link files from wherever it points into the report.
  const traceDirOk = Object.create(null);
  for (const v of variants) {
    const tdir = join(flow, v, 'traces');
    try {
      if (isSymlink(join(flow, v)) || isSymlink(tdir)) {
        warnings.push(v + '/traces is reached through a symlink - trace links omitted');
        traceDirOk[v] = false;
      } else {
        const real = realpathSync(tdir);
        traceDirOk[v] = real.startsWith(flowRoot + (WIN ? '\\' : '/'));
      }
    } catch { traceDirOk[v] = false; }
  }

  // Same as the full builder: drop non-baseline variants with zero result
  // rows (a vN/ holding only change.md while the run warms up would render a
  // blank column), and refuse to build when the first variant itself has none
  // rather than overwrite a good trajectory/scores.tsv with a header-only file.
  for (let i = variants.length - 1; i > 0; i--) {
    const v = variants[i];
    if (Object.keys(rows[v]).length) continue;
    warnings.push(v + ': zero result rows - dropping (run not started or results.jsonl missing/empty)');
    variants.splice(i, 1);
  }
  if (!Object.keys(rows[variants[0]]).length)
    throw new Error(variants[0] + '/ has no result rows under ' + flow + ' - nothing to report'
      + (skippedHardLinks.length ? ' (not read, second hard link: ' + skippedHardLinks.map(p => p.slice(flow.length + 1)).join(', ') + ' - replace with plain copies if they are yours)' : ''));

  const header = Object.create(null);
  for (const v of variants) {
    const vdir = join(flow, v);
    const change = readText(join(vdir, 'change.md')).split('\n').find(l => l.trim()) || '';
    const summary = readJSON(join(vdir, 'summary.json'), {}) || {};
    // Model: summary.json wins, else the most common row.model (what the app called).
    const tally = Object.create(null);
    for (const reps of Object.values(rows[v])) for (const r of reps) if (typeof r.model === 'string') tally[r.model] = (tally[r.model] || 0) + 1;
    const rowModel = Object.entries(tally).sort((a, b) => b[1] - a[1])[0]?.[0] || '';
    // summary.json is optional and written by the orchestrator: its description
    // stands in for a missing change.md, and its suspicious note is surfaced.
    const susp = typeof summary.suspicious === 'string' ? summary.suspicious
      : summary.suspicious && typeof summary.suspicious.note === 'string' ? summary.suspicious.note : '';
    header[v] = { change: change.replace(/^#+\s*/, '') || (typeof summary.description === 'string' ? summary.description : ''),
      model: summary.model || rowModel, errors: countErrors(vdir), suspicious: susp };
  }

  // Metrics: _state.json `metrics` (or the legacy `criteria` key) wins,
  // keeping each declared `kind`; else inferred from grade keys. Primary is
  // the first declared-or-inferred binary metric, else the first - the same
  // selection the full builder's lib/adapter.mjs makes, so both builders
  // report the same metric's numbers on the same flow.
  const metricsCfg = state.metrics || state.criteria;
  const declared = (Array.isArray(metricsCfg) ? metricsCfg : [])
    .map(m => (typeof m === 'string' ? { id: m } : { id: m?.id, kind: m?.kind, label: m?.label }))
    .filter(m => m.id);
  const metrics = declared.length ? declared : inferMetrics(rows, variants);
  if (!metrics.length) metrics.push({ id: 'score', kind: 'float' });
  const primary = (metrics.find(m => m.kind === 'binary') || metrics[0]).id;

  // Cases: union of ids across variants, in first-seen order.
  const ids = [];
  const seenId = new Set();
  for (const v of variants) for (const pid of Object.keys(rows[v])) if (!seenId.has(pid)) { seenId.add(pid); ids.push(pid); }

  const okReps = reps => reps.filter(r => r.status == null || r.status === 'ok');
  const caseMean = (reps, m) => {
    const vals = okReps(reps).map(r => coerceScores(r.grade)[m]).filter(isNum);
    return vals.length ? vals.reduce((s, x) => s + x, 0) / vals.length : null;
  };
  const cases = ids.map(pid => {
    const first = variants.map(v => rows[v][pid]?.[0]).find(Boolean) || {};
    const tags = Array.isArray(first.tags) ? first.tags.map(String) : [];
    const per = {};
    for (const v of variants) {
      const reps = rows[v][pid] || [];
      per[v] = {
        mean: caseMean(reps, primary),
        reps: reps.length,
        truncated: reps.filter(r => r.status === 'truncated').length,
        traces: reps.map((r, k) => {
          // Link only to files directly inside this variant's traces/ dir:
          // ids and reps come from results.jsonl (data, not trusted), so the
          // composed path must not resolve anywhere else, and a symlinked
          // trace must not become a link out of the tree. Keep the rep
          // number so the link label matches the file it points at even when
          // earlier reps have no trace. For rep 0 also accept the flat
          // traces/<id>.json a single-rep runner may write (the full builder
          // reads it as rep 0 too).
          // The href is resolved by a browser with URL semantics, not the
          // filesystem's: allowlist the id to the scaffold's path-safe
          // charset (so `#`, `?`, `\\`, `%` and separators never reach the
          // link) and require an integer rep, on top of the traceDirOk
          // resolved-path check above and the lexical one below.
          if (!traceDirOk[v]) return null;
          const rep = r.rep ?? k;
          if (!/^[A-Za-z0-9_.-]+$/.test(pid) || /^\.+$/.test(pid)) return null;
          if (!Number.isInteger(+rep) || +rep < 0) return null;
          const stems = +rep === 0 ? [pid + '_rep' + rep, pid] : [pid + '_rep' + rep];
          for (const stem of stems) {
            const rel = v + '/traces/' + stem + '.json';
            const abs = resolve(flow, rel);
            if (dirname(abs) !== join(flow, v, 'traces')) return null;
            // A hard-linked trace is no more ours than a symlinked one.
            const ls = lstatSync(abs, { throwIfNoEntry: false });
            if (ls?.isFile() && !(ls.nlink > 1)) return { rep, rel };
          }
          return null;
        }).filter(Boolean),
      };
    }
    return { id: pid, split: splitOf[pid] || '', tags, prompt: String(first.prompt ?? ''), per };
  });

  // Per-variant aggregate: mean over cases that have a value, per split.
  const agg = {};
  for (const v of variants) {
    const all = cases.map(c => c.per[v].mean).filter(isNum);
    const test = cases.filter(c => c.split === 'test').map(c => c.per[v].mean).filter(isNum);
    const m = xs => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);
    agg[v] = { all: m(all), n: all.length, test: m(test), nTest: test.length,
      truncated: cases.reduce((s, c) => s + c.per[v].truncated, 0) };
  }

  // trajectory/scores.tsv - same shape as the full builder.
  mkdirNoFollow(join(flow, 'trajectory'), flowRoot);
  const tsv = [['id', 'split', ...variants].join('\t')];
  for (const c of cases) tsv.push([tsvCell(c.id), tsvCell(c.split || 'all'), ...variants.map(v => fmt(c.per[v].mean))].join('\t'));
  writeNoFollow(join(flow, 'trajectory', 'scores.tsv'), tsv.join('\n') + '\n', flowRoot);

  // trajectory/status.md - the hillclimb per-round status table, computed from
  // the files so no number in it is ever typed in by hand.
  const status = buildStatus({ variants, rows, cases, metrics, primary, state, header, warnings });
  writeNoFollow(join(flow, 'trajectory', 'status.md'), status.md, flowRoot);

  // _state.json's `best` is {round, test_score}; map the round to a variant
  // name the way the full builder does (0 = baseline), and drop it when the
  // named variant isn't on disk.
  let best = null;
  if (Number.isInteger(state.best?.round)) {
    best = state.best.round === 0 ? 'baseline' : 'v' + state.best.round;
    if (!variants.includes(best)) best = null;
  }

  for (const p of new Set(skippedHardLinks))
    warnings.push(p.slice(flow.length + 1) + ' has a second hard link - not read; replace it with a plain copy if it is yours');
  const html = render({ flow, variants, header, agg, cases, primary, metrics: metrics.map(m => m.id), warnings, best, status });
  writeNoFollow(join(flow, 'report.html'), html, flowRoot);
  return { variants: variants.length, cases: cases.length, warnings };
}

// ---- status table ----------------------------------------------------------
// Everything below is mirrored line for line in build_report_lite.py; the two
// must write byte-identical status.md, so keep iteration order and float
// operation order the same when editing either.

const meanOf = xs => { if (!xs.length) return null; let s = 0; for (const x of xs) s += x; return s / xs.length; };
// Half-width of a Wald 95% CI over xs (sample sd); null when n < 2.
const ci95 = xs => {
  if (xs.length < 2) return null;
  const m = meanOf(xs);
  let ss = 0;
  for (const x of xs) ss += (x - m) * (x - m);
  return 1.96 * Math.sqrt(ss / (xs.length - 1)) / Math.sqrt(xs.length);
};
const signed = x => (x >= 0 ? '+' : '') + x.toFixed(3);
const fmtUsd = x => '$' + (x >= 1 ? x.toFixed(2) : x >= 0.01 ? x.toFixed(3) : x.toFixed(4));
const oneLine = s => String(s).replace(/[\r\n\t]+/g, ' ').trim();
const mdCell = s => oneLine(s).replace(/\|/g, '\\|');
const clip = (s, n) => { const cps = Array.from(s); return cps.length > n ? cps.slice(0, n).join('') + '...' : s; };

// _state.json.prices: { model_id: { in, out, cache_read?, cache_write? } } in
// USD per million tokens. Exact id first, else a key the id extends by a date
// snapshot only ('-20260101', '@20260101', '-2026-01-01') - so 'gpt-x' never
// prices 'gpt-x-mini'.
function priceFor(prices, model) {
  if (!prices || typeof prices !== 'object' || typeof model !== 'string' || !model) return null;
  let key = Object.prototype.hasOwnProperty.call(prices, model) ? model : null;
  if (key == null)
    for (const k of Object.keys(prices))
      if (k && model.startsWith(k) && /^[-@](\d{8}|\d{4}-\d{2}-\d{2})$/.test(model.slice(k.length))) key = k;
  const p = key == null ? null : prices[key];
  return p && typeof p === 'object' && isNum(p.in) && isNum(p.out) ? p : null;
}
// -> { usd } | { unpriced: model } | { noUsage: true }
function costOf(model, usage, prices) {
  if (!usage || typeof usage !== 'object' || Array.isArray(usage)) return { noUsage: true };
  const p = priceFor(prices, model);
  if (!p) return { unpriced: String(model ?? '(no model)') };
  const t = k => (isNum(usage[k]) ? usage[k] : 0);
  const cr = isNum(p.cache_read) ? p.cache_read : 0.1 * p.in;
  const cw = isNum(p.cache_write) ? p.cache_write : 1.25 * p.in;
  return { usd: (t('input_tokens') * p.in + t('output_tokens') * p.out
    + t('cache_read_input_tokens') * cr + t('cache_creation_input_tokens') * cw) / 1e6 };
}
// One results row or error row: app call plus judge call, when present.
function rowCost(r, prices, isErrorRow) {
  const parts = [];
  const hasJudge = r.judge_usage && typeof r.judge_usage === 'object';
  if (r.usage != null || !isErrorRow) parts.push(costOf(r.model, r.usage, prices));
  if (hasJudge) parts.push(costOf(r.judge_model, r.judge_usage, prices));
  let usd = 0;
  for (const c of parts) {
    if (c.noUsage) return { noUsage: true };
    if (c.unpriced != null) return { unpriced: c.unpriced };
    usd += c.usd;
  }
  return { usd };
}

const PERF_LABELS = { latency_s: 'latency', out_tokens: 'out toks', in_tokens: 'in toks', tool_calls: 'tool calls', web_searches: 'web searches', cost_usd: '$/case' };
function perfValue(r, id, prices) {
  const u = r.usage && typeof r.usage === 'object' ? r.usage : null;
  if (id === 'out_tokens') return u && isNum(u.output_tokens) ? u.output_tokens : null;
  if (id === 'in_tokens') {
    if (!u) return null;
    let t = 0, any = false;
    for (const k of ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens']) if (isNum(u[k])) { t += u[k]; any = true; }
    return any ? t : null;
  }
  if (id === 'cost_usd') { const c = rowCost(r, prices, false); return c.usd ?? null; }
  return isNum(r[id]) ? r[id] : null;
}
function fmtPerf(id, unit, x) {
  if (id === 'cost_usd' || unit === '$') return fmtUsd(x);
  const body = /tokens$/.test(id) ? x.toFixed(0) : (id === 'latency_s' || id === 'tool_calls' || id === 'web_searches') ? x.toFixed(1) : x.toFixed(2);
  return body + (id === 'latency_s' && unit == null ? 's' : unit && unit !== '$' ? unit : '');
}

function buildStatus({ variants, rows, cases, metrics, primary, state, header, warnings }) {
  const prices = state.prices && typeof state.prices === 'object' && !Array.isArray(state.prices) ? state.prices : null;
  // Copilot-native runner: no token usage reaches the flow, so the cost columns
  // count subagent calls instead of dollars.
  const copilot = state.runner === 'copilot';
  const callsOf = (r, isErrorRow) => (isNum(r.calls) ? r.calls : 1 + (!isErrorRow && r.judge_model ? 1 : 0));
  const runCalls = v => {
    let n = 0;
    for (const pid of Object.keys(rows[v])) for (const r of rows[v][pid]) n += callsOf(r, false);
    for (const e of header[v].errors.rows) n += callsOf(e, true);
    return n;
  };
  const okReps = reps => reps.filter(r => r.status == null || r.status === 'ok');
  const caseMean = (reps, m) => meanOf(okReps(reps).map(r => coerceScores(r.grade)[m]).filter(isNum));
  const labelOf = m => (typeof m.label === 'string' && m.label ? m.label : m.id);
  const has = sp => cases.some(c => c.split === sp);
  const splits = ['test', 'val', 'train'].filter(has);
  const head = splits.includes('test') ? 'test' : null; // null = all cases (no held-out split)
  const inHead = c => (head == null ? true : c.split === head);

  // Per-case means per metric per variant, for the cases of a split.
  const caseMeans = (v, m, pred) => {
    const out = [];
    for (const c of cases) if (pred(c)) {
      const x = m === primary ? c.per[v].mean : caseMean(rows[v][c.id] || [], m);
      if (isNum(x)) out.push({ id: c.id, x });
    }
    return out;
  };
  const paired = (v, m) => {
    const base = new Map(caseMeans(variants[0], m, inHead).map(o => [o.id, o.x]));
    const d = [];
    for (const o of caseMeans(v, m, inHead)) if (base.has(o.id)) d.push(o.x - base.get(o.id));
    if (!d.length) return null;
    const mu = meanOf(d), h = ci95(d);
    return { mu, h, n: d.length, sig: h != null && (mu - h > 0 || mu + h < 0) };
  };

  // Perf columns: _state.json.perf_fields when declared, else the defaults the
  // runner records; a column nobody recorded is left out.
  const perfDecl = Array.isArray(state.perf_fields) ? state.perf_fields
    .map(f => (typeof f === 'string' ? { id: f } : f && typeof f === 'object' && typeof f.id === 'string' ? { id: f.id, label: f.label, unit: f.unit } : null))
    .filter(f => f && f.id) : null;
  const perf = (perfDecl || [{ id: 'latency_s' }, { id: 'out_tokens' }, { id: 'tool_calls' }]).filter(f =>
    variants.some(v => Object.keys(rows[v]).some(pid => okReps(rows[v][pid]).some(r => perfValue(r, f.id, prices) != null))));
  const perfMean = (v, id) => {
    const xs = [];
    for (const pid of Object.keys(rows[v])) for (const r of okReps(rows[v][pid])) { const x = perfValue(r, id, prices); if (x != null) xs.push(x); }
    return meanOf(xs);
  };

  // $/run = every billed call in the variant's pass: all results rows (any
  // status) plus failed attempts that carry usage. Unpriced or usage-less rows
  // make it 'not measured' rather than a partial number.
  const unpriced = new Set();
  const runCost = v => {
    let usd = 0, noUsage = 0, ok = true;
    for (const pid of Object.keys(rows[v])) for (const r of rows[v][pid]) {
      const c = rowCost(r, prices, false);
      if (c.noUsage) { noUsage++; ok = false; } else if (c.unpriced != null) { unpriced.add(c.unpriced); ok = false; } else usd += c.usd;
    }
    for (const e of header[v].errors.rows) {
      if (!(e.usage && typeof e.usage === 'object') && !(e.judge_usage && typeof e.judge_usage === 'object')) continue;
      const c = rowCost(e, prices, true);
      if (c.unpriced != null) { unpriced.add(c.unpriced); ok = false; } else if (!c.noUsage) usd += c.usd;
    }
    if (noUsage) warnings.push(v + ': ' + noUsage + ' result row(s) carry no usage - $/run not measured (the runner must record usage on every row)');
    return ok ? usd : null;
  };

  const cols = ['variant', 'change'];
  if (head == null) cols.push('all ' + primary, 'Δ vs baseline');
  else { cols.push('test ' + primary, 'Δ test vs baseline'); for (const sp of splits) if (sp !== 'test') cols.push(sp + ' ' + primary); }
  const others = metrics.filter(m => m.id !== primary);
  for (const m of others) cols.push(labelOf(m) + (head == null ? '' : ' (test)'));
  for (const f of perf) cols.push(typeof f.label === 'string' && f.label ? f.label
    : Object.prototype.hasOwnProperty.call(PERF_LABELS, f.id) ? PERF_LABELS[f.id] : f.id);
  cols.push(copilot ? 'calls/run' : '$/run', copilot ? 'calls total' : 'spend', 'not scored');

  const baseVals = {};
  let spend = 0, spendOk = true;
  const body = variants.map((v, vi) => {
    const cells = [v, vi === 0 && !header[v].change ? '(baseline)' : clip(oneLine(header[v].change), 80)];
    const splitCell = pred => {
      const xs = caseMeans(v, primary, pred).map(o => o.x);
      const mu = meanOf(xs);
      if (mu == null) return '-';
      const h = ci95(xs);
      return mu.toFixed(3) + (h != null ? ' ±' + h.toFixed(3) : '') + ' (n=' + xs.length + ')';
    };
    const deltaCell = () => {
      if (vi === 0) return '-';
      const p = paired(v, primary);
      if (!p) return '-';
      return p.h == null ? signed(p.mu) + ' (n=' + p.n + ')'
        : signed(p.mu) + ' [' + signed(p.mu - p.h) + ', ' + signed(p.mu + p.h) + ']' + (p.sig ? ' *' : '');
    };
    cells.push(splitCell(inHead), deltaCell());
    if (head != null) for (const sp of splits) if (sp !== 'test') cells.push(splitCell(c => c.split === sp));
    for (const m of others) {
      const mu = meanOf(caseMeans(v, m.id, inHead).map(o => o.x));
      if (mu == null) { cells.push('-'); continue; }
      const p = vi === 0 ? null : paired(v, m.id);
      cells.push(mu.toFixed(3) + (p ? ' (' + signed(p.mu) + (p.sig ? '*' : '') + ')' : ''));
    }
    for (const f of perf) {
      const x = perfMean(v, f.id);
      if (vi === 0) baseVals[f.id] = x;
      if (x == null) { cells.push('-'); continue; }
      const b = baseVals[f.id];
      cells.push(fmtPerf(f.id, f.unit, x) + (vi > 0 && isNum(b) && b > 0 ? ' (' + (x / b).toFixed(1) + '×)' : ''));
    }
    if (copilot) {
      const c = runCalls(v);
      spend += c;
      cells.push(c.toFixed(0), spend.toFixed(0));
    } else {
      const cost = runCost(v);
      if (cost == null) spendOk = false; else spend += cost;
      cells.push(cost == null ? 'not measured' : fmtUsd(cost), spendOk ? fmtUsd(spend) : 'not measured');
    }
    const e = header[v].errors.total, t = cases.reduce((s, c) => s + c.per[v].truncated, 0);
    cells.push(e || t ? [e ? e + ' failed' : '', t ? t + ' truncated' : ''].filter(Boolean).join(', ') : '0');
    return cells;
  });
  if (copilot) { /* calls, not dollars - no price warnings */ }
  else if (!prices) warnings.push('no _state.json.prices - $/run and spend not measured; record the rate card (USD per million tokens) as prices: {model_id: {in, out}}');
  else if (unpriced.size) warnings.push('no price in _state.json.prices for: ' + [...unpriced].sort().join(', ') + ' - $/run and spend not measured');

  const md = ['| ' + cols.map(mdCell).join(' | ') + ' |', '|' + cols.map(() => '---').join('|') + '|']
    .concat(body.map(r => '| ' + r.map(mdCell).join(' | ') + ' |'));
  const notes = [];
  notes.push(head == null
    ? 'No held-out split in _state.json: every number is over all cases - directional, not a held-out result.'
    : 'Headline: test split. Δ = paired per-case delta vs baseline with 95% CI; * = CI excludes 0 (outside noise). Other metrics: test mean (paired Δ, * outside noise).');
  notes.push('Means are per case over status-ok reps, then over cases; failed and truncated attempts are counted in "not scored", never in the means.');
  notes.push(copilot
    ? 'Runner: Copilot subagents - token usage and dollars are not observable; calls/run counts the SUT and judge subagent calls recorded in the pass, failed attempts included.'
    : '$/run prices every billed call in the pass (app + judge + failed attempts with usage) from _state.json.prices; cache reads at 0.1x input and cache writes at 1.25x input unless the price entry sets cache_read / cache_write.');
  for (const v of variants) if (header[v].suspicious) notes.push('Suspicious - ' + v + ': ' + oneLine(header[v].suspicious));
  if (state.goal && typeof state.goal === 'object') {
    const g = state.goal;
    notes.push('Goal: ' + oneLine(g.target ?? '?') + ' (' + oneLine(g.direction ?? 'higher') + ' is better)'
      + (Array.isArray(g.hold) && g.hold.length ? '; hold: ' + g.hold.map(oneLine).join(', ') : ''));
  }
  if (Number.isInteger(state.best?.round)) {
    const bv = state.best.round === 0 ? 'baseline' : 'v' + state.best.round;
    notes.push('Best so far (_state.json): ' + bv + (isNum(state.best.test_score) ? ' (test ' + state.best.test_score.toFixed(3) + ')' : ''));
  }
  return { md: md.join('\n') + '\n\n' + notes.map(n => '- ' + n).join('\n') + '\n', cols, body, notes };
}

function render({ flow, variants, header, agg, cases, primary, metrics, warnings, best, status }) {
  const hasSplit = cases.some(c => c.split);
  const hasTest = variants.some(v => agg[v].nTest > 0);
  const th = (label, key) => '<th data-k="' + esc(key) + '">' + esc(label) + '</th>';

  const variantRows = variants.map(v => {
    const h = header[v], a = agg[v];
    const err = h.errors.total
      ? h.errors.total + ' (' + Object.entries(h.errors.byClass).map(([k, n]) => esc(k) + ' ' + n).join(', ') + ')'
      : '0';
    return '<tr' + (best === v ? ' class="best"' : '') + '><td>' + esc(v) + (best === v ? ' <span class="pill">best</span>' : '') + '</td>'
      + '<td>' + esc(h.change) + '</td><td>' + esc(h.model) + '</td>'
      + '<td class="num">' + fmt(a.all) + '</td><td class="num">' + a.n + '</td>'
      + (hasTest ? '<td class="num">' + fmt(a.test) + '</td><td class="num">' + a.nTest + '</td>' : '')
      + '<td class="num">' + a.truncated + '</td><td>' + err + '</td></tr>';
  }).join('\n');

  const caseRows = cases.map(c => {
    const cells = variants.map(v => {
      const p = c.per[v];
      const links = p.traces.map(t => '<a href="' + esc(t.rel) + '">rep' + esc(t.rep) + '</a>').join(' ');
      return '<td class="num" data-v="' + fmt(p.mean) + '">' + fmt(p.mean)
        + (p.truncated ? ' <span class="pill warn">' + p.truncated + ' truncated</span>' : '')
        + (links ? '<div class="links">' + links + '</div>' : '') + '</td>';
    }).join('');
    const prompt = c.prompt.length > 600 ? c.prompt.slice(0, 600) + '...' : c.prompt;
    return '<tr><td class="id">' + esc(c.id) + '</td>'
      + (hasSplit ? '<td>' + esc(c.split) + '</td>' : '')
      + '<td>' + c.tags.map(t => '<span class="pill">' + esc(t) + '</span>').join(' ') + '</td>'
      + cells
      + '<td class="prompt"><details><summary>' + esc(prompt.split('\n')[0].slice(0, 80)) + '</summary><pre>' + esc(prompt) + '</pre></details></td></tr>';
  }).join('\n');

  const warn = warnings.length
    ? '<section class="warnings"><h2>Warnings</h2><ul>' + warnings.map(w => '<li>' + esc(w) + '</li>').join('') + '</ul></section>'
    : '';

  return '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<title>' + esc(flow.split(/[\\/]/).pop()) + ' - eval report</title>'
    + '<style>' + CSS + '</style></head><body>'
    + '<header><h1>' + esc(flow.split(/[\\/]/).pop()) + '</h1>'
    + '<p class="meta">' + variants.length + ' variant' + (variants.length === 1 ? '' : 's') + ' &middot; ' + cases.length + ' cases'
    + ' &middot; primary metric <code>' + esc(primary) + '</code>'
    + (metrics.length > 1 ? ' (also: ' + metrics.filter(m => m !== primary).map(esc).join(', ') + ')' : '')
    + ' &middot; generated ' + new Date().toISOString().slice(0, 19).replace('T', ' ') + ' UTC'
    + ' &middot; lite report (no transcripts inlined; click rep links)</p></header>'
    + '<section><h2>Status</h2><p class="hint">Same table as <code>trajectory/status.md</code>.</p><table><thead><tr>'
    + status.cols.map(c => '<th>' + esc(c) + '</th>').join('') + '</tr></thead><tbody>'
    + status.body.map(r => '<tr>' + r.map((c, i) => '<td' + (i > 1 ? ' class="num"' : '') + '>' + esc(c) + '</td>').join('') + '</tr>').join('\n')
    + '</tbody></table><ul class="hint">' + status.notes.map(n => '<li>' + esc(n) + '</li>').join('') + '</ul></section>'
    + '<section><h2>Variants</h2><table><thead><tr>' + th('variant', 's') + th('change', 's') + th('model', 's')
    + th('mean ' + primary, 'n') + th('cases', 'n')
    + (hasTest ? th('test mean', 'n') + th('test cases', 'n') : '')
    + th('truncated', 'n') + th('failed attempts', 's') + '</tr></thead><tbody>' + variantRows + '</tbody></table></section>'
    + '<section><h2>Cases</h2><p class="hint">Click a column header to sort. Per-case values are the mean of <code>' + esc(primary) + '</code> over status-ok reps.</p>'
    + '<table id="cases"><thead><tr>' + th('id', 's') + (hasSplit ? th('split', 's') : '') + th('tags', 's')
    + variants.map(v => th(v, 'n')).join('') + th('prompt', 's') + '</tr></thead><tbody>' + caseRows + '</tbody></table></section>'
    + warn
    + '<script>' + SORT_JS + '</script></body></html>\n';
}

const CSS = [
  ':root{color-scheme:light dark;--fg:#1a1a1a;--bg:#fff;--muted:#666;--line:#ddd;--pill:#eee;--warn:#b45309;--best:#ecfdf5}',
  '@media(prefers-color-scheme:dark){:root{--fg:#e6e6e6;--bg:#121212;--muted:#9a9a9a;--line:#333;--pill:#2a2a2a;--warn:#f59e0b;--best:#0f2a1f}}',
  'body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:24px;max-width:1400px}',
  'h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.meta,.hint{color:var(--muted);margin:0 0 8px}',
  'table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}',
  'th{cursor:pointer;user-select:none;white-space:nowrap}th.asc:after{content:" \\25B4"}th.desc:after{content:" \\25BE"}',
  'td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}',
  '.pill{display:inline-block;background:var(--pill);border-radius:10px;padding:0 8px;font-size:12px}.pill.warn{color:var(--warn)}',
  'tr.best td{background:var(--best)}.links{font-size:12px}.links a{margin-right:6px}',
  'td.prompt{max-width:520px}details summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;font-size:12px;margin:6px 0 0}',
  '.warnings{color:var(--warn)}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}',
].join('');

const SORT_JS = [
  'document.querySelectorAll("th[data-k]").forEach(function(th){th.addEventListener("click",function(){',
  'var t=th.closest("table"),tb=t.tBodies[0],i=Array.prototype.indexOf.call(th.parentNode.children,th);',
  'var asc=!th.classList.contains("asc");t.querySelectorAll("th").forEach(function(h){h.classList.remove("asc","desc")});',
  'th.classList.add(asc?"asc":"desc");var num=th.dataset.k==="n";',
  'var rows=Array.prototype.slice.call(tb.rows);rows.sort(function(a,b){var x=a.cells[i],y=b.cells[i];',
  'var u=num?parseFloat("v" in x.dataset?x.dataset.v:x.textContent):x.textContent.trim().toLowerCase();',
  'var w=num?parseFloat("v" in y.dataset?y.dataset.v:y.textContent):y.textContent.trim().toLowerCase();',
  'if(num){if(isNaN(u))u=-Infinity;if(isNaN(w))w=-Infinity}return (u<w?-1:u>w?1:0)*(asc?1:-1)});',
  'rows.forEach(function(r){tb.appendChild(r)})})});',
].join('');

const arg = process.argv[2];
if (!arg || arg === '-h' || arg === '--help') {
  eprint('usage: node build-report-lite.mjs <flow-dir>   (e.g. .hillclimb/<flow>/)');
  process.exit(arg ? 0 : 2);
}
try {
  const { variants, cases, warnings } = build(arg);
  for (const w of warnings) eprint('warning: ' + w);
  eprint('wrote ' + join(resolve(arg), 'report.html') + ' (' + variants + ' variants, ' + cases + ' cases), trajectory/scores.tsv and trajectory/status.md');
} catch (e) {
  eprint('build-report-lite: ' + (e?.message || e));
  process.exit(1);
}
