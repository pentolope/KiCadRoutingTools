"""Routing and placement on tests/repro/toy_pcba.kicad_pcb repeat exactly.

CLAUDE.md and CONTRIBUTING.md both assert determinism. This runs each step
several times with PYTHONHASHSEED varied and the heap shifted underneath it,
and compares canonicalized copper -- so a set of strings walked in hash order,
or a set keyed on object identity, would show up as a difference.
"""

import os
import subprocess
import sys

RUN_ALL_FAST_OK = False
RUN_ALL_TIMEOUT = 1800

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _d))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'repro'))

from determinism_probe import describe                          # noqa: E402

REPRO = os.path.join(ROOT, 'tests', 'repro')
BOARD = os.path.join(REPRO, 'toy_pcba.kicad_pcb')
PROBE = os.path.join(REPRO, 'determinism_probe.py')

FAILURES = []

CASES = [
    ('route', ['python3', 'py_router/route.py', BOARD, '{out}']),
    ('route_one_layer', ['python3', 'py_router/route.py', BOARD, '{out}',
                         '--layers', 'F.Cu']),
    ('planes', ['python3', 'py_router/route_planes.py', BOARD, '{out}',
                '--nets', 'GND', '--plane-layers', 'B.Cu']),
    ('place', ['python3', 'py_placer/place_optimize.py', BOARD, '{out}']),
]


def report(name, ok, detail=''):
    print(('  PASS  ' if ok else '  FAIL  ') + name
          + (('  -- ' + detail) if detail else ''))
    if not ok:
        FAILURES.append(name)


def main():
    if not os.path.isfile(BOARD):
        gen = os.path.join(REPRO, 'make_toy_pcba.py')
        rc = subprocess.run([sys.executable, gen, BOARD], cwd=ROOT).returncode
        if rc != 0 or not os.path.isfile(BOARD):
            report('toy board built', False, 'make_toy_pcba.py exit %d' % rc)
            return 1

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for name, cmd in CASES:
            argv = [sys.executable, PROBE, '--runs', '3',
                    '--out-dir', os.path.join(tmp, name),
                    '--hash-seed', 'vary', '--ballast', '--'] + cmd
            proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
            tail = (proc.stdout or '').strip().splitlines()[-8:]
            report(name, proc.returncode == 0,
                   'probe exit %d: %s' % (proc.returncode, ' | '.join(tail))
                   if proc.returncode else ' '.join(tail[-1:]))

    print('\n%s' % ('FAILURES: ' + ', '.join(FAILURES) if FAILURES else 'all pass'))
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
