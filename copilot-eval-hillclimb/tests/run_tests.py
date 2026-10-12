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
import json
import os
import random
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
    return os.path.join(work, flow)


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


def synth(root, name, seed, split=True, prices=True, odd=False):
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
                     "model": rnd.choice(["m-a", "m-a-20260101", "m-b"]) if not (odd and vi == 3) else "m-zzz",
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
        print("report builders")
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
        for i, kw in enumerate([{}, {"split": False}, {"prices": False}, {"odd": True}, {"odd": True, "split": False}]):
            for seed in range(3):
                build_both(synth(syn, "f%d_%d" % (i, seed), seed * 7 + i, **kw), "synthetic %s seed %d" % (kw or "default", seed))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\n%s" % ("ALL PASSED" if not FAILS else "%d FAILED:\n  " % len(FAILS) + "\n  ".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
