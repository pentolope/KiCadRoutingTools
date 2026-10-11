#!/usr/bin/env python3
"""Hindsight congestion (py_router/hindsight.py) -- unit gates.

  1. OFF by default: no seed, no restart -> the ghost dict comes back as is.
  2. The seed is binned into areas: each bin expands to the grid cells it
     covers, priced at KICAD_HINDSIGHT_COST x its weight against the board's
     REF_PCT bin; bins under FLOOR are dropped.
  3. The controls keep the map's total cost (shift, scramble, blanket=mass)
     and the blur keeps it to within the cells it fades out.
  4. The priority modes exempt what they say: collar the bins near the net's
     own pads, net the whole map for a net with a pad in a hot bin.
  5. The restart re-enters once, seeded with the attempt's field, never from
     inside the restarted attempt, never without failed nets, and leaves no
     seed behind.
  6. Every hook is wired: the four obstacle builders, route.py's restart and
     pass runner, history_congestion's field registration.

    python3 tests/test_hindsight.py
"""
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'py_router'))

import numpy as np

import hindsight as hs
from history_congestion import HistoryField
from routing_config import GridRouteConfig

DEFAULTS = dict(hs.KNOBS)
failures = []


def check(name, ok, detail=''):
    if not ok:
        failures.append(f'{name}{": " + detail if detail else ""}')


def knobs(**kw):
    hs.KNOBS.clear()
    hs.KNOBS.update(DEFAULTS)
    hs.KNOBS.update(kw)
    hs._SEED_MAPS.clear()


def points(cells):
    """(names, x, y, w, passes) from [(layer, x_mm, y_mm, w), ...]."""
    a = list(zip(*cells))
    return (np.array(a[0]), np.array(a[1], float), np.array(a[2], float),
            np.array(a[3], float), 1)


def seed_map(pts, layers=('F.Cu', 'B.Cu')):
    return hs._SeedMap(pts, 0.1, list(layers))


def total(rows):
    return int(rows[:, 3].astype(np.int64).sum()) if rows is not None else 0


CFG = GridRouteConfig(layers=['F.Cu', 'B.Cu'])

# A hot 3x3-bin block on F.Cu around (10, 10), a weak bin, a B.Cu bin.
HOT = [('F.Cu', 9.0 + 1.5 * i, 9.0 + 1.5 * j, 1.0) for i in range(3) for j in range(3)]
PTS = points(HOT + [('F.Cu', 30.0, 30.0, 0.05), ('B.Cu', 20.0, 5.0, 1.0)])

# 1. off by default
knobs()
ghosts = {('history',): np.zeros((0, 4), np.int32)}
check('off: ghost dict unchanged', hs.add_hindsight_source(ghosts, CFG, None, 1) is ghosts)
check('off: no seed map', hs._seed_map(CFG) is None)
check('off: restart disabled', not hs.restart_enabled())
check('off: defaults are the measured ones',
      DEFAULTS['cost'] == 0.6 and DEFAULTS['mode'] == 'uniform', str(DEFAULTS))

# 2. binning and pricing
knobs()
sm = seed_map(PTS)
cells = sm.rows
check('bins expand to 15x15 grid cells', len(cells) == 10 * 225, str(len(cells)))
check('full-weight bin priced at cost x 10000 per cell',
      int(cells[:, 3].max()) == int(0.6 * 10000), str(cells[:, 3].max()))
check('weak bin under the floor dropped',
      not np.any((cells[:, 1] >= 300) & (cells[:, 2] >= 300)))
check('layers kept apart', set(np.unique(cells[:, 0]).tolist()) == {0, 1})

# 3. controls keep the total cost; blur keeps it up to the faded cells
base_total = total(sm.rows)
for name, kw in [('shift', dict(shift=True)), ('scramble', dict(scramble=3)),
                 ('blanket mass', dict(blanket='mass'))]:
    knobs(**kw)
    t = total(seed_map(PTS).rows)
    check(f'{name} keeps total cost', abs(t - base_total) <= 0.01 * base_total,
          f'{t} vs {base_total}')
knobs(blur=1.5)
t = total(seed_map(PTS).rows)
check('blur keeps total cost', 0.9 * base_total <= t <= base_total, f'{t} vs {base_total}')
knobs(scramble=3)
a = seed_map(PTS).rows
knobs(scramble=4)
b = seed_map(PTS).rows
check('scramble seeds differ', not np.array_equal(a, b))

# 4. priority modes
pad_in = types.SimpleNamespace(global_x=10.5, global_y=10.5, size_x=0.5, size_y=0.5)
pad_out = types.SimpleNamespace(global_x=60.0, global_y=60.0, size_x=0.5, size_y=0.5)
pcb = types.SimpleNamespace(pads_by_net={1: [pad_in], 2: [pad_out]})
knobs(mode='uniform')
sm = seed_map(PTS)
check('uniform: every net pays the whole map', sm.rows_for(pcb, (1,)) is sm.rows)
knobs(mode='collar', collar=0.5)
sm = seed_map(PTS)
r1, r2 = sm.rows_for(pcb, (1,)), sm.rows_for(pcb, (2,))
check('collar: own-pad bins exempted', len(r1) < len(sm.rows))
check('collar: a far net pays everything', len(r2) == len(sm.rows))
near = (np.abs(r1[:, 1] * 0.1 - 10.5) < 0.3) & (np.abs(r1[:, 2] * 0.1 - 10.5) < 0.3)
check('collar: no cost left on the pad', not near.any())
knobs(mode='net', hot=0.9)
sm = seed_map(PTS)
check('net: a net with a pad in a hot bin ignores the map', sm.rows_for(pcb, (1,)) is None)
check('net: other nets pay it', sm.rows_for(pcb, (2,)) is sm.rows)

# 5. restart gating
knobs(restart=True)
field = HistoryField()
field.accumulate(np.array([(0 << 48) | ((100 + (1 << 23)) << 24) | (100 + (1 << 23))],
                          dtype=np.int64), 0.1)
CFG._history_cong = field
calls = []


def rerun():
    calls.append(hs._seed_map(CFG) is not None)
    nested = hs.maybe_restart(True, 5, 0, CFG, lambda: calls.append('nested'))
    return ('result', nested)


check('no restart without failed nets', hs.maybe_restart(True, 0, 0, CFG, rerun) is None)
check('no restart below the outermost call', hs.maybe_restart(False, 5, 0, CFG, rerun) is None)
res = hs.maybe_restart(True, 5, 0, CFG, rerun)
check('restart returns (True, rerun result)', res == (True, ('result', None)), str(res))
check('restarted attempt sees the seed', calls == [True], str(calls))
check('no restart from inside the restarted attempt', 'nested' not in calls)
check('seed cleared after the restart', hs._seed_map(CFG) is None)
knobs()
check('restart knob off -> inert', hs.maybe_restart(True, 5, 0, CFG, rerun) is None)

# 6. wiring
src = {n: open(os.path.join(REPO, 'py_router', n)).read()
       for n in ('route.py', 'routing_context.py', 'history_congestion.py')}
check('four builders compose the hindsight source',
      src['routing_context.py'].count('ghost_costs=add_hindsight_source(') == 4)
check('route.py restarts after the routing phases', 'maybe_restart(' in src['route.py'])
check('route.py runs passes', 'run_passes(args.output_file)' in src['route.py'])
check('history fields are registered for the dump',
      src['history_congestion.py'].count('register_field(field, config)') == 2)

if failures:
    for f in failures:
        print(f'FAIL: {f}')
    sys.exit(1)
print('PASS: hindsight is off by default, bins and prices the seed, its controls '
      'keep the total cost, the priority modes exempt what they say, the restart '
      'fires once and cleans up, and every hook is wired')
