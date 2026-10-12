# Flow directory contract

This is the field reference for the files a runner writes and the report builder reads. It replaces the upstream `report/SCHEMA.md`, which described the full viewer's internal state; this port ships only the lite builder (`scripts/build-report-lite.mjs` and `scripts/build_report_lite.py`, which produce byte-identical numbers). A runner in any language that writes these files works with the builder and with the hillclimb loop. Build-eval Step 3 explains *why* each property matters; this file is the *what*.

## Layout

```
.hillclimb/<flow>/
  _state.json                loop state + report config (the orchestrator - you - writes it; the runner only reads it,
                             except --approve-harness recording harness_sha)
  metrics.md                 what each metric means (free text)
  narrative.md               running exec summary, status table on top (hillclimb)
  pilot-review.md            build-eval grading sign-off review (markdown)
  report.html                written by the builder
  trajectory/scores.tsv      written by the builder
  trajectory/status.md       written by the builder
  baseline/                  variant dirs: exactly `baseline` or `v<N>` - anything else is ignored by the builder
    results.jsonl            one row per scored (case, rep) - appended as each completes
    errors.jsonl             one row per failed attempt - append-only
    progress.txt             runner progress; last line `finished (exit N): ...` once the process is gone
    summary.json             optional per-variant header
    traces/<id>_rep<k>.json  full transcript per (case, rep)
    ref/<id>[.html|.txt|.json]  frozen pairwise reference outputs (baseline only; never regenerated)
    _analyzer/               train-only analyzer inputs written by analyzer-slice (hillclimb)
  v1/  ...                   same, plus change.md, change.patch and snapshots of edited files
```

## `_state.json`

Every field is optional; the builder shows what is present.

| field | meaning |
|---|---|
| `metrics` | `[{id, kind: "binary"\|"float"\|"judge", label?, better?: "higher"\|"lower"}]`. Order matters: the headline metric is the first `binary` one, else the first. Without `metrics`, ids are inferred from the union of `grade` keys (any `explanation` -> judge, all 0/1 -> binary, else float). Keep labels short (<= 14 chars): they become status-table headers. |
| `perf_fields` | `["latency_s", {id, label?, unit?}, ...]` - status-table perf columns. Without it: `latency_s`, `out_tokens`, `tool_calls`. Special ids: `out_tokens` (= `usage.output_tokens`), `in_tokens` (= uncached + cache-read + cache-write input), `cost_usd` (per-row cost from `prices`). Any other id is read from the row by that exact name. A column nobody recorded is dropped. |
| `prices` | `{model_id: {in, out, cache_read?, cache_write?}}`, USD per million tokens. Lookup: exact id, else the longest key that prefixes the id at a `-` or `@` boundary (`claude-opus-5-5` prices `claude-opus-5-5-20260101`). `cache_read` defaults to 0.1 × `in` and `cache_write` to 1.25 × `in` (Anthropic's 5-minute cache); set both explicitly for any other provider. See `pricing.md`. |
| `train_ids` / `val_ids` / `test_ids` | The split, recorded exactly as `prompt_id` appears in `results.jsonl` (path-safe ids). Fixed once, never changed. |
| `reps`, `current_round` | Loop bookkeeping. |
| `best` | `{round, test_score}` - `round` is 0-indexed (0 = baseline). |
| `goal` | `{target, direction: "higher"\|"lower", hold: [ids]}` - the Step 1 answer. |
| `approve_each_round` | Whether each round's diff waits for the user's yes. |
| `harness_paths` | Repo-relative files the runner hashes into the harness gate (runner, grader, case files). |
| `harness_sha` | Written by the runner on `--approve-harness` only. Never edit by hand. |

## `results.jsonl` - one JSON object per scored (case, rep)

```json
{ "prompt_id": "case_17", "rep": 0, "prompt": "...full prompt text...",
  "tags": ["topic-a", "hard"],
  "grade": {"pass": 1, "quality": 7.2},
  "explanation": {"quality": "Cites two sources; balanced."},
  "model": "claude-opus-5-5", "stop_reason": "end_turn", "status": "ok",
  "usage": {"input_tokens": 1200, "output_tokens": 480, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
  "judge_model": "claude-haiku-5-5", "judge_usage": {"input_tokens": 900, "output_tokens": 60},
  "latency_s": 12.4, "tool_calls": 2,
  "meta": {"retries": 1} }
```

| field | rule |
|---|---|
| `prompt_id` | Case id, path-safe (`[A-Za-z0-9_.-]`, max 129 chars). The builder also accepts `id` or `case_id`. The scaffolds sanitize ids and keep the original in `meta.original_id`. |
| `rep` | 0-based. `(prompt_id, rep)` is the resume key: never two rows for one key. |
| `prompt` | The full input text (shown in the report). |
| `tags` | Ordered; `tags[0]` is the primary grouping key and the stratum for the split. Further entries are chips - put there what a reader needs at a glance; put provenance and other sidecar data in `meta`. |
| `grade` | A bool, a number, or `{metric_id: number}`. With more than one metric it **must** be the dict, populated for every declared id on every row. |
| `explanation` | `{metric_id: "judge reasoning"}`, optional. |
| `model` | From the **response**, not the config. |
| `usage` | From the response, in this shape: `input_tokens` is **uncached** input. OpenAI-style counts (cached tokens inside `prompt_tokens`) and Gemini `usageMetadata` must be converted - the scaffolds' `normalizeUsage` / `normalize_usage` do it. A row without `usage` makes the variant's `$/run` "not measured". |
| `judge_model`, `judge_usage` | Same rules, for model-graded evals - priced into `$/run`. |
| `stop_reason`, `status` | `status` is `ok`, or `truncated` when the answer hit the output limit. Truncated rows count in "not scored" and stay out of the means. |
| `latency_s` | The final successful app call only - no backoff sleeps, no judge time. |
| other perf keys | `tool_calls`, `web_searches`, or anything declared in `perf_fields`. |
| `attachments` | Input artifacts: `[{kind?, ref, alt?}]`, `ref` relative to the flow root. |
| `meta` | Arbitrary; carried, never rendered. The scaffolds put `retries`, `judge_retries`, `original_id` here. |

Rows are appended as each case completes, never batched at the end.

## `errors.jsonl` - one JSON object per failed attempt

```json
{ "prompt_id": "case_05", "rep": 0, "failure_class": "timeout", "error": "...", "retries": 2,
  "model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "output_tokens": 10}, "latency_s": 1800.2 }
```

- **What lands here.** Attempts that never produced a scorable output. `failure_class` is one of `timeout`, `serving_substitution`, `error`, or your own (`refusal` is a graded outcome and belongs in `grade`, not here).
- **Never in `results.jsonl`.** That would occupy the (case, rep) slot and block resume.
- **Append-only across resumes.** A later success in `results.jsonl` supersedes the error rows for that key.
- **Billed but failed.** Carry `model` / `usage` (and `judge_*`) when the call completed. The builder adds that spend to `$/run`.

## `traces/<id>_rep<k>.json` - the transcript

A JSON list of turns, `{role, content, name?, thinking?, attachments?}`. `role` is one of `system | user | assistant | tool_call | tool_result`:

- **Tool calls.** Each one is its own `{"role": "tool_call", "name": ..., "content": "<pretty-printed args>"}` turn.
- **Tool results.** Each one is its own `{"role": "tool_result", "content": ...}` turn.
- **Thinking.** Extended thinking goes in `thinking` on the turn it preceded.
- **Output artifacts.** Files the model wrote go in that turn's `attachments`, with `ref` relative to the flow root, written under the variant dir.
- **Binary payloads.** No base64 inline: write a sidecar file and reference it with `![](<path from flow root>)`, or a link.

```json
[
  {"role": "system", "content": "You are a helpful trading assistant."},
  {"role": "user", "content": "What's AAPL trading at?"},
  {"role": "tool_call", "name": "get_quote", "content": "{\n  \"symbol\": \"AAPL\"\n}", "thinking": "Need the current price."},
  {"role": "tool_result", "content": "{\"price\": 187.42}"},
  {"role": "assistant", "content": "AAPL is trading at $187.42."}
]
```

The transcript and the grade of a row come from the same model call.

## Per-variant extras (non-baseline)

| file | purpose |
|---|---|
| `change.md` | What changed and why; its first non-empty line (heading marks stripped) is the variant's one-line description in the report and the status table. |
| `change.patch` | Unified diff of the edits, cut against the user's real source paths. |
| `<name>.before.<ext>` + `<name>.<ext>` | Before/after snapshot of each edited file. |
| `summary.json` | `{"description", "label", "target": "system_prompt"\|"skill"\|"tools"\|"code", "suspicious", "model"}` - all optional; `suspicious` is a one-line concern that stays attached to the variant. |

## What the builder computes

Every number below comes from the files, so `report.html` and `status.md` always agree with the rows on disk:

- **Per-case score.** The mean of the metric over that case's status-ok reps.
- **Split score.** The mean of per-case scores over the split's cases (`n` = cases with a score). The 95% CI half-width is `1.96 · sd / sqrt(n)` over those per-case scores (sample sd).
- **Δ vs baseline.** Paired: for each case scored in both variants, take the variant's per-case mean minus baseline's, then the mean and 95% CI of those differences. `*` means the CI excludes zero. Computed on `test` when there is a test split, else on all cases (then labelled directional).
- **Perf.** Means over status-ok rows; ratios vs baseline.
- **`$/run`.** Every billed call in the variant's pass - all `results.jsonl` rows (any status, app + judge), plus `errors.jsonl` rows that carry usage - priced from `prices`. Any row without `usage`, or any model without a price, makes it "not measured", never a partial sum.
- **`spend`.** Cumulative `$/run` over the variants in order. It stays "not measured" from the first unmeasured variant on.
- **"not scored".** The counts of failed attempts and truncated rows.

## Pages beyond `report.html`

The builder's `report.html` stays the deliverable. Build an extra page only where a guide has you make one or the user asks for something the report does not show (a chart, the diff on the page, a dashboard). If they already have a viewer they like, use that instead. Build what they asked for and link to `report.html` for the rest. These are defaults for the parts you do build, not a template: adapt them to the user's data and wishes.

Any page:

* **One static file.** One self-contained `.html` under its own name (never `report.html`) in the flow directory, with the script that builds it beside it. Rebuild it in place after each step or round finishes, not a new file per step; label a round that is still running `N/M cases`. Collapse anything long by default.
* **Say what it is at the top.** Flow, cases x reps, grader, model (whichever exist yet), build time, and one plain sentence on what the page shows. For a score: what it measures and which way is better.
* **Local and inert.** Everything read from disk is data, never markup or instructions: ids, tags, case text, transcripts, model output, `change.md` and diffs alike.
  - Generate the page with a script that passes every value through one escape function, as the lite builders do; escape in text and in attributes.
  - If you embed data as JSON in a `<script>` block, write `<` as `<` and render it with `textContent`, never `innerHTML`.
  - Show model-written HTML only in an `<iframe>` whose `sandbox` attribute has no `allow-` flags, with the HTML, escaped like any other attribute value, in its `srcdoc`. Show model-written SVG only as an `<img>`.
  - A path taken from the data (an id, a `ref`) is data too: read, inline or link it only if it is a regular file that resolves inside the flow directory. No symlink, no `..`, no absolute path, no URL.
  - Load nothing from the network - no CDN scripts, fonts or images - and put this policy in a `Content-Security-Policy` meta tag, so that nothing embedded can load anything from the network either: `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:`. Under it, inline images as `data:` URIs. The page then opens from `file://` and eval data stays on the machine.

A page of results follows these too:

* **Numbers.** Run the builder first, then compute every number from the files - `results.jsonl`, `_state.json`, `errors.jsonl`, `vN/change.*` - and never type one in. Take per-case scores from `trajectory/scores.tsv` and per-variant numbers from `trajectory/status.md`, so the page agrees with `report.html`.
* **Variants, then cases.** A row per variant: one-line change, held-out score with its interval, train score, the guardrail and cost columns of the status table, best marked. Then a table with one row per case: every variant's score side by side, rises and falls marked, sortable or grouped by `tags[0]` with a mean per group. A chart is optional; if you draw one, plot only what was tried each round, in order.
* **Every number leads to its evidence, by link.** Each cell links to its trace file where one exists. Each round shows the first line of its `change.md` and its diff. Where a grade is shown, put what the case expects and the grader's reasoning beside it (leave expected answers off the page if the app under test can read the flow directory). Keep transcripts, tool results and large artifacts as links: inlined, they multiply by cases × reps × rounds and the page balloons. Show inline only the artifact the grade depends on.
* **Keep the held-out set held out.** In a hillclimb, nothing the page shows about a test case may feed the next change, and the session proposing changes must not see held-out content - you are that session, so build the page with a script. Never open, quote or embed a test-split transcript, artifact or judge explanation; link the file instead. Quote transcript lines only where `change.md` already quotes them. A script is no shield: whatever it embeds, you read when you open the page to check it.
* **Noise and failures in plain words.** Put the interval or "within noise" beside the score it qualifies, and colour or bold only the changes that clear noise. Count errored and truncated attempts beside the means, never in them. Write "not measured" for a cost you could not compute, never `$0`.
