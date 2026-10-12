#!/usr/bin/env python3
"""Lite report builder for the build-eval / hillclimb loop - Python twin of
build-report-lite.mjs (VS Code Copilot port of the claude-api skill's evals).

    python build_report_lite.py .hillclimb/<flow>/

Use it when `node` / `bun` are not on PATH. It reads the same on-disk layout
(baseline/, v<N>/, results.jsonl, errors.jsonl, change.md, summary.json,
traces/, _state.json) and writes the same three files:

  report.html            single static page, every string from disk escaped,
                         traces linked by relative path, nothing executed
  trajectory/scores.tsv  per-case mean of the primary metric, one column per
                         variant - byte-identical to the Node builder's
  trajectory/status.md   the hillclimb per-round status table - byte-identical
                         to the Node builder's (see that file's header for
                         what each column means)

Byte-identical output needs JavaScript semantics in a few places, so this file
emulates them on purpose: object key order (integer-like keys first),
String(number), Number.prototype.toFixed rounding, String.prototype.trim's
whitespace set, and plain left-to-right float summation (Python 3.12+ sum()
is compensated and would differ in the last bit). Keep the status-table code
in step with the Node file when editing either.

Standard library only. Python 3.8+.
"""

import html
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

WIN = os.name == "nt"
NOFOLLOW = 0 if WIN else getattr(os, "O_NOFOLLOW", 0)
O_BINARY = getattr(os, "O_BINARY", 0)

# ---- JavaScript-compatible primitives ---------------------------------------

JS_WS = "\t\n\x0b\x0c\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_TRIM = re.compile("^[" + JS_WS + "]+|[" + JS_WS + "]+$")
_HEADING = re.compile("^#+[" + JS_WS + "]*")
_ONE_LINE = re.compile("[\r\n\t]+")
_ARRAY_INDEX = re.compile(r"^(?:0|[1-9][0-9]*)$")


def js_trim(s):
    return _JS_TRIM.sub("", s)


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and _finite(x)


def _finite(x):
    try:
        return math.isfinite(x)
    except OverflowError:  # an int too large for a float
        return False


def num(x):
    """A JSON number as the float JavaScript would hold."""
    return float(x)


def js_truthy(x):
    if x is None or x is False:
        return False
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return x == x and x != 0  # NaN and 0 are falsy
    if isinstance(x, str):
        return x != ""
    return True  # objects and arrays, even empty ones


def js_num_str(x):
    """String(number) for a finite float or int."""
    if isinstance(x, int):
        x = float(x)
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    r = repr(x)
    if "e" in r:
        mant, exp = r.split("e")
        sign = "-" if exp.startswith("-") else "+"
        r = mant + "e" + sign + exp.lstrip("+-").lstrip("0")
    elif abs(x) < 1e-6:
        pass  # repr already uses an exponent here
    return r


def js_str(x):
    """String(x) for JSON values."""
    if x is None:
        return "null"
    if x is True:
        return "true"
    if x is False:
        return "false"
    if isinstance(x, (int, float)):
        if isinstance(x, float) and x != x:
            return "NaN"
        if not _finite(x):
            return "Infinity" if x > 0 else "-Infinity"
        return js_num_str(x)
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return ",".join("" if e is None else js_str(e) for e in x)
    return "[object Object]"


def js_fixed(x, digits):
    """Number.prototype.toFixed: exact binary value, ties away from zero."""
    x = float(x)
    if x == 0:
        x = 0.0  # (-0).toFixed() has no sign
    q = Decimal(1).scaleb(-digits)
    neg = x < 0
    d = Decimal(-x if neg else x).quantize(q, rounding=ROUND_HALF_UP)
    return ("-" if neg else "") + format(d, "f")


def js_keys(d):
    """Object.keys order: array-index keys ascending, then insertion order."""
    idx, rest = [], []
    for k in d:
        if _ARRAY_INDEX.match(k) and int(k) <= 4294967294:
            idx.append(k)
        else:
            rest.append(k)
    idx.sort(key=int)
    return idx + rest


def js_sorted(strings):
    """Array.prototype.sort() default order: UTF-16 code units."""
    return sorted(strings, key=lambda s: s.encode("utf-16-be", "surrogatepass"))


def _reject_constant(name):
    raise ValueError("not JSON: " + name)


def json_parse(text):
    """JSON.parse: NaN / Infinity are not JSON."""
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON nested too deeply")


def js_mean(xs):
    if not xs:
        return None
    s = 0.0
    for x in xs:
        s += x
    return s / len(xs)


def utf8(s):
    """Encode like Node: a lone surrogate becomes U+FFFD."""
    return re.sub("[\ud800-\udfff]", "\ufffd", s).encode("utf-8")


# ---- safe file access (mirrors the Node builder) -----------------------------

ESC_SEQ = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def term_safe(s):
    return CONTROL.sub("", ESC_SEQ.sub("", str(s)))


def eprint(*a):
    print(*(term_safe(x) for x in a), file=sys.stderr)


def is_symlink(p):
    try:
        return stat.S_ISLNK(os.lstat(p).st_mode)
    except FileNotFoundError:
        return False


skipped_hard_links = []


def read_text(p):
    """File contents, or '' for anything missing, non-regular, symlinked or hard-linked."""
    fd = None
    try:
        if WIN and is_symlink(p):
            return ""
        fd = os.open(p, os.O_RDONLY | NOFOLLOW | O_BINARY)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return ""
        if st.st_nlink > 1:
            skipped_hard_links.append(p)
            return ""
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        if fd is not None:
            os.close(fd)


def read_json(p, dflt):
    try:
        return json_parse(read_text(p))
    except ValueError:
        return dflt


def assert_contained(d, root):
    if root is None:
        raise RuntimeError("refusing to write: output root not resolved")
    real = os.path.realpath(d)
    if real != root and not real.startswith(root + os.sep):
        raise RuntimeError("refusing to write outside " + root + ": " + d)


def resolve_output_root(root):
    root = re.sub(r"(.)[\\/]+$" if WIN else r"(.)/+$", r"\1", str(root))
    segments = re.split(r"[\\/]", root) if WIN else root.split("/")
    if any(seg in (".", "..") for seg in segments):
        raise RuntimeError("refusing to write into " + root + ": '.'/'..' path segment")
    if not os.path.isabs(root):
        walk = ""
        for part in [x for x in segments if x][:-1]:
            walk = os.path.join(walk, part) if walk else part
            if is_symlink(walk):
                raise RuntimeError("refusing to write into " + root + ": ancestor " + walk + " is a symlink")
    if is_symlink(root):
        raise RuntimeError("refusing to write into " + root + ": it is a symlink")
    return os.path.realpath(root)


def write_no_follow(p, data, root):
    parent = os.path.dirname(p)
    if is_symlink(parent):
        raise RuntimeError("refusing to write through symlinked directory: " + parent)
    assert_contained(parent, root)
    if WIN and is_symlink(p):
        raise RuntimeError("refusing to write through symlink: " + p)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | NOFOLLOW | O_BINARY, 0o644)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("refusing to write to non-regular file: " + p)
        if st.st_nlink > 1:
            raise RuntimeError("refusing to write to " + p + ": it has a second hard link (another name for "
                               "the same file); replace it with a plain copy if it is yours")
        os.ftruncate(fd, 0)
        view = memoryview(utf8(data))
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        os.close(fd)


def mkdir_no_follow(d, root):
    if is_symlink(d):
        raise RuntimeError("refusing to use symlinked directory: " + d)
    os.makedirs(d, exist_ok=True)
    assert_contained(d, root)


def esc(s):
    return html.escape("" if s is None else js_str(s), quote=True).replace("&#x27;", "&#39;")


def fmt(x):
    return "" if x is None else js_fixed(x, 3)


def tsv_cell(s):
    flat = re.sub(r"[\t\n\r]", " ", js_str(s))
    return "'" + flat if re.match(r"[=+\-@]", flat) else flat


# ---- loading -----------------------------------------------------------------

VARIANT_RE = re.compile(r"^(baseline|v[0-9]+)$")


def discover_variants(flow):
    names = [e.name for e in os.scandir(flow) if e.is_dir(follow_symlinks=False) and VARIANT_RE.match(e.name)]
    return sorted(names, key=lambda n: -1 if n == "baseline" else int(n[1:]))


def coerce_scores(g):
    if isinstance(g, dict):
        out = {}
        for k in js_keys(g):
            v = g[k]
            if is_num(v) or isinstance(v, bool):
                out[k] = num(v)
        return out
    if isinstance(g, bool):
        return {"score": 1.0 if g else 0.0}
    if is_num(g):
        return {"score": num(g)}
    return {}


def first_present(r, keys):
    for k in keys:
        if r.get(k) is not None:
            return r[k]
    return ""


def load_rows(p, warnings, d):
    by_id = {}
    for i, line in enumerate(read_text(p).split("\n")):
        line = js_trim(line)
        if not line:
            continue
        try:
            r = json_parse(line)
        except ValueError:
            warnings.append(d + "/results.jsonl:" + str(i + 1) + ": malformed JSON, skipped")
            continue
        if not isinstance(r, dict):
            warnings.append(d + "/results.jsonl:" + str(i + 1) + ": not a JSON object, skipped")
            continue
        pid = js_str(first_present(r, ("prompt_id", "id", "case_id")))
        if not pid:
            warnings.append(d + "/results.jsonl:" + str(i + 1) + ": no prompt_id, skipped")
            continue
        by_id.setdefault(pid, []).append(r)
    return by_id


def count_errors(vdir):
    total, by_class, rows = 0, {}, []
    for line in read_text(os.path.join(vdir, "errors.jsonl")).split("\n"):
        if not js_trim(line):
            continue
        try:
            e = json_parse(line)
        except ValueError:
            continue
        total += 1
        fc = e.get("failure_class") if isinstance(e, dict) else None
        c = js_str("error" if fc is None else fc)
        by_class[c] = by_class.get(c, 0) + 1
        if isinstance(e, dict):
            rows.append(e)
    return {"total": total, "by_class": by_class, "rows": rows}


def infer_metrics(rows, variants):
    seen = {}
    for v in variants:
        for pid in js_keys(rows[v]):
            for r in rows[v][pid]:
                expl = r.get("explanation") if isinstance(r.get("explanation"), (dict, list)) else {}
                for k, val in coerce_scores(r.get("grade")).items():
                    m = seen.get(k) or {"binary": True, "judge": False}
                    if val != 0 and val != 1:
                        m["binary"] = False
                    if isinstance(expl, dict) and k in expl:
                        m["judge"] = True
                    seen[k] = m
    return [{"id": k, "kind": "judge" if s["judge"] else "binary" if s["binary"] else "float"} for k, s in seen.items()]


def ok_reps(reps):
    return [r for r in reps if r.get("status") is None or r.get("status") == "ok"]


def case_mean(reps, m):
    vals = []
    for r in ok_reps(reps):
        x = coerce_scores(r.get("grade")).get(m)
        if x is not None and is_num(x):
            vals.append(x)
    return js_mean(vals)


def is_int_like(x):
    if isinstance(x, bool):
        return False
    if isinstance(x, int):
        return True
    return isinstance(x, float) and _finite(x) and x.is_integer()


# ---- build ---------------------------------------------------------------------

def build(flow_arg):
    flow = os.path.abspath(flow_arg)
    if not os.path.isdir(flow):
        raise RuntimeError("not a directory: " + flow)
    flow_root = resolve_output_root(flow_arg)
    variants = discover_variants(flow)
    if not variants:
        raise RuntimeError("no variant directories (baseline/, v1/, ...) under " + flow)
    warnings = []
    non_variant = {"trajectory", "attachments", "refs", "ref", "inputs", "out", "target", "__pycache__"}
    for e in os.scandir(flow):
        if (e.is_dir(follow_symlinks=False) and e.name not in variants and e.name not in non_variant
                and not e.name.startswith(".") and not e.name.startswith("_")):
            warnings.append("ignored directory '" + e.name + "/' - variant dirs must be named 'baseline' or "
                            "'v<N>'; put the descriptive name in change.md's first line instead")
    state = read_json(os.path.join(flow, "_state.json"), {})
    if not isinstance(state, dict):
        state = {}

    split_of = {}
    for sp in ("train", "val", "test"):
        ids = state.get(sp + "_ids")
        if ids is None:
            continue
        if not isinstance(ids, list):
            warnings.append("_state.json " + sp + "_ids is not a list - ignored")
            continue
        for pid in ids:
            split_of[js_str(pid)] = sp

    rows = {v: load_rows(os.path.join(flow, v, "results.jsonl"), warnings, v) for v in variants}

    trace_dir_ok = {}
    for v in variants:
        tdir = os.path.join(flow, v, "traces")
        try:
            if is_symlink(os.path.join(flow, v)) or is_symlink(tdir):
                warnings.append(v + "/traces is reached through a symlink - trace links omitted")
                trace_dir_ok[v] = False
            else:
                trace_dir_ok[v] = os.path.isdir(tdir) and os.path.realpath(tdir).startswith(flow_root + os.sep)
        except OSError:
            trace_dir_ok[v] = False

    for i in range(len(variants) - 1, 0, -1):
        v = variants[i]
        if rows[v]:
            continue
        warnings.append(v + ": zero result rows - dropping (run not started or results.jsonl missing/empty)")
        del variants[i]
    if not rows[variants[0]]:
        extra = ""
        if skipped_hard_links:
            extra = (" (not read, second hard link: " + ", ".join(p[len(flow) + 1:] for p in skipped_hard_links)
                     + " - replace with plain copies if they are yours)")
        raise RuntimeError(variants[0] + "/ has no result rows under " + flow + " - nothing to report" + extra)

    header = {}
    for v in variants:
        vdir = os.path.join(flow, v)
        change = next((l for l in read_text(os.path.join(vdir, "change.md")).split("\n") if js_trim(l)), "")
        summary = read_json(os.path.join(vdir, "summary.json"), {})
        if not isinstance(summary, dict):
            summary = {}
        tally = {}
        for pid in js_keys(rows[v]):
            for r in rows[v][pid]:
                if isinstance(r.get("model"), str):
                    tally[r["model"]] = tally.get(r["model"], 0) + 1
        ordered = sorted(((k, tally[k]) for k in js_keys(tally)), key=lambda kv: -kv[1])
        row_model = ordered[0][0] if ordered else ""
        sm = summary.get("model")
        header[v] = {"change": _HEADING.sub("", change, count=1),
                     "model": sm if js_truthy(sm) else row_model,
                     "errors": count_errors(vdir)}

    metrics_cfg = state.get("metrics") if js_truthy(state.get("metrics")) else state.get("criteria")
    declared = []
    for m in metrics_cfg if isinstance(metrics_cfg, list) else []:
        if isinstance(m, str):
            d = {"id": m}
        elif isinstance(m, dict):
            d = {"id": m.get("id"), "kind": m.get("kind"), "label": m.get("label")}
        else:
            d = {"id": None}
        if js_truthy(d["id"]):
            d["id"] = js_str(d["id"])
            declared.append(d)
    metrics = declared if declared else infer_metrics(rows, variants)
    if not metrics:
        metrics.append({"id": "score", "kind": "float"})
    primary = next((m for m in metrics if m.get("kind") == "binary"), metrics[0])["id"]

    ids, seen_id = [], set()
    for v in variants:
        for pid in js_keys(rows[v]):
            if pid not in seen_id:
                seen_id.add(pid)
                ids.append(pid)

    cases = []
    traces_dir = {v: os.path.join(flow, v, "traces") for v in variants}
    for pid in ids:
        first = next((rows[v][pid][0] for v in variants if rows[v].get(pid)), {})
        tags = [js_str(t) for t in first["tags"]] if isinstance(first.get("tags"), list) else []
        per = {}
        for v in variants:
            reps = rows[v].get(pid) or []
            traces = []
            for k, r in enumerate(reps):
                link = _trace_link(flow, v, pid, r.get("rep") if r.get("rep") is not None else k,
                                   trace_dir_ok[v], traces_dir[v])
                if link:
                    traces.append(link)
            per[v] = {"mean": case_mean(reps, primary), "reps": len(reps),
                      "truncated": sum(1 for r in reps if r.get("status") == "truncated"), "traces": traces}
        prompt = first.get("prompt")
        cases.append({"id": pid, "split": split_of.get(pid, ""), "tags": tags,
                      "prompt": js_str("" if prompt is None else prompt), "per": per})

    agg = {}
    for v in variants:
        all_ = [c["per"][v]["mean"] for c in cases if c["per"][v]["mean"] is not None]
        test = [c["per"][v]["mean"] for c in cases if c["split"] == "test" and c["per"][v]["mean"] is not None]
        agg[v] = {"all": js_mean(all_), "n": len(all_), "test": js_mean(test), "n_test": len(test),
                  "truncated": sum(c["per"][v]["truncated"] for c in cases)}

    mkdir_no_follow(os.path.join(flow, "trajectory"), flow_root)
    tsv = ["\t".join(["id", "split"] + variants)]
    for c in cases:
        tsv.append("\t".join([tsv_cell(c["id"]), tsv_cell(c["split"] or "all")]
                             + [fmt(c["per"][v]["mean"]) for v in variants]))
    write_no_follow(os.path.join(flow, "trajectory", "scores.tsv"), "\n".join(tsv) + "\n", flow_root)

    status = build_status(variants, rows, cases, metrics, primary, state, header, warnings)
    write_no_follow(os.path.join(flow, "trajectory", "status.md"), status["md"], flow_root)

    best = None
    b = state.get("best")
    if isinstance(b, dict) and is_int_like(b.get("round")):
        rnd = int(b["round"])
        best = "baseline" if rnd == 0 else "v" + str(rnd)
        if best not in variants:
            best = None

    for p in dict.fromkeys(skipped_hard_links):
        warnings.append(p[len(flow) + 1:] + " has a second hard link - not read; replace it with a plain copy if it is yours")
    page = render(flow, variants, header, agg, cases, primary, [m["id"] for m in metrics], warnings, best, status)
    write_no_follow(os.path.join(flow, "report.html"), page, flow_root)
    return len(variants), len(cases), warnings


SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _trace_link(flow, v, pid, rep, dir_ok, tdir):
    if not dir_ok:
        return None
    if not SAFE_ID.match(pid) or re.match(r"^\.+$", pid):
        return None
    if not is_int_like(rep) or rep < 0:
        return None
    rep = int(rep)
    stems = [pid + "_rep" + str(rep), pid] if rep == 0 else [pid + "_rep" + str(rep)]
    for stem in stems:
        rel = v + "/traces/" + stem + ".json"
        ab = os.path.abspath(os.path.join(flow, rel))
        if os.path.dirname(ab) != tdir:
            return None
        try:
            ls = os.lstat(ab)
        except OSError:
            continue
        if stat.S_ISREG(ls.st_mode) and not ls.st_nlink > 1:
            return {"rep": rep, "rel": rel}
    return None


# ---- status table (mirrors build-report-lite.mjs line for line) ---------------

def ci95(xs):
    if len(xs) < 2:
        return None
    m = js_mean(xs)
    ss = 0.0
    for x in xs:
        ss += (x - m) * (x - m)
    return 1.96 * math.sqrt(ss / (len(xs) - 1)) / math.sqrt(len(xs))


def signed(x):
    return ("+" if x >= 0 else "") + js_fixed(x, 3)


def fmt_usd(x):
    return "$" + (js_fixed(x, 2) if x >= 1 else js_fixed(x, 3) if x >= 0.01 else js_fixed(x, 4))


def one_line(s):
    return js_trim(_ONE_LINE.sub(" ", js_str(s)))


def md_cell(s):
    return one_line(s).replace("|", "\\|")


def clip(s, n):
    return s[:n] + "..." if len(s) > n else s


def price_for(prices, model):
    if not isinstance(prices, dict) or not isinstance(model, str) or not model:
        return None
    key = model if model in prices else None
    if key is None:
        for k in js_keys(prices):
            if k and model.startswith(k) and re.match(r"[-@]", model[len(k):]) and (key is None or len(k) > len(key)):
                key = k
    p = None if key is None else prices[key]
    return p if isinstance(p, dict) and is_num(p.get("in")) and is_num(p.get("out")) else None


def _is_obj(x):
    return isinstance(x, (dict, list))


def cost_of(model, usage, prices):
    if not isinstance(usage, dict):
        return {"no_usage": True}
    p = price_for(prices, model)
    if not p:
        return {"unpriced": js_str("(no model)" if model is None else model)}

    def t(k):
        return num(usage[k]) if is_num(usage.get(k)) else 0.0

    p_in, p_out = num(p["in"]), num(p["out"])
    cr = num(p["cache_read"]) if is_num(p.get("cache_read")) else 0.1 * p_in
    cw = num(p["cache_write"]) if is_num(p.get("cache_write")) else 1.25 * p_in
    return {"usd": (t("input_tokens") * p_in + t("output_tokens") * p_out
                    + t("cache_read_input_tokens") * cr + t("cache_creation_input_tokens") * cw) / 1e6}


def row_cost(r, prices, is_error_row):
    parts = []
    has_judge = js_truthy(r.get("judge_usage")) and _is_obj(r.get("judge_usage"))
    if r.get("usage") is not None or not is_error_row:
        parts.append(cost_of(r.get("model"), r.get("usage"), prices))
    if has_judge:
        parts.append(cost_of(r.get("judge_model"), r.get("judge_usage"), prices))
    usd = 0.0
    for c in parts:
        if c.get("no_usage"):
            return {"no_usage": True}
        if c.get("unpriced") is not None:
            return {"unpriced": c["unpriced"]}
        usd += c["usd"]
    return {"usd": usd}


PERF_LABELS = {"latency_s": "latency", "out_tokens": "out toks", "in_tokens": "in toks",
               "tool_calls": "tool calls", "web_searches": "web searches", "cost_usd": "$/case"}


def perf_value(r, fid, prices):
    u = r.get("usage") if isinstance(r.get("usage"), dict) else None
    if fid == "out_tokens":
        return num(u["output_tokens"]) if u is not None and is_num(u.get("output_tokens")) else None
    if fid == "in_tokens":
        if u is None:
            return None
        t, any_ = 0.0, False
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if is_num(u.get(k)):
                t += num(u[k])
                any_ = True
        return t if any_ else None
    if fid == "cost_usd":
        return row_cost(r, prices, False).get("usd")
    return num(r[fid]) if is_num(r.get(fid)) else None


def fmt_perf(fid, unit, x):
    if fid == "cost_usd" or unit == "$":
        return fmt_usd(x)
    if fid.endswith("tokens"):
        body = js_fixed(x, 0)
    elif fid in ("latency_s", "tool_calls", "web_searches"):
        body = js_fixed(x, 1)
    else:
        body = js_fixed(x, 2)
    if fid == "latency_s" and unit is None:
        return body + "s"
    return body + (js_str(unit) if js_truthy(unit) and unit != "$" else "")


def build_status(variants, rows, cases, metrics, primary, state, header, warnings):
    prices = state.get("prices") if isinstance(state.get("prices"), dict) else None

    def label_of(m):
        lab = m.get("label")
        return lab if isinstance(lab, str) and lab else m["id"]

    splits = [sp for sp in ("test", "val", "train") if any(c["split"] == sp for c in cases)]
    head = "test" if "test" in splits else None

    def in_head(c):
        return True if head is None else c["split"] == head

    def case_means(v, m, pred):
        out = []
        for c in cases:
            if pred(c):
                x = c["per"][v]["mean"] if m == primary else case_mean(rows[v].get(c["id"]) or [], m)
                if x is not None and is_num(x):
                    out.append((c["id"], x))
        return out

    def paired(v, m):
        base = dict(case_means(variants[0], m, in_head))
        d = [x - base[cid] for cid, x in case_means(v, m, in_head) if cid in base]
        if not d:
            return None
        mu, h = js_mean(d), ci95(d)
        return {"mu": mu, "h": h, "n": len(d), "sig": h is not None and (mu - h > 0 or mu + h < 0)}

    perf_decl = None
    if isinstance(state.get("perf_fields"), list):
        perf_decl = []
        for f in state["perf_fields"]:
            if isinstance(f, str):
                d = {"id": f}
            elif isinstance(f, dict) and isinstance(f.get("id"), str):
                d = {"id": f["id"], "label": f.get("label"), "unit": f.get("unit")}
            else:
                continue
            if d["id"]:
                perf_decl.append(d)
    candidates = perf_decl if perf_decl is not None else [{"id": "latency_s"}, {"id": "out_tokens"}, {"id": "tool_calls"}]
    perf = [f for f in candidates if any(perf_value(r, f["id"], prices) is not None
                                         for v in variants for pid in js_keys(rows[v]) for r in ok_reps(rows[v][pid]))]

    def perf_mean(v, fid):
        xs = []
        for pid in js_keys(rows[v]):
            for r in ok_reps(rows[v][pid]):
                x = perf_value(r, fid, prices)
                if x is not None:
                    xs.append(x)
        return js_mean(xs)

    unpriced = set()

    def run_cost(v):
        usd, no_usage, ok = 0.0, 0, True
        for pid in js_keys(rows[v]):
            for r in rows[v][pid]:
                c = row_cost(r, prices, False)
                if c.get("no_usage"):
                    no_usage += 1
                    ok = False
                elif c.get("unpriced") is not None:
                    unpriced.add(c["unpriced"])
                    ok = False
                else:
                    usd += c["usd"]
        for e in header[v]["errors"]["rows"]:
            if not (js_truthy(e.get("usage")) and _is_obj(e.get("usage"))) and \
                    not (js_truthy(e.get("judge_usage")) and _is_obj(e.get("judge_usage"))):
                continue
            c = row_cost(e, prices, True)
            if c.get("unpriced") is not None:
                unpriced.add(c["unpriced"])
                ok = False
            elif not c.get("no_usage"):
                usd += c["usd"]
        if no_usage:
            warnings.append(v + ": " + str(no_usage) + " result row(s) carry no usage - $/run not measured "
                            "(the runner must record usage on every row)")
        return usd if ok else None

    cols = ["variant", "change"]
    if head is None:
        cols += ["all " + primary, "\u0394 vs baseline"]
    else:
        cols += ["test " + primary, "\u0394 test vs baseline"]
        cols += [sp + " " + primary for sp in splits if sp != "test"]
    others = [m for m in metrics if m["id"] != primary]
    cols += [label_of(m) + ("" if head is None else " (test)") for m in others]
    for f in perf:
        lab = f.get("label")
        cols.append(lab if isinstance(lab, str) and lab else PERF_LABELS.get(f["id"], f["id"]))
    cols += ["$/run", "spend", "not scored"]

    base_vals = {}
    spend, spend_ok = 0.0, True
    body = []
    for vi, v in enumerate(variants):
        cells = [v, "(baseline)" if vi == 0 and not header[v]["change"] else clip(one_line(header[v]["change"]), 80)]

        def split_cell(pred):
            xs = [x for _, x in case_means(v, primary, pred)]
            mu = js_mean(xs)
            if mu is None:
                return "-"
            h = ci95(xs)
            return js_fixed(mu, 3) + (" \u00b1" + js_fixed(h, 3) if h is not None else "") + " (n=" + str(len(xs)) + ")"

        def delta_cell():
            if vi == 0:
                return "-"
            p = paired(v, primary)
            if not p:
                return "-"
            if p["h"] is None:
                return signed(p["mu"]) + " (n=" + str(p["n"]) + ")"
            return (signed(p["mu"]) + " [" + signed(p["mu"] - p["h"]) + ", " + signed(p["mu"] + p["h"]) + "]"
                    + (" *" if p["sig"] else ""))

        cells += [split_cell(in_head), delta_cell()]
        if head is not None:
            for sp in splits:
                if sp != "test":
                    cells.append(split_cell(lambda c, sp=sp: c["split"] == sp))
        for m in others:
            mu = js_mean([x for _, x in case_means(v, m["id"], in_head)])
            if mu is None:
                cells.append("-")
                continue
            p = None if vi == 0 else paired(v, m["id"])
            cells.append(js_fixed(mu, 3) + (" (" + signed(p["mu"]) + ("*" if p["sig"] else "") + ")" if p else ""))
        for f in perf:
            x = perf_mean(v, f["id"])
            if vi == 0:
                base_vals[f["id"]] = x
            if x is None:
                cells.append("-")
                continue
            b = base_vals.get(f["id"])
            ratio = " (" + js_fixed(x / b, 1) + "\u00d7)" if vi > 0 and b is not None and b > 0 else ""
            cells.append(fmt_perf(f["id"], f.get("unit"), x) + ratio)
        cost = run_cost(v)
        if cost is None:
            spend_ok = False
        else:
            spend += cost
        cells += ["not measured" if cost is None else fmt_usd(cost), fmt_usd(spend) if spend_ok else "not measured"]
        e = header[v]["errors"]["total"]
        t = sum(c["per"][v]["truncated"] for c in cases)
        cells.append(", ".join(x for x in [str(e) + " failed" if e else "", str(t) + " truncated" if t else ""] if x)
                     if e or t else "0")
        body.append(cells)
    if prices is None:
        warnings.append("no _state.json.prices - $/run and spend not measured; record the rate card "
                        "(USD per million tokens) as prices: {model_id: {in, out}}")
    elif unpriced:
        warnings.append("no price in _state.json.prices for: " + ", ".join(js_sorted(unpriced))
                        + " - $/run and spend not measured")

    md = ["| " + " | ".join(md_cell(c) for c in cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    md += ["| " + " | ".join(md_cell(c) for c in r) + " |" for r in body]
    notes = []
    notes.append("No held-out split in _state.json: every number is over all cases - directional, not a held-out result."
                 if head is None else
                 "Headline: test split. \u0394 = paired per-case delta vs baseline with 95% CI; * = CI excludes 0 "
                 "(outside noise). Other metrics: test mean (paired \u0394, * outside noise).")
    notes.append('Means are per case over status-ok reps, then over cases; failed and truncated attempts are '
                 'counted in "not scored", never in the means.')
    notes.append("$/run prices every billed call in the pass (app + judge + failed attempts with usage) from "
                 "_state.json.prices; cache reads at 0.1x input and cache writes at 1.25x input unless the price "
                 "entry sets cache_read / cache_write.")
    g = state.get("goal")
    if js_truthy(g) and _is_obj(g):
        g = g if isinstance(g, dict) else {}
        target = g.get("target")
        direction = g.get("direction")
        hold = g.get("hold")
        notes.append("Goal: " + one_line("?" if target is None else target) + " ("
                     + one_line("higher" if direction is None else direction) + " is better)"
                     + ("; hold: " + ", ".join(one_line(x) for x in hold) if isinstance(hold, list) and hold else ""))
    b = state.get("best")
    if isinstance(b, dict) and is_int_like(b.get("round")):
        rnd = int(b["round"])
        bv = "baseline" if rnd == 0 else "v" + str(rnd)
        notes.append("Best so far (_state.json): " + bv
                     + (" (test " + js_fixed(b["test_score"], 3) + ")" if is_num(b.get("test_score")) else ""))
    return {"md": "\n".join(md) + "\n\n" + "\n".join("- " + n for n in notes) + "\n",
            "cols": cols, "body": body, "notes": notes}


# ---- HTML ----------------------------------------------------------------------

CSS = "".join([
    ":root{color-scheme:light dark;--fg:#1a1a1a;--bg:#fff;--muted:#666;--line:#ddd;--pill:#eee;--warn:#b45309;--best:#ecfdf5}",
    "@media(prefers-color-scheme:dark){:root{--fg:#e6e6e6;--bg:#121212;--muted:#9a9a9a;--line:#333;--pill:#2a2a2a;--warn:#f59e0b;--best:#0f2a1f}}",
    "body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:24px;max-width:1400px}",
    "h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}.meta,.hint{color:var(--muted);margin:0 0 8px}",
    "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}",
    "th{cursor:pointer;user-select:none;white-space:nowrap}th.asc:after{content:\" \\25B4\"}th.desc:after{content:\" \\25BE\"}",
    "td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}td.id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}",
    ".pill{display:inline-block;background:var(--pill);border-radius:10px;padding:0 8px;font-size:12px}.pill.warn{color:var(--warn)}",
    "tr.best td{background:var(--best)}.links{font-size:12px}.links a{margin-right:6px}",
    "td.prompt{max-width:520px}details summary{cursor:pointer;color:var(--muted)}pre{white-space:pre-wrap;font-size:12px;margin:6px 0 0}",
    ".warnings{color:var(--warn)}code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}",
])

SORT_JS = "".join([
    'document.querySelectorAll("th[data-k]").forEach(function(th){th.addEventListener("click",function(){',
    'var t=th.closest("table"),tb=t.tBodies[0],i=Array.prototype.indexOf.call(th.parentNode.children,th);',
    'var asc=!th.classList.contains("asc");t.querySelectorAll("th").forEach(function(h){h.classList.remove("asc","desc")});',
    'th.classList.add(asc?"asc":"desc");var num=th.dataset.k==="n";',
    'var rows=Array.prototype.slice.call(tb.rows);rows.sort(function(a,b){var x=a.cells[i],y=b.cells[i];',
    'var u=num?parseFloat("v" in x.dataset?x.dataset.v:x.textContent):x.textContent.trim().toLowerCase();',
    'var w=num?parseFloat("v" in y.dataset?y.dataset.v:y.textContent):y.textContent.trim().toLowerCase();',
    'if(num){if(isNaN(u))u=-Infinity;if(isNaN(w))w=-Infinity}return (u<w?-1:u>w?1:0)*(asc?1:-1)});',
    'rows.forEach(function(r){tb.appendChild(r)})})});',
])


def render(flow, variants, header, agg, cases, primary, metrics, warnings, best, status):
    has_split = any(c["split"] for c in cases)
    has_test = any(agg[v]["n_test"] > 0 for v in variants)

    def th(label, key):
        return '<th data-k="' + esc(key) + '">' + esc(label) + "</th>"

    variant_rows = []
    for v in variants:
        h, a = header[v], agg[v]
        errs = h["errors"]
        err = (str(errs["total"]) + " (" + ", ".join(esc(k) + " " + str(n) for k, n in errs["by_class"].items()) + ")"
               if errs["total"] else "0")
        variant_rows.append(
            "<tr" + (' class="best"' if best == v else "") + "><td>" + esc(v)
            + (' <span class="pill">best</span>' if best == v else "") + "</td>"
            + "<td>" + esc(h["change"]) + "</td><td>" + esc(h["model"]) + "</td>"
            + '<td class="num">' + fmt(a["all"]) + '</td><td class="num">' + str(a["n"]) + "</td>"
            + ('<td class="num">' + fmt(a["test"]) + '</td><td class="num">' + str(a["n_test"]) + "</td>" if has_test else "")
            + '<td class="num">' + str(a["truncated"]) + "</td><td>" + err + "</td></tr>")

    case_rows = []
    for c in cases:
        cells = []
        for v in variants:
            p = c["per"][v]
            links = " ".join('<a href="' + esc(t["rel"]) + '">rep' + esc(t["rep"]) + "</a>" for t in p["traces"])
            cells.append('<td class="num" data-v="' + fmt(p["mean"]) + '">' + fmt(p["mean"])
                         + (' <span class="pill warn">' + str(p["truncated"]) + " truncated</span>" if p["truncated"] else "")
                         + ('<div class="links">' + links + "</div>" if links else "") + "</td>")
        prompt = c["prompt"][:600] + "..." if len(c["prompt"]) > 600 else c["prompt"]
        case_rows.append(
            '<tr><td class="id">' + esc(c["id"]) + "</td>"
            + ("<td>" + esc(c["split"]) + "</td>" if has_split else "")
            + "<td>" + " ".join('<span class="pill">' + esc(t) + "</span>" for t in c["tags"]) + "</td>"
            + "".join(cells)
            + '<td class="prompt"><details><summary>' + esc(prompt.split("\n")[0][:80]) + "</summary><pre>"
            + esc(prompt) + "</pre></details></td></tr>")

    warn = ('<section class="warnings"><h2>Warnings</h2><ul>' + "".join("<li>" + esc(w) + "</li>" for w in warnings)
            + "</ul></section>") if warnings else ""
    name = re.split(r"[\\/]", flow)[-1]
    status_html = ('<section><h2>Status</h2><p class="hint">Same table as <code>trajectory/status.md</code>.</p>'
                   "<table><thead><tr>" + "".join("<th>" + esc(c) + "</th>" for c in status["cols"]) + "</tr></thead><tbody>"
                   + "\n".join("<tr>" + "".join("<td" + (' class="num"' if i > 1 else "") + ">" + esc(c) + "</td>"
                                                for i, c in enumerate(r)) + "</tr>" for r in status["body"])
                   + '</tbody></table><ul class="hint">' + "".join("<li>" + esc(n) + "</li>" for n in status["notes"])
                   + "</ul></section>")
    others = [m for m in metrics if m != primary]
    return ("<!DOCTYPE html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            + '<meta name="viewport" content="width=device-width, initial-scale=1">'
            + "<title>" + esc(name) + " - eval report</title>"
            + "<style>" + CSS + "</style></head><body>"
            + "<header><h1>" + esc(name) + "</h1>"
            + '<p class="meta">' + str(len(variants)) + " variant" + ("" if len(variants) == 1 else "s")
            + " &middot; " + str(len(cases)) + " cases"
            + " &middot; primary metric <code>" + esc(primary) + "</code>"
            + (" (also: " + ", ".join(esc(m) for m in others) + ")" if len(metrics) > 1 else "")
            + " &middot; generated " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " UTC"
            + " &middot; lite report (no transcripts inlined; click rep links)</p></header>"
            + status_html
            + "<section><h2>Variants</h2><table><thead><tr>" + th("variant", "s") + th("change", "s") + th("model", "s")
            + th("mean " + primary, "n") + th("cases", "n")
            + (th("test mean", "n") + th("test cases", "n") if has_test else "")
            + th("truncated", "n") + th("failed attempts", "s") + "</tr></thead><tbody>" + "\n".join(variant_rows)
            + "</tbody></table></section>"
            + '<section><h2>Cases</h2><p class="hint">Click a column header to sort. Per-case values are the mean of <code>'
            + esc(primary) + "</code> over status-ok reps.</p>"
            + '<table id="cases"><thead><tr>' + th("id", "s") + (th("split", "s") if has_split else "") + th("tags", "s")
            + "".join(th(v, "n") for v in variants) + th("prompt", "s") + "</tr></thead><tbody>" + "\n".join(case_rows)
            + "</tbody></table></section>"
            + warn
            + "<script>" + SORT_JS + "</script></body></html>\n")


def main(argv):
    arg = argv[1] if len(argv) > 1 else None
    if not arg or arg in ("-h", "--help"):
        eprint("usage: python build_report_lite.py <flow-dir>   (e.g. .hillclimb/<flow>/)")
        return 0 if arg else 2
    try:
        n_variants, n_cases, warnings = build(arg)
    except Exception as e:  # noqa: BLE001 - one sanitized line, like the Node builder
        eprint("build_report_lite: " + str(e))
        return 1
    for w in warnings:
        eprint("warning: " + w)
    eprint("wrote " + os.path.join(os.path.abspath(arg), "report.html") + " (" + str(n_variants) + " variants, "
           + str(n_cases) + " cases), trajectory/scores.tsv and trajectory/status.md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
