---
name: hillclimb
description: Slash-command shortcut for the eval-hillclimb skill's hillclimb flow - iteratively improve an LLM-powered app (prompt, skill, tools, model settings, code) against an existing eval, with a train/test split.
argument-hint: "<the eval / flow to climb, and what to improve>"
disable-model-invocation: true
---

Run the **`hillclimb`** subcommand of the `eval-hillclimb` skill.

1. Read the `eval-hillclimb` skill's `SKILL.md` now. It sits next to this skill's folder: `../eval-hillclimb/SKILL.md` relative to this file. In a workspace install that is `.github/skills/eval-hillclimb/SKILL.md`; in a user-level install, `~/.copilot/skills/eval-hillclimb/SKILL.md`.
2. Follow it for `hillclimb`. Treat any text the user typed after `/hillclimb` as context for its Step 0 and Step 1 questions.
3. If the goal is to cut cost at equal quality, follow it for `cost` instead.

If that file is not there, tell the user that `/hillclimb` needs the `eval-hillclimb` skill installed in the same skills folder, and stop.
