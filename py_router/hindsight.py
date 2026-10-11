"""Hindsight congestion: restart a failed attempt with its own conflict map.

The #590 history field learns where nets fought DURING a routing call, but it
only steers the nets routed after each conflict: the nets laid early, before
the congestion formed, keep whatever path they took through it.

KICAD_HINDSIGHT_RESTART=1: when the outermost batch_route has failed nets
after its main loop and reroute loop (checked once the multipoint taps have
run), it re-enters itself on the untouched input with that attempt's field as
an extra soft cost source, so every net sees the congestion from the start.
Rescue, plane finalize, the oracle and the reconcile laps run once, in the
restarted attempt. A cancelled or checkpoint-stopped attempt never restarts.

The map is the field's cells binned into KICAD_HINDSIGHT_BIN mm squares per
layer (1.5), so it prices congested AREAS rather than the thin corridors and
frontiers the history records. A bin's cost is KICAD_HINDSIGHT_COST
mm-equivalent per cell (0.6), scaled by its weight against the
KICAD_HINDSIGHT_REF_PCT percentile of the board's bins (90); bins under
KICAD_HINDSIGHT_FLOOR of that are dropped (0.1). Every net pays the same map.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

import numpy as np

from routing_config import REFERENCE_GRID_STEP

_OFF = 1 << 23


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, '') else default


KNOBS = {
    'restart': os.environ.get('KICAD_HINDSIGHT_RESTART', '') not in ('', '0'),
    'cost': _env_float('KICAD_HINDSIGHT_COST', 0.6),
    'bin': _env_float('KICAD_HINDSIGHT_BIN', 1.5),
    'ref_pct': _env_float('KICAD_HINDSIGHT_REF_PCT', 90.0),
    'floor': _env_float('KICAD_HINDSIGHT_FLOOR', 0.1),
}

SOURCE_KEY = ('hindsight',)


def _field_points(field, grid_step, layers):
    """(layer names, x mm, y mm, weight) of a HistoryField's cells, or None."""
    k = field.keys
    if k.size == 0:
        return None
    li = (k >> 48).astype(np.int64)
    gx = ((k >> 24) & ((1 << 24) - 1)).astype(np.int64) - _OFF
    gy = (k & ((1 << 24) - 1)).astype(np.int64) - _OFF
    ok = (li >= 0) & (li < len(layers))
    if not ok.any():
        return None
    names = np.asarray(layers)[li[ok]]
    return (names, gx[ok] * grid_step, gy[ok] * grid_step,
            field.weights[ok].astype(np.float64))


def _map_rows(pts, grid_step: float, layers) -> Optional[np.ndarray]:
    """(N, 4) [layer, gx, gy, cost] rows pricing the binned map, or None.

    Bins partition the grid cells exactly (ceil on both edges), so rows are
    unique per (layer, cell), as the SUM composition modes require.
    """
    names, x, y, w = pts
    B = KNOBS['bin']
    lidx = {n: i for i, n in enumerate(layers)}
    li = np.array([lidx.get(str(n), -1) for n in names], dtype=np.int64)
    ok = li >= 0
    if not ok.any():
        return None
    bx = np.floor(x[ok] / B).astype(np.int64)
    by = np.floor(y[ok] / B).astype(np.int64)
    packed = (li[ok] << 48) | ((bx + _OFF) << 24) | (by + _OFF)
    uniq, inv = np.unique(packed, return_inverse=True)
    val = np.bincount(inv, weights=w[ok], minlength=uniq.size)
    ref = float(np.percentile(val, KNOBS['ref_pct']))
    if ref <= 0:
        return None
    norm = np.minimum(1.0, val / ref)
    keep = norm >= KNOBS['floor']
    uniq, norm = uniq[keep], norm[keep]
    if not uniq.size:
        return None
    L = (uniq >> 48).astype(np.int64)
    BX = ((uniq >> 24) & ((1 << 24) - 1)).astype(np.int64) - _OFF
    BY = (uniq & ((1 << 24) - 1)).astype(np.int64) - _OFF

    # Expand every kept bin to the grid cells it covers.
    g = grid_step
    gx0 = np.ceil(BX * B / g - 1e-9).astype(np.int64)
    gx1 = np.ceil((BX + 1) * B / g - 1e-9).astype(np.int64)
    gy0 = np.ceil(BY * B / g - 1e-9).astype(np.int64)
    gy1 = np.ceil((BY + 1) * B / g - 1e-9).astype(np.int64)
    W = int(max((gx1 - gx0).max(), (gy1 - gy0).max()))
    off = np.arange(W, dtype=np.int64)
    ox, oy = np.meshgrid(off, off, indexing='ij')
    cx = gx0[:, None] + ox.ravel()[None, :]
    cy = gy0[:, None] + oy.ravel()[None, :]
    inside = (cx < gx1[:, None]) & (cy < gy1[:, None])
    n_cells = inside.sum(axis=1)
    cost = (KNOBS['cost'] * norm * (1000.0 / REFERENCE_GRID_STEP)).astype(np.int64)
    rows = np.empty((int(n_cells.sum()), 4), dtype=np.int32)
    rows[:, 0] = np.repeat(L, n_cells)
    rows[:, 1] = cx[inside]
    rows[:, 2] = cy[inside]
    rows[:, 3] = np.repeat(np.minimum(cost, np.iinfo(np.int32).max), n_cells)
    rows = rows[rows[:, 3] > 0]
    return rows if len(rows) else None


# The restarted attempt's seed (the first attempt's field as points), whether
# that attempt is running, and its rows per (grid_step, layer stack).
_RESTART = {'points': None, 'active': False}
_ROWS: Dict[tuple, Optional[np.ndarray]] = {}


def add_hindsight_source(ghost_costs, config):
    """Fold the restart's map into a merge_track_proximity_costs ghost dict
    (unchanged outside a restarted attempt)."""
    if _RESTART['points'] is None or KNOBS['cost'] <= 0:
        return ghost_costs
    k = (float(config.grid_step), tuple(config.layers))
    if k not in _ROWS:
        _ROWS[k] = _map_rows(_RESTART['points'], k[0], k[1])
        n = 0 if _ROWS[k] is None else len(_ROWS[k])
        print(f"Hindsight congestion armed: {n} cell row(s) at "
              f"{KNOBS['cost']}mm-equiv, {KNOBS['bin']}mm bins", flush=True)
    rows = _ROWS[k]
    if rows is None:
        return ghost_costs
    merged = dict(ghost_costs or {})
    merged[SOURCE_KEY] = rows
    return merged


def restart_enabled() -> bool:
    """True when this call should snapshot its arguments for a restart: the
    knob is on and no restarted attempt is already running."""
    return KNOBS['restart'] and not _RESTART['active']


def maybe_restart(top_level: bool, failed: int, iterations: int, config, rerun,
                  stopped: bool = False):
    """KICAD_HINDSIGHT_RESTART: once the routing phases of the OUTERMOST
    attempt are done, re-enter batch_route (``rerun``) seeded with this
    attempt's map when nets failed. Never after a cancel or checkpoint stop
    (``stopped``). Returns (True, rerun's result) when it restarted, or None
    to carry on with this attempt."""
    if (not (KNOBS['restart'] and top_level) or _RESTART['active']
            or stopped or failed <= 0):
        return None
    field = getattr(config, '_history_cong', None)
    pts = _field_points(field, float(config.grid_step), tuple(config.layers)) \
        if field is not None else None
    if pts is None:
        return None
    print(f"HINDSIGHT_RESTART: {failed} net(s) failed in the main and reroute "
          f"loops ({iterations} iterations); restarting from the input with "
          f"the {pts[0].size}-cell congestion map", flush=True)
    # The CLI and the GUI reset the narrowing ledger before calling
    # batch_route; the restarted attempt must not report this one's.
    import fab_tiers
    fab_tiers.reset_ledger()
    _RESTART['points'] = pts
    _RESTART['active'] = True
    _ROWS.clear()
    try:
        return True, rerun()
    finally:
        _RESTART['points'] = None
        _RESTART['active'] = False
        _ROWS.clear()
