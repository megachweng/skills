#!/usr/bin/env python3
"""End-to-end checks for the eval-hillclimb scripts (stdlib only).

    python tests/run_tests.py            # Node checks are skipped when node is not on PATH

What it covers:
  - both runner scaffolds, filled in with a fake app: harness gate (refuse,
    approve, refuse after an edit), --dry-run, --limit, resume idempotence,
    429 backoff, wall-clock timeout, served-model mismatch, truncation,
    path-safe ids, frozen pairwise refs, the live-pass guard, the 'finished'
    marker, errors.jsonl
  - both report builders: byte-identical trajectory/scores.tsv and
    trajectory/status.md on the runner output and on synthetic edge cases
  - both wait-run helpers
"""
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), ".github", "skills", "eval-hillclimb", "scripts")
NODE = shutil.which("node") or shutil.which("bun")
PY = sys.executable
FAILS = []


def check(cond, what):
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        FAILS.append(what)


def run(cmd, cwd, env=None, timeout=120):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


CASES = [{"id": "c%d" % i, "prompt": "question %d" % i, "tags": ["easy" if i % 2 else "hard"]} for i in range(1, 7)]
CASES += [{"id": "flaky", "prompt": "flaky"}, {"id": "slow", "prompt": "slow"},
          {"id": "wrongmodel", "prompt": "wm"}, {"id": "trunc", "prompt": "tr"}, {"id": "a/b", "prompt": "weird id"}]

NODE_FILL = {
    "  throw new Error('TODO: loadCases');":
        "  return JSON.parse(readFileSync(process.env.CASES, 'utf8'));",
    "  throw new Error('TODO: runCase');": r"""
  const id = input.id;
  if (id === 'flaky' && !globalThis.__flaked) { globalThis.__flaked = true; throw Object.assign(new Error('rate limited'), { status: 429 }); }
  if (id === 'slow') await new Promise(r => setTimeout(r, 4000));
  const model = id === 'wrongmodel' ? 'other-model' : ctx.model + '-20260101';
  return { output: 'ANSWER:' + id + ':' + ctx.variant, model,
           usage: normalizeUsage({ prompt_tokens: 120, completion_tokens: 30, prompt_tokens_details: { cached_tokens: 20 } }),
           stop_reason: id === 'trunc' ? 'max_tokens' : 'end_turn',
           transcript: [{ role: 'user', content: input.prompt }, { role: 'assistant', content: 'ANSWER:' + id }] };""",
    "  throw new Error('TODO: gradeCase');": r"""
  const pass = (String(input.id).length + (ctx.variant === 'baseline' ? 0 : 1)) % 2;
  const grade = { pass, quality: 5 + pass * 3 };
  if (ref != null) grade.win = ref === run.output ? 0.5 : 1;
  else if (ctx.variant === 'baseline') grade.win = 0.5;
  else throw new Error('missing frozen ref for ' + input.id);
  return { grade, explanation: { quality: 'fake judge' }, judge_model: 'judge-mini', judge_usage: { input_tokens: 10, output_tokens: 5 } };""",
}

PY_FILL = {
    '    raise NotImplementedError("TODO: load_cases")':
        '    return json.load(open(os.environ["CASES"], encoding="utf-8"))',
    '    raise NotImplementedError("TODO: run_case")': r'''
    cid = case["id"]
    if cid == "flaky" and not getattr(run_case, "flaked", False):
        run_case.flaked = True
        e = Exception("rate limited")
        e.status_code = 429
        raise e
    if cid == "slow":
        await asyncio.sleep(4)
    model = "other-model" if cid == "wrongmodel" else ctx["model"] + "-20260101"
    return {"output": "ANSWER:%s:%s" % (cid, ctx["variant"]), "model": model,
            "usage": normalize_usage({"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 20}}),
            "stop_reason": "max_tokens" if cid == "trunc" else "end_turn",
            "transcript": [{"role": "user", "content": case["prompt"]}, {"role": "assistant", "content": "ANSWER:" + cid}]}''',
    '    raise NotImplementedError("TODO: grade_case")': r'''
    p = (len(str(case["id"])) + (0 if ctx["variant"] == "baseline" else 1)) % 2
    grade = {"pass": p, "quality": 5 + p * 3}
    if ref is not None:
        grade["win"] = 0.5 if ref == run["output"] else 1
    elif ctx["variant"] == "baseline":
        grade["win"] = 0.5
    else:
        raise Exception("missing frozen ref for " + case["id"])
    return {"grade": grade, "explanation": {"quality": "fake judge"}, "judge_model": "judge-mini",
            "judge_usage": {"input_tokens": 10, "output_tokens": 5}}''',
}


def make_runner(kind, work):
    src = os.path.join(SCRIPTS, "runner-scaffold.mjs" if kind == "node" else "runner_scaffold.py")
    with open(src, encoding="utf-8") as f:
        s = f.read()
    for old, new in (NODE_FILL if kind == "node" else PY_FILL).items():
        assert s.count(old) == 1, (kind, old)
        s = s.replace(old, new)
    dst = os.path.join(work, "run-eval.mjs" if kind == "node" else "run_eval.py")
    with open(dst, "w", encoding="utf-8") as f:
        f.write(s)
    return [NODE, dst] if kind == "node" else [PY, dst]


def test_runner(kind, work):
    print("runner (%s)" % kind)
    cmd = make_runner(kind, work)
    with open(os.path.join(work, "cases.json"), "w") as f:
        json.dump(CASES, f)
    env = {"CASES": os.path.join(work, "cases.json")}
    flow = ".hillclimb/demo"
    os.makedirs(os.path.join(work, flow), exist_ok=True)
    with open(os.path.join(work, flow, "_state.json"), "w") as f:
        json.dump({"train_ids": ["c1", "c2", "c3", "flaky", "slow"], "test_ids": ["c4", "c5", "c6", "trunc", "wrongmodel"],
                   "metrics": [{"id": "pass", "kind": "binary"}, {"id": "quality", "kind": "judge"}, {"id": "win", "kind": "float"}],
                   "prices": {"test-model": {"in": 3, "out": 15}, "judge-mini": {"in": 0.1, "out": 0.5}},
                   "harness_paths": ["cases.json"]}, f)
    base = cmd + ["--flow", flow, "--model", "test-model", "--timeout-s", "2", "--concurrency", "3"]
    vb = os.path.join(work, flow, "baseline")

    code, _, err = run(base + ["--variant", "baseline"], work, env)
    check(code == 2 and "approve-harness" in err, "refuses before --approve-harness")
    check(open(os.path.join(vb, "progress.txt")).read().strip().splitlines()[-1].startswith("finished (exit 2)"),
          "refusal ends progress.txt with a finished line")
    code, _, err = run(base + ["--variant", "baseline", "--approve-harness", "--dry-run"], work, env)
    check(code == 0 and "11 of 11 (id,rep) to run" in err and "dry run" in err, "approve + dry run prints resolved scope")
    check(not os.path.exists(os.path.join(vb, "results.jsonl")), "dry run writes no rows")
    with open(os.path.join(work, "cases.json"), "a") as f:
        f.write("\n")
    code, _, err = run(base + ["--variant", "baseline", "--dry-run"], work, env)
    check(code == 2 and "harness changed" in err, "edit to a harness_paths file stops the next run")
    run(base + ["--variant", "baseline", "--approve-harness", "--dry-run"], work, env)

    code, _, err = run(base + ["--variant", "baseline", "--limit", "1"], work, env)
    check(code == 0 and len(rows(os.path.join(vb, "results.jsonl"))) == 1, "--limit 1 runs one case")
    code, _, err = run(base + ["--variant", "baseline", "--ids", "nope"], work, env)
    check(code == 2 and "no such case" in err, "--ids with an unknown id is a stop")

    code, out, err = run(base + ["--variant", "baseline"], work, env)
    res = rows(os.path.join(vb, "results.jsonl"))
    errs = rows(os.path.join(vb, "errors.jsonl"))
    ids = sorted(r["prompt_id"] for r in res)
    check(code == 1, "full pass exits 1 when some attempts failed (got %d)" % code)
    check(len(res) == 9 and len(set((r["prompt_id"], r["rep"]) for r in res)) == 9,
          "9 scored rows, no duplicate (case, rep) (got %d)" % len(res))
    check("flaky" in ids and any(r.get("meta", {}).get("retries") == 1 for r in res if r["prompt_id"] == "flaky"),
          "429 retried with backoff and the retry counted in meta")
    classes = sorted(e["failure_class"] for e in errs)
    check(classes == ["serving_substitution", "timeout"], "timeout and served-model mismatch go to errors.jsonl (%s)" % classes)
    wm = [e for e in errs if e["failure_class"] == "serving_substitution"]
    check(bool(wm) and wm[0].get("usage", {}).get("input_tokens") == 100, "billed usage kept on the error row")
    tr = [r for r in res if r["prompt_id"] == "trunc"]
    check(bool(tr) and tr[0]["status"] == "truncated", "max_tokens row marked truncated")
    weird = [r for r in res if r.get("meta", {}).get("original_id") == "a/b"]
    check(bool(weird) and weird[0]["prompt_id"].startswith("a_b-"), "path-safe id with original kept in meta")
    check(os.path.exists(os.path.join(vb, "traces", weird[0]["prompt_id"] + "_rep0.json")) if weird else False,
          "trace written under the path-safe id")
    check(os.path.exists(os.path.join(vb, "ref", "c1")), "baseline output frozen as the pairwise ref")
    check(res[0]["usage"] == {"input_tokens": 100, "output_tokens": 30, "cache_read_input_tokens": 20,
                              "cache_creation_input_tokens": 0}, "normalizeUsage splits OpenAI cached tokens out")
    check(open(os.path.join(vb, "progress.txt")).read().strip().splitlines()[-1].startswith("finished (exit 1)"),
          "progress.txt ends with the finished marker")

    code, _, err = run(base + ["--variant", "baseline"], work, env)
    check(len(rows(os.path.join(vb, "results.jsonl"))) == 9 + 0 and "2 of 11" in err,
          "resume re-runs only the failed (case, rep)s")

    # live-pass guard: a fresh progress.txt without 'finished'
    with open(os.path.join(vb, "progress.txt"), "w") as f:
        f.write("[baseline] 3/11 done\n")
    code, _, err = run(base + ["--variant", "baseline"], work, env)
    check(code == 2 and "looks live" in err, "refuses while another pass looks live")
    with open(os.path.join(vb, "progress.txt"), "w") as f:
        f.write("finished (exit 0): x\n")

    code, _, err = run(base + ["--variant", "v1", "--reps", "2"], work, env)
    v1 = rows(os.path.join(work, flow, "v1", "results.jsonl"))
    check(len(v1) == 18 and all(r["grade"].get("win") == 1 for r in v1), "v1 judged against frozen baseline refs (18 rows)")

    code, out, _ = run([PY, os.path.join(SCRIPTS, "wait_run.py"), os.path.join(flow, "v1"), "--max-wait-s", "1", "--expect", "22"], work)
    check(code == 0 and "rows 18/22" in out, "wait_run.py sees the finished pass")
    if NODE:
        code, out, _ = run([NODE, os.path.join(SCRIPTS, "wait-run.mjs"), os.path.join(flow, "v1"), "--max-wait-s", "1"], work)
        check(code == 0 and "finished" in out, "wait-run.mjs sees the finished pass")
    with open(os.path.join(work, flow, "v1", "progress.txt"), "w") as f:
        f.write("[v1] 1/2 done\n")
    code, out, _ = run([PY, os.path.join(SCRIPTS, "wait_run.py"), os.path.join(flow, "v1"), "--max-wait-s", "1", "--interval-s", "0.5"], work)
    check(code == 3 and "still running" in out, "wait_run.py exits 3 while a pass is running")
    old = time.time() - 300
    os.utime(os.path.join(work, flow, "v1", "progress.txt"), (old, old))
    for helper in ([[NODE, os.path.join(SCRIPTS, "wait-run.mjs")]] if NODE else []) + [[PY, os.path.join(SCRIPTS, "wait_run.py")]]:
        code, out, _ = run(helper + [os.path.join(flow, "v1"), "--max-wait-s", "5"], work)
        check(code == 4 and "stalled" in out, "%s exits 4 when the runner died" % os.path.basename(helper[1]))
    with open(os.path.join(work, flow, "v1", "progress.txt"), "w") as f:
        f.write("finished (exit 0): old pass\n")
    os.utime(os.path.join(work, flow, "v1", "progress.txt"), (old, old))
    t = time.time()
    code, out, _ = run([PY, os.path.join(SCRIPTS, "wait_run.py"), os.path.join(flow, "v1"), "--max-wait-s", "1", "--just-launched"], work, timeout=60)
    check(time.time() - t >= 25, "wait_run.py --just-launched ignores a stale 'finished' line at first")
    return os.path.join(work, flow)


def test_copilot_runner(root):
    """Simulates the worker subagent: reads task sheets, writes replies and judge verdicts, records."""
    print("copilot runner (copilot_eval.py)")
    work = os.path.join(root, "copilot")
    os.makedirs(os.path.join(work, "eval"))
    gold_dir = os.path.join(root, "gold-outside-workspace")
    os.makedirs(gold_dir)
    cases = [{"id": "q%d" % i, "input": "What is %d + %d? ```tricky``` ===" % (i, i), "tags": ["math"]} for i in range(1, 6)]
    with open(os.path.join(work, "eval", "cases.jsonl"), "w") as f:
        f.write("\n".join(json.dumps(c) for c in cases) + "\n")
    with open(os.path.join(gold_dir, "gold.json"), "w") as f:
        json.dump({c["id"]: str(2 * int(c["id"][1:])) for c in cases}, f)
    grader = os.path.join(work, "eval", "grade.py")
    with open(grader, "w") as f:
        f.write("def grade(case, output, expected, ref):\n    return {'correct': 1 if expected in output else 0}\n")
    flow = ".hillclimb/math"
    os.makedirs(os.path.join(work, flow))
    state = {"runner": "copilot", "cases": "eval/cases.jsonl", "gold": os.path.join(gold_dir, "gold.json"),
             "grader": "eval/grade.py", "train_ids": ["q1", "q2", "q3"], "test_ids": ["q4", "q5"],
             "sut": {"agent": "eval-sut-math", "model": "GPT-5 mini (copilot)"},
             "judge": {"agent": "eval-judge", "model": "Claude Sonnet 4.5 (copilot)", "rubric": "eval/rubric.md", "mode": "pointwise"},
             "metrics": [{"id": "correct", "kind": "binary"}, {"id": "quality", "kind": "judge"}]}
    with open(os.path.join(work, "eval", "rubric.md"), "w") as f:
        f.write("Score quality 1-10.\n")
    with open(os.path.join(work, flow, "_state.json"), "w") as f:
        json.dump(state, f)
    ce = [PY, os.path.join(SCRIPTS, "copilot_eval.py")]

    def cmd(sub, variant, *extra):
        return run(ce + [sub, "--flow", flow, "--variant", variant] + list(extra), work)

    code, out, err = cmd("plan", "baseline")
    check(code == 2 and "approve-harness" in err, "copilot: plan refuses before harness approval")
    code, out, err = cmd("plan", "baseline", "--dry-run", "--approve-harness")
    check(code == 0 and "dry run" in out and not os.path.exists(os.path.join(work, flow, "baseline", "_work", "plan.json")),
          "copilot: --dry-run --approve-harness approves without planning a pass")
    code, out, err = cmd("plan", "baseline", "--reps", "2", "--batch-size", "4")
    check(code == 0 and "10 of 10 (id,rep) to run in 3 batch(es)" in out, "copilot: plan groups reps of a case in one batch (%s)" % out.splitlines()[:1])
    plan = json.load(open(os.path.join(work, flow, "baseline", "_work", "plan.json")))

    def do_batch(variant, k, reply, judge_reply, fail_ids=()):
        code, sheet, err = cmd("batch", variant, "--batch", str(k))
        check(code == 0 and "TASK SHEET" in sheet, "copilot: %s batch %d task sheet" % (variant, k))
        for m in re.finditer(r"--- item \d+/\d+: (\S+) rep (\d+) ---\nwrite the SUT's final reply, verbatim, to: (.+)\n", sheet):
            sid, rep, path = m.group(1), int(m.group(2)), m.group(3)
            if sid in fail_ids:
                with open(os.path.join(os.path.dirname(path), "error.txt"), "w") as f:
                    f.write("timeout\nsubagent did not answer\n")
            else:
                with open(path, "w") as f:
                    f.write(reply(sid, rep))
        code, jsheet, err = cmd("judge-prep", variant, "--batch", str(k))
        for m in re.finditer(r"judge file: (.+)\njudge prompt:\n((?:.+\n)+?)\n", jsheet):
            with open(m.group(1), "w") as f:
                f.write(judge_reply(m.group(2)))
        check("Expected:" not in jsheet, "copilot: no expected file handed out unless judge.expected is set")
        return cmd("record", variant, "--batch", str(k))

    ans = lambda sid, rep: "The answer is %d." % (2 * int(sid[1:]) if (int(sid[1:]) + rep) % 2 else 0)
    code, out, err = do_batch("baseline", 1, ans, lambda p: 'Sure! {"grade": {"quality": 7}, "explanation": {"quality": "fine"}}', fail_ids=("q2",))
    check(code == 0 and "FAILED q2 rep0: timeout" in out and "recorded q1 rep0: correct=1, quality=7" in out and "recorded q1 rep1: correct=0" in out,
          "copilot: record grades programmatically + judge, logs SUT failures (%s)" % out.replace("\n", " | ")[:200])
    code, out, err = do_batch("baseline", 2, ans, lambda p: "I refuse to output JSON")
    check(code == 1 and "NOT RECORDED" in out, "copilot: unusable judge reply is not recorded")
    for d in glob.glob(os.path.join(work, flow, "baseline", "_work", "*", "judge.json")):
        os.remove(d)
    code, out, err = do_batch("baseline", 2, ans, lambda p: '{"grade": {"quality": 5}}')
    check(code == 0 and out.count("recorded") >= 2, "copilot: re-judged items record on the second pass")
    code, out, err = do_batch("baseline", 3, ans, lambda p: '{"grade": {"quality": 9}}')
    code, out, err = cmd("progress", "baseline")
    check(code == 1 and "q2#0" in out, "copilot: progress lists the failed item as missing")
    code, out, err = cmd("plan", "baseline", "--reps", "2")
    check("2 of 10 (id,rep) to run" in out, "copilot: re-plan picks up only the failed (case, rep)s")
    do_batch("baseline", 1, ans, lambda p: '{"grade": {"quality": 6}}')
    code, out, err = cmd("progress", "baseline")
    res = rows(os.path.join(work, flow, "baseline", "results.jsonl"))
    check(code == 0 and len(res) == 10 and len({(r["prompt_id"], r["rep"]) for r in res}) == 10,
          "copilot: 10 rows, no duplicates, progress complete")
    check(os.path.exists(os.path.join(work, flow, "baseline", "ref", "q1")), "copilot: baseline reply frozen as reference")
    check(not glob.glob(os.path.join(work, flow, "**", "expected.md"), recursive=True), "copilot: no expected answers left in the workspace")

    # pairwise variant against the frozen baseline
    state["judge"]["mode"] = "pairwise"
    with open(os.path.join(work, flow, "_state.json")) as f:
        state["harness_sha"] = json.load(f)["harness_sha"]
    with open(os.path.join(work, flow, "_state.json"), "w") as f:
        json.dump(state, f)
    code, out, err = cmd("plan", "v1", "--reps", "1", "--batch-size", "5")
    check(code == 0, "copilot: pairwise plan (judge mode is not part of the harness sha)")

    def pick_mine(prompt):
        a = re.search(r"Answer A: (.+)", prompt).group(1)
        return '{"winner": "%s", "reason": "x"}' % ("A" if a.endswith("output.md") else "B")
    code, out, err = do_batch("v1", 1, lambda sid, rep: "It is %d." % (2 * int(sid[1:])), pick_mine)
    v1 = rows(os.path.join(work, flow, "v1", "results.jsonl"))
    check(len(v1) == 5 and all(r["grade"]["win"] == 1 for r in v1), "copilot: pairwise A/B mapped back to the candidate")

    # regrade after a grader change; the gate stops record until re-approved
    with open(grader, "w") as f:
        f.write("def grade(case, output, expected, ref):\n    return {'correct': 1}\n")
    code, out, err = cmd("record", "v1", "--batch", "1")
    check(code == 2 and "harness changed" in err, "copilot: grader edit stops the next command until re-approved")
    cmd("plan", "v1", "--dry-run", "--approve-harness")
    code, out, err = cmd("regrade", "baseline")
    check(code == 0 and "regraded 10 row(s)" in out and "changed" in out, "copilot: regrade re-scores stored replies (%s)" % out.strip())
    check(all(r["grade"]["correct"] == 1 for r in rows(os.path.join(work, flow, "baseline", "results.jsonl"))),
          "copilot: regraded rows carry the new grades")
    return os.path.join(work, flow)


def test_settings_rules(root):
    """The generated auto-approve regexes, run through a real JavaScript regex engine."""
    print("vscode settings rules")
    if not NODE:
        print("  (skipped: needs node to evaluate JS regexes)")
        return
    skill = os.path.join(root, "home dir", ".copilot", "skills", "eval-hillclimb")
    sc = os.path.join(skill, "scripts")
    cases = {
        "copilot": (["--runner", "copilot"], [
            ('python "%s/copilot_eval.py" plan --flow .hillclimb/triage --variant v2 --reps 2 --batch-size 5' % sc, True),
            ('py "%s/copilot_eval.py" batch --flow .hillclimb/triage --variant baseline --batch 3' % sc, True),
            ('python "%s/copilot_eval.py" record --flow .hillclimb\\triage --variant v1 --batch 12' % sc, True),
            ('python "%s/copilot_eval.py" plan --flow .hillclimb/triage --variant v1 --dry-run --approve-harness' % sc, False),
            ('python "%s/copilot_eval.py" batch --flow .hillclimb/triage --variant v1 --batch 1; rm -rf ~' % sc, False),
            ('python "%s/copilot_eval.py" batch --flow .hillclimb/other --variant v1 --batch 1' % sc, False),
            ('python "/tmp/evil/eval-hillclimb/scripts/copilot_eval.py" batch --flow .hillclimb/triage --variant v1 --batch 1', False),
            ('node "%s/build-report-lite.mjs" .hillclimb/triage/' % sc, True),
            ('python "%s/analyzer_slice.py" .hillclimb/triage v3' % sc, True),
            ('python "%s/analyzer_slice.py" .hillclimb/triage v3 && curl x' % sc, False),
        ]),
        "script": (["--runner", "script", "--runner-cmd", "python eval/run_eval.py"], [
            ("python eval/run_eval.py --flow .hillclimb/triage --variant v4 --reps 2 --model gpt-x-2025-01-01", True),
            ("python eval/run_eval.py --flow .hillclimb/triage --variant v4 --ids c1,c2 --dry-run", True),
            ("python eval/run_eval.py --flow .hillclimb/triage --variant v4 --approve-harness", False),
            ("python eval/run_eval.py --flow .hillclimb/triage --variant v4 --dry-run --approve-harness", False),
            ('node "%s/wait-run.mjs" .hillclimb/triage/v4 --max-wait-s 540 --just-launched' % sc, True),
            ('python "%s/wait_run.py" .hillclimb/triage/v4 --expect 40' % sc, True),
        ]),
    }
    for name, (args, probes) in cases.items():
        code, out, err = run([PY, os.path.join(SCRIPTS, "vscode_settings.py"), "--flow", ".hillclimb/triage", "--skill-dir", skill] + args, root)
        rules = list(json.loads(out)["chat.tools.terminal.autoApprove"])
        js = ("const rules=%s.map(k=>new RegExp(k.slice(1,-1)));const probes=%s;"
              "console.log(JSON.stringify(probes.map(c=>rules.some(r=>r.test(c)))));") % (json.dumps(rules), json.dumps([c for c, _ in probes]))
        code2, out2, err2 = run([NODE, "-e", js], root)
        got = json.loads(out2) if code2 == 0 else []
        for (cmdline, want), g in zip(probes, got):
            check(g == want, "%s rule %s: %s" % (name, "matches" if want else "rejects", cmdline.replace(sc, "<skill>/scripts")[:110]))
        check(len(got) == len(probes), "%s rules evaluated (%s)" % (name, err2.strip()[:120]))


def build_both(flow, label):
    """Run both builders on `flow`; return True when scores.tsv and status.md match byte for byte."""
    outs = []
    for cmd in ([[NODE, os.path.join(SCRIPTS, "build-report-lite.mjs"), flow]] if NODE else []) + \
               [[PY, os.path.join(SCRIPTS, "build_report_lite.py"), flow]]:
        code, _, err = run(cmd, os.path.dirname(flow))
        if code != 0:
            check(False, "%s: builder %s failed: %s" % (label, os.path.basename(cmd[1]), err.strip()[-300:]))
            return None
        files = {}
        for name in ("scores.tsv", "status.md"):
            with open(os.path.join(flow, "trajectory", name), "rb") as f:
                files[name] = f.read()
        outs.append(files)
    if len(outs) == 2:
        same = outs[0] == outs[1]
        if not same:
            for name in outs[0]:
                if outs[0][name] != outs[1][name]:
                    print("    node %s:\n%s\n    python %s:\n%s" % (name, outs[0][name].decode()[:1500], name, outs[1][name].decode()[:1500]))
        check(same, "%s: Node and Python builders agree byte for byte" % label)
    return outs[-1]


def synth(root, name, seed, split=True, prices=True, odd=False, copilot=False):
    rnd = random.Random(seed)
    flow = os.path.join(root, name)
    ids = ["case_%02d" % i for i in range(1, 9)] + ["10", "2", "0", "07", "id-\u00e9\U0001F600", "-lead", "=formula"]
    st = {"metrics": [{"id": "acc", "kind": "binary", "label": "Accuracy"}, {"id": "len", "kind": "float", "better": "lower"}]}
    if split:
        st["train_ids"], st["test_ids"] = ids[::2], ids[1::2]
    if prices:
        st["prices"] = {"m-a": {"in": 3, "out": 15}, "m-b": {"in": 0.8, "out": 4, "cache_read": 0.08, "cache_write": 1},
                        "judge": {"in": 0.1, "out": 0.5}}
    if odd:
        st["perf_fields"] = ["latency_s", {"id": "cost_usd", "label": "cost/case"}, {"id": "in_tokens"}, "constructor"]
        st["goal"] = {"target": "acc", "direction": "higher", "hold": ["len", 3]}
        st["best"] = {"round": 2.0, "test_score": 0.6125}
    if copilot:
        st["runner"] = "copilot"
    os.makedirs(flow)
    with open(os.path.join(flow, "_state.json"), "w") as f:
        json.dump(st, f)
    for vi, v in enumerate(["baseline", "v1", "v2", "v10"] if odd else ["baseline", "v1", "v2"]):
        os.makedirs(os.path.join(flow, v, "traces"))
        lines = []
        for pid in ids:
            for rep in range(rnd.choice([1, 2, 3]) if odd else 2):
                r = {"prompt_id": pid, "rep": rep, "prompt": "P " + pid, "tags": [pid[-1]],
                     "grade": {"acc": rnd.random() < 0.4 + 0.1 * vi, "len": rnd.choice([1, 2.5, 0.0625, 1 / 3, 7])},
                     "model": rnd.choice(["m-a", "m-a-20260101", "m-b", "m-a-mini"] if odd else ["m-a", "m-a-20260101", "m-b"]) if not (odd and vi == 3) else "m-zzz",
                     "usage": {"input_tokens": rnd.randint(1, 5000), "output_tokens": rnd.randint(1, 900),
                               "cache_read_input_tokens": rnd.choice([0, 1000, 4096]),
                               "cache_creation_input_tokens": rnd.choice([0, 0, 2048])},
                     "latency_s": round(rnd.uniform(0.2, 30), 3), "tool_calls": rnd.randint(0, 9)}
                if rnd.random() < 0.3:
                    r["judge_model"], r["judge_usage"] = "judge", {"input_tokens": 333, "output_tokens": 77}
                if rnd.random() < 0.08:
                    r["status"] = "truncated"
                if odd and rnd.random() < 0.05:
                    del r["usage"]
                lines.append(json.dumps(r))
        if odd:
            lines.insert(3, "not json")
            lines.insert(5, "null")
            lines.insert(7, json.dumps({"id": 5, "grade": True, "usage": {"input_tokens": 1}, "model": "m-a"}))
        with open(os.path.join(flow, v, "results.jsonl"), "w", encoding="utf-8") as f:
            f.write(("\ufeff" if odd and vi == 1 else "") + "\n".join(lines) + "\n")
        if vi:
            with open(os.path.join(flow, v, "change.md"), "w", encoding="utf-8") as f:
                f.write("\n# Round %d | tighten \u00e9 rule %s\r\n\nwhy\n" % (vi, "x" * (90 if odd else 3)))
        if vi == 2:
            with open(os.path.join(flow, v, "summary.json"), "w") as f:
                json.dump({"description": "from summary", "suspicious": "judge jump | check"}, f)
            if odd:
                os.remove(os.path.join(flow, v, "change.md"))
        if vi == 1:
            with open(os.path.join(flow, v, "errors.jsonl"), "w") as f:
                f.write(json.dumps({"prompt_id": "case_01", "rep": 0, "failure_class": "timeout",
                                    "model": "m-a", "usage": {"input_tokens": 50, "output_tokens": 0}}) + "\n")
                f.write(json.dumps({"prompt_id": "case_02", "rep": 0, "failure_class": "error"}) + "\n")
    return flow


def main():
    if not NODE:
        print("note: node/bun not on PATH - Node checks skipped")
    root = tempfile.mkdtemp(prefix="evalhc-")
    try:
        flows = []
        for kind in (["node"] if NODE else []) + ["python"]:
            work = os.path.join(root, kind)
            os.makedirs(work)
            flows.append(test_runner(kind, work))
        test_settings_rules(root)
        copilot_flow = test_copilot_runner(root)
        print("report builders")
        out = build_both(copilot_flow, "copilot runner output")
        if out:
            st = out["status.md"].decode()
            check("calls/run" in st and "not measured" not in st, "copilot runner output: calls columns, no dollar columns")
        for flow in flows:
            out = build_both(flow, os.path.basename(os.path.dirname(os.path.dirname(flow))) + " runner output")
            if out:
                st = out["status.md"].decode()
                # the served-model mismatch billed 'other-model', which has no price: the
                # builder must say 'not measured', never print a partial sum
                check("not measured" in st.split("\n\n")[0], "runner output: unpriced substituted model -> not measured")
                print("\n".join("    " + l for l in st.splitlines()[:5]))
        print("analyzer slice")
        for flow in flows:
            outs = []
            for cmd in ([[NODE, os.path.join(SCRIPTS, "analyzer-slice.mjs")]] if NODE else []) + \
                       [[PY, os.path.join(SCRIPTS, "analyzer_slice.py")]]:
                code, out, err = run(cmd + [flow, "v1"], os.path.dirname(flow))
                files = {}
                for name in ("results.train.jsonl", "scores.train.tsv", "traces.txt"):
                    with open(os.path.join(flow, "v1", "_analyzer", name), "rb") as f:
                        files[name] = f.read()
                outs.append(files)
                kept = {json.loads(l)["prompt_id"] for l in files["results.train.jsonl"].decode().splitlines()}
                check(code == 0 and "8 train rows" in out and "8 trace files" in out and kept == {"c1", "c2", "c3", "flaky"},
                      "%s: train-only rows and traces (%s)" % (os.path.basename(cmd[1]), out.strip().splitlines()[0] if out else err))
                check(b"case_4" not in files["traces.txt"] and b"c4_" not in files["traces.txt"], "%s: no test trace listed" % os.path.basename(cmd[1]))
            if len(outs) == 2:
                check(outs[0] == outs[1], "Node and Python slices agree")
        syn = os.path.join(root, "synth")
        os.makedirs(syn)
        for i, kw in enumerate([{}, {"split": False}, {"prices": False}, {"odd": True}, {"odd": True, "split": False},
                                {"copilot": True}]):
            for seed in range(3):
                build_both(synth(syn, "f%d_%d" % (i, seed), seed * 7 + i, **kw), "synthetic %s seed %d" % (kw or "default", seed))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\n%s" % ("ALL PASSED" if not FAILS else "%d FAILED:\n  " % len(FAILS) + "\n  ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
