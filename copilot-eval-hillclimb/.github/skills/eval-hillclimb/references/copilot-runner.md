# The Copilot runner: evals where every model call goes through Copilot

Use this runner when the only sanctioned way to reach a model is the Copilot subscription inside VS Code. That rules out a script calling an LLM API - the typical company computer. Here the system under test (SUT) and the judge both run as Copilot **subagents**. `scripts/copilot_eval.py` does everything that is not a model call: planning, task sheets, programmatic grading, and writing rows. It never calls a model and never touches the network.

The files it writes follow the same contract as the script runner (`data-contract.md`). So the report builders, `analyzer-slice`, the status table, and every step of `build-eval.md` and `eval-hillclimb.md` work unchanged. Wherever those guides say "the runner", read "this protocol".

**Decide the runner in build-eval Step 0** and record it as `"runner": "copilot"` in `_state.json`:

- If the app's own code can call its model from a script on this machine, the script runner (`runner-scaffold.*`) is the more faithful choice.
- If not, use this one.

## What the SUT is

| You are measuring | The SUT | `_state.json` `sut` |
|---|---|---|
| **A Copilot custom agent** (`.agent.md`) | That agent, as Copilot runs it | `{"agent": "<its name>", "model": "<model>"}` |
| **A skill, an instructions file** (`copilot-instructions.md`, `AGENTS.md`, `*.instructions.md`) **or a prompt file**, as Copilot uses it | Copilot's default agent, with the customization installed | `{"agent": null, "model": "<model>"}` - the case input is what a user would type |
| **A product prompt** that will ship elsewhere - an API app, a service - measured here through a Copilot model | A tool-less custom agent whose body is the product prompt: copy `references/templates/eval-sut.agent.md` to `.github/agents/eval-sut-<flow>.agent.md` and paste the prompt in | `{"agent": "eval-sut-<flow>", "model": "<target model>"}` |

**What a product-prompt measurement can tell you.** Copilot wraps every subagent in its own system prompt, so the SUT sees your prompt plus Copilot's. Treat the absolute scores as a **proxy** for the production setup. Comparisons between variants measured the same way - same model, same VS Code version - are the useful signal. Say so in `metrics.md` and in the handover. Pin the model, and record the VS Code version in the environment fingerprint (`eval-hillclimb.md` Step 3).

**Customizations: prove they engage.** For a skill or instructions file, follow `eval-hillclimb.md` Step 0.5 ("prove the mechanism is actually wired"): run one case with the customization removed and one with it present, and confirm the replies differ the way the customization should make them differ. Whether a subagent picks up a given customization depends on the VS Code version; don't assume it.

**Model names.** Use the exact names the Copilot model picker shows, such as `"GPT-5 mini (copilot)"`. A subagent cannot use a model in a higher cost tier than the session's own model, so pick the session model accordingly. For the judge, prefer a model from a different family than the SUT.

## Files and configuration

```
eval/cases.jsonl          {id, input, tags?, meta?} per line - the inputs, no answers
<outside the workspace>   gold answers, when the SUT has file tools: {id: expected} or {id, expected} rows
eval/grade.py             optional programmatic grader: def grade(case, output, expected, ref) -> {metric: number}
eval/rubric.md            optional judge rubric: concrete, checkable criteria, metric ids and scales
.hillclimb/<flow>/_state.json
```

```json
{
  "runner": "copilot",
  "cases": "eval/cases.jsonl",
  "gold": "C:/Users/me/.hillclimb-gold/triage.jsonl",
  "grader": "eval/grade.py",
  "sut": {"agent": "eval-sut-triage", "model": "GPT-5 mini (copilot)"},
  "judge": {"agent": "eval-judge", "model": "Claude Sonnet 4.5 (copilot)", "rubric": "eval/rubric.md",
            "mode": "pointwise", "expected": false},
  "reps": 2, "batch_size": 5,
  "metrics": [{"id": "correct", "kind": "binary"}, {"id": "quality", "kind": "judge", "scale": 10}],
  "harness_paths": [".github/agents/eval-case-worker.agent.md", ".github/agents/eval-judge.agent.md"]
}
```

| Field | Notes |
|---|---|
| `cases` | Each `input` (or `prompt`) is sent to the SUT verbatim. Paths are relative to the repo root. **Run every command from the repo root.** |
| `grader` | Runs locally, in-process. It receives the expected answer from `gold` (or from a case's own `expected`) and, for variants, the frozen baseline reply as `ref`. It returns metric numbers. Prefer it whenever the output space allows (build-eval Step 2). |
| `judge` | A subagent judge, with the `eval-judge` agent shipped in `.github/agents/`. **`pointwise`**: one answer against the rubric; the judge returns `{"grade": {...}, "explanation": {...}}`. **`pairwise`**: the candidate against the frozen baseline reply, in an A/B order the script randomizes per (case, rep) and maps back to a `win` metric (1 / 0.5 / 0); the baseline's own rows get `win = 0.5`. **`expected: true`** shows the judge the expected answer - the file is written only after every rep of that case has its reply, and deleted when the case is recorded. |
| `harness_paths` | List the worker and judge agent files when they live in the workspace. They are part of the measuring instrument. The SUT agent file is **not** harness: it is what a hillclimb tunes. |

**Ground truth must be unreachable from the SUT.** A SUT with file or search tools can open anything in the workspace, so keep `expected` out of `cases` and keep `gold` outside the workspace. `plan` warns when it isn't. For a tool-less SUT (the product-prompt template) this is moot.

## One-time setup (the user does these)

1. **Install the agents** next to the skill: `.github/agents/eval-case-worker.agent.md` and `eval-judge.agent.md`, plus the SUT agent if you made one.
2. **Apply the settings.** Run `python "<skill dir>/scripts/vscode_settings.py" --flow .hillclimb/<flow> --runner copilot` and have the user merge the JSON it prints into `.vscode/settings.json`. It sets:
   - `chat.subagents.allowInvocationsFromSubagents: true`, so a worker can call the SUT and the judge;
   - anchored `chat.tools.terminal.autoApprove` rules for `copilot_eval.py`, the report builder and `analyzer-slice`. Workers run `copilot_eval.py` several times per batch, and without these rules every call waits for a click. The rules never match `--approve-harness`. The script calls no model and no network, and its grader is pinned by the harness sha;
   - `chat.agent.maxRequests`;
   - `search.exclude` for `.hillclimb/`.

   Type commands exactly in the form it prints.
3. **Approve the harness** once the cases, grader and rubric are final. Run from the repo root:

   ```
   python "<skill dir>/scripts/copilot_eval.py" plan --flow .hillclimb/<flow> --variant baseline --dry-run --approve-harness
   ```

   This records the sha and plans nothing. After any harness edit, every command refuses until the user runs it again.

`references/vscode-copilot.md` §Settings explains each setting.

## Running a pass

The outer agent - you - does this. It is the same for a pilot, a baseline and a hillclimb round.

1. **Plan.**

   ```
   python "<skill dir>/scripts/copilot_eval.py" plan --flow .hillclimb/<flow> --variant <baseline|vN> [--reps R] [--limit N | --ids a,b] [--launcher py]
   ```

   It prints the resolved scope: (case, rep) count, batches, SUT, judge, and how many subagent calls the pass makes. Compare that to what the user approved. `--dry-run` prints without planning. Planning skips (case, rep)s already recorded, which is how resume works. It keeps all reps of a case in one batch.
2. **Dispatch one `eval-case-worker` per batch.** Call `runSubagent` with `agentName: "eval-case-worker"`, `description: "eval <variant> batch <k>"`, and this prompt:

   ```
   Run batch <k> of variant <variant> for the eval flow .hillclimb/<flow>.
   Start with this command, from the repo root:
   python "<skill dir>/scripts/copilot_eval.py" batch --flow .hillclimb/<flow> --variant <variant> --batch <k>
   Follow the task sheet exactly and return only the lines `record` prints.
   ```

   Dispatch several workers in one turn - three or four is a sensible start - and raise or lower that if Copilot starts rate-limiting. Each worker returns score lines only. You never see a reply or a judge text, so in a hillclimb the held-out wall holds.
3. **Check on disk.** Run `progress`. It lists missing (case, rep)s per batch and logged failures. For anything missing, re-plan, which picks up only what's missing, and dispatch again. A worker that returned `NESTED_SUBAGENTS_DISABLED` means step 2 of the setup is missing; see the fallback below.
4. **Build the report** (`build-eval.md` §Report builder). `trajectory/status.md` shows `calls/run` and `calls total` in place of dollars.

**Fallback when nested subagents are off.** You run the worker protocol yourself: read the task sheet, call the SUT and the judge, write the files, record. That is fine for a build-eval pilot. In a hillclimb, though, the replies of held-out cases would enter your context. Tell the user, and either have them enable nested subagents or get their explicit OK first.

## Pilots, checks and re-grading

- **Pilot (build-eval Step 3).** Run `plan --limit 3` (or `--ids`) on `baseline` and dispatch it. The pilot's rows count toward the full baseline. If the rubric or grader changes after the pilot, `regrade` re-scores the stored replies with no new SUT calls. For a judge-rubric change: run `judge-prep --batch k --rejudge`, make those judge calls, then `regrade`.
- **Oracle and null checks** (`eval-audit.md` §2). Use a separate flow, such as `.hillclimb/<flow>-checks`, with the same `_state.json` minus the split. Write the replies by hand: the expected answer for the oracle, an empty string or "I don't know" for the null. Write each into the `output.md` its task sheet names and run `record`. No SUT call is needed. The oracle should score near 100% and the null near 0%. That check flow needs its own harness approval.
- **Pilot replies are build-eval material.** Write `pilot-review.md` from the pilot's trace files (each trace holds the input and the final reply). Before a hillclimb splits the set, put the pilot's cases in train; if any land in test, don't reread their replies.

## Time and cost

Copilot doesn't give the agent token counts or prices, so the eval budgets in **subagent calls** and **wall-clock** time:

- **Calls.** A pass makes cases × reps SUT calls, plus the same number of judge calls when a judge is used (none for a pairwise baseline), plus one worker per batch. `plan` prints the count, and `status.md` records `calls/run` and the cumulative `calls total`.
- **Wall-clock.** `progress` prints the seconds since the plan was made. Time the pilot, then scale by N/M - never estimate.
- **Quota.** How subagent calls count against the user's Copilot quota depends on their plan and on the model; don't guess - ask, or point them at their admin. Present the plan as "N cases × R reps = X SUT calls + Y judge calls, about Z minutes", and get their yes.
- **When it is too much**, offer the usual levers: fewer reps, a cheaper judge model, programmatic grading instead of a judge, or trimming to the discriminating cases.

## What this runner cannot see

Name these limits to the user when they matter. `eval-audit.md` items that depend on them become "not measurable here", not passes.

- **Token usage and cost**, so no `$/run`, no cache analysis, and no `cost-hillclimb.md` cost gates. A cost goal reduces to fewer calls or cheaper models.
- **The served model.** The requested model is recorded, but nothing asserts which model actually served the call.
- **The SUT's intermediate steps.** Only the final reply comes back, so traces are input + reply. Graders that need tool-call trajectories can't work. For an agentic SUT, grade the **end state** it leaves in a fixture directory, through the programmatic grader, and keep the fixture per (case, rep).
- **Latency per case.** The `latency` column is absent.
- **Truncation.** No stop reason comes back, so let the grader flag an obviously cut-off reply as a failure.
- **Exact reproducibility across VS Code updates.** Copilot's own prompts change between versions. Re-baseline when VS Code updates mid-loop.

**Replies are copied by the worker.** Each reply passes through the worker model, which writes it to disk, so a very long reply can be cut or altered. If replies run longer than a few pages, have the grader check the reply's expected structure (for example, its closing section) and use small batches.
