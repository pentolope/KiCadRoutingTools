"""Run one routing/placement command N times and compare the boards it writes.

Each run gets its own output directory, because route.py reads back the
sibling .kicad_pro it wrote last time. The comparison is on canonicalized
copper -- segments, vias, zone outlines, footprint poses, rounded and sorted
-- so per-run UUIDs do not register as a difference.

    python3 tests/repro/determinism_probe.py --runs 4 --out-dir /tmp/probe -- \
        python3 py_router/route.py tests/repro/toy_pcba.kicad_pcb {out}

Exit 0 when every run agreed, 1 when they did not, 2 on a bad invocation or a
failed run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, 'py_router'), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BALLAST_RUNNER = os.path.join(_HERE, 'ballast_run.py')


def canonical(board_path: str):
    """The board's copper and placement, with nothing per-run in it."""
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board_path)
    segs = sorted((s.net_id, s.layer, round(s.start_x, 6), round(s.start_y, 6),
                   round(s.end_x, 6), round(s.end_y, 6), round(s.width, 6))
                  for s in pcb.segments)
    vias = sorted((v.net_id, round(v.x, 6), round(v.y, 6), round(v.size, 6),
                   round(v.drill, 6)) for v in pcb.vias)
    parts = sorted((ref, round(fp.x, 6), round(fp.y, 6), round(fp.rotation, 6),
                    fp.layer)
                   for ref, fp in (pcb.footprints or {}).items())
    zones = sorted((z.net_name, z.layer, z.priority,
                    tuple((round(x, 6), round(y, 6)) for x, y in z.polygon))
                   for z in (pcb.zones or []))
    return {'segments': segs, 'vias': vias, 'footprints': parts, 'zones': zones}


def describe(a, b) -> list:
    """What changed between two canonical boards, as report lines."""
    lines = []
    for key in ('segments', 'vias', 'footprints', 'zones'):
        sa, sb = set(a[key]), set(b[key])
        only_a, only_b = sa - sb, sb - sa
        if not only_a and not only_b:
            lines.append("  %-10s identical (%d)" % (key, len(sa)))
            continue
        lines.append("  %-10s %d vs %d objects; %d only in A, %d only in B"
                     % (key, len(a[key]), len(b[key]), len(only_a), len(only_b)))
        if key in ('segments', 'vias'):
            nets_a = {t[0] for t in only_a}
            nets_b = {t[0] for t in only_b}
            lines.append("             net ids touched: %s"
                         % sorted(nets_a | nets_b))
        for item in sorted(only_a)[:3]:
            lines.append("             only in A: %s" % (item,))
        for item in sorted(only_b)[:3]:
            lines.append("             only in B: %s" % (item,))
    return lines


def run_once(cmd, out_board, env, ballast_n, cwd):
    os.makedirs(os.path.dirname(out_board), exist_ok=True)
    argv = [out_board if tok == '{out}' else tok.replace('{out}', out_board)
            for tok in cmd]
    if not any('{out}' in tok for tok in cmd):
        argv = list(cmd) + [out_board]
    if ballast_n is not None:
        argv = [argv[0], BALLAST_RUNNER, str(ballast_n)] + argv[1:]
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    return argv, proc


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run a command N times and compare the boards it writes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument('--runs', type=int, default=4, help="How many runs (default 4)")
    ap.add_argument('--out-dir', required=True,
                    help="Directory for the per-run output directories")
    ap.add_argument('--hash-seed', default=None,
                    help="'vary' for a different PYTHONHASHSEED per run, or a "
                         "fixed value to pin it (default: leave it alone, so "
                         "Python randomizes it per process)")
    ap.add_argument('--ballast', action='store_true',
                    help="Allocate a different number of live objects per run "
                         "before the tool imports anything, shifting every id()")
    ap.add_argument('--project', default=None,
                    help="A .kicad_pro to copy beside each run's output board "
                         "(route.py reads its DRC floor from there)")
    ap.add_argument('--keep-going', action='store_true',
                    help="Compare the runs that did succeed even if one failed")
    ap.add_argument('command', nargs=argparse.REMAINDER,
                    help="-- then the command; {out} is that run's board")
    args = ap.parse_args(argv)

    cmd = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not cmd:
        ap.error("no command given (put it after '--')")
    if args.runs < 2:
        ap.error("--runs must be at least 2")

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    cwd = _ROOT

    boards, failures = [], []
    for i in range(args.runs):
        run_dir = os.path.join(out_dir, 'run%d' % i)
        shutil.rmtree(run_dir, ignore_errors=True)
        os.makedirs(run_dir)
        out_board = os.path.join(run_dir, 'board.kicad_pcb')
        if args.project:
            shutil.copy2(args.project, out_board.replace('.kicad_pcb', '.kicad_pro'))
        env = dict(os.environ)
        if args.hash_seed == 'vary':
            env['PYTHONHASHSEED'] = str(i)
        elif args.hash_seed is not None:
            env['PYTHONHASHSEED'] = str(args.hash_seed)
        ballast = (i * 400_000) if args.ballast else None
        argv_used, proc = run_once(cmd, out_board, env, ballast, cwd)
        with open(os.path.join(run_dir, 'stdout.txt'), 'w') as fh:
            fh.write(proc.stdout or '')
            fh.write(proc.stderr or '')
        label = "run%d" % i
        if args.hash_seed == 'vary':
            label += " PYTHONHASHSEED=%d" % i
        if ballast is not None:
            label += " ballast=%d" % ballast
        if proc.returncode != 0 or not os.path.isfile(out_board):
            print("%s FAILED (exit %d) -- see %s/stdout.txt"
                  % (label, proc.returncode, run_dir))
            failures.append(label)
            if not args.keep_going:
                return 2
            continue
        print("%s ok -> %s" % (label, out_board))
        boards.append((label, canonical(out_board)))

    if len(boards) < 2:
        print("\nnot enough successful runs to compare")
        return 2

    base_label, base = boards[0]
    differing = []
    for label, other in boards[1:]:
        if other != base:
            differing.append((label, other))

    print("\n%d run(s) compared against %s" % (len(boards) - 1, base_label))
    if not differing:
        counts = {k: len(v) for k, v in base.items()}
        print("IDENTICAL: every run produced the same %d segments, %d vias, "
              "%d footprint positions, %d zones"
              % (counts['segments'], counts['vias'], counts['footprints'],
                 counts['zones']))
        return 2 if failures else 0

    print("DIFFERENT: %d of %d run(s) disagreed with %s"
          % (len(differing), len(boards) - 1, base_label))
    for label, other in differing:
        print("\n%s vs %s:" % (base_label, label))
        for line in describe(base, other):
            print(line)
    return 1


if __name__ == '__main__':
    sys.exit(main())
