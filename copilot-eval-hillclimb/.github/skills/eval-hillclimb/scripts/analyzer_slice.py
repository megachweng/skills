#!/usr/bin/env python3
"""Prepare the analyzer's train-only inputs for one hillclimb round, without the
outer agent reading any of them (Node twin: analyzer-slice.mjs).

    python analyzer_slice.py .hillclimb/<flow> <variant> [--all]

Writes <flow>/<variant>/_analyzer/:
  results.train.jsonl   the variant's results.jsonl rows whose case is in _state.json.train_ids
  scores.train.tsv      trajectory/scores.tsv header + train rows (run the report builder first)
  traces.txt            absolute paths of the variant's train trace files, one per line
and prints only counts and paths - never row or transcript content. Hand those
three files to the eval-analyzer subagent. --all includes every case (only for a
flow with no held-out split, whose numbers are directional anyway).
Exit 2 when there is no train split (and no --all) or the variant has no rows.
"""
import json
import os
import re
import stat
import sys


def lst(p):
    try:
        return os.lstat(p)
    except OSError:
        return None


def is_file(p):
    st = lst(p)
    return st is not None and stat.S_ISREG(st.st_mode)


def is_dir(p):
    st = lst(p)
    return st is not None and stat.S_ISDIR(st.st_mode)


def read(p):
    """Regular files only: never follow a planted link out of the flow dir."""
    if not is_file(p):
        return ""
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


def js_id(x):
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return "true" if x is True else "false" if x is False else str(x)


def main(argv):
    args = argv[1:]
    use_all = "--all" in args
    pos = [a for a in args if a != "--all"]
    if len(pos) != 2 or not re.fullmatch(r"baseline|v[1-9][0-9]*", pos[1]):
        print("usage: python analyzer_slice.py <flow-dir> <baseline|vN> [--all]", file=sys.stderr)
        return 2
    flow, variant = os.path.abspath(pos[0]), pos[1]
    try:
        state = json.loads(read(os.path.join(flow, "_state.json")) or "{}") or {}
    except ValueError:
        print("_state.json is not valid JSON", file=sys.stderr)
        return 2
    train = {js_id(x) for x in state.get("train_ids") or []} if isinstance(state, dict) else set()
    if not train and not use_all:
        print("no _state.json.train_ids - draw the split first (or pass --all for a no-split flow)", file=sys.stderr)
        return 2

    def keep(i):
        return use_all or i in train

    vdir = os.path.join(flow, variant)
    if not is_dir(vdir):
        print("no variant directory %s/ (or it is a symlink)" % variant, file=sys.stderr)
        return 2
    out = os.path.join(vdir, "_analyzer")
    st = lst(out)
    if st is not None and stat.S_ISLNK(st.st_mode):
        print("refusing: symlinked _analyzer directory", file=sys.stderr)
        return 2
    os.makedirs(out, exist_ok=True)
    if not os.path.realpath(out).startswith(os.path.realpath(flow) + os.sep):
        print("refusing: _analyzer resolves outside the flow", file=sys.stderr)
        return 2

    def write_out(name, text):
        p = os.path.join(out, name)
        s = lst(p)
        if s is not None and (stat.S_ISLNK(s.st_mode) or s.st_nlink > 1):
            print("refusing to overwrite " + p, file=sys.stderr)
            sys.exit(2)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    rows = []
    for line in read(os.path.join(vdir, "results.jsonl")).split("\n"):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            pid = next((r[k] for k in ("prompt_id", "id", "case_id") if r.get(k) is not None), "")
            if keep(js_id(pid)):
                rows.append(line)
    if not rows:
        print("%s/results.jsonl has no %srows" % (variant, "" if use_all else "train "), file=sys.stderr)
        return 2
    write_out("results.train.jsonl", "\n".join(rows) + "\n")

    tsv = [l for l in read(os.path.join(flow, "trajectory", "scores.tsv")).split("\n") if l]
    tsv_rows = [tsv[0]] + [l for l in tsv[1:] if keep(re.sub(r"^'", "", l.split("\t")[0]))] if tsv else []
    write_out("scores.train.tsv", "\n".join(tsv_rows) + "\n" if tsv_rows else "")

    tdir = os.path.join(vdir, "traces")
    traces = []
    if is_dir(tdir):
        for f in sorted(os.listdir(tdir)):
            m = re.fullmatch(r"(.+?)(?:_rep\d+)?\.json", f)
            p = os.path.join(tdir, f)
            if m and keep(m.group(1)) and is_file(p) and lst(p).st_nlink == 1:
                traces.append(os.path.join(os.path.realpath(tdir), f))
    write_out("traces.txt", "\n".join(traces) + ("\n" if traces else ""))

    print("%s: %d %srows, %d scores.tsv rows, %d trace files" % (
        variant, len(rows), "" if use_all else "train ", max(0, len(tsv_rows) - 1), len(traces)))
    print("hand the analyzer: %s, %s, and the paths listed in %s" % (
        os.path.join(out, "results.train.jsonl"), os.path.join(out, "scores.train.tsv"), os.path.join(out, "traces.txt")))
    if not tsv_rows:
        print("note: no trajectory/scores.tsv yet - run the report builder first")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
