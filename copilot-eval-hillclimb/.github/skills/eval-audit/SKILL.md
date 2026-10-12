---
name: eval-audit
description: Slash-command shortcut for the eval-hillclimb skill's audit - check whether an existing eval (cases, runner, grader) is trustworthy before relying on its numbers.
argument-hint: "<path to the eval, or the flow it covers>"
disable-model-invocation: true
---

Run the **`audit`** subcommand of the `eval-hillclimb` skill.

1. Read the `eval-hillclimb` skill's `SKILL.md` now. It sits next to this skill's folder: `../eval-hillclimb/SKILL.md` relative to this file. In a workspace install that is `<repo root>/.github/skills/eval-hillclimb/SKILL.md`; in a user-level install, `<home>/.copilot/skills/eval-hillclimb/SKILL.md`, where `<home>` is the absolute home folder (`$HOME`, or `%USERPROFILE%` on Windows). The read tool needs the absolute path.
2. Follow it for `audit`. Treat any text the user typed after `/eval-audit` as where the eval lives.

If that file is not there, tell the user that `/eval-audit` needs the `eval-hillclimb` skill installed in the same skills folder, and stop.
