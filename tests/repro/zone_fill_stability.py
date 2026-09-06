"""Fill a board's zones N times and report whether KiCad returns the same polygons.

`plane_fragility.py` rasterizes KiCad's exact fill into the router's A* cost
field, so a fill that varies run to run makes the routed copper vary with it.
This probe measures the fill alone: one fresh `pcbnew.ZONE_FILLER` per run, no
routing, no KiCadRoutingTools engine code.

    python3 tests/repro/zone_fill_stability.py BOARD.kicad_pcb [--runs 16]
    python3 tests/repro/zone_fill_stability.py BOARD.kicad_pcb --pin-cpu

`--pin-cpu` reruns everything under `taskset -c 0`. KiCad's filler is threaded,
so a board that is unstable unpinned and stable on one core is showing a race
in the filler rather than anything about the board.

Exit 0 when every run agreed, 1 when they did not, 2 on a bad invocation.
"""
from __future__ import annotations

import argparse
import collections
import os
import shutil
import subprocess
import sys

_WORKER = """
import sys
import pcbnew
board = pcbnew.LoadBoard(sys.argv[1])
zones = board.Zones()
pcbnew.ZONE_FILLER(board).Fill(zones)
rows = []
for z in zones:
    for layer in z.GetLayerSet().Seq():
        poly = z.GetFilledPolysList(layer)
        if poly is None or poly.OutlineCount() == 0:
            continue
        for i in range(poly.OutlineCount()):
            o = poly.Outline(i)
            n = o.PointCount()
            a = 0.0
            for j in range(n):
                p, q = o.CPoint(j), o.CPoint((j + 1) % n)
                a += p.x * q.y - q.x * p.y
            rows.append("%s/%s#%d verts=%d area=%.4f"
                        % (z.GetNetname(), pcbnew.LayerName(layer), i, n,
                           abs(a) / 2.0 / 1e12))
print(" | ".join(sorted(rows)))
"""


def one_fill(board, worker, pin_cpu):
    argv = [sys.executable, worker, board]
    if pin_cpu:
        taskset = shutil.which('taskset')
        if not taskset:
            return None, "taskset is not installed"
        argv = [taskset, '-c', '0'] + argv
    proc = subprocess.run(argv, capture_output=True, text=True)
    out = [l for l in (proc.stdout or '').splitlines() if '/' in l and 'verts=' in l]
    if proc.returncode != 0 or not out:
        return None, (proc.stderr or proc.stdout or '').strip().splitlines()[-1:]
    return out[-1], None


def measure(board, runs, pin_cpu, worker):
    seen = collections.Counter()
    for _ in range(runs):
        sig, err = one_fill(board, worker, pin_cpu)
        if sig is None:
            print("  fill failed: %s" % (err,))
            return None
        seen[sig] += 1
    return seen


def report(label, seen, runs):
    print("%s: %d distinct fill(s) over %d run(s)" % (label, len(seen), runs))
    if len(seen) < 2:
        return
    per_zone = collections.defaultdict(set)
    for sig in seen:
        for item in sig.split(' | '):
            key = item.split(' ')[0]
            per_zone[key].add(item)
    for key in sorted(per_zone):
        if len(per_zone[key]) > 1:
            print("  %s varies:" % key)
            for item in sorted(per_zone[key]):
                count = sum(c for s, c in seen.items() if item in s)
                print("    %-60s x%d" % (item, count))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument('board')
    ap.add_argument('--runs', type=int, default=16)
    ap.add_argument('--pin-cpu', action='store_true',
                    help="Also measure under taskset -c 0, for the contrast")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.board):
        print("no such board: %s" % a.board, file=sys.stderr)
        return 2
    if a.runs < 2:
        print("--runs must be at least 2", file=sys.stderr)
        return 2

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        worker = os.path.join(tmp, 'fill_once.py')
        with open(worker, 'w') as fh:
            fh.write(_WORKER)

        free = measure(a.board, a.runs, False, worker)
        if free is None:
            return 2
        report("unpinned", free, a.runs)
        if a.pin_cpu:
            pinned = measure(a.board, a.runs, True, worker)
            if pinned is None:
                return 2
            report("pinned to one core", pinned, a.runs)
        return 1 if len(free) > 1 else 0


if __name__ == '__main__':
    sys.exit(main())
