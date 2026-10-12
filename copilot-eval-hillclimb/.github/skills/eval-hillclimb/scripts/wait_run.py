#!/usr/bin/env python3
"""Block until an eval pass started in the background has finished, or until
--max-wait-s passes - whichever is first (Node twin: wait-run.mjs).

    python wait_run.py .hillclimb/<flow>/<variant> [--max-wait-s 540] [--expect N] [--just-launched]

For VS Code agent mode: run it as a sync terminal call with a tool timeout a
little above --max-wait-s. It prints one status line every --interval-s
(default 30 s) and reads only <variant>/progress.txt and the line count of
<variant>/results.jsonl - never transcripts.

Exit 0: progress.txt ends with the runner's 'finished' line.
Exit 3: still running when --max-wait-s ran out - call again.
Exit 4: stalled - no 'finished' line and no progress update for 90 s (the runner
        rewrites progress.txt every 30 s): the process is most likely gone. Re-run
        the same runner command; it resumes.
Exit 2: bad arguments, or no progress.txt (nothing was started there).
--just-launched: first wait (up to 30 s) for progress.txt to be rewritten, so a
        'finished' line left by the previous pass is not mistaken for this one's.
"""
import argparse
import os
import re
import stat
import sys
import time

CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]|\x1b\[[0-?]*[ -/]*[@-~]")


def read(p):
    """Regular files only - never follow a planted link out of the flow dir."""
    try:
        if not stat.S_ISREG(os.lstat(p).st_mode):
            return None
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--max-wait-s", type=float, default=540)
    ap.add_argument("--interval-s", type=float, default=30)
    ap.add_argument("--expect", type=int)
    ap.add_argument("--just-launched", dest="just_launched", action="store_true")
    a = ap.parse_args()
    if a.max_wait_s < 0 or a.interval_s <= 0:
        ap.error("--max-wait-s must be >= 0 and --interval-s > 0")
    t0 = time.time()
    prog_path = os.path.join(a.dir, "progress.txt")

    def mtime():
        try:
            return os.lstat(prog_path).st_mtime
        except OSError:
            return None

    if a.just_launched:
        while (mtime() or 0) < t0 - 2 and time.time() - t0 < 30:
            time.sleep(1)
    while True:
        prog = read(os.path.join(a.dir, "progress.txt"))
        if prog is None:
            print("no progress.txt in %s - was the pass started there?" % a.dir, file=sys.stderr)
            return 2
        lines = CONTROL.sub("", prog).strip().split("\n")
        last = lines.pop() if lines else ""
        n = sum(1 for l in (read(os.path.join(a.dir, "results.jsonl")) or "").split("\n") if l.strip())
        row_info = "rows %d%s" % (n, "/%d" % a.expect if a.expect is not None else "")
        if last.startswith("finished"):
            print("%s | %s | %s" % (lines.pop() if lines else "", last, row_info), flush=True)
            return 0
        waited = time.time() - t0
        age = time.time() - (mtime() or time.time())
        if age > 90:
            print("%s | %s | stalled: no progress update for %ds and no 'finished' line - the runner is most likely "
                  "gone; re-run the same runner command to resume" % (last, row_info, round(age)), flush=True)
            return 4
        print("%s | %s | waited %ds" % (last, row_info, round(waited)), flush=True)
        if waited + a.interval_s > a.max_wait_s:
            print("still running - call wait_run again", flush=True)
            return 3
        time.sleep(a.interval_s)


if __name__ == "__main__":
    sys.exit(main())
