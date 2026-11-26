"""Exact same-layer escape from a boxed pad.

The grid map blocks a cell when a track end there could come within
clearance of foreign copper, and it widens every pad's CORNER by half a cell
(`routing_utils.pad_blocked_cells_array`'s corner buffer) so a diagonal step
past a corner cannot shave it. At a fine-pitch pin whose only exit is a
channel exactly as wide as the rules allow -- a 0.15 mm track between two
neighbours held 0.25 mm off it by a .kicad_dru pad rule on a 0.5 mm pitch --
the channel is open along the pad and closed where it passes the neighbours'
corners, so the search starts on the pad and never leaves it, although a
straight track out along the channel keeps every clearance.

`escape_launches` looks for such an exit near a pad of a stuck side: a path
over the map's free cells plus the blocked cells near the pad that an EXACT
test passes (pads with their real corner radius and the board's rules, tracks,
vias, holes, the board edge, keep-outs, pour barriers, sense copper), from
the pad's own copper to a free cell outside the pocket the search was stuck
in. Each path becomes straight segments at the run's track width, tested
again exactly as laid. The caller adds the far cell as a terminal of the
stuck side and ships the segments when the route ends there.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Dict, List, Optional, Tuple

from kicad_parser import Segment

#: Exact-tested cells reach at most this far (mm) past the pad's copper.
ESCAPE_REACH_MM = 0.3
#: Free cells a launch must have among its 8 neighbours outside the pocket.
LAUNCH_MIN_OPEN = 3
#: Launches tried per stuck pad.
MAX_LAUNCHES = 4
#: How deep inside the pad's copper (mm) a stub's pad end lies.
INSIDE_MM = 0.01
#: Piece length (mm) a shipped segment's pad distance is sampled over.
FINAL_SAMPLE_MM = 0.002

_NB8 = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


class _ExactTest:
    """Exact clearance of new track copper of `net_id` at width `w`."""

    def __init__(self, pcb_data, config, net_id, w):
        self.pcb = pcb_data
        self.cfg = config
        self.net = net_id
        self.w = w
        from single_ended_routing import _keepout_gate
        self.gate = _keepout_gate(pcb_data)
        from obstacle_map import resolve_hole_clearance
        from routing_defaults import NPTH_TO_TRACK_CLEARANCE
        self.hole_clr = max(resolve_hole_clearance(pcb_data, config) or 0.0,
                            NPTH_TO_TRACK_CLEARANCE)
        self.edge_clr = (config.board_edge_clearance
                         if getattr(config, 'board_edge_clearance', 0) > 0
                         else config.clearance)
        from check_drc import board_edge_geometry
        bi = getattr(pcb_data, 'board_info', None)
        self.rings, self.outer, self.cutouts = (
            board_edge_geometry(bi, rect_ring=True) if bi is not None
            else ([], None, []))
        self.barriers = self._barriers()
        self.areas = self._areas()

    def _barriers(self):
        out = {}
        bars = getattr(self.cfg, 'pour_barriers', None) or []
        if not bars:
            return out
        from shapely.geometry import Polygon
        name_to_id = {n.name: nid for nid, n in self.pcb.nets.items()}
        for b in bars:
            nid = name_to_id.get(b.get('net'))
            pair = (self.cfg.obstacle_clearance(nid) if nid is not None
                    else self.cfg.clearance)
            clr = max(pair, b.get('clearance') or 0.0)
            for poly in b.get('polygons') or []:
                if len(poly) >= 3:
                    out.setdefault(b.get('layer'), []).append(
                        (Polygon(poly).buffer(0), clr))
        return out

    def _areas(self):
        """{layer: [(region, clearance)]}: the rule areas and mask artwork
        the map keeps tracks out of (mask_artwork.routing_keepouts) and the
        net-filtered keep-outs binding this net, each held at the clearance
        the map stamps it at."""
        from shapely.geometry import Polygon
        from mask_artwork import routing_keepouts
        from net_keepouts import binds
        name = getattr(self.pcb.nets.get(self.net), 'name', '')
        kos = list(routing_keepouts(self.pcb))
        kos += [k for k in (getattr(self.pcb, '_net_keepouts', None) or [])
                if not k.get('layer_ban') and not k.get('edge_band')
                and name and binds(k, name)]
        out = {}
        for k in kos:
            if k.get('tracks_allowed', True) or len(k.get('polygon') or ()) < 3:
                continue
            clr = k.get('clearance')
            clr = self.cfg.clearance if clr is None else clr
            poly = Polygon(k['polygon'], [h for h in k.get('holes') or [] if len(h) >= 3]).buffer(0)
            lays = k.get('layers') or {'*.Cu'}
            for ln in self.cfg.layers:
                if (ln in lays or '*.Cu' in lays
                        or (ln in ('F.Cu', 'B.Cu') and ({'F&B.Cu', 'F&B'} & set(lays)))):
                    out.setdefault(ln, []).append((poly, clr))
        return out

    def ok(self, x1, y1, x2, y2, layer, final=False) -> bool:
        """May the track (x1, y1)-(x2, y2) be laid? `final`: the segment
        ships, so the pad distance (sampled along the segment) is taken over
        pieces of at most FINAL_SAMPLE_MM, which bounds its error far below
        a micron."""
        from single_ended_routing import (
            _seg_foreign_pad_dist, _seg_foreign_seg_dist,
            _seg_foreign_via_dist, _seg_foreign_hole_dist)
        cfg, net, hw = self.cfg, self.net, self.w / 2.0
        own = cfg.layer_clearance(layer, cfg.obstacle_clearance(net))
        nc = getattr(cfg, 'net_clearances', None) or None
        tc = getattr(cfg, 'track_clearances', None) or None
        need = own + hw - 1e-6
        n = (max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / FINAL_SAMPLE_MM))
             if final else 1)
        for i in range(n):
            a, b = i / n, (i + 1) / n
            if _seg_foreign_pad_dist(self.pcb, net, x1 + (x2 - x1) * a, y1 + (y2 - y1) * a,
                                     x1 + (x2 - x1) * b, y1 + (y2 - y1) * b, layer,
                                     base_clearance=own, net_clearances=nc,
                                     config=cfg) < need:
                return False
        if _seg_foreign_seg_dist(self.pcb, net, x1, y1, x2, y2, layer,
                                 net_clearances=nc, base_clearance=own,
                                 track_clearances=tc, exact=True,
                                 config=cfg) < need:
            return False
        if _seg_foreign_via_dist(self.pcb, net, x1, y1, x2, y2, layer,
                                 net_clearances=nc, base_clearance=own,
                                 config=cfg) < need:
            return False
        if _seg_foreign_hole_dist(self.pcb, net, x1, y1, x2, y2,
                                  base_clearance=self.hole_clr) \
                < self.hole_clr + hw - 1e-6:
            return False
        from sense_branch import terminal_sense_dist
        if terminal_sense_dist(self.pcb, net, x1, y1, x2, y2, layer, hw) < need:
            return False
        if self.rings:
            from check_drc import _segment_to_rings_distance, _point_on_board
            for x, y in ((x1, y1), (x2, y2)):
                if not _point_on_board(x, y, self.outer, self.cutouts):
                    return False
            if _segment_to_rings_distance(x1, y1, x2, y2, self.rings) \
                    < self.edge_clr + hw - 1e-6:
                return False
        if self.gate is not None and self.gate(segments=[Segment(
                start_x=x1, start_y=y1, end_x=x2, end_y=y2, width=self.w,
                layer=layer, net_id=net)]):
            return False
        for z in getattr(cfg, 'bga_exclusion_zones', None) or ():
            if (min(x1, x2) - hw <= z[2] and max(x1, x2) + hw >= z[0]
                    and min(y1, y2) - hw <= z[3] and max(y1, y2) + hw >= z[1]):
                return False
        regions = self.barriers.get(layer, []) + self.areas.get(layer, [])
        if regions:
            from shapely.geometry import LineString, Point
            g = (Point(x1, y1) if (x1, y1) == (x2, y2)
                 else LineString([(x1, y1), (x2, y2)]))
            for poly, clr in regions:
                if poly.distance(g) < clr + hw - 1e-6:
                    return False
        return True


def _simplify(cells):
    """Grid cells -> the cells where the direction changes (ends kept)."""
    if len(cells) <= 2:
        return list(cells)
    out = [cells[0]]
    for a, b, c in zip(cells, cells[1:], cells[2:]):
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            out.append(b)
    out.append(cells[-1])
    return out


def escape_launches(obstacles, config, pcb_data, net_id, pad, layer_idx,
                    side_cells, coord) -> List[Tuple[Tuple[int, int, int], list]]:
    """[((gx, gy, layer_idx), [Segment, ...]), ...]: free cells outside the
    stuck pocket of `side_cells` around `pad` on layer `layer_idx`, each with
    the exact-clear straight segments joining it to the pad's copper. Empty
    when the pocket is not closed near the pad or no such exit exists."""
    from check_drc import point_to_pad_distance
    layer = config.layers[layer_idx]
    step = coord.grid_step
    w = min(config.track_width, config.get_net_track_width(net_id, layer))
    test = _ExactTest(pcb_data, config, net_id, w)

    hx, hy = pad.size_x / 2.0, pad.size_y / 2.0
    if getattr(pad, 'rect_rotation', 0):
        r = math.hypot(hx, hy)
        hx = hy = r
    span = ESCAPE_REACH_MM + 3 * step
    gx0, gy0 = coord.to_grid(pad.global_x - hx - span, pad.global_y - hy - span)
    gx1, gy1 = coord.to_grid(pad.global_x + hx + span, pad.global_y + hy + span)

    def inside(c):
        return gx0 <= c[0] <= gx1 and gy0 <= c[1] <= gy1

    def free(c):
        return not obstacles.is_blocked(c[0], c[1], layer_idx)

    # The pocket: what the stuck search could reach from its own cells.
    seeds = [(c[0], c[1]) for c in side_cells if c[2] == layer_idx and inside(c)]
    if not seeds:
        return []
    pocket = set(seeds)
    q = deque(seeds)
    while q:
        c = q.popleft()
        for dx, dy in _NB8:
            n = (c[0] + dx, c[1] + dy)
            if n in pocket or not free(n):
                continue
            if not inside(n):
                return []           # not closed near this pad
            pocket.add(n)
            q.append(n)

    # Cell verdicts carry over between calls on the same copper (the rescue
    # and reconciliation laps ask about the same pads); the segments a
    # launch ships are always tested afresh.
    store = getattr(pcb_data, '_pad_escape_memo', None)
    if store is None or len(store) > 64:
        store = pcb_data._pad_escape_memo = {}
    exact_memo: Dict[Tuple[int, int], bool] = store.setdefault(
        (getattr(pcb_data, '_copper_epoch', 0), len(pcb_data.segments),
         len(pcb_data.vias), net_id, layer, w, step, config.clearance,
         hash(frozenset((getattr(config, 'net_clearances', None) or {}).items()))), {})

    def to_mm(c):
        return c[0] * step, c[1] * step

    def pad_gap(c):
        x, y = to_mm(c)
        return point_to_pad_distance(x, y, pad)

    def passable(c):
        if free(c):
            return True
        v = exact_memo.get(c)
        if v is None:
            v = pad_gap(c) <= ESCAPE_REACH_MM and test.ok(*to_mm(c), *to_mm(c), layer)
            exact_memo[c] = v
        return v

    def is_launch(c):
        if c in pocket or not free(c):
            return False
        n_open = sum(1 for dx, dy in _NB8
                     if (c[0] + dx, c[1] + dy) not in pocket
                     and free((c[0] + dx, c[1] + dy)))
        return n_open >= LAUNCH_MIN_OPEN

    def on_pad(c):
        # inside the pad's copper by INSIDE_MM, so the stub's end is the
        # pad's by any reading of the edge
        x, y = to_mm(c)
        return all(point_to_pad_distance(x + dx, y + dy, pad) <= 0.0
                   for dx, dy in ((INSIDE_MM, 0), (-INSIDE_MM, 0),
                                  (0, INSIDE_MM), (0, -INSIDE_MM)))

    starts = []
    for gx in range(gx0, gx1 + 1):
        for gy in range(gy0, gy1 + 1):
            c = (gx, gy)
            if on_pad(c) and passable(c):
                starts.append(c)
    if not starts:
        return []

    out = []
    banned = set()
    for _ in range(3 * MAX_LAUNCHES):
        if len(out) >= MAX_LAUNCHES:
            break
        prev = {c: None for c in starts}
        q = deque(starts)
        goal = None
        while q and goal is None:
            c = q.popleft()
            for dx, dy in _NB8:
                n = (c[0] + dx, c[1] + dy)
                if n in prev or n in banned or not inside(n) or not passable(n):
                    continue
                prev[n] = c
                if is_launch(n):
                    goal = n
                    break
                q.append(n)
        if goal is None:
            break
        cells = []
        c = goal
        while c is not None:
            cells.append(c)
            c = prev[c]
        cells.reverse()
        # Start from the LAST cell on the pad's copper: the stub begins there.
        k = max(i for i, c in enumerate(cells) if on_pad(c))
        pts = _simplify(cells[k:])
        if len(pts) > 2:
            # one straight octilinear leg from the pad when one is clear
            line = _straight_leg(starts, goal, test, to_mm, layer)
            if line is not None:
                pts = [line, goal]
        segs = []
        good = True
        for a, b in zip(pts, pts[1:]):
            (x1, y1), (x2, y2) = to_mm(a), to_mm(b)
            if not test.ok(x1, y1, x2, y2, layer, final=True):
                good = False
                break
            segs.append(Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                                width=w, layer=layer, net_id=net_id))
        banned.add(goal)
        if good and segs:
            out.append(((goal[0], goal[1], layer_idx), segs))
    # the fewest legs first; a straight exit makes the bent ones moot
    out.sort(key=lambda r: (len(r[1]), sum(math.hypot(g.end_x - g.start_x,
                                                       g.end_y - g.start_y)
                                            for g in r[1])))
    if out and len(out[0][1]) == 1:
        out = [r for r in out if len(r[1]) == 1]
    return out


def _straight_leg(starts, goal, test, to_mm, layer):
    """The start cell nearest `goal` on an octilinear line to it whose
    straight segment is exact-clear, or None."""
    best = None
    for c in starts:
        dx, dy = goal[0] - c[0], goal[1] - c[1]
        if not (dx == 0 or dy == 0 or abs(dx) == abs(dy)):
            continue
        d = max(abs(dx), abs(dy))
        if best is not None and d >= best[0]:
            continue
        if test.ok(*to_mm(c), *to_mm(goal), layer):
            best = (d, c)
    return None if best is None else best[1]
