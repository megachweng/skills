---
name: eval-hillclimb
description: Build a trustworthy eval for an LLM-powered app, then improve the app against it (hill-climbing) - from VS Code Copilot agent mode. Use when the user wants an eval set for a prompt or feature, asks "how do I know my change helped", "is my eval any good?", wants to iterate a system prompt / skill / tool descriptions / model settings against an eval, or wants to cut cost or latency at equal quality. Subcommands - build-eval, hillclimb, audit, cost. Provider-neutral (Anthropic, OpenAI / Azure OpenAI, Gemini, company gateways); scripts run on Node or Python.
argument-hint: "build-eval | hillclimb | audit | cost  <what to measure or improve>"
license: Apache-2.0 (derived from Anthropic's claude-api skill; see LICENSE.txt)
---

# Eval building and hill-climbing (VS Code Copilot)

This skill is the `build-eval` / `hillclimb` part of Anthropic's claude-api skill, ported to GitHub Copilot in VS Code. Use it to build an eval the user trusts, then move a number against it - quality up, or cost and latency down while quality holds. It is not tied to one model provider.

## Subcommands

When the request names a subcommand - `/eval-hillclimb build-eval ...`, or the `/build-eval`, `/hillclimb`, `/eval-audit` shortcuts - or plainly asks for one, follow the matching row. Treat any text after the subcommand as the user's answer to that guide's first question.

| Subcommand | Action |
|---|---|
| `build-eval` | **Read `references/build-eval.md` immediately** and run its interview: Step 0 (what's being evaluated), Step 1 (source the inputs), Step 2 (grading method), Step 3 (runnable script plus a measured pilot), Step 4 (baseline report). Before building on anything, get the user's explicit sign-off on three things: the inputs, the grading method, and the cost. |
| `hillclimb` | **Read `references/eval-hillclimb.md` immediately** and follow it. Step 0: confirm a runnable eval exists; if it doesn't, switch to `build-eval`. Step 0.5: prove the eval can be climbed. Steps 1-2: goal, scope, stopping condition and budget, then plan approval. Then the analyze -> apply -> run -> record loop, with a train/test split and state on disk. |
| `audit` | "Is my eval any good?" - **read `references/eval-audit.md`**, run it against the user's eval (cases, runner, grader) and report per its §6. Change nothing unless asked. |
| `cost` | A hillclimb whose goal is lower cost at equal quality. Run `hillclimb` with the goal set to cost, and **read `references/cost-hillclimb.md`** before planning rounds. |

No subcommand but an eval-shaped request? Pick the matching row: no eval yet means `build-eval`; an eval plus "make it better" means `hillclimb`.

## Before you start - every subcommand

1. **Read `references/vscode-copilot.md`.** The guides were written for Claude Code. That file says how to do their steps here:
   - ask the user with `vscode/askQuestions`;
   - run long passes in the terminal, sync with a timeout or async plus `wait-run`;
   - start the analyzer with `runSubagent` and `agentName: "eval-analyzer"`;
   - set up terminal auto-approval.
2. **Locate the skill folder.** It is the folder that holds this `SKILL.md`; all relative paths here start from it. Scripts live in `scripts/`; run them by absolute path, from there only. Never copy them into the project to run from, and never edit them during a build or a loop - they are the measuring instrument.
3. **Pick a runtime** by checking `node --version` and `python --version` (or `py --version` on Windows):

   | | Node (or Bun) | Python 3.8+, standard library only |
   |---|---|---|
   | Report builder | `scripts/build-report-lite.mjs` | `scripts/build_report_lite.py` |
   | Runner scaffold (copied into the user's repo) | `scripts/runner-scaffold.mjs` | `scripts/runner_scaffold.py` |
   | Wait helper | `scripts/wait-run.mjs` | `scripts/wait_run.py` |
   | Analyzer's train-only inputs | `scripts/analyzer-slice.mjs` | `scripts/analyzer_slice.py` |

   - The two report builders write byte-identical `trajectory/scores.tsv` and `trajectory/status.md`.
   - Copy the runner scaffold that matches the project's language.
   - The upstream "full viewer" (`build-report.mjs`) is not part of this port; the lite builder is the report.
4. **Default locations.** Flow data goes in `.hillclimb/<flow>/` (layout and field reference: `references/data-contract.md`). Rates go in `_state.json.prices` (`references/pricing.md`).

## Standing rules

These override any convenience. The guides explain why.

- **Money.** Every pass that calls a model spends real money. Get the user's go - cases × reps × model, plus a measured cost whenever they ask about cost - before the first paid call, and again whenever the scope changes.
- **Sign-offs are the user's words.** That covers the inputs, the grading method, the hillclimb plan, and consent to spend. A tool answering automatically (Autopilot, auto-reply) is not a sign-off.
- **`--approve-harness` belongs to the user.** Give them the command to run in their own terminal. Never run it yourself, and never write an auto-approve rule that matches it.
- **Held-out data stays held out.** In a hillclimb you see scores only. The analyzer subagent reads train-split transcripts only. Never open, search, or quote a test-split transcript, and take numbers from `trajectory/status.md`, not `report.html`.
- **Never type a number in.** Every score, interval, delta and dollar figure you report comes from the builder's `trajectory/status.md` (or `scores.tsv`). If a number you need isn't there, fix the data or the runner so the builder computes it.
- **Talk to the user in their language.** When they write in Chinese, reply in Chinese. Keep ids, paths, commands and metric names verbatim.

## Files

| Path | What |
|---|---|
| `references/build-eval.md` | Building an eval: interview, sign-offs, runner, pilot, baseline report |
| `references/eval-hillclimb.md` | The hill-climbing loop |
| `references/eval-audit.md` | Eval health checklist (used by both, and by `audit`) |
| `references/cost-hillclimb.md` | Cost-goal search (model × effort staircase, adoption gates) |
| `references/vscode-copilot.md` | VS Code tool mapping, running passes, settings, the analyzer subagent |
| `references/data-contract.md` | Flow directory layout and every file's fields; how the builder computes its numbers; extra pages |
| `references/pricing.md` | `_state.json.prices`, Anthropic list prices, other providers' cache rules |
| `scripts/*` | Runner scaffolds, report builders, wait helpers, analyzer slicers (Node and Python) |
