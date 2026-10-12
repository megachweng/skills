#!/usr/bin/env python3
"""Print the VS Code settings an unattended eval loop needs, with the terminal
auto-approve rules built from this skill's real install path - no hand-escaped
regexes.

    python vscode_settings.py --flow .hillclimb/<flow> --runner copilot
    python vscode_settings.py --flow .hillclimb/<flow> --runner script --runner-cmd "python eval/run_eval.py"

It prints a JSON object for the user to review and merge into
.vscode/settings.json themselves (this script writes nothing), followed by
the exact command forms the agent must type so the rules match. Every rule is
anchored to one script and one flow, allows only that script's own flags, and
never matches --approve-harness, `;`, `&&` or a pipe.
"""
import argparse
import json
import os
import re
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VARIANT = "(baseline|v[1-9][0-9]*)"
PY = "(python|python3|py)"


def rx(text):
    """Escape text for a JavaScript regex literal (VS Code's "/.../" rule keys)."""
    return re.sub(r"([\\^$.*+?()\[\]{}|/])", r"\\\1", text)


SEP = "[\\\\/]"  # regex: either path separator


def path_core(path):
    """A path with either separator, after an optional opening quote (no closing quote)."""
    parts = [rx(p) for p in re.split(r"[\\/]+", path) if p != ""]
    lead = SEP if path[:1] in "\\/" else ""
    return '"?' + lead + SEP.join(parts)


def path_rx(path):
    return path_core(path) + '"?'


def flow_rx(flow):
    return path_core(flow) + SEP + '?"?'


def rule(body):
    return "/^" + body + "$/"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--flow", required=True, help="repo-relative flow directory, e.g. .hillclimb/triage")
    ap.add_argument("--runner", choices=["copilot", "script"], required=True)
    ap.add_argument("--runner-cmd", help='script runner only: the command before its flags, e.g. "python eval/run_eval.py"')
    ap.add_argument("--skill-dir", default=SKILL_DIR, help=argparse.SUPPRESS)  # tests
    ap.add_argument("--max-requests", type=int, default=200)
    a = ap.parse_args(argv)
    if a.runner == "script" and not a.runner_cmd:
        ap.error("--runner script needs --runner-cmd")
    if re.search(r"[;&|`$<>]", a.flow + (a.runner_cmd or "")):
        ap.error("flow and runner command must be plain paths/words")
    scripts = os.path.join(a.skill_dir, "scripts")
    flow = flow_rx(a.flow)
    s = lambda name: path_rx(os.path.join(scripts, name))
    rules, commands = {}, []

    if a.runner == "copilot":
        ce = s("copilot_eval.py")
        rules[rule(PY + " " + ce + " plan --flow " + flow + " --variant " + VARIANT
                   + "( --(reps|batch-size|limit) [0-9]+| --ids [A-Za-z0-9_.,-]+| --launcher (python|python3|py)| --dry-run)*")] = True
        rules[rule(PY + " " + ce + " (batch|judge-prep|record|regrade|progress) --flow " + flow + " --variant " + VARIANT
                   + "( --batch [0-9]+| --rejudge| --id [A-Za-z0-9_.-]+| --rep [0-9]+)*")] = True
        commands += ['python "%s" plan --flow %s --variant v1' % (os.path.join(scripts, "copilot_eval.py"), a.flow),
                     'python "%s" batch --flow %s --variant v1 --batch 1' % (os.path.join(scripts, "copilot_eval.py"), a.flow)]
    else:
        rules[rule(rx(a.runner_cmd.strip()).replace(" ", " +") + " --flow " + flow + " --variant " + VARIANT
                   + "( --(reps|limit|concurrency|timeout-s) [0-9]+| --model [A-Za-z0-9._@:-]+| --ids [A-Za-z0-9_.,-]+| --dry-run)*")] = True
        rules[rule("(node " + s("wait-run.mjs") + "|" + PY + " " + s("wait_run.py") + ") " + path_core(a.flow) + SEP
                   + VARIANT + SEP + '?"?( --(max-wait-s|interval-s|expect) [0-9]+| --just-launched)*')] = True
        commands += ['%s --flow %s --variant v1 --reps 2' % (a.runner_cmd.strip(), a.flow),
                     'node "%s" %s/v1 --max-wait-s 540' % (os.path.join(scripts, "wait-run.mjs"), a.flow)]
    rules[rule("(node " + s("build-report-lite.mjs") + "|" + PY + " " + s("build_report_lite.py") + ") " + flow)] = True
    rules[rule("(node " + s("analyzer-slice.mjs") + "|" + PY + " " + s("analyzer_slice.py") + ") " + flow
               + " " + VARIANT + "( --all)?")] = True
    commands += ['node "%s" %s' % (os.path.join(scripts, "build-report-lite.mjs"), a.flow),
                 'python "%s" %s v1' % (os.path.join(scripts, "analyzer_slice.py"), a.flow)]

    settings = {"chat.tools.terminal.autoApprove": {k: {"approve": v, "matchCommandLine": True} for k, v in rules.items()},
                "chat.agent.maxRequests": a.max_requests,
                "search.exclude": {"**/.hillclimb/**": True}}
    if a.runner == "copilot":
        settings["chat.subagents.allowInvocationsFromSubagents"] = True
    print(json.dumps(settings, indent=2))
    print("\n// Command forms these rules match (type them exactly this way; `py` or `python3` may replace `python`):",
          file=sys.stderr)
    for c in commands:
        print("//   " + c, file=sys.stderr)
    print("// --approve-harness is deliberately not covered: the user runs it by hand.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
