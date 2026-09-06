"""place_optimize -> route_planes (GND on B.Cu) -> route, as one command, so
the determinism probe can drive a whole chain.

    python3 tests/repro/toy_chain.py OUT.kicad_pcb [--board B.kicad_pcb]

Intermediates land beside OUT. Exits with the first failing step's code.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))


def step(name, argv, log_path):
    with open(log_path, 'w') as fh:
        proc = subprocess.run(argv, cwd=_ROOT, stdout=fh,
                              stderr=subprocess.STDOUT, text=True)
    print("  %-8s exit %d  (%s)" % (name, proc.returncode, log_path))
    return proc.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('output')
    ap.add_argument('--board', default=os.path.join(_HERE, 'toy_pcba.kicad_pcb'))
    args = ap.parse_args(argv)

    out = os.path.abspath(args.output)
    work = os.path.dirname(out)
    os.makedirs(work, exist_ok=True)
    placed = os.path.join(work, 'placed.kicad_pcb')
    poured = os.path.join(work, 'poured.kicad_pcb')
    py = sys.executable

    rc = step('place', [py, os.path.join(_ROOT, 'py_placer', 'place_optimize.py'),
                        os.path.abspath(args.board), placed],
              os.path.join(work, 'place.log'))
    if rc:
        return rc
    rc = step('planes', [py, os.path.join(_ROOT, 'py_router', 'route_planes.py'),
                         placed, poured, '--nets', 'GND',
                         '--plane-layers', 'B.Cu'],
              os.path.join(work, 'planes.log'))
    if rc:
        return rc
    return step('route', [py, os.path.join(_ROOT, 'py_router', 'route.py'),
                          poured, out],
                os.path.join(work, 'route.log'))


if __name__ == '__main__':
    sys.exit(main())
