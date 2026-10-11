"""Hindsight congestion: replay a failed attempt's conflict map from the first net.

The #590 history field learns where nets fought DURING a routing call, but it
only steers the nets routed after each conflict: the nets laid early, before
the congestion formed, keep whatever path they took through it. Hindsight
carries the map into a second attempt that starts over from the same input,
so every net sees it from the start.

Every knob is OFF by default. Each one below is marked with how it measured
on the fixed-chain corpus (pour GND, then route.py --clearance 0.15; 36 boards
from sets 1-13 that end with open nets, baseline 220 open nets, compared with
placebo maps of the same total cost):

  WORKED       the map, every net paying it, at cost 0.6: 178 open vs
               placebos 204-240 (the defaults below)
  CHEAPER      the in-process restart (KICAD_HINDSIGHT_RESTART): reruns only
               the routing phases (~10% of a run's wall time); matches a
               fresh process seeded with the same map
  DID NOT HELP everything marked so below; kept for reference

The map
-------
  KICAD_HINDSIGHT_COST      mm-equivalent per cell at full weight. 0.6 worked;
                            0.3 was no better than a shifted placebo (10-board
                            screen); 0.9 was worse (202 open). Default 0.6.
  KICAD_HINDSIGHT_BIN       bin size in mm (1.5): the field's thin corridors
                            and frontiers become congested AREAS
  KICAD_HINDSIGHT_REF_PCT   a bin's weight is scaled against this percentile
                            of the board's bins (90)
  KICAD_HINDSIGHT_FLOOR     bins under this fraction of it are dropped (0.1)
  KICAD_HINDSIGHT_BLUR      DID NOT HELP. Gaussian sigma in mm, total cost
                            kept: blurred, the real map was no better than
                            shifted placebos -- the gain is in the exact
                            location.

Who pays it (KICAD_HINDSIGHT_MODE) -- the "priority" idea
----------------------------------------------------------
A net that must reach into a congested area has no way around it, so pricing
that area for it could only make it wander.

  uniform   WORKED (default). Every net pays the whole map.
  collar    DID NOT HELP at 0.6 (193 open, fewest DRC). A net ignores bins
            within KICAD_HINDSIGHT_COLLAR mm of its pads. At cost 0.9 it beat
            uniform-at-0.9 (185 vs 202) but not uniform-at-0.6.
  blob      DID NOT HELP. collar, plus every hot area (bins at >=
            KICAD_HINDSIGHT_HOT of full cost, 8-connected) that one of its
            pads reaches into.
  net       DID NOT HELP. A net with any pad reaching a hot bin ignores the
            whole map.

KICAD_HINDSIGHT_FIRST=1  DID NOT HELP (worse every time it was tried). Nets
whose pads reach a hot bin route first, within each #472 direct-first block;
BIG nets (cost above KICAD_HINDSIGHT_FIRST_BIG x the board median) stay put.

Attempts
--------
  KICAD_HINDSIGHT_RESTART=1  CHEAPER, the recommended form. When the
      outermost batch_route still has failed nets after the routing phases
      (main loop, reroute loop, multipoint taps), it re-enters itself on the
      untouched input, seeded with the map those phases recorded. Rescue,
      plane finalize, the oracle and the reconcile laps run once, in the
      restarted attempt. Measured trigger on 40 boards: fires on 32/36
      failing boards and on 2 of 4 clean ones.
  KICAD_HINDSIGHT_PASSES=N   route.py runs the whole step as child processes
      from the same input; a pass whose JSON_SUMMARY_MIN leaves anything open
      is followed by one seeded with the maps so far, and a complete pass is
      shipped. This is how the results above were measured (2 full passes,
      ~2.1x wall on failing boards). KICAD_HINDSIGHT_QUICK_FIRST=1 makes pass
      1 a quick map-only pass and always runs pass 2: same map quality, but
      pass 2 then also runs on clean boards and damaged some.
  KICAD_HINDSIGHT_DUMP=<file>  write every routing call's history field
      (cells in mm, raw weights) at exit
  KICAD_HINDSIGHT_SEED=<file>  price that map from the first net on (a
      comma-separated list is summed); a dump taken while seeded carries the
      seed forward, so passes chain
  KICAD_HINDSIGHT_QUICK=reroute|phase3  measurement tool: a top-level
      route.py run stops after that phase and dumps its map (nothing written).
      phase3 reproduces the full run's map (cosine 0.95-1.00 on 6 boards);
      reroute does not (multipoint taps carry much of the conflict).

Controls (same total cost, moved off the congestion)
----------------------------------------------------
  KICAD_HINDSIGHT_SHIFT=1       shift by KICAD_HINDSIGHT_SHIFT_FRAC (0.5) of
                                the map's extent in x and y, wrapping
  KICAD_HINDSIGHT_SCRAMBLE=<n>  move every kept 2-D bin, with its per-layer
                                values, to a random distinct bin of the map's
                                bounding box (seed n)
  KICAD_HINDSIGHT_BLANKET=mass|mean  one flat level over that bounding box:
                                the map's total cost (mass) or its mean bin
                                cost (mean)
Controls run before the blur, so a blurred control is blurred like the map.
"""
from __future__ import annotations

import atexit
import math
import os
from typing import Dict, Optional, Tuple

import numpy as np

from routing_config import REFERENCE_GRID_STEP

_OFF = 1 << 23
_MODES = ('uniform', 'collar', 'blob', 'net')


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, '') else default


KNOBS = {
    'dump': os.environ.get('KICAD_HINDSIGHT_DUMP') or None,
    'seed': os.environ.get('KICAD_HINDSIGHT_SEED') or None,
    'cost': _env_float('KICAD_HINDSIGHT_COST', 0.6),
    'bin': _env_float('KICAD_HINDSIGHT_BIN', 1.5),
    'ref_pct': _env_float('KICAD_HINDSIGHT_REF_PCT', 90.0),
    'floor': _env_float('KICAD_HINDSIGHT_FLOOR', 0.1),
    'mode': (os.environ.get('KICAD_HINDSIGHT_MODE') or 'uniform').strip().lower(),
    'collar': _env_float('KICAD_HINDSIGHT_COLLAR', 1.0),
    'hot': _env_float('KICAD_HINDSIGHT_HOT', 0.25),
    'shift': os.environ.get('KICAD_HINDSIGHT_SHIFT', '') not in ('', '0'),
    'shift_frac': _env_float('KICAD_HINDSIGHT_SHIFT_FRAC', 0.5),
    'blanket': (os.environ.get('KICAD_HINDSIGHT_BLANKET') or '').strip().lower(),
    'scramble': int(os.environ.get('KICAD_HINDSIGHT_SCRAMBLE') or 0),
    'blur': _env_float('KICAD_HINDSIGHT_BLUR', 0.0),
    'quick': (os.environ.get('KICAD_HINDSIGHT_QUICK') or '').strip().lower(),
    'restart': os.environ.get('KICAD_HINDSIGHT_RESTART', '') not in ('', '0'),
    'first': os.environ.get('KICAD_HINDSIGHT_FIRST', '') not in ('', '0'),
    'first_big': _env_float('KICAD_HINDSIGHT_FIRST_BIG', 4.0),
}
if KNOBS['blanket'] not in ('', 'mass', 'mean'):
    raise ValueError(f"KICAD_HINDSIGHT_BLANKET must be mass or mean, "
                     f"got {KNOBS['blanket']!r}")
if sum(bool(KNOBS[k]) for k in ('shift', 'blanket', 'scramble')) > 1:
    raise ValueError("KICAD_HINDSIGHT_SHIFT, _BLANKET and _SCRAMBLE are "
                     "separate controls; set one")
if KNOBS['mode'] not in _MODES:
    raise ValueError(f"KICAD_HINDSIGHT_MODE must be one of {_MODES}, "
                     f"got {KNOBS['mode']!r}")

SOURCE_KEY = ('hindsight',)

# --------------------------------------------------------------------------
# Pass 1: collect every routing call's history field and write it at exit.

_FIELDS = []    # (HistoryField, grid_step, layer names)


def register_field(field, config) -> None:
    """Called wherever a history field is created; inert unless dumping."""
    if KNOBS['dump'] and field is not None:
        _FIELDS.append((field, float(config.grid_step), tuple(config.layers)))


def _field_points(field, grid_step, layers):
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


def _load_points(spec: str):
    """(names, x, y, w, passes) from one or more comma-separated dumps."""
    names, xs, ys, ws, passes = [], [], [], [], 0
    for path in [p for p in spec.split(',') if p]:
        with np.load(path, allow_pickle=False) as z:
            ln = z['layer_names']
            names.append(ln[z['layer']] if z['layer'].size else ln[:0])
            xs.append(z['x'])
            ys.append(z['y'])
            ws.append(z['w'])
            passes = max(passes, int(z['passes']))
    if not xs:
        return None
    return (np.concatenate(names), np.concatenate(xs), np.concatenate(ys),
            np.concatenate(ws), passes)


def _write_dump() -> None:
    path = KNOBS['dump']
    chunks, passes = [], 0
    if KNOBS['seed']:
        seed = _load_points(KNOBS['seed'])
        if seed is not None:
            chunks.append(seed[:4])
            passes = seed[4]
    for field, g, layers in _FIELDS:
        pts = _field_points(field, g, layers)
        if pts is not None:
            chunks.append(pts)
    if chunks:
        names = np.concatenate([c[0] for c in chunks]).astype(str)
        x = np.concatenate([c[1] for c in chunks])
        y = np.concatenate([c[2] for c in chunks])
        w = np.concatenate([c[3] for c in chunks])
    else:
        names = np.empty(0, dtype=str)
        x = y = w = np.empty(0, dtype=np.float64)
    layer_names, layer = np.unique(names, return_inverse=True)
    tmp = f"{path}.tmp"
    with open(tmp, 'wb') as fh:
        np.savez_compressed(fh, layer_names=layer_names.astype(str),
                            layer=layer.astype(np.int32), x=x, y=y, w=w,
                            passes=np.int32(passes + 1),
                            calls=np.int32(len(_FIELDS)))
    os.replace(tmp, path)
    print(f"Hindsight map written: {path} ({x.size} cell(s) from "
          f"{len(_FIELDS)} routing call(s), pass {passes + 1})", flush=True)


if KNOBS['dump']:
    atexit.register(_write_dump)

# --------------------------------------------------------------------------
# Pass 2: price the seeded map, per net.


def _blur_bins(L, BX, BY, norm, sigma_bins):
    """Gaussian-blur each layer's bins (sigma in bins). The blur keeps each
    layer's total; cells it fades under 2% of full strength are dropped."""
    from scipy.ndimage import gaussian_filter
    pad = int(math.ceil(4 * sigma_bins)) + 1
    x0, y0 = int(BX.min()) - pad, int(BY.min()) - pad
    nx, ny = int(BX.max()) - x0 + 1 + pad, int(BY.max()) - y0 + 1 + pad
    outL, outX, outY, outV = [], [], [], []
    for lay in np.unique(L):
        m = L == lay
        g = np.zeros((nx, ny))
        np.add.at(g, (BX[m] - x0, BY[m] - y0), norm[m])
        g = gaussian_filter(g, sigma_bins, mode='constant')
        ix, iy = np.nonzero(g > 0)
        outL.append(np.full(ix.size, lay, dtype=np.int64))
        outX.append(ix.astype(np.int64) + x0)
        outY.append(iy.astype(np.int64) + y0)
        outV.append(g[ix, iy])
    L, BX, BY = np.concatenate(outL), np.concatenate(outX), np.concatenate(outY)
    v = np.concatenate(outV)
    keep = v >= 0.02
    return L[keep], BX[keep], BY[keep], v[keep]


class _SeedMap:
    """The seed binned at one (grid_step, layer stack), with per-net rows."""

    def __init__(self, pts, grid_step: float, layers):
        names, x, y, w, passes = pts
        B = KNOBS['bin']
        self.grid_step = grid_step
        self.rows = None
        self.row_bin = None       # packed 2-D bin key of every row
        self.hot = set()          # packed 2-D keys of hot bins
        self.label = {}           # packed 2-D key -> hot-area label
        self.area_bins = {}       # label -> packed 2-D keys
        self._per_net: Dict[tuple, Optional[np.ndarray]] = {}
        lidx = {n: i for i, n in enumerate(layers)}
        li = np.array([lidx.get(str(n), -1) for n in names], dtype=np.int64)
        ok = li >= 0
        if not ok.any():
            self._announce(passes, 0)
            return
        bx = np.floor(x[ok] / B).astype(np.int64)
        by = np.floor(y[ok] / B).astype(np.int64)
        if KNOBS['shift']:
            span_x = int(bx.max() - bx.min() + 1)
            span_y = int(by.max() - by.min() + 1)
            dx = int(KNOBS['shift_frac'] * span_x)
            dy = int(KNOBS['shift_frac'] * span_y)
            bx = bx.min() + (bx - bx.min() + dx) % span_x
            by = by.min() + (by - by.min() + dy) % span_y
        packed = ((li[ok] << 48) | ((bx + _OFF) << 24) | (by + _OFF))
        uniq, inv = np.unique(packed, return_inverse=True)
        val = np.bincount(inv, weights=w[ok], minlength=uniq.size)
        ref = float(np.percentile(val, KNOBS['ref_pct'])) if val.size else 0.0
        if ref <= 0:
            self._announce(passes, 0)
            return
        norm = np.minimum(1.0, val / ref)
        keep = norm >= KNOBS['floor']
        uniq, norm = uniq[keep], norm[keep]
        L = (uniq >> 48).astype(np.int64)
        BX = ((uniq >> 24) & ((1 << 24) - 1)).astype(np.int64) - _OFF
        BY = (uniq & ((1 << 24) - 1)).astype(np.int64) - _OFF
        if KNOBS['scramble'] and norm.size:
            rng = np.random.default_rng(KNOBS['scramble'])
            k2 = ((BX + _OFF) << 24) | (BY + _OFF)
            u2, inv2 = np.unique(k2, return_inverse=True)
            x0, y0 = BX.min(), BY.min()
            ny = int(BY.max() - y0 + 1)
            n_box = int(BX.max() - x0 + 1) * ny
            pick = rng.choice(n_box, size=u2.size, replace=False)
            BX = (x0 + pick // ny)[inv2]
            BY = (y0 + pick % ny)[inv2]
        if KNOBS['blanket'] and norm.size:
            lp = np.unique(L)
            ax, ay = np.meshgrid(np.arange(BX.min(), BX.max() + 1),
                                 np.arange(BY.min(), BY.max() + 1), indexing='ij')
            n2 = ax.size
            level = (norm.sum() / (n2 * lp.size) if KNOBS['blanket'] == 'mass'
                     else norm.mean())
            L = np.repeat(lp, n2)
            BX = np.tile(ax.ravel(), lp.size)
            BY = np.tile(ay.ravel(), lp.size)
            norm = np.full(L.size, min(1.0, float(level)))
        if KNOBS['blur'] > 0 and norm.size:
            L, BX, BY, norm = _blur_bins(L, BX, BY, norm, KNOBS['blur'] / B)
        key2 = ((BX + _OFF) << 24) | (BY + _OFF)

        # Hot areas: 2-D bins whose hottest layer reaches KICAD_HINDSIGHT_HOT,
        # grouped 8-connected.
        hot_keys = np.unique(key2[norm >= KNOBS['hot']])
        self.hot = set(hot_keys.tolist())
        if hot_keys.size:
            from scipy.ndimage import label as _label
            hx = ((hot_keys >> 24) & ((1 << 24) - 1)).astype(np.int64) - _OFF
            hy = (hot_keys & ((1 << 24) - 1)).astype(np.int64) - _OFF
            grid = np.zeros((hx.max() - hx.min() + 1, hy.max() - hy.min() + 1),
                            dtype=bool)
            grid[hx - hx.min(), hy - hy.min()] = True
            lab, _n = _label(grid, structure=np.ones((3, 3), dtype=bool))
            labs = lab[hx - hx.min(), hy - hy.min()]
            self.label = dict(zip(hot_keys.tolist(), labs.tolist()))
            for k, lb in self.label.items():
                self.area_bins.setdefault(lb, []).append(k)

        # Expand every kept bin to the grid cells it covers.
        g = grid_step
        gx0 = np.ceil(BX * B / g - 1e-9).astype(np.int64)
        gx1 = np.ceil((BX + 1) * B / g - 1e-9).astype(np.int64)
        gy0 = np.ceil(BY * B / g - 1e-9).astype(np.int64)
        gy1 = np.ceil((BY + 1) * B / g - 1e-9).astype(np.int64)
        W = int(max((gx1 - gx0).max(), (gy1 - gy0).max()))
        off = np.arange(W, dtype=np.int64)
        ox, oy = np.meshgrid(off, off, indexing='ij')
        ox, oy = ox.ravel(), oy.ravel()
        cx = gx0[:, None] + ox[None, :]
        cy = gy0[:, None] + oy[None, :]
        inside = (cx < gx1[:, None]) & (cy < gy1[:, None])
        n_cells = inside.sum(axis=1)
        cost = (KNOBS['cost'] * norm * (1000.0 / REFERENCE_GRID_STEP)
                ).astype(np.int64)
        rows = np.empty((int(n_cells.sum()), 4), dtype=np.int32)
        rows[:, 0] = np.repeat(L, n_cells)
        rows[:, 1] = cx[inside]
        rows[:, 2] = cy[inside]
        rows[:, 3] = np.repeat(np.minimum(cost, np.iinfo(np.int32).max), n_cells)
        live = rows[:, 3] > 0
        self.rows = rows[live]
        self.row_bin = np.repeat(key2, n_cells)[live]
        self._announce(passes, int(np.unique(key2).size))

    def _announce(self, passes, n_bins):
        n_rows = 0 if self.rows is None else len(self.rows)
        print(f"Hindsight congestion armed (seed from pass {passes}): "
              f"{n_bins} bin(s) of {KNOBS['bin']}mm, {len(self.hot)} hot in "
              f"{len(self.area_bins)} area(s), {n_rows} cell row(s); mode "
              f"{KNOBS['mode']}, cost {KNOBS['cost']}mm-equiv, collar "
              f"{KNOBS['collar']}mm, hot {KNOBS['hot']}"
              f"{', SHIFTED control by ' + str(KNOBS['shift_frac']) if KNOBS['shift'] else ''}"
              f"{', BLANKET ' + KNOBS['blanket'] + ' control' if KNOBS['blanket'] else ''}"
              f"{', SCRAMBLE seed ' + str(KNOBS['scramble']) if KNOBS['scramble'] else ''}"
              f"{', blur ' + str(KNOBS['blur']) + 'mm' if KNOBS['blur'] > 0 else ''}",
              flush=True)

    def _pad_bins(self, pads) -> set:
        B, c = KNOBS['bin'], KNOBS['collar']
        out = set()
        for p in pads:
            hx = (getattr(p, 'size_x', 0.0) or 0.0) / 2 + c
            hy = (getattr(p, 'size_y', 0.0) or 0.0) / 2 + c
            for bx in range(math.floor((p.global_x - hx) / B),
                            math.floor((p.global_x + hx) / B) + 1):
                for by in range(math.floor((p.global_y - hy) / B),
                                math.floor((p.global_y + hy) / B) + 1):
                    out.add(((bx + _OFF) << 24) | (by + _OFF))
        return out

    def rows_for(self, pcb_data, net_ids) -> Optional[np.ndarray]:
        if self.rows is None or not len(self.rows):
            return None
        mode = KNOBS['mode']
        if mode == 'uniform':
            return self.rows
        key = tuple(sorted(set(net_ids)))
        if key in self._per_net:
            return self._per_net[key]
        pads = [p for nid in key
                for p in (getattr(pcb_data, 'pads_by_net', {}) or {}).get(nid, [])]
        near = self._pad_bins(pads)
        if mode == 'net':
            out = None if (near & self.hot) else self.rows
        else:
            exempt = set(near)
            if mode == 'blob':
                for lb in {self.label[k] for k in near if k in self.label}:
                    exempt.update(self.area_bins[lb])
            if exempt:
                mask = ~np.isin(self.row_bin,
                                np.fromiter(exempt, dtype=np.int64))
                out = self.rows[mask] if not mask.all() else self.rows
            else:
                out = self.rows
        self._per_net[key] = out
        return out


_SEED_POINTS = None
_SEED_MAPS: Dict[Tuple[float, tuple], _SeedMap] = {}
# The in-process restart's seed (points from the first attempt's field), and
# whether the restarted attempt is running.
_RESTART = {'points': None, 'active': False}


def _seed_map(config) -> Optional[_SeedMap]:
    global _SEED_POINTS
    if _RESTART['points'] is not None:
        k = ('restart', float(config.grid_step), tuple(config.layers))
        sm = _SEED_MAPS.get(k)
        if sm is None:
            sm = _SeedMap(_RESTART['points'], k[1], k[2])
            _SEED_MAPS[k] = sm
        return sm
    if not KNOBS['seed']:
        return None
    if _SEED_POINTS is None:
        _SEED_POINTS = _load_points(KNOBS['seed']) or False
    if _SEED_POINTS is False:
        return None
    k = (float(config.grid_step), tuple(config.layers))
    sm = _SEED_MAPS.get(k)
    if sm is None:
        sm = _SeedMap(_SEED_POINTS, k[0], k[1])
        _SEED_MAPS[k] = sm
    return sm


def add_hindsight_source(ghost_costs, config, pcb_data, net_ids):
    """Fold the seeded map into a merge_track_proximity_costs ghost dict for
    the net(s) being routed (unchanged when no seed is armed)."""
    if KNOBS['cost'] <= 0:
        return ghost_costs
    sm = _seed_map(config)
    if sm is None:
        return ghost_costs
    ids = tuple(net_ids) if isinstance(net_ids, (tuple, list, set)) \
        else (net_ids,)
    rows = sm.rows_for(pcb_data, ids)
    if rows is None or not len(rows):
        return ghost_costs
    merged = dict(ghost_costs or {})
    merged[SOURCE_KEY] = rows
    return merged


def order_first(net_ids, pcb_data, config, front_ids=frozenset()):
    """KICAD_HINDSIGHT_FIRST: nets whose pads reach a hot bin route first,
    keeping relative order and the direct-first block (``front_ids``) ahead."""
    if not KNOBS['first']:
        return net_ids
    sm = _seed_map(config)
    if sm is None or not sm.hot:
        return net_ids
    pads_by_net = getattr(pcb_data, 'pads_by_net', {}) or {}
    hot = {nid for _nm, nid in net_ids
           if sm._pad_bins(pads_by_net.get(nid, [])) & sm.hot}
    if hot and KNOBS['first_big'] > 0:
        from net_cost import net_cost, big_net_threshold
        th = big_net_threshold(pcb_data, [nid for _nm, nid in net_ids],
                               factor=KNOBS['first_big'])
        if th > 0:
            hot = {nid for nid in hot if net_cost(pcb_data, nid) <= th}
    if not hot:
        return net_ids
    out = sorted(net_ids, key=lambda t: (t[1] not in front_ids, t[1] not in hot))
    names = [nm for nm, nid in out if nid in hot]
    print(f"Hindsight route-first: {len(hot)} net(s) reaching a hot area "
          f"moved ahead: {', '.join(names[:8])}{', ...' if len(names) > 8 else ''}",
          flush=True)
    return out


def _summary_open(summary: Optional[dict]) -> bool:
    """True when a pass's JSON_SUMMARY_MIN says something is still open."""
    if not summary:
        return True
    ppo = summary.get('pad_pairs_open') or {}
    return bool(summary.get('failed') or summary.get('failed_single')
                or summary.get('open_single') or summary.get('multipoint_deficit')
                or (ppo.get('count') if isinstance(ppo, dict) else ppo))


def run_passes(output_file: str) -> None:
    """KICAD_HINDSIGHT_PASSES > 1: run route.py's whole step as child passes,
    each seeded with the earlier passes' maps, then exit. Returns (doing
    nothing) when the knob is unset or this is already a child pass."""
    import json
    import subprocess
    import sys
    n = int(os.environ.get('KICAD_HINDSIGHT_PASSES') or 1)
    if n < 2:
        return
    quick_first = os.environ.get('KICAD_HINDSIGHT_QUICK_FIRST', '') not in ('', '0')
    hits = [i for i, a in enumerate(sys.argv) if a == output_file]
    if len(hits) != 1:
        print(f"Hindsight passes: cannot place the output {output_file!r} in "
              f"argv uniquely; routing a single pass", flush=True)
        return
    from copy_board import copy_board
    out = os.path.abspath(output_file)
    work = out + '.hindsight'
    os.makedirs(work, exist_ok=True)
    seeds = []
    rc = 0
    for p in range(1, n + 1):
        last = p == n
        dst = out if last else os.path.join(work, f'p{p}',
                                            os.path.basename(out))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        argv = [sys.executable] + list(sys.argv)
        argv[hits[0] + 1] = dst
        env = dict(os.environ)
        env['KICAD_HINDSIGHT_PASSES'] = '1'
        env.pop('REDO_MANIFEST', None)
        env['KICAD_HINDSIGHT_DUMP'] = os.path.join(work, f'p{p}.npz')
        if seeds:
            env['KICAD_HINDSIGHT_SEED'] = seeds[-1]
        else:
            env.pop('KICAD_HINDSIGHT_SEED', None)
        if quick_first and p == 1:
            env['KICAD_HINDSIGHT_QUICK'] = 'phase3'
        else:
            env.pop('KICAD_HINDSIGHT_QUICK', None)
        print(f"===== Hindsight pass {p}/{n}"
              f"{' (seeded from pass ' + str(p - 1) + ')' if seeds else ''}"
              f" -> {dst}", flush=True)
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors='replace')
        summary = None
        for line in proc.stdout:
            sys.stdout.write(line)
            if line.startswith('JSON_SUMMARY_MIN: '):
                try:
                    summary = json.loads(line[len('JSON_SUMMARY_MIN: '):])
                except ValueError:
                    summary = None
        sys.stdout.flush()
        rc = proc.wait()
        if last:
            break
        if quick_first and p == 1:
            seeds.append(env['KICAD_HINDSIGHT_DUMP'])
            continue
        if not _summary_open(summary):
            copy_board(dst, out)
            print(f"===== Hindsight pass {p} left nothing open; shipped it",
                  flush=True)
            break
        seeds.append(env['KICAD_HINDSIGHT_DUMP'])
    sys.exit(rc)


def quick_exit(stage: str, failed: int, iterations: int, top_level: bool,
               routed_results=None, config=None) -> None:
    """KICAD_HINDSIGHT_QUICK: end a top-level run after ``stage`` (the dump is
    written by the atexit hook). The status line carries the candidate restart
    triggers: failed nets, unreached multipoint pads, and the history events."""
    if KNOBS['quick'] != stage or not top_level:
        return
    import sys
    rr = routed_results or {}
    tap_failed = sum(r.get('tap_edges_failed', 0) for r in rr.values())
    open_nets = sum(1 for r in rr.values() if r.get('failed_pads_info'))
    hf = getattr(config, '_history_cong', None)
    ev = (f" contests={hf.contests} rips={hf.rips} frontiers={hf.frontiers}"
          f" cells={hf.keys.size}") if hf is not None else ""
    print(f"HINDSIGHT_QUICK: stopped after {stage}: failed={failed} "
          f"tap_edges_failed={tap_failed} nets_with_unreached_pads={open_nets}"
          f" iterations={iterations}{ev}", flush=True)
    sys.exit(0)


def restart_enabled() -> bool:
    return KNOBS['restart']


def maybe_restart(top_level: bool, failed: int, iterations: int, config, rerun):
    """KICAD_HINDSIGHT_RESTART: after the routing phases of the OUTERMOST
    attempt, re-enter batch_route (``rerun``) seeded with this attempt's map
    when nets are still failed. Returns (True, rerun's result) when it
    restarted, or None to carry on with this attempt."""
    if not (KNOBS['restart'] and top_level) or _RESTART['active'] or failed <= 0:
        return None
    field = getattr(config, '_history_cong', None)
    pts = _field_points(field, float(config.grid_step), tuple(config.layers)) \
        if field is not None else None
    if pts is None:
        return None
    print(f"HINDSIGHT_RESTART: {failed} net(s) failed after the routing phases "
          f"({iterations} iterations); restarting them from the input with "
          f"the {pts[0].size}-cell congestion map", flush=True)
    import fab_tiers
    fab_tiers.reset_ledger()
    _RESTART['points'] = pts + (1,)
    _RESTART['active'] = True
    _SEED_MAPS.clear()
    try:
        return True, rerun()
    finally:
        _RESTART['points'] = None
        _RESTART['active'] = False
        _SEED_MAPS.clear()
