#!/usr/bin/env python3
"""Hindsight restart (py_router/hindsight.py) -- unit gates.

  1. OFF by default: the ghost dict comes back as is, nothing restarts.
  2. The map is binned into areas: each bin expands to the grid cells it
     covers, priced at KICAD_HINDSIGHT_COST x its weight against the board's
     REF_PCT bin; bins under FLOOR are dropped; a map that keeps nothing
     prices nothing instead of failing.
  3. The restart re-enters once, seeded with the attempt's field, and only
     from the outermost call, only with failed nets, never after a cancel or
     checkpoint stop, never from inside the restarted attempt (which also
     takes no second snapshot), and leaves no seed behind.
  4. Every hook is wired: the four obstacle builders and route.py's restart,
     which hands it the cancel / checkpoint state.

    python3 tests/test_hindsight.py
"""
import os
import sys

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
    hs._ROWS.clear()


def points(cells):
    a = list(zip(*cells))
    return (np.array(a[0]), np.array(a[1], float), np.array(a[2], float),
            np.array(a[3], float))


CFG = GridRouteConfig(layers=['F.Cu', 'B.Cu'])
LAYERS = ['F.Cu', 'B.Cu']
HOT = [('F.Cu', 9.0 + 1.5 * i, 9.0 + 1.5 * j, 1.0) for i in range(3) for j in range(3)]
PTS = points(HOT + [('F.Cu', 30.0, 30.0, 0.05), ('B.Cu', 20.0, 5.0, 1.0)])

# 1. off by default
knobs()
ghosts = {('history',): np.zeros((0, 4), np.int32)}
check('off: ghost dict unchanged', hs.add_hindsight_source(ghosts, CFG) is ghosts)
check('off: restart disabled', not hs.restart_enabled())
check('off: maybe_restart inert',
      hs.maybe_restart(True, 5, 0, CFG, lambda: 1) is None)
check('defaults', DEFAULTS['cost'] == 0.6 and DEFAULTS['bin'] == 1.5, str(DEFAULTS))

# 2. binning and pricing
knobs()
rows = hs._map_rows(PTS, 0.1, LAYERS)
check('bins expand to 15x15 grid cells', len(rows) == 10 * 225, str(len(rows)))
check('full-weight bin priced at cost x 10000 per cell',
      int(rows[:, 3].max()) == 6000, str(rows[:, 3].max()))
check('weak bin under the floor dropped',
      not np.any((rows[:, 1] >= 300) & (rows[:, 2] >= 300)))
check('layers kept apart', set(np.unique(rows[:, 0]).tolist()) == {0, 1})
keys = rows[:, 0].astype(np.int64) * (1 << 40) + (rows[:, 1] + (1 << 19)).astype(np.int64) \
    * (1 << 20) + (rows[:, 2] + (1 << 19))
check('rows unique per (layer, cell)', len(np.unique(keys)) == len(rows))
neg = hs._map_rows(points([('F.Cu', -0.7, -3.1, 1.0), ('F.Cu', -2.2, -3.1, 1.0)]),
                   0.0635, LAYERS)
nk = neg[:, 1].astype(np.int64) * 100000 + neg[:, 2]
check('negative coordinates on an odd grid: unique cells', len(np.unique(nk)) == len(neg))
knobs(floor=2.0)
check('a map that keeps nothing prices nothing', hs._map_rows(PTS, 0.1, LAYERS) is None)

# 3. restart gating
knobs(restart=True)
field = HistoryField()
field.accumulate(np.array([((100 + (1 << 23)) << 24) | (100 + (1 << 23))],
                          dtype=np.int64), 0.1)
CFG._history_cong = field
calls = []


def rerun():
    calls.append(hs.add_hindsight_source({}, CFG).get(hs.SOURCE_KEY) is not None)
    calls.append(hs.restart_enabled())
    nested = hs.maybe_restart(True, 5, 0, CFG, lambda: calls.append('nested'))
    return ('result', nested)


check('no restart without failed nets', hs.maybe_restart(True, 0, 0, CFG, rerun) is None)
check('no restart below the outermost call', hs.maybe_restart(False, 5, 0, CFG, rerun) is None)
check('no restart after a cancel / checkpoint stop',
      hs.maybe_restart(True, 5, 0, CFG, rerun, stopped=True) is None)
check('nothing ran for the refused restarts', calls == [], str(calls))
res = hs.maybe_restart(True, 5, 0, CFG, rerun)
check('restart returns (True, rerun result)', res == (True, ('result', None)), str(res))
check('restarted attempt prices the map', calls[:1] == [True], str(calls))
check('restarted attempt takes no second snapshot', calls[1:2] == [False], str(calls))
check('no restart from inside the restarted attempt', 'nested' not in calls)
check('seed cleared after the restart', hs.add_hindsight_source({}, CFG) == {})
CFG._history_cong = HistoryField()
check('no restart with an empty field', hs.maybe_restart(True, 5, 0, CFG, rerun) is None)

# 4. wiring
src = {n: open(os.path.join(REPO, 'py_router', n)).read()
       for n in ('route.py', 'routing_context.py')}
check('four builders compose the hindsight source',
      src['routing_context.py'].count('ghost_costs=add_hindsight_source(') == 4)
check('route.py restarts after the routing phases', 'maybe_restart(' in src['route.py'])
check('route.py passes the cancel / checkpoint state',
      '_ckpt_stop or user_quit' in src['route.py'])

if failures:
    for f in failures:
        print(f'FAIL: {f}')
    sys.exit(1)
print('PASS: hindsight is off by default, bins and prices the map, restarts once '
      'only from the outermost call with failed nets and no cancel, takes no '
      'second snapshot, cleans up, and every hook is wired')
