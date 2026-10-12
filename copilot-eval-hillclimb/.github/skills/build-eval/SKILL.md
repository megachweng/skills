---
name: build-eval
description: Slash-command shortcut for the eval-hillclimb skill's build-eval flow - build an eval set (inputs, grader, runnable script, baseline report) for an LLM-powered app.
argument-hint: "<the feature or flow you want a number for>"
disable-model-invocation: true
---

Run the **`build-eval`** subcommand of the `eval-hillclimb` skill.

1. Read the `eval-hillclimb` skill's `SKILL.md` now. It sits next to this skill's folder: `../eval-hillclimb/SKILL.md` relative to this file. In a workspace install that is `.github/skills/eval-hillclimb/SKILL.md`; in a user-level install, `~/.copilot/skills/eval-hillclimb/SKILL.md`.
2. Follow it for `build-eval`. Treat any text the user typed after `/build-eval` as their answer to its first question: what they want to measure.

If that file is not there, tell the user that `/build-eval` needs the `eval-hillclimb` skill installed in the same skills folder, and stop.
