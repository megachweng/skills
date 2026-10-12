#!/usr/bin/env python3
"""Runner scaffold for the build-eval / hillclimb loop - Python twin of
runner-scaffold.mjs (VS Code Copilot port of the claude-api skill's evals).

Copy this into the user's repo and fill in load_cases / run_case / grade_case
(and, for a non-Anthropic provider, model_matches) below - the I/O shape, file
naming, resume, and CLI surface are already hillclimb-ready, so adding v2, v3,
... is `--variant v3`, not a refactor. The fill-ins may be plain `def` or
`async def`; plain functions run in a worker thread.

    python run_eval.py --flow .hillclimb/<name> --variant baseline --reps 2
    python run_eval.py --flow .hillclimb/<name> --variant v3 --limit 1    (canary / premise probe)
    python run_eval.py --flow .hillclimb/<name> --variant v3 --dry-run    (print resolved scope, spend nothing)

Structural properties this encodes (same as the Node scaffold):
  - parameterized by --variant / --model / --reps (no hardcoded A/B pair)
  - --limit N / --ids a,b select a subset for pilots and canaries without
    editing the harness (their rows count toward the full pass on resume)
  - --dry-run prints cases x reps x model and the (id, rep) still to run
  - rep-aware filenames + resume (traces/<id>_rep<k>.json), idempotent at (case, rep)
  - reads _state.json, never writes it - the ONE exception is --approve-harness
    recording `harness_sha`
  - refuses to run when the harness (this file, a lockfile beside it or in the
    working directory, and _state.json.harness_paths) changed since the sha a
    human last approved with --approve-harness
  - refuses to start while another pass in the same variant dir looks live
  - pairwise graders judge against frozen baseline/ref/<id>.* on disk
  - writes rows as cases complete (crash-safe)
  - jittered exponential backoff on transient 429 / overloaded / 5xx errors
  - hard per-case wall-clock ceiling (--timeout-s)
  - served-model assertion via model_matches()
  - failed attempts land in errors.jsonl with a failure class and, when the call
    completed, the billed model / usage (never in results.jsonl)
  - progress.txt rewritten every 30 s and ended with a 'finished' line, so an
    agent with no completion notification can poll it (wait_run.py)
  - row ids, trace filenames and frozen refs share one path-safe id (the
    original kept in meta.original_id when sanitization changed it) - the same
    id the Node scaffold computes

Standard library only. Python 3.8+.
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import random
import re
import stat
import sys
import time

# --- fill these in -------------------------------------------------------------


def load_cases():
    """Return the list of input cases. Each must have a stable `id`
    (plus `prompt` / `input`, optional `tags`, `attachments`, `meta`)."""
    # e.g. with open("eval/cases.json", encoding="utf-8") as f: return json.load(f)
    raise NotImplementedError("TODO: load_cases")


async def run_case(case, ctx):
    """Run the app on one input - through the app's REAL entry point, not a
    re-implemented model call. ctx["model"] and ctx["variant"] are the CLI args;
    use them to pick the model and (for hillclimb) the harness/prompt under test.

    Return a dict:
      output       the answer (text, or anything the grader reads)
      transcript   Turn[] - see references/data-contract.md
      model        from the RESPONSE, not your config
      usage        normalize_usage(response usage)
      stop_reason  'max_tokens' when the answer was cut off (or set status: 'truncated')
      optional: status, judge_model, judge_usage, attachments, plus perf fields
    """
    # Anthropic:
    #   res = client.messages.create(model=ctx["model"], ...)
    #   return {"output": res.content[-1].text, "transcript": [...], "model": res.model,
    #           "usage": normalize_usage(res.usage.model_dump()), "stop_reason": res.stop_reason}
    # OpenAI / Azure OpenAI (chat.completions):
    #   res = client.chat.completions.create(model=ctx["model"], ...)
    #   ch = res.choices[0]
    #   return {"output": ch.message.content, "transcript": [...], "model": res.model,
    #           "usage": normalize_usage(res.usage.model_dump()),
    #           "stop_reason": "max_tokens" if ch.finish_reason == "length" else ch.finish_reason}
    raise NotImplementedError("TODO: run_case")


async def grade_case(case, run, ref, ctx):
    """Grade one output. For pairwise, `ref` is the FROZEN baseline output read
    from disk (baseline/ref/<id>.*) - never a freshly co-generated one.
    Return {"grade": {metric_id: number, ...}, "explanation"?: {metric_id: str},
            "judge_model"?, "judge_usage"?}.
    If the judge call succeeded but grading still fails (parse error, bad schema),
    raise EvalFailure(..., judge_model=..., judge_usage=...) so the errors sidecar
    keeps the billed judge spend counted."""
    # return {"grade": {"pass": 1 if case["expected"] in run["output"] else 0}}
    raise NotImplementedError("TODO: grade_case")


def perf_from(run):
    """Side-channel perf fields beyond the built-ins (latency_s etc.), e.g. {"tool_calls": n}."""
    return {}


def model_matches(requested, served):
    """True when `served` (the model the RESPONSE names) is an acceptable answer
    to `requested` (--model). The default accepts an exact match or a documented
    alias->snapshot resolution - 'foo-latest' / 'foo-0' / 'foo' served as
    'foo-20250101', 'foo@20250101' or 'foo-2025-01-01'. Adjust for id schemes it
    does not know (an Azure OpenAI deployment name, a gateway alias, Bedrock's
    'anthropic.claude-...-v1:0'). Never return True unconditionally - a silently
    substituted model invalidates the comparison."""
    if served == requested:
        return True
    base = re.sub(r"-latest$|-0$", "", requested)
    if not served.startswith(base):
        return False
    return re.fullmatch(r"[-@](\d{8}|\d{4}-\d{2}-\d{2})", served[len(base):]) is not None


def normalize_usage(u):
    """Map a provider's usage dict onto the shape the report prices:
    {input_tokens (UNCACHED input), output_tokens, cache_read_input_tokens,
     cache_creation_input_tokens}. Anthropic already reports that shape; OpenAI /
    Azure OpenAI and Gemini count cached tokens inside the prompt count, so they
    are split out here and never double-counted."""
    if not isinstance(u, dict):
        return u

    def n(x):
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else 0

    if "prompt_tokens" in u or "completion_tokens" in u:  # OpenAI chat.completions
        cached = n((u.get("prompt_tokens_details") or {}).get("cached_tokens"))
        return {"input_tokens": n(u.get("prompt_tokens")) - cached, "output_tokens": n(u.get("completion_tokens")),
                "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    if "input_tokens_details" in u or "output_tokens_details" in u:  # OpenAI Responses API
        cached = n((u.get("input_tokens_details") or {}).get("cached_tokens"))
        return {"input_tokens": n(u.get("input_tokens")) - cached, "output_tokens": n(u.get("output_tokens")),
                "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    if "promptTokenCount" in u or "candidatesTokenCount" in u:  # Gemini usageMetadata
        cached = n(u.get("cachedContentTokenCount"))
        return {"input_tokens": n(u.get("promptTokenCount")) - cached,
                "output_tokens": n(u.get("candidatesTokenCount")) + n(u.get("thoughtsTokenCount")),
                "cache_read_input_tokens": cached, "cache_creation_input_tokens": 0}
    return u  # Anthropic (already canonical)


# --- harness (you usually won't need to touch below this line) ------------------


class EvalFailure(Exception):
    """An attempt that produced no scorable output. failure_class lands in errors.jsonl."""

    def __init__(self, message, failure_class="error", judge_model=None, judge_usage=None):
        super().__init__(message)
        self.failure_class = failure_class
        self.judge_model = judge_model
        self.judge_usage = judge_usage


WIN = os.name == "nt"
NOFOLLOW = 0 if WIN else getattr(os, "O_NOFOLLOW", 0)
O_BINARY = getattr(os, "O_BINARY", 0)
ESC_SEQ = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def term_safe(s):
    return CONTROL.sub("", ESC_SEQ.sub("", str(s)))


def eprint(*a):
    print(*(term_safe(x) for x in a), file=sys.stderr, flush=True)


def utf8(s):
    """Encode like Node: a lone surrogate becomes U+FFFD."""
    return re.sub("[\ud800-\udfff]", "�", s).encode("utf-8")


def js_str(x):
    """String(x) for the JSON scalars a case id can be (matches the Node scaffold's ids)."""
    if x is None:
        return "null"
    if x is True:
        return "true"
    if x is False:
        return "false"
    if isinstance(x, float) and math.isfinite(x) and x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    return str(x)


def _clean_json(x):
    """JSON.stringify writes NaN / Infinity as null; Python would write invalid JSON."""
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        return {str(k): _clean_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean_json(v) for v in x]
    return x


def first_set(*xs):
    """JavaScript's a ?? b ?? c."""
    return next((x for x in xs if x is not None), None)


def rel(p):
    try:
        return os.path.relpath(p)
    except ValueError:  # another drive on Windows
        return p


def to_json(x, indent=None):
    return json.dumps(_clean_json(x), ensure_ascii=False, indent=indent,
                      separators=None if indent else (",", ":"), default=str)


# Every output write refuses symlinks and hard links and is bound to the flow
# root resolved once at startup: the flow dir is model-influenced, and a
# prompt-injected round can plant `results.jsonl -> ~/.bashrc` where the next
# unattended run would append. POSIX opens O_NOFOLLOW; Windows lstat-refuses
# first. The check and the open are separate lookups, so this stops a planted
# link, not a writer racing the run.
flow_real_root = None


def lstat_or_none(p):
    try:
        return os.lstat(p)
    except FileNotFoundError:
        return None


def is_symlink(p):
    st = lstat_or_none(p)
    return st is not None and stat.S_ISLNK(st.st_mode)


def assert_in_flow(d, what):
    if flow_real_root is None:
        raise RuntimeError("refusing to " + what + ": flow root not resolved yet")
    real = os.path.realpath(d)
    if real != flow_real_root and not real.startswith(flow_real_root + os.sep):
        raise RuntimeError("refusing to " + what + ": " + d + " resolves outside the flow directory")


def open_no_follow(p, flags):
    parent = os.path.dirname(p) or "."
    if is_symlink(parent):
        raise RuntimeError("refusing to open through symlinked directory: " + parent)
    assert_in_flow(parent, "open")
    if WIN and is_symlink(p):
        raise RuntimeError("refusing to open through symlink: " + p)
    fd = os.open(p, flags | NOFOLLOW | O_BINARY, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("refusing to use non-regular file: " + p)
        if st.st_nlink > 1:
            raise RuntimeError("refusing to use " + p + ": it has a second hard link (another name for the same "
                               "file); replace it with a plain copy if it is yours")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd, data):
    view = memoryview(utf8(data))
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError("write made no progress")
        view = view[n:]


def write_file_no_follow(p, data):
    fd = open_no_follow(p, os.O_WRONLY | os.O_CREAT)
    try:
        os.ftruncate(fd, 0)
        _write_all(fd, data)
    finally:
        os.close(fd)


def append_file_no_follow(p, data):
    fd = open_no_follow(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    try:
        _write_all(fd, data)
    finally:
        os.close(fd)


def read_file_no_follow(p):
    fd = open_no_follow(p, os.O_RDONLY)
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


def read_if_present(p):
    """None when the file is absent; any other failure (a planted link included) raises."""
    try:
        return read_file_no_follow(p)
    except FileNotFoundError:
        return None


def mkdir_no_follow(d):
    if is_symlink(d):
        raise RuntimeError("refusing to use symlinked directory: " + d)
    os.makedirs(d, exist_ok=True)
    assert_in_flow(d, "create directory")


REF_EXTS = ["", ".html", ".txt", ".json"]


def path_safe_id(case_id):
    """Same id the Node scaffold computes: [A-Za-z0-9_.-] kept, anything else '_'
    (one per UTF-16 code unit), and ids that changed or run past 129 chars cut to
    120 plus an 8-hex sha256 of the original."""
    raw = js_str(case_id)
    cleaned = "".join(ch if re.match(r"[A-Za-z0-9_.\-]", ch) else ("__" if ord(ch) > 0xFFFF else "_") for ch in raw)
    if cleaned == raw and len(raw) <= 129:
        return raw
    return cleaned[:120] + "-" + hashlib.sha256(utf8(raw)).hexdigest()[:8]


def parse_args(argv):
    ap = argparse.ArgumentParser(description="Eval runner (build-eval / hillclimb).")
    ap.add_argument("--flow", default=".hillclimb/flow")
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--model")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout-s", dest="timeout_s", type=float, default=1800, help="0 = no ceiling")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    ap.add_argument("--dry-run", dest="dry_run", action="store_true")
    ap.add_argument("--approve-harness", dest="approve_harness", action="store_true")
    a = ap.parse_args(argv)
    if not re.fullmatch(r"baseline|v[1-9][0-9]*", a.variant):
        # The report only reads directories named 'baseline' or 'v<N>'.
        ap.error("--variant must be 'baseline' or 'v<N>', got '%s'" % a.variant)
    if (not math.isfinite(a.timeout_s) or a.timeout_s < 0 or a.reps < 1 or a.concurrency < 1
            or (a.limit is not None and a.limit < 1) or (a.ids is not None and not a.ids)):
        ap.error("invalid --timeout-s / --reps / --concurrency / --limit / --ids")
    return a


# Harness integrity gate - see the Node scaffold for the full rationale. A
# change detector, not a security boundary: what bounds an unattended run is the
# terminal auto-approve rule the user grants this exact command line.
LOCKFILES = ["package-lock.json", "bun.lock", "bun.lockb", "yarn.lock", "pnpm-lock.yaml",
             "requirements.txt", "requirements.lock", "poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock"]


def check_harness(state_path, st, approve):
    me = os.path.abspath(__file__)
    listed = [js_str(p) for p in st.get("harness_paths")] if isinstance(st.get("harness_paths"), list) else []
    lockfiles = sorted({os.path.join(d, f) for d in (os.path.dirname(me), os.getcwd()) for f in LOCKFILES
                        if os.path.exists(os.path.join(d, f))})
    paths = sorted({me, *lockfiles, *(os.path.abspath(p) for p in listed)})
    h = hashlib.sha256()
    hashed = []
    for p in paths:
        try:
            with open(p, "rb") as f:
                buf = f.read()
        except OSError as e:
            if p == me:
                raise
            eprint("warning: harness path '%s' not readable (%s) - skipped" % (rel(p), e.__class__.__name__))
            continue
        r = rel(p)
        h.update(utf8(r) + b"\0" + buf + b"\0")
        hashed.append(r)
    sha = h.hexdigest()
    if st.get("harness_sha") == sha:
        return
    if not lockfiles:
        eprint("note: no lockfile beside the runner or in the current directory - dependency changes are outside "
               "the harness sha")
    if approve:
        st["harness_sha"] = sha
        write_file_no_follow(state_path, to_json(st, indent=2) + "\n")
        eprint("harness approved: sha256 %s over %d file(s) recorded in %s" % (sha[:12], len(hashed), state_path))
        return
    if st.get("harness_sha") is None:
        eprint("no approved harness sha in %s (computed %s over: %s)." % (state_path, sha[:12], ", ".join(hashed)))
        eprint("Review the harness, then run once with --approve-harness to record it.")
    else:
        eprint("harness changed since last approved run (files: %s); approved %s, now %s."
               % (", ".join(hashed), js_str(st["harness_sha"])[:12], sha[:12]))
        eprint("Re-run with --approve-harness after reviewing the diff.")
    finish("refused: harness changed or not yet approved (a human runs --approve-harness)", 2)


async def call(fn, *args):
    """Await an async fill-in, or run a plain one in a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, lambda: fn(*args))
    return await res if inspect.isawaitable(res) else res


def _status_of(e):
    for v in (getattr(e, "status_code", None), getattr(e, "status", None),
              getattr(getattr(e, "response", None), "status_code", None)):
        if isinstance(v, int):
            return v
    return None


async def with_backoff(fn, retry, deadline=math.inf, tries=5):
    """Transient provider errors (429 / 529 / 5xx / 'overloaded' / 'rate limit')
    retry with jittered exponential backoff; the retry count lands in the row's
    meta or the errors sidecar, so churn is visible in the data, not just the bill."""
    attempt = 0
    while True:
        if time.time() >= deadline:
            raise EvalFailure("wall-clock ceiling exceeded before attempt", "timeout")
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - classified below
            status = _status_of(e)
            transient = (status in (429, 529) or (status is not None and 500 <= status < 600)
                         or re.search(r"overloaded|rate.?limit", str(e), re.I) is not None)
            if not transient or attempt >= tries - 1:
                raise
            delay = min(60.0, 2.0 ** attempt) * (0.5 + random.random())
            if time.time() + delay >= deadline:
                raise
            retry["count"] += 1
            await asyncio.sleep(delay)
        attempt += 1


progress_path = None
workers_started = False


def finish(what, code):
    """Exit; the last line of progress.txt says the process is gone (wait_run.py polls it)."""
    if progress_path:
        try:
            append_file_no_follow(progress_path, "finished (exit %d): %s\n" % (code, term_safe(what)))
        except Exception:  # noqa: BLE001
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # also abandons worker threads still inside a timed-out call


def refuse(msg):
    eprint(msg)
    sys.exit(2)


async def main(args):
    global flow_real_root, progress_path, workers_started
    args.flow = re.sub(r"(.)[\\/]+$" if WIN else r"(.)/+$", r"\1", args.flow)
    segments = re.split(r"[\\/]", args.flow) if WIN else args.flow.split("/")
    if any(seg in (".", "..") for seg in segments):
        refuse("refusing to run: --flow must not contain '.' or '..' segments, got '%s'" % args.flow)
    vdir = os.path.join(args.flow, args.variant)
    for p in [args.flow, os.path.join(args.flow, "baseline"), vdir, os.path.join(vdir, "traces"),
              os.path.join(vdir, "results.jsonl"), os.path.join(vdir, "errors.jsonl"), os.path.join(vdir, "progress.txt"),
              os.path.join(args.flow, "baseline", "ref"), os.path.join(args.flow, "_state.json")]:
        if is_symlink(p):
            refuse("refusing to run: %s is a symlink (the flow dir must hold regular files)" % p)
    if not os.path.isabs(args.flow):
        walk = ""
        for part in [x for x in segments if x][:-1]:
            walk = os.path.join(walk, part) if walk else part
            if is_symlink(walk):
                refuse("refusing to run: %s is a symlink (ancestor of --flow)" % walk)
    os.makedirs(args.flow, exist_ok=True)
    flow_real_root = os.path.realpath(args.flow)
    mkdir_no_follow(os.path.join(vdir, "traces"))

    # One pass per variant dir at a time (see the Node scaffold).
    prev_path = os.path.join(vdir, "progress.txt")
    prev = read_if_present(prev_path)
    if prev is not None:
        last = (prev.rstrip().split("\n") or [""])[-1]
        age = time.time() - os.lstat(prev_path).st_mtime
        if not last.startswith("finished") and age < 90:
            refuse("refusing to run: another pass looks live in %s (progress.txt updated %ds ago, no 'finished' line). "
                   "Wait for it (wait_run.py), or retry in 90 s if it died." % (vdir, round(age)))
    progress_path = prev_path
    write_file_no_follow(progress_path, "[%s] starting\n" % args.variant)

    # _state.json is READ-ONLY here (the orchestrator owns it).
    state_path = os.path.join(args.flow, "_state.json")
    st = {}
    state_text = read_if_present(state_path)
    if state_text is not None:
        try:
            st = json.loads(state_text) or {}
            if not isinstance(st, dict):
                raise ValueError
        except ValueError:
            eprint("%s exists but is not a valid JSON object - fix it before spending a pass" % state_path)
            finish("refused: invalid _state.json", 2)
    check_harness(state_path, st, args.approve_harness)
    ctx = dict(vars(args), state=st)

    results_path = os.path.join(vdir, "results.jsonl")
    done = set()
    for ln in (read_if_present(results_path) or "").split("\n"):
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
            done.add((js_str(r.get("prompt_id")), r.get("rep")))
        except (ValueError, AttributeError):
            pass

    cases = await call(load_cases)
    seen = {}
    for c in cases:
        k = path_safe_id(c["id"]).lower()
        if k in seen:
            eprint("duplicate case id after sanitization: '%s' collides with '%s'" % (c["id"], seen[k]))
            finish("refused: duplicate case id", 2)
        seen[k] = c["id"]
    safe_ids = {path_safe_id(c["id"]) for c in cases}
    for k in ("train_ids", "val_ids", "test_ids"):
        if st.get(k) is not None and not isinstance(st.get(k), list):
            eprint("_state.json %s must be a list of ids" % k)
            finish("refused: _state.json %s is not a list" % k, 2)
    for sid in (st.get("train_ids") or []) + (st.get("val_ids") or []) + (st.get("test_ids") or []):
        s = js_str(sid)
        if s in safe_ids:
            continue
        if s != path_safe_id(s):
            eprint("_state.json split id '%s' is not a path-safe id - record split ids exactly as they appear in "
                   "results.jsonl's prompt_id" % s)
            finish("refused: split id is not path-safe", 2)
        eprint("note: split id '%s' matches no loaded case (expected for a trimmed subset run)" % s)

    ref_dir = os.path.join(args.flow, "baseline", "ref")
    selected = cases
    if args.ids:
        want = set(args.ids)
        selected = [c for c in cases if js_str(c["id"]) in want or path_safe_id(c["id"]) in want]
        got = {x for c in selected for x in (js_str(c["id"]), path_safe_id(c["id"]))}
        missing = [x for x in args.ids if x not in got]
        if missing:
            eprint("--ids: no such case id(s): " + ", ".join(missing))
            finish("refused: unknown --ids", 2)
    if args.limit is not None:
        selected = selected[:args.limit]
    tasks = [(c, rep) for c in selected for rep in range(args.reps) if (path_safe_id(c["id"]), rep) not in done]
    eprint("[%s] %d of %d (id,rep) to run%s; reps %d; model %s; concurrency %d; flow %s" % (
        args.variant, len(tasks), len(selected) * args.reps,
        " (subset: %d of %d cases)" % (len(selected), len(cases)) if len(selected) != len(cases) else "",
        args.reps, args.model or "(not pinned - served-model check off)", args.concurrency, args.flow))
    if args.dry_run:
        eprint("[%s] dry run - no model called, no rows written" % args.variant)
        finish("dry run", 0)

    errors_path = os.path.join(vdir, "errors.jsonl")
    for p in (results_path, errors_path):
        tail = read_if_present(p)
        if tail and not tail.endswith("\n"):
            append_file_no_follow(p, "\n")

    counters = {"i": 0, "ok": 0, "fail": 0}
    t_start = time.time()

    def progress():
        n_done, total = counters["ok"] + counters["fail"], len(tasks)
        el = time.time() - t_start
        eta = round(el / n_done * (total - n_done)) if n_done else None
        line = "[%s] %d/%d done (%d ok, %d failed), %ds elapsed%s" % (
            args.variant, n_done, total, counters["ok"], counters["fail"], round(el),
            ", ~%ds left" % eta if eta is not None else "")
        eprint(line)
        try:
            write_file_no_follow(progress_path, line + "\n")
        except Exception:  # noqa: BLE001
            pass

    async def one(c, rep):
        safe_id = path_safe_id(c["id"])
        t0 = time.time()
        deadline = t0 + args.timeout_s if args.timeout_s > 0 else math.inf
        app_retry, judge_retry = {"count": 0}, {"count": 0}
        state = {"last_run": None, "row_written": False}

        async def attempt():
            t_attempt = [t0]

            async def app():
                t_attempt[0] = time.time()
                return await call(run_case, c, ctx)

            run = await with_backoff(app, app_retry, deadline)
            if not isinstance(run, dict):
                raise EvalFailure("run_case must return a dict")
            state["last_run"] = run
            latency_s = time.time() - t_attempt[0]
            # Serving identity - the rule lives in model_matches() above.
            if args.model and run.get("model") and not model_matches(args.model, js_str(run["model"])):
                raise EvalFailure("served model %s != requested %s" % (run["model"], args.model), "serving_substitution")
            ref = None
            if args.variant != "baseline":
                for ext in REF_EXTS:
                    try:
                        ref = read_file_no_follow(os.path.join(ref_dir, safe_id) + ext)
                        break
                    except FileNotFoundError:
                        continue
            g = await with_backoff(lambda: call(grade_case, c, run, ref, ctx), judge_retry, deadline)
            if not isinstance(g, dict):
                raise EvalFailure("grade_case must return a dict")
            return run, g, latency_s

        try:
            if args.timeout_s > 0:
                try:
                    run, g, latency_s = await asyncio.wait_for(attempt(), args.timeout_s)
                except asyncio.TimeoutError:
                    raise EvalFailure("%s rep%d: exceeded %gs wall-clock ceiling" % (c["id"], rep, args.timeout_s), "timeout")
            else:
                run, g, latency_s = await attempt()
            meta = c.get("meta")
            if safe_id != js_str(c["id"]) or app_retry["count"] or judge_retry["count"]:
                meta = dict(meta or {})
                if safe_id != js_str(c["id"]):
                    meta["original_id"] = js_str(c["id"])
                if app_retry["count"]:
                    meta["retries"] = app_retry["count"]
                if judge_retry["count"]:
                    meta["judge_retries"] = judge_retry["count"]
            prompt = c.get("prompt")
            if prompt is None:
                prompt = c.get("input")
            if prompt is None:
                prompt = c["id"]
            row = {"prompt_id": safe_id, "rep": rep, "prompt": prompt,
                   "tags": c.get("tags"), "attachments": c.get("attachments"), "meta": meta,
                   "model": run.get("model"), "usage": run.get("usage"), "stop_reason": run.get("stop_reason"),
                   "status": first_set(run.get("status"), "truncated" if run.get("stop_reason") == "max_tokens" else "ok"),
                   "judge_model": first_set(g.get("judge_model"), run.get("judge_model")),
                   "judge_usage": first_set(g.get("judge_usage"), run.get("judge_usage")),
                   "latency_s": latency_s}
            row.update(perf_from(run) or {})
            row["grade"] = g.get("grade")
            row["explanation"] = g.get("explanation")
            append_file_no_follow(results_path, to_json({k: v for k, v in row.items() if v is not None}) + "\n")
            state["row_written"] = True  # the attempt is scored - a later failure must not add an error row
            if run.get("transcript") is not None:
                write_file_no_follow(os.path.join(vdir, "traces", "%s_rep%d.json" % (safe_id, rep)),
                                     to_json(run["transcript"], indent=2))
            if (args.variant == "baseline" and run.get("output") is not None
                    and not any(lstat_or_none(os.path.join(ref_dir, safe_id) + ext) for ext in REF_EXTS)):
                mkdir_no_follow(ref_dir)
                out = run["output"]
                write_file_no_follow(os.path.join(ref_dir, safe_id), out if isinstance(out, str) else to_json(out))
            counters["ok"] += 1
        except Exception as e:  # noqa: BLE001 - every failure is data
            counters["fail"] += 1
            if state["row_written"]:
                eprint("  [%s] %s rep%d scored, but a post-row write failed: %s" % (args.variant, c["id"], rep, e))
                return
            last = state["last_run"] or {}
            err = {"prompt_id": safe_id, "rep": rep,
                   "original_id": js_str(c["id"]) if safe_id != js_str(c["id"]) else None,
                   "failure_class": getattr(e, "failure_class", None) or "error",
                   "error": str(e) or e.__class__.__name__,
                   "retries": app_retry["count"], "judge_retries": judge_retry["count"],
                   "model": last.get("model"), "usage": last.get("usage"),
                   "judge_model": first_set(getattr(e, "judge_model", None), last.get("judge_model")),
                   "judge_usage": first_set(getattr(e, "judge_usage", None), last.get("judge_usage")),
                   "latency_s": time.time() - t0}
            append_file_no_follow(errors_path, to_json({k: v for k, v in err.items() if v is not None}) + "\n")
            eprint("  [%s] %s rep%d FAILED: %s" % (args.variant, c["id"], rep, e))

    async def worker():
        while counters["i"] < len(tasks):
            c, rep = tasks[counters["i"]]
            counters["i"] += 1
            await one(c, rep)

    async def ticker():
        while True:
            await asyncio.sleep(30)
            progress()

    workers_started = True
    progress()
    tick = asyncio.ensure_future(ticker())
    await asyncio.gather(*(worker() for _ in range(max(1, args.concurrency))))
    tick.cancel()
    progress()
    eprint("[%s] done - %d ok, %d failed -> %s" % (args.variant, counters["ok"], counters["fail"], results_path))
    finish("%d ok, %d failed" % (counters["ok"], counters["fail"]), 1 if counters["fail"] else 0)


def entry():
    args = parse_args(sys.argv[1:])
    try:
        asyncio.run(main(args))
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - one sanitized line, not a raw stack
        m = str(e) or e.__class__.__name__
        if workers_started:
            eprint("stopped mid-run (rows already written are kept; re-run to resume): " + m)
            finish("stopped mid-run: " + m, 1)
        msg = m if m.startswith("refusing to ") else "refusing to run: " + m
        eprint(msg)
        finish(msg, 2)


if __name__ == "__main__":
    entry()
