#!/usr/bin/env python3
"""Copilot-native eval runner for the build-eval / hillclimb loop.

Every model call happens inside VS Code Copilot - the system under test (SUT)
and the judge run as subagents (`runSubagent`). This script never calls a
model and never touches the network. It is the bookkeeping half of the runner:
it plans the (case, rep) work, hands each worker subagent a task sheet,
grades what the workers wrote to disk, and writes the same files the script
runner writes (results.jsonl, errors.jsonl, traces/, baseline/ref/,
progress.txt), so the report builders, analyzer-slice and the hillclimb loop
work unchanged. See references/copilot-runner.md for the whole protocol.

  python copilot_eval.py plan       --flow .hillclimb/<flow> --variant v1 [--reps 2] [--batch-size 5]
                                    [--limit N | --ids a,b] [--launcher py] [--dry-run] [--approve-harness]
  python copilot_eval.py batch      --flow ... --variant v1 --batch 3     task sheet for one worker
  python copilot_eval.py judge-prep --flow ... --variant v1 --batch 3     judge sheet (after the SUT replies)
                                    [--rejudge]                         ... for recorded items too (then regrade)
  python copilot_eval.py record     --flow ... --variant v1 --batch 3     grade + write rows (or --id X --rep K)
  python copilot_eval.py regrade    --flow ... --variant v1               re-grade stored replies in place
  python copilot_eval.py progress   --flow ... --variant v1               what is recorded / missing / failed

Configuration lives in <flow>/_state.json (the runner only reads it, except
plan --approve-harness recording harness_sha):

  "runner": "copilot",
  "cases":  "eval/cases.jsonl",        JSON list or JSONL: {id, input|prompt, tags?, meta?, attachments?}
  "gold":   "C:/Users/me/.hillclimb-gold/flow.jsonl",
                                       optional; {id: expected} or rows {id, expected}. Keep it OUTSIDE
                                       the workspace when the SUT has file tools.
  "grader": "eval/grade.py",           optional; defines grade(case, output, expected, ref) returning
                                       {metric: number} or {"grade": {...}, "explanation": {...}}
  "sut":    {"agent": "eval-sut-flow", "model": "GPT-5 mini (copilot)"},
  "judge":  {"agent": "eval-judge", "model": "Claude Sonnet 4.5 (copilot)", "rubric": "eval/rubric.md",
             "mode": "pointwise" | "pairwise", "expected": false},
  "reps": 2, "batch_size": 5, "harness_paths": [...], "harness_sha": "..."

Exit codes: 0 ok; 1 something needs attention (missing replies, judge errors);
2 refused (bad arguments, harness gate, invalid config).

Standard library only. Python 3.8+.
"""

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time

WIN = os.name == "nt"
NOFOLLOW = 0 if WIN else getattr(os, "O_NOFOLLOW", 0)
O_BINARY = getattr(os, "O_BINARY", 0)
ME = os.path.abspath(__file__)
ESC_SEQ = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class Refusal(Exception):
    """Exit 2 with this message."""


class JudgeProblem(Exception):
    """The judge's reply is missing or unusable - re-run the judge for that item."""


def term_safe(s):
    return CONTROL.sub("", ESC_SEQ.sub("", str(s)))


def eprint(*a):
    print(*(term_safe(x) for x in a), file=sys.stderr, flush=True)


def utf8(s):
    return re.sub("[\ud800-\udfff]", "\ufffd", s).encode("utf-8")


def js_str(x):
    if x is None:
        return "null"
    if x is True:
        return "true"
    if x is False:
        return "false"
    if isinstance(x, float) and math.isfinite(x) and x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    return str(x)


def to_json(x, indent=None):
    def clean(v):
        if isinstance(v, float) and not math.isfinite(v):
            return None
        if isinstance(v, dict):
            return {str(k): clean(w) for k, w in v.items()}
        if isinstance(v, (list, tuple)):
            return [clean(w) for w in v]
        return v
    return json.dumps(clean(x), ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"),
                      default=str)


def path_safe_id(case_id):
    """Same id the runner scaffolds compute."""
    raw = js_str(case_id)
    cleaned = "".join(ch if re.match(r"[A-Za-z0-9_.\-]", ch) else ("__" if ord(ch) > 0xFFFF else "_") for ch in raw)
    if cleaned == raw and len(raw) <= 129:
        return raw
    return cleaned[:120] + "-" + hashlib.sha256(utf8(raw)).hexdigest()[:8]


# ---- symlink-safe file access bound to the flow root (as in the scaffolds) ----

FLOW_ROOT = None


def lstat_or_none(p):
    try:
        return os.lstat(p)
    except FileNotFoundError:
        return None


def is_symlink(p):
    st = lstat_or_none(p)
    return st is not None and stat.S_ISLNK(st.st_mode)


def assert_in_flow(d):
    real = os.path.realpath(d)
    if real != FLOW_ROOT and not real.startswith(FLOW_ROOT + os.sep):
        raise Refusal("refusing: " + d + " resolves outside the flow directory")


def open_no_follow(p, flags):
    parent = os.path.dirname(p) or "."
    if is_symlink(parent):
        raise Refusal("refusing to open through symlinked directory: " + parent)
    assert_in_flow(parent)
    if WIN and is_symlink(p):
        raise Refusal("refusing to open through symlink: " + p)
    fd = os.open(p, flags | NOFOLLOW | O_BINARY, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise Refusal("refusing to use " + p + ": not a plain regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_text(p, data, append=False):
    fd = open_no_follow(p, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else 0))
    try:
        if not append:
            os.ftruncate(fd, 0)
        view = memoryview(utf8(data))
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def read_text(p):
    """Contents of a flow file, or None when absent."""
    try:
        fd = open_no_follow(p, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        os.close(fd)


def read_outside(p):
    """A config-named file outside the flow (cases, gold, grader, rubric): regular file, no symlink."""
    st = lstat_or_none(p)
    if st is None:
        raise Refusal("not found: " + p)
    if not stat.S_ISREG(st.st_mode):
        raise Refusal("not a regular file (symlinks refused): " + p)
    with open(p, "rb") as f:
        return f.read().decode("utf-8", "replace")


def mkdirs(d):
    if is_symlink(d):
        raise Refusal("refusing to use symlinked directory: " + d)
    os.makedirs(d, exist_ok=True)
    assert_in_flow(d)


class Lock:
    """Cross-process lock for appends: workers run in parallel, and Windows
    O_APPEND is not atomic across processes."""

    def __init__(self, vdir):
        self.path = os.path.join(vdir, ".lock")

    def __enter__(self):
        deadline = time.time() + 60
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | NOFOLLOW)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                st = lstat_or_none(self.path)
                if st is not None and time.time() - st.st_mtime > 120:
                    try:
                        os.remove(self.path)  # stale lock from a killed process
                    except OSError:
                        pass
                    continue
                if time.time() > deadline:
                    raise Refusal("could not take " + self.path + " for 60 s - another record is stuck?")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---- config ------------------------------------------------------------------

def load_jsonish(text, what):
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except ValueError:
        rows = []
        for i, line in enumerate(text.split("\n")):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    raise Refusal("%s: line %d is not JSON" % (what, i + 1))
        return rows


class Flow:
    def __init__(self, args):
        global FLOW_ROOT
        flow = re.sub(r"(.)[\\/]+$" if WIN else r"(.)/+$", r"\1", args.flow)
        if any(seg in (".", "..") for seg in re.split(r"[\\/]", flow)):
            raise Refusal("--flow must not contain '.' or '..' segments")
        if is_symlink(flow):
            raise Refusal("refusing: " + flow + " is a symlink")
        os.makedirs(flow, exist_ok=True)
        FLOW_ROOT = os.path.realpath(flow)
        self.flow = flow
        self.variant = args.variant
        if not re.fullmatch(r"baseline|v[1-9][0-9]*", self.variant):
            raise Refusal("--variant must be 'baseline' or 'v<N>'")
        self.vdir = os.path.join(flow, self.variant)
        mkdirs(os.path.join(self.vdir, "traces"))
        self.work = os.path.join(self.vdir, "_work")
        mkdirs(self.work)
        self.state_path = os.path.join(flow, "_state.json")
        text = read_text(self.state_path)
        try:
            self.state = json.loads(text) if text else {}
        except ValueError:
            raise Refusal(self.state_path + " is not valid JSON")
        if not isinstance(self.state, dict):
            raise Refusal(self.state_path + " must hold a JSON object")
        st = self.state
        if st.get("runner") not in (None, "copilot"):
            raise Refusal("_state.json runner is %r - this flow is run by a script runner, not copilot_eval.py"
                          % st.get("runner"))
        if not isinstance(st.get("cases"), str):
            raise Refusal('_state.json needs "cases": "<path to the cases file>" (repo-relative or absolute)')
        self.sut = st.get("sut") if isinstance(st.get("sut"), dict) else {}
        self.judge = st.get("judge") if isinstance(st.get("judge"), dict) and st["judge"].get("agent") else None
        if self.judge and self.judge.get("mode", "pointwise") not in ("pointwise", "pairwise"):
            raise Refusal('judge.mode must be "pointwise" or "pairwise"')
        self._cases = None
        self._gold = None

    # files that define the measurement (hashed by the harness gate)
    def harness_files(self):
        st = self.state
        files = [ME, st["cases"]]
        for k in ("grader", "gold"):
            if isinstance(st.get(k), str):
                files.append(st[k])
        if self.judge and isinstance(self.judge.get("rubric"), str):
            files.append(self.judge["rubric"])
        if isinstance(st.get("harness_paths"), list):
            files += [js_str(p) for p in st["harness_paths"]]
        return sorted({os.path.abspath(p) for p in files})

    def harness_sha(self):
        h = hashlib.sha256()
        for p in self.harness_files():
            try:
                rel = os.path.relpath(p)
            except ValueError:
                rel = p
            try:
                with open(p, "rb") as f:
                    buf = f.read()
            except OSError:
                buf = b"<missing>"
            h.update(utf8(rel) + b"\0" + buf + b"\0")
        return h.hexdigest()

    def check_harness(self, approve=False):
        sha = self.harness_sha()
        if self.state.get("harness_sha") == sha:
            return
        if approve:
            self.state["harness_sha"] = sha
            write_text(self.state_path, to_json(self.state, indent=2) + "\n")
            eprint("harness approved: sha256 %s over %d file(s) recorded in %s"
                   % (sha[:12], len(self.harness_files()), self.state_path))
            return
        listing = ", ".join(os.path.relpath(p) if not WIN or os.path.splitdrive(p)[0] == os.path.splitdrive(os.getcwd())[0]
                            else p for p in self.harness_files())
        if self.state.get("harness_sha") is None:
            raise Refusal("no approved harness sha in %s (would cover: %s). The USER reviews the harness and runs "
                          "`plan ... --dry-run --approve-harness` once, from the repo root." % (self.state_path, listing))
        raise Refusal("harness changed since it was approved (files: %s). The USER reviews the diff and re-runs "
                      "`plan ... --dry-run --approve-harness` from the repo root." % listing)

    def cases(self):
        if self._cases is None:
            data = load_jsonish(read_outside(self.state["cases"]), "cases file")
            if isinstance(data, dict) and isinstance(data.get("cases"), list):
                data = data["cases"]
            if not isinstance(data, list) or not all(isinstance(c, dict) and "id" in c for c in data):
                raise Refusal("cases file must be a list (or JSONL) of objects with an id")
            seen = {}
            for c in data:
                k = path_safe_id(c["id"]).lower()
                if k in seen:
                    raise Refusal("duplicate case id after sanitization: %r vs %r" % (c["id"], seen[k]))
                seen[k] = c["id"]
                if c.get("input") is None and c.get("prompt") is None:
                    raise Refusal("case %r has no input / prompt" % c["id"])
            self._cases = {path_safe_id(c["id"]): c for c in data}
        return self._cases

    def gold(self):
        if self._gold is None:
            self._gold = {}
            if isinstance(self.state.get("gold"), str):
                data = load_jsonish(read_outside(self.state["gold"]), "gold file")
                if isinstance(data, dict):
                    self._gold = {path_safe_id(k): v for k, v in data.items()}
                elif isinstance(data, list):
                    for r in data:
                        if isinstance(r, dict) and "id" in r:
                            self._gold[path_safe_id(r["id"])] = r.get("expected")
                else:
                    raise Refusal("gold file must be {id: expected} or rows {id, expected}")
        return self._gold

    def expected(self, sid):
        g = self.gold()
        if sid in g:
            return g[sid]
        return self.cases()[sid].get("expected")

    def item_dir(self, sid, rep):
        return os.path.join(self.work, "%s_rep%d" % (sid, rep))

    def recorded(self):
        done = set()
        for ln in (read_text(os.path.join(self.vdir, "results.jsonl")) or "").split("\n"):
            if ln.strip():
                try:
                    r = json.loads(ln)
                    done.add((js_str(r.get("prompt_id")), int(r.get("rep", 0))))
                except (ValueError, TypeError, AttributeError):
                    pass
        return done

    def plan(self):
        text = read_text(os.path.join(self.work, "plan.json"))
        if not text:
            raise Refusal("no plan for %s - run `plan` first" % self.variant)
        return json.loads(text)

    def launcher(self, plan=None):
        return (plan or {}).get("launcher") or "python"

    def cmd(self, plan, sub, extra=""):
        return '%s "%s" %s --flow %s --variant %s%s' % (self.launcher(plan), ME, sub, self.flow, self.variant, extra)


# ---- grading -------------------------------------------------------------------

def ab_slot(sid, rep, variant):
    """Deterministic A/B position of the candidate for pairwise judging."""
    return "A" if hashlib.sha256(("%s|%d|%s" % (sid, rep, variant)).encode()).digest()[0] % 2 == 0 else "B"


def parse_judge(text):
    """The judge's reply: the outermost JSON object in it."""
    if text is None:
        return None
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        obj = json.loads(text[s:e + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


_GRADER = {}


def run_grader(fl, case, output, ref):
    path = fl.state.get("grader")
    if not isinstance(path, str):
        return {}, {}
    if path not in _GRADER:
        read_outside(path)  # regular file, no symlink
        spec = importlib.util.spec_from_file_location("eval_grader", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, "grade", None)):
            raise Refusal(path + " must define grade(case, output, expected, ref)")
        _GRADER[path] = mod.grade
    res = _GRADER[path](case, output, fl.expected(path_safe_id(case["id"])), ref)
    if isinstance(res, dict) and isinstance(res.get("grade"), dict):
        return res["grade"], res.get("explanation") or {}
    if isinstance(res, dict):
        return res, {}
    if isinstance(res, (bool, int, float)):
        return {"score": float(res)}, {}
    raise ValueError("grader returned %r" % type(res).__name__)


def grade_item(fl, sid, rep, output):
    """-> (grade, explanation, judge_called). Raises JudgeProblem for a bad judge reply; anything
    else raised is a grader problem."""
    case = fl.cases()[sid]
    ref = None
    if fl.variant != "baseline":
        for ext in ("", ".md", ".txt", ".html", ".json"):
            ref = read_text(os.path.join(fl.flow, "baseline", "ref", sid + ext))
            if ref is not None:
                break
    grade, expl = run_grader(fl, case, output, ref)
    grade, expl = dict(grade), dict(expl)
    judge_called = False
    if fl.judge:
        pairwise = fl.judge.get("mode") == "pairwise"
        if pairwise and fl.variant == "baseline":
            grade["win"] = 0.5  # the reference itself: neutral by definition
        else:
            j = parse_judge(read_text(os.path.join(fl.item_dir(sid, rep), "judge.json")))
            if j is None:
                raise JudgeProblem("judge.json missing or not a JSON object")
            judge_called = True
            if pairwise:
                w = str(j.get("winner", "")).strip().upper().replace(" ", "_")
                mine = ab_slot(sid, rep, fl.variant)
                if w not in ("A", "B", "TIE", "BOTH_BAD"):
                    raise JudgeProblem("winner must be A, B, tie or both_bad, got %r" % j.get("winner"))
                grade["win"] = 1.0 if w == mine else 0.0 if w in ("A", "B") else 0.5
                grade["both_bad"] = 1.0 if w == "BOTH_BAD" else 0.0
                if j.get("reason") is not None:
                    expl["win"] = js_str(j.get("reason"))
            else:
                jg = j.get("grade") if isinstance(j.get("grade"), dict) else {
                    k: v for k, v in j.items() if isinstance(v, (int, float, bool))}
                if not jg:
                    raise JudgeProblem("no numeric grade in judge.json")
                grade.update(jg)
                if isinstance(j.get("explanation"), dict):
                    expl.update({k: js_str(v) for k, v in j["explanation"].items()})
    clean = {}
    for k, v in grade.items():
        if isinstance(v, bool):
            v = 1.0 if v else 0.0
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError("grade %r is not a finite number: %r" % (k, v))
        clean[k] = v
    return clean, expl, judge_called


# ---- subcommands -----------------------------------------------------------------

def update_progress(fl):
    try:
        plan = fl.plan()
    except Refusal:
        return
    items = [tuple(x) for b in plan["batches"] for x in b]
    done = fl.recorded()
    n_done = sum(1 for sid, rep in items if (sid, rep) in done)
    n_err = sum(1 for ln in (read_text(os.path.join(fl.vdir, "errors.jsonl")) or "").split("\n") if ln.strip())
    line = "[%s] %d/%d recorded, %d failed attempt(s) logged" % (fl.variant, n_done, len(items), n_err)
    if n_done == len(items):
        line += "\nfinished (exit 0): all planned (case, rep) recorded"
    write_text(os.path.join(fl.vdir, "progress.txt"), line + "\n")


def cmd_plan(fl, a):
    fl.check_harness(approve=a.approve_harness)
    cases = fl.cases()
    reps = a.reps or int(fl.state.get("reps") or 1)
    size = a.batch_size or int(fl.state.get("batch_size") or 5)
    if reps < 1 or size < 1:
        raise Refusal("--reps and --batch-size must be >= 1")
    ids = list(cases)
    if a.ids:
        want = set(a.ids)
        ids = [s for s in ids if s in want or js_str(cases[s]["id"]) in want]
        missing = want - set(ids) - {js_str(cases[s]["id"]) for s in ids}
        if missing:
            raise Refusal("--ids: no such case id(s): " + ", ".join(sorted(missing)))
    if a.limit:
        ids = ids[:a.limit]
    done = fl.recorded()
    batches, cur = [], []
    for sid in ids:  # all reps of a case stay in one batch (see judge-prep)
        todo = [[sid, r] for r in range(reps) if (sid, r) not in done]
        if not todo:
            continue
        if cur and len(cur) + len(todo) > size:
            batches.append(cur)
            cur = []
        cur += todo
    if cur:
        batches.append(cur)
    n = sum(len(b) for b in batches)
    sut = fl.sut.get("agent") or "(default agent)"
    print("[%s] %d of %d (id,rep) to run in %d batch(es) of <=%d; reps %d%s; SUT %s, model %s; judge %s"
          % (fl.variant, n, len(ids) * reps, len(batches), size, reps,
             " (subset: %d of %d cases)" % (len(ids), len(cases)) if len(ids) != len(cases) else "",
             sut, fl.sut.get("model") or "(session default)",
             "none" if not fl.judge else "%s %s, model %s" % (fl.judge.get("agent"), fl.judge.get("mode", "pointwise"),
                                                              fl.judge.get("model") or "(session default)")))
    if fl.judge and fl.judge.get("model") and fl.judge.get("model") == fl.sut.get("model"):
        print("note: the judge model is the SUT model - a model judging its own output is biased; pick another")
    gold = fl.state.get("gold")
    if isinstance(gold, str) and os.path.realpath(gold).startswith(os.path.realpath(os.getcwd()) + os.sep):
        print("note: the gold file is inside the workspace - fine only if the SUT has no file tools")
    if any("expected" in c for c in cases.values()):
        print("note: the cases file carries `expected` - a SUT with file tools could read it; move it to the gold file")
    calls = n * (1 + (1 if fl.judge and not (fl.judge.get("mode") == "pairwise" and fl.variant == "baseline") else 0))
    print("subagent calls this pass: ~%d (SUT %d + judge %d), plus one worker per batch" % (calls, n, calls - n))
    for i, b in enumerate(batches, 1):
        print("batch %d: %s" % (i, " ".join("%s#%d" % (s, r) for s, r in b)))
    if a.dry_run:
        print("dry run - no plan written")
        return 0
    write_text(os.path.join(fl.work, "plan.json"),
               to_json({"variant": fl.variant, "reps": reps, "launcher": a.launcher, "created": time.time(),
                        "batches": batches}, indent=1) + "\n")
    update_progress(fl)
    print("next: one worker subagent per batch, e.g. `%s`" % fl.cmd({"launcher": a.launcher}, "batch", " --batch 1"))
    return 0


def batch_items(fl, plan, k):
    if not 1 <= k <= len(plan["batches"]):
        raise Refusal("--batch must be 1..%d" % len(plan["batches"]))
    return [(s, int(r)) for s, r in plan["batches"][k - 1]]


def cmd_batch(fl, a):
    fl.check_harness()
    plan = fl.plan()
    items = batch_items(fl, plan, a.batch)
    done = fl.recorded()
    sut = fl.sut.get("agent")
    out = ["TASK SHEET - variant %s - batch %d of %d - %d item(s)" % (fl.variant, a.batch, len(plan["batches"]), len(items)),
           "SUT call: runSubagent with %s%s, prompt = the item's input VERBATIM (nothing added, nothing removed)."
           % ('agentName "%s"' % sut if sut else "no agentName (default agent)",
              ', model "%s"' % fl.sut["model"] if fl.sut.get("model") else ""),
           ""]
    todo = 0
    for i, (sid, rep) in enumerate(items, 1):
        d = fl.item_dir(sid, rep)
        head = "--- item %d/%d: %s rep %d ---" % (i, len(items), sid, rep)
        if (sid, rep) in done:
            out += [head, "already recorded - skip", ""]
            continue
        mkdirs(d)
        case = fl.cases()[sid]
        text = js_str(case.get("input") if case.get("input") is not None else case.get("prompt"))
        write_text(os.path.join(d, "input.md"), text)
        reply = os.path.join(os.path.abspath(d), "output.md")
        if read_text(os.path.join(d, "output.md")) is not None:
            out += [head, "reply already on disk (%s) - do NOT call the SUT again; go to the judge step" % reply, ""]
            continue
        todo += 1
        fence = "=" * max(3, max((len(m) for m in re.findall(r"=+", text)), default=0) + 1)
        out += [head,
                "write the SUT's final reply, verbatim, to: " + reply,
                "if the SUT call fails or times out, instead write to %s: first line timeout|error|refusal, then the message"
                % os.path.join(os.path.abspath(d), "error.txt")]
        if case.get("attachments"):
            out.append("attachments the input refers to: " + ", ".join(js_str(x.get("ref") if isinstance(x, dict) else x)
                                                                      for x in case["attachments"]))
        out += ["input (between the %s lines, exclusive):" % fence, fence, text, fence, ""]
    nxt = fl.cmd(plan, "judge-prep" if fl.judge and not (fl.judge.get("mode") == "pairwise" and fl.variant == "baseline")
                 else "record", " --batch %d" % a.batch)
    out.append("%d SUT call(s) to make. Then run: %s" % (todo, nxt))
    print("\n".join(out))
    return 0


def cmd_judge_prep(fl, a):
    fl.check_harness()
    plan = fl.plan()
    if not fl.judge:
        raise Refusal("no judge configured in _state.json - run record")
    pairwise = fl.judge.get("mode") == "pairwise"
    if pairwise and fl.variant == "baseline":
        print("pairwise baseline: no judge calls (the baseline is the reference). Run: " + fl.cmd(plan, "record", " --batch %d" % a.batch))
        return 0
    rubric = fl.judge.get("rubric")
    rubric_abs = os.path.abspath(rubric) if isinstance(rubric, str) else None
    done = fl.recorded()
    out = ["JUDGE SHEET - variant %s - batch %d - mode %s" % (fl.variant, a.batch, "pairwise" if pairwise else "pointwise"),
           'judge call: runSubagent agentName "%s"%s, prompt = the item\'s judge prompt below, verbatim.'
           % (fl.judge["agent"], ', model "%s"' % fl.judge["model"] if fl.judge.get("model") else ""),
           "write each judge reply, verbatim, to the item's judge file.", ""]
    calls, bad = 0, 0
    for sid, rep in batch_items(fl, plan, a.batch):
        if (sid, rep) in done and not a.rejudge:
            continue
        d = fl.item_dir(sid, rep)
        if read_text(os.path.join(d, "output.md")) is None:
            if read_text(os.path.join(d, "error.txt")) is None:
                out += ["--- %s rep %d: no output.md and no error.txt - finish its SUT call first ---" % (sid, rep), ""]
                bad += 1
            continue
        if read_text(os.path.join(d, "judge.json")) is not None and not a.rejudge:
            out += ["--- %s rep %d: judge.json already on disk - skip ---" % (sid, rep), ""]
            continue
        parts = ["Rubric: " + (rubric_abs or "(no rubric file - use the rules in your instructions)"),
                 "Input: " + os.path.abspath(os.path.join(d, "input.md"))]
        exp = fl.expected(sid) if fl.judge.get("expected") else None
        if exp is not None:
            # written only now, after every rep of this case has its reply, and removed by record
            write_text(os.path.join(d, "expected.md"), exp if isinstance(exp, str) else to_json(exp, indent=2))
            parts.append("Expected: " + os.path.abspath(os.path.join(d, "expected.md")))
        if pairwise:
            ref = None
            for ext in ("", ".md", ".txt", ".html", ".json"):
                p = os.path.join(fl.flow, "baseline", "ref", sid + ext)
                if read_text(p) is not None:
                    ref = os.path.abspath(p)
                    break
            if ref is None:
                out += ["--- %s rep %d: no frozen baseline reference - run the baseline first ---" % (sid, rep), ""]
                bad += 1
                continue
            mine = os.path.abspath(os.path.join(d, "output.md"))
            a_path, b_path = (mine, ref) if ab_slot(sid, rep, fl.variant) == "A" else (ref, mine)
            parts += ["Answer A: " + a_path, "Answer B: " + b_path,
                      'Compare the two answers. Reply with only {"winner": "A" | "B" | "tie" | "both_bad", "reason": "..."}.']
        else:
            parts += ["Answer: " + os.path.abspath(os.path.join(d, "output.md")),
                      'Grade the answer. Reply with only {"grade": {...}, "explanation": {...}} as your instructions define.']
        calls += 1
        out += ["--- %s rep %d ---" % (sid, rep), "judge file: " + os.path.abspath(os.path.join(d, "judge.json")),
                "judge prompt:", "\n".join(parts), ""]
    out.append("%d judge call(s) to make (overwrite any existing judge file). Then run: %s"
               % (calls, fl.cmd(plan, "regrade") if a.rejudge else fl.cmd(plan, "record", " --batch %d" % a.batch)))
    print("\n".join(out))
    return 1 if bad else 0


def record_one(fl, sid, rep, a, lines):
    d = fl.item_dir(sid, rep)
    output = read_text(os.path.join(d, "output.md"))
    err = read_text(os.path.join(d, "error.txt"))
    model = getattr(a, "model", None) or fl.sut.get("model") or "copilot (session default)"
    if output is None:
        if err is None:
            lines.append("MISSING %s rep%d: no output.md / error.txt - make its SUT call, then record again" % (sid, rep))
            return False
        first, _, rest = err.strip().partition("\n")
        cls = re.sub(r"[^a-z_]", "", first.strip().lower()) or "error"
        with Lock(fl.vdir):
            write_text(os.path.join(fl.vdir, "errors.jsonl"), to_json({
                "prompt_id": sid, "rep": rep, "failure_class": cls, "error": (rest.strip() or first.strip())[:2000],
                "model": model, "runner": "copilot", "calls": 1}) + "\n", append=True)
        os.replace(os.path.join(d, "error.txt"), os.path.join(d, "error.recorded.%d.txt" % int(time.time() * 1000)))
        lines.append("FAILED %s rep%d: %s (logged to errors.jsonl; the next plan retries it)" % (sid, rep, cls))
        return True
    output = output.rstrip()
    try:
        grade, expl, judged = grade_item(fl, sid, rep, output)
    except JudgeProblem as e:
        lines.append("NOT RECORDED %s rep%d: judge reply unusable (%s) - repeat that item's judge call (same prompt as on "
                     "the judge sheet), overwrite its judge.json, then record again" % (sid, rep, e))
        return False
    except Exception as e:  # noqa: BLE001 - a grader crash is a harness error, not a model failure
        with Lock(fl.vdir):
            write_text(os.path.join(fl.vdir, "errors.jsonl"), to_json({
                "prompt_id": sid, "rep": rep, "failure_class": "grader_error", "error": str(e)[:2000],
                "model": model, "runner": "copilot", "calls": 1}) + "\n", append=True)
        lines.append("GRADER ERROR %s rep%d: %s (reply kept; fix the grader, then `regrade`/`record`)" % (sid, rep, e))
        return False
    case = fl.cases()[sid]
    prompt = js_str(case.get("input") if case.get("input") is not None else case.get("prompt"))
    meta = dict(case["meta"]) if isinstance(case.get("meta"), dict) else {}
    if sid != js_str(case["id"]):
        meta["original_id"] = js_str(case["id"])
    row = {"prompt_id": sid, "rep": rep, "prompt": prompt, "tags": case.get("tags"), "attachments": case.get("attachments"),
           "meta": meta or None, "model": model, "status": "ok", "runner": "copilot",
           "judge_model": (fl.judge.get("model") or "copilot (session default)") if judged else None,
           "calls": 1 + (1 if judged else 0), "grade": grade, "explanation": expl or None}
    trace = [{"role": "system", "content": "SUT: %s via a Copilot subagent, model %s. Only the final reply is captured."
              % (fl.sut.get("agent") or "default agent", model)},
             {"role": "user", "content": prompt}, {"role": "assistant", "content": output}]
    with Lock(fl.vdir):
        if (sid, rep) in fl.recorded():
            lines.append("skip %s rep%d: already recorded" % (sid, rep))
            return True
        write_text(os.path.join(fl.vdir, "results.jsonl"), to_json({k: v for k, v in row.items() if v is not None}) + "\n",
                   append=True)
    write_text(os.path.join(fl.vdir, "traces", "%s_rep%d.json" % (sid, rep)), to_json(trace, indent=2))
    if fl.variant == "baseline":
        ref_dir = os.path.join(fl.flow, "baseline", "ref")
        if not any(lstat_or_none(os.path.join(ref_dir, sid + ext)) for ext in ("", ".md", ".txt", ".html", ".json")):
            mkdirs(ref_dir)
            write_text(os.path.join(ref_dir, sid), output)
    if lstat_or_none(os.path.join(d, "expected.md")):
        os.remove(os.path.join(d, "expected.md"))
    lines.append("recorded %s rep%d: %s" % (sid, rep, ", ".join("%s=%g" % (k, v) for k, v in sorted(grade.items()))))
    return True


def cmd_record(fl, a):
    fl.check_harness()
    if a.batch:
        items = batch_items(fl, fl.plan(), a.batch)
    elif a.id is not None:
        items = [(path_safe_id(a.id), a.rep)]
        if items[0][0] not in fl.cases():
            raise Refusal("no such case id: " + a.id)
    else:
        raise Refusal("record needs --batch K or --id ID --rep K")
    done = fl.recorded()
    lines, ok = [], True
    for sid, rep in items:
        if (sid, rep) in done:
            continue
        ok = record_one(fl, sid, rep, a, lines) and ok
    update_progress(fl)
    print("\n".join(lines) if lines else "nothing to record (all items already recorded)")
    return 0 if ok else 1


def cmd_regrade(fl, a):
    fl.check_harness()
    path = os.path.join(fl.vdir, "results.jsonl")
    with Lock(fl.vdir):
        text = read_text(path) or ""
        out, regraded, changed, kept = [], 0, 0, 0
        for ln in text.split("\n"):
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
                sid, rep = js_str(r.get("prompt_id")), int(r.get("rep", 0))
            except (ValueError, TypeError, AttributeError):
                out.append(ln)  # not ours to fix - keep the line as it was
                kept += 1
                continue
            output = read_text(os.path.join(fl.item_dir(sid, rep), "output.md"))
            if output is None or sid not in fl.cases():
                out.append(ln)
                kept += 1
                continue
            try:
                grade, expl, _ = grade_item(fl, sid, rep, output.rstrip())
            except Exception as e:  # noqa: BLE001
                raise Refusal("regrade stopped at %s rep%d: %s - nothing was rewritten" % (sid, rep, e))
            regraded += 1
            exp_file = os.path.join(fl.item_dir(sid, rep), "expected.md")
            if lstat_or_none(exp_file):
                os.remove(exp_file)  # a --rejudge sheet put it there; it must not outlive grading
            if grade != r.get("grade"):
                changed += 1
            r["grade"], r["explanation"] = grade, expl or None
            out.append(to_json({k: v for k, v in r.items() if v is not None}))
        backup = path + ".bak-%d" % int(time.time())
        write_text(backup, text)
        write_text(path, "".join(x + "\n" for x in out))
    print("[%s] regraded %d row(s), %d changed, %d kept as-is (no stored reply); backup: %s"
          % (fl.variant, regraded, changed, kept, backup))
    return 0


def cmd_progress(fl, a):
    plan = fl.plan()
    items = [(s, int(r)) for b in plan["batches"] for s, r in b]
    done = fl.recorded()
    missing = [(s, r) for s, r in items if (s, r) not in done]
    errs = [json.loads(l) for l in (read_text(os.path.join(fl.vdir, "errors.jsonl")) or "").split("\n") if l.strip()]
    update_progress(fl)
    print("[%s] %d/%d planned (case, rep) recorded; %d failed attempt(s) in errors.jsonl; %ds since plan"
          % (fl.variant, len(items) - len(missing), len(items), len(errs), round(time.time() - plan.get("created", time.time()))))
    if missing:
        by_batch = {}
        for k, b in enumerate(plan["batches"], 1):
            for s, r in b:
                if (s, int(r)) in set(missing):
                    by_batch.setdefault(k, []).append("%s#%s" % (s, r))
        for k, xs in by_batch.items():
            print("  batch %d missing: %s" % (k, " ".join(xs)))
    return 0 if not missing else 1


def main(argv):
    ap = argparse.ArgumentParser(description="Copilot-native eval runner (bookkeeping half).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--flow", required=True)
        p.add_argument("--variant", required=True)
        return p

    p = common(sub.add_parser("plan"))
    p.add_argument("--reps", type=int)
    p.add_argument("--batch-size", dest="batch_size", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--ids", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    p.add_argument("--launcher", default="python", choices=["python", "python3", "py"])
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    p.add_argument("--approve-harness", dest="approve_harness", action="store_true")
    common(sub.add_parser("batch")).add_argument("--batch", type=int, required=True)
    p = common(sub.add_parser("judge-prep"))
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--rejudge", action="store_true")
    p = common(sub.add_parser("record"))
    p.add_argument("--batch", type=int)
    p.add_argument("--id")
    p.add_argument("--rep", type=int, default=0)
    p.add_argument("--model")
    common(sub.add_parser("regrade"))
    common(sub.add_parser("progress"))
    a = ap.parse_args(argv)
    try:
        fl = Flow(a)
        return {"plan": cmd_plan, "batch": cmd_batch, "judge-prep": cmd_judge_prep, "record": cmd_record,
                "regrade": cmd_regrade, "progress": cmd_progress}[a.cmd](fl, a)
    except Refusal as e:
        eprint(str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
