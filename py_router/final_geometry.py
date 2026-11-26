"""Track geometry a shipped board must hold, laid by construction.

Every track end lands exactly on what it joins (a via centre, inside a pad,
another track's vertex or centreline); no piece is shorter than
MIN_TRACK_FRAGMENT unless it enters a pad or a via; every corner turns by
0, 45 or 90 degrees, except on the net's own pad. `legalize_track_geometry`
repairs a board towards that by re-laying short runs of a track chain as
octolinear paths that clear foreign copper at the smoother's rules and grow
no foreign pour's carve, and keeps a net's edit only when the net is no
less connected. Locked, sense-held, arc and read-only copper is never
touched. The work is bounded by counts: a window search per defect, and a
number of edits per chain proportional to its length.
"""

import math
from collections import defaultdict

from kicad_parser import Segment, is_immutable_copper
from routing_constants import MIN_TRACK_FRAGMENT

ANGLE_TOL_DEG = 0.05
_PERMITTED = (0.0, 45.0, 90.0)
_MERGE_DEVIATION = 0.001
_EPS = 1e-6


def _key(x, y):
    return (int(round(x * 1e6)), int(round(y * 1e6)))


def _snap(p):
    return (round(p[0] * 1e6) / 1e6, round(p[1] * 1e6) / 1e6)


def turn(corner, a, b):
    """The ROUTE.ANGLE_STYLE turn at `corner` between legs ending at a, b."""
    v1 = (a[0] - corner[0], a[1] - corner[1])
    v2 = (b[0] - corner[0], b[1] - corner[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if l1 < _EPS or l2 < _EPS:
        return None
    c = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
    return 180.0 - math.degrees(math.acos(max(-1.0, min(1.0, c))))


def legal_turn(t):
    return t is None or any(abs(t - p) <= ANGLE_TOL_DEG for p in _PERMITTED)


def joint_ok(corner, legs):
    """The gate's corner rule at a meeting of `legs` (far points)."""
    if len(legs) < 2:
        return True
    if len(legs) == 2:
        return legal_turn(turn(corner, legs[0], legs[1]))
    for i, a in enumerate(legs):
        ts = [turn(corner, a, b) for j, b in enumerate(legs) if j != i]
        ts = [t for t in ts if t is not None]
        if ts and not legal_turn(min(ts)):
            return False
        if any(not legal_turn(min(t, 180.0 - t)) for t in ts):
            return False
    return True


_OCTO_TOL_DEG = ANGLE_TOL_DEG / 2.0


def exact_octolinear(dx, dy):
    return abs(dx) <= _EPS or abs(dy) <= _EPS or abs(abs(dx) - abs(dy)) <= 2 * _EPS


def octolinear(dx, dy):
    if exact_octolinear(dx, dy):
        return True
    a = math.degrees(math.atan2(abs(dy), abs(dx)))
    return min(a, abs(a - 45.0), 90.0 - a) <= _OCTO_TOL_DEG


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def _foot(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L <= 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L))
    return (ax + t * dx, ay + t * dy)


def _line_cross(a, b, c, d):
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    return (a[0] + t * rx, a[1] + t * ry)


def _split_offsets(total):
    """Where a Z path may put its middle leg along a run of `total` mm."""
    m = MIN_TRACK_FRAGMENT
    out = []
    for a in (m, 1.5 * m, 2 * m, 3 * m, total / 2.0,
              total - 3 * m, total - 2 * m, total - 1.5 * m, total - m):
        a = round(a * 1e6) / 1e6
        if m - _EPS <= a <= total - m + _EPS and a not in out:
            out.append(a)
    return out


def octolinear_paths(A, B, z_paths=True):
    """Candidate octolinear polylines from A to B: the straight run, the two
    diagonal+axis elbows, the two axis+axis elbows, and (`z_paths`) Z paths
    whose middle leg sits at a few offsets along the run."""
    dx, dy = B[0] - A[0], B[1] - A[1]
    if math.hypot(dx, dy) < _EPS:
        return []
    if octolinear(dx, dy):
        return [[A, B]]
    sx, sy = (1 if dx > 0 else -1), (1 if dy > 0 else -1)
    ax, ay = abs(dx), abs(dy)
    m = min(ax, ay)
    out = [[A, (A[0] + sx * m, A[1] + sy * m), B],
           [A, (B[0] - sx * m, B[1] - sy * m), B],
           [A, (B[0], A[1]), B],
           [A, (A[0], B[1]), B]]
    if not z_paths:
        return [[_snap(p) for p in path] for path in out]
    rest = abs(ax - ay)
    along_x = ax > ay
    for a in _split_offsets(rest):
        if along_x:
            k1 = (A[0] + sx * a, A[1])
            k2 = (k1[0] + sx * m, B[1])
        else:
            k1 = (A[0], A[1] + sy * a)
            k2 = (B[0], k1[1] + sy * m)
        out.append([A, k1, k2, B])
    for d in _split_offsets(m):
        k1 = (A[0] + sx * d, A[1] + sy * d)
        k2 = ((k1[0] + sx * rest, k1[1]) if along_x
              else (k1[0], k1[1] + sy * rest))
        out.append([A, k1, k2, B])
    return [[_snap(p) for p in path] for path in out]


_CELL = 1.0


def _cells(x0, y0, x1, y1):
    for cx in range(int(math.floor(x0 / _CELL)), int(math.floor(x1 / _CELL)) + 1):
        for cy in range(int(math.floor(y0 / _CELL)), int(math.floor(y1 / _CELL)) + 1):
            yield (cx, cy)


class _Grid:
    """Items bucketed by bounding box, for near-point and near-segment queries."""

    def __init__(self):
        self.cells = defaultdict(dict)
        self.where = {}

    def add(self, obj, box):
        cs = list(_cells(*box))
        self.where[id(obj)] = cs
        for c in cs:
            self.cells[c][id(obj)] = obj

    def remove(self, obj):
        for c in self.where.pop(id(obj), ()):
            self.cells[c].pop(id(obj), None)

    def near(self, x0, y0, x1, y1):
        seen = {}
        for c in _cells(x0, y0, x1, y1):
            seen.update(self.cells.get(c, {}))
        return list(seen.values())


def _seg_box(s, grow=0.0):
    r = s.width / 2.0 + grow
    return (min(s.start_x, s.end_x) - r, min(s.start_y, s.end_y) - r,
            max(s.start_x, s.end_x) + r, max(s.start_y, s.end_y) + r)


class _NetLayer:
    """One net's tracks on one layer, with what anchors them; edited in place."""

    def __init__(self, net_id, layer, segs, vias, pads, copper_layers):
        from connectivity import via_copper_layers
        self.net_id, self.layer = net_id, layer
        self.vias = [v for v in vias if layer in via_copper_layers(v, copper_layers)]
        self.via_keys = {_key(v.x, v.y): v for v in self.vias}
        self.pads = [p for p in pads
                     if layer in p.layers or any('*' in L for L in p.layers)]
        self.pad_grid = _Grid()
        for pd in self.pads:
            r = math.hypot(pd.size_x, pd.size_y) / 2.0
            self.pad_grid.add(pd, (pd.global_x - r, pd.global_y - r,
                                   pd.global_x + r, pd.global_y + r))
        self.seg_grid = _Grid()
        self.segs = {}
        self.adj = defaultdict(list)
        self._inside = {}
        self._reached = {}
        self.custody_map = defaultdict(set)
        for s in segs:
            self._add(s)

    # -- incremental edits -------------------------------------------------
    def _touch(self, k):
        for oid in self._reached.pop(k, ()):
            self.custody_map[oid].discard(k)

    def _add(self, s):
        self.segs[id(s)] = s
        self.seg_grid.add(s, _seg_box(s))
        for k in (_key(s.start_x, s.start_y), _key(s.end_x, s.end_y)):
            self._touch(k)
            self.adj[k].append(s)

    def _drop(self, s):
        self.segs.pop(id(s), None)
        self.seg_grid.remove(s)
        for k in (_key(s.start_x, s.start_y), _key(s.end_x, s.end_y)):
            self._touch(k)
            lst = [t for t in self.adj.get(k, ()) if t is not s]
            if lst:
                self.adj[k] = lst
            else:
                self.adj.pop(k, None)

    def apply(self, old, new):
        for s in old:
            self._drop(s)
        for s in new:
            self._add(s)

    # -- geometry queries --------------------------------------------------
    def near_pads(self, p, r):
        return self.pad_grid.near(p[0] - r, p[1] - r, p[0] + r, p[1] + r)

    def near_segs(self, p, r):
        return self.seg_grid.near(p[0] - r, p[1] - r, p[0] + r, p[1] + r)

    def inside_pad(self, p):
        k = _key(*p)
        hit = self._inside.get(k)
        if hit is None:
            from check_drc import point_to_pad_distance
            hit = any(point_to_pad_distance(p[0], p[1], pd) <= _EPS
                      for pd in self.near_pads(p, 0.0))
            self._inside[k] = hit
        return hit

    def _pads_reaching(self, p, w):
        from check_drc import point_to_pad_distance
        for pd in self.near_pads(p, w):
            if point_to_pad_distance(p[0], p[1], pd) <= w / 2.0:
                yield pd

    def reaches_pad(self, p, w):
        return next(self._pads_reaching(p, w), None)

    def pads_reached(self, p, w):
        return {id(pd) for pd in self._pads_reaching(p, w)}

    def vias_reached(self, p, w):
        return {id(v) for v in self.vias
                if math.hypot(p[0] - v.x, p[1] - v.y) <= v.size / 2.0 + w / 2.0}

    def reached(self, k):
        got = self._reached.get(k)
        if got is None:
            p = (k[0] / 1e6, k[1] / 1e6)
            w = max(s.width for s in self.adj[k])
            got = self._reached[k] = self.pads_reached(p, w) | self.vias_reached(p, w)
            for oid in got:
                self.custody_map[oid].add(k)
        return got

    def custody(self):
        """pad or via id -> the vertex keys whose caps reach it."""
        for k in list(self.adj):
            self.reached(k)
        return self.custody_map

    def reaches_via(self, p, w):
        for v in self.vias:
            if math.hypot(p[0] - v.x, p[1] - v.y) <= v.size / 2.0 + w / 2.0:
                return v
        return None

    def in_via_disc(self, p):
        return any(math.hypot(p[0] - v.x, p[1] - v.y) <= v.size / 2.0
                   for v in self.vias)

    def body_host(self, p, exclude):
        """A same-net segment whose centreline passes within half its width of p."""
        for o in self.near_segs(p, 0.0):
            if o is exclude:
                continue
            if _pt_seg(p[0], p[1], o.start_x, o.start_y, o.end_x, o.end_y) <= o.width / 2.0:
                return o
        return None

    def supported(self, p, seg):
        return (self.inside_pad(p) or self.in_via_disc(p)
                or self.body_host(p, seg) is not None)

    def entry(self, a, b):
        return (_key(*a) in self.via_keys or _key(*b) in self.via_keys
                or self.inside_pad(a) or self.inside_pad(b))

    def far(self, s, k):
        if _key(s.start_x, s.start_y) == k:
            return (s.end_x, s.end_y)
        return (s.start_x, s.start_y)

    def legs_at(self, k, exclude=()):
        ex = {id(x) for x in exclude}
        return [self.far(s, k) for s in self.adj.get(k, ()) if id(s) not in ex]

    def tee_legs(self, p, seg, exclude=()):
        """The gate's body-tee legs at a lone end p landing on a host's body."""
        if self.in_via_disc(p):
            return None
        ex = {id(x) for x in exclude}
        for o in self.near_segs(p, 0.0):
            if o is seg or id(o) in ex:
                continue
            a, b = (o.start_x, o.start_y), (o.end_x, o.end_y)
            if _key(*p) in (_key(*a), _key(*b)):
                continue
            if _pt_seg(p[0], p[1], *a, *b) > o.width / 2.0:
                continue
            fwd = (p[0] + b[0] - a[0], p[1] + b[1] - a[1])
            return [fwd, (2 * p[0] - fwd[0], 2 * p[1] - fwd[1])]
        return None

    def anchor(self, k, widths=True):
        ss = self.adj.get(k, ())
        if len(ss) != 2 or k in self.via_keys or (widths and ss[0].width != ss[1].width):
            return True
        custody = self.custody_map
        return any(len(custody[oid]) == 1 for oid in self.reached(k))

    def chains(self):
        self.custody()
        seen = set()
        out = []
        for k0 in list(self.adj):
            if not self.anchor(k0):
                continue
            for s0 in self.adj[k0]:
                if id(s0) in seen:
                    continue
                segs = []
                pts = [(s0.start_x, s0.start_y) if _key(s0.start_x, s0.start_y) == k0
                       else (s0.end_x, s0.end_y)]
                k, s = k0, s0
                while True:
                    seen.add(id(s))
                    segs.append(s)
                    nxt = self.far(s, k)
                    pts.append(nxt)
                    k = _key(*nxt)
                    if k == k0 or self.anchor(k):
                        break
                    rest = [t for t in self.adj[k] if id(t) not in seen]
                    if not rest:
                        break
                    s = rest[0]
                if k != k0:
                    out.append((pts, segs))
        return out


def _deviation(path, pts):
    """How far the two polylines stray from each other, vertex to polyline."""
    def gap(p, poly):
        return min(_pt_seg(p[0], p[1], *poly[q], *poly[q + 1])
                   for q in range(len(poly) - 1))
    return max([gap(p, pts) for p in path[1:-1]]
               + [gap(p, path) for p in pts[1:-1]] + [0.0])


class _Editor:
    def __init__(self, board, config, clears, readonly_ids, stats):
        self.board, self.config, self.clears = board, config, clears
        self.readonly_ids = readonly_ids
        self.stats = stats
        self.added = {}
        self.removed = {}
        self.copper_layers = list(getattr(board.board_info, 'copper_layers', None) or ())
        self.by_nl = defaultdict(list)
        for s in board.segments:
            self.by_nl[(s.net_id, s.layer)].append(s)
        self.vias_by_net = defaultdict(list)
        for v in board.vias:
            self.vias_by_net[v.net_id].append(v)
        self._copper = None
        self.gone = {}               # id -> segment: held, so no id is reused
        self.fresh = []
        self._pours = {}

    def editable(self, s):
        return (id(s) not in self.readonly_ids and not is_immutable_copper(s)
                and not getattr(s, 'graphic', False) and not getattr(s, 'arc', False))

    def view(self, net_id, layer):
        segs = [s for s in self.by_nl.get((net_id, layer), ()) if id(s) not in self.gone]
        return _NetLayer(net_id, layer, segs, self.vias_by_net.get(net_id, []),
                         self.board.pads_by_net.get(net_id, []), self.copper_layers)

    def sync(self):
        """Fold the edits into board.segments (the clearance probe reads it)."""
        if self.gone or self.fresh:
            self.board.segments = [s for s in self.board.segments
                                   if id(s) not in self.gone] + self.fresh
            for s in self.fresh:
                self.by_nl[(s.net_id, s.layer)].append(s)
            self.by_nl = defaultdict(list, {k: [s for s in v if id(s) not in self.gone]
                                            for k, v in self.by_nl.items()})
            self.gone, self.fresh = {}, []
            self.board._foreign_seg_arr_cache = None

    def replace(self, nl, old, new_pts, width):
        new = [Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                       width=width, layer=nl.layer, net_id=nl.net_id)
               for a, b in zip(new_pts, new_pts[1:])]
        for s in old:
            self.gone[id(s)] = s
            if id(s) in self.added:
                del self.added[id(s)]
            else:
                self.removed[id(s)] = s
        self.fresh = [s for s in self.fresh if id(s) not in self.gone]
        self.fresh.extend(new)
        for s in new:
            self.added[id(s)] = s
        if self._copper is not None:
            # the pour check reads this net's copper as it stands now
            for s in old:
                self._copper[s.layer].remove(s)
            for s in new:
                self._copper[s.layer].add(s, _seg_box(s))
        nl.apply(old, new)
        return new

    def undo_net(self, added0, removed0):
        """Drop every edit since the net began (all still pending)."""
        if self._copper is not None:
            for s in self.fresh:
                self._copper[s.layer].remove(s)
            for s in self.gone.values():
                if id(s) not in self.added:
                    self._copper[s.layer].add(s, _seg_box(s))
        self.gone, self.fresh = {}, []
        self.added, self.removed = added0, removed0

    def copper_near(self, layer, bounds, grow):
        """(net, id, shape) of the copper on `layer` near `bounds`, each grown by
        `grow`: tracks as committed (ed.sync), vias, pads."""
        from shapely.geometry import LineString, Point, box as _box
        if self._copper is None:
            self._copper = defaultdict(_Grid)
            for s in self.board.segments:
                self._copper[s.layer].add(s, _seg_box(s))
            for v in self.board.vias:
                for L in self.copper_layers or ('F.Cu', 'B.Cu'):
                    from connectivity import via_copper_layers
                    if L in via_copper_layers(v, self.copper_layers):
                        r = v.size / 2.0
                        self._copper[L].add(v, (v.x - r, v.y - r, v.x + r, v.y + r))
            for pads in self.board.pads_by_net.values():
                for pd in pads:
                    r = math.hypot(pd.size_x, pd.size_y) / 2.0
                    for L in self.copper_layers or ('F.Cu', 'B.Cu'):
                        if L in pd.layers or any('*' in x for x in pd.layers):
                            self._copper[L].add(pd, (pd.global_x - r, pd.global_y - r,
                                                     pd.global_x + r, pd.global_y + r))
        out = []
        for o in self._copper[layer].near(*bounds):
            if hasattr(o, 'start_x'):
                g = LineString([(o.start_x, o.start_y), (o.end_x, o.end_y)]).buffer(o.width / 2.0 + grow)
            elif hasattr(o, 'drill') and hasattr(o, 'size') and not hasattr(o, 'size_x'):
                g = Point(o.x, o.y).buffer(o.size / 2.0 + grow)
            else:
                hx, hy = o.size_x / 2.0, o.size_y / 2.0
                g = _box(o.global_x - hx, o.global_y - hy, o.global_x + hx, o.global_y + hy)
                if getattr(o, 'rect_rotation', 0):
                    from shapely import affinity
                    g = affinity.rotate(g, -o.rect_rotation, origin=(o.global_x, o.global_y))
                g = g.buffer(grow)
            out.append((o.net_id, id(o), g))
        return out

    def pours(self, layer, net_id):
        """Foreign pours on `layer`: (outline polygon, fill clearance)."""
        if layer not in self._pours:
            from shapely.geometry import Polygon
            rows = []
            for z in getattr(self.board, 'zones', None) or ():
                if z.layer != layer or not z.net_id or len(z.polygon) < 3:
                    continue
                poly = Polygon(z.polygon)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                clr = z.clearance if z.clearance is not None else \
                    getattr(self.config, 'clearance', 0.0) or 0.0
                thick = z.min_thickness if z.min_thickness is not None else _MIN_THICKNESS
                rows.append((z.net_id, poly, clr, thick))
            self._pours[layer] = rows
        return [row for row in self._pours[layer] if row[0] != net_id]

    def net_clearance(self, net_id):
        """The moving net's own clearance, which a pour keeps from it when it
        is larger than the pour's."""
        cfg = self.config
        base = getattr(cfg, 'clearance', 0.0) or 0.0
        nc = getattr(cfg, 'net_clearances', None) or {}
        return max(base, nc.get(net_id, base))


def _pinned(nl, segs):
    """Chain segments another same-net track's end lands on away from their
    own ends, or that another same-net track crosses (pads and vias are held
    by _keeps_custody)."""
    from geometry_utils import segments_intersect
    pinned = set()
    mine = {id(s) for s in segs}
    ends = [(segs[0].start_x, segs[0].start_y), (segs[0].end_x, segs[0].end_y),
            (segs[-1].start_x, segs[-1].start_y), (segs[-1].end_x, segs[-1].end_y)]
    for s in segs:
        a, b = (s.start_x, s.start_y), (s.end_x, s.end_y)
        ka, kb = _key(*a), _key(*b)
        box = _seg_box(s, 0.5)
        for o in nl.seg_grid.near(*box):
            if id(o) in mine:
                continue
            oa, ob = (o.start_x, o.start_y), (o.end_x, o.end_y)
            # the track a chain end lands on is the chain's own anchor
            if any(_pt_seg(e[0], e[1], *oa, *ob) <= o.width / 2.0 + _EPS for e in ends):
                continue
            if any(_key(*p) not in (ka, kb) and
                   _pt_seg(p[0], p[1], *a, *b) <= (s.width + o.width) / 2.0 for p in (oa, ob)):
                pinned.add(id(s))
                break
            if (_key(*oa) not in (ka, kb) and _key(*ob) not in (ka, kb)
                    and segments_intersect(*a, *b, *oa, *ob)):
                pinned.add(id(s))
                break
    return pinned


def _chain_defects(nl, pts, segs):
    """Indices to repair: ('leg', k) short unjustified legs and ('vertex', k)
    illegal turns, ends included where the chain meets other copper."""
    out = []
    n = len(segs)
    for k in range(n):
        a, b = pts[k], pts[k + 1]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < MIN_TRACK_FRAGMENT - _EPS \
                and not nl.entry(a, b):
            out.append(('leg', k))
    for k in range(1, n):
        if nl.inside_pad(pts[k]):
            continue
        if not legal_turn(turn(pts[k], pts[k - 1], pts[k + 1])):
            out.append(('vertex', k))
    for k, seg in ((0, segs[0]), (n, segs[-1])):
        p = pts[k]
        if nl.inside_pad(p):
            continue
        legs = nl.legs_at(_key(*p))
        if len(legs) >= 2:
            if not joint_ok(p, legs):
                out.append(('vertex', k))
        else:
            tee = nl.tee_legs(p, seg)
            if tee and not joint_ok(p, [nl.far(seg, _key(*p))] + tee):
                out.append(('vertex', k))
    return out


def _window_geometry(nl, pts, segs, i, j, path, start_override=False,
                     end_override=False):
    """None when `path` cannot replace segs[i:j] (pts[i]..pts[j]) by the
    gate's rules, else how many of its legs are short pad/via entries."""
    legs = list(zip(path, path[1:]))
    if not legs:
        return None
    n = len(segs)
    entries = 0
    for q, (a, b) in enumerate(legs):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        if L < _EPS or not octolinear(b[0] - a[0], b[1] - a[1]):
            return None
        if L < MIN_TRACK_FRAGMENT - _EPS:
            ends_ok = ((q == 0 and i == 0) or (q == len(legs) - 1 and j == n))
            if not (ends_ok and nl.entry(a, b)):
                return None
            entries += 1
    for q in range(1, len(path) - 1):
        if not nl.inside_pad(path[q]) and \
                not legal_turn(turn(path[q], path[q - 1], path[q + 1])):
            return None
    # a moved end enters what it lands on with its own leg only: a leg inside
    # that barrel or pad already is copper the strict collapse calls removable
    if end_override and _enters_early(nl, path[-1], path[:-1]):
        return None
    if start_override and _enters_early(nl, path[0], path[1:]):
        return None
    if any(_enters_early(nl, a, (b,)) for a, b in legs):
        return None
    gone = segs[i:j]
    for P, inner, idx, outside, override in (
            (path[0], path[1], i, i > 0, start_override),
            (path[-1], path[-2], j, j < n, end_override)):
        if nl.inside_pad(P):
            continue
        if outside and not override:
            nb = pts[idx - 1] if idx == i else pts[idx + 1]
            if not legal_turn(turn(P, nb, inner)):
                return None
            continue
        legs_p = nl.legs_at(_key(*P), exclude=gone) + [inner]
        if len(legs_p) >= 2:
            if not joint_ok(P, legs_p):
                return None
        else:
            tee = nl.tee_legs(P, None, exclude=gone)
            if tee and not joint_ok(P, [inner] + tee):
                return None
    return entries


def _enters_early(nl, target, others):
    """Does any of `others` already lie in the via barrel or pad `target` is in?"""
    for v in nl.vias:
        if math.hypot(target[0] - v.x, target[1] - v.y) <= v.size / 2.0 and \
                any(math.hypot(p[0] - v.x, p[1] - v.y) <= v.size / 2.0 for p in others):
            return True
    if nl.inside_pad(target):
        from check_drc import point_to_pad_distance
        for pd in nl.near_pads(target, 0.0):
            if point_to_pad_distance(target[0], target[1], pd) <= _EPS and \
                    any(point_to_pad_distance(p[0], p[1], pd) <= _EPS for p in others):
                return True
    return False


def _residual(nl, poly):
    """Short unjustified legs and illegal interior turns left on a polyline."""
    bad = 0
    for k in range(len(poly) - 1):
        a, b = poly[k], poly[k + 1]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < MIN_TRACK_FRAGMENT - _EPS \
                and not nl.entry(a, b):
            bad += 1
    for k in range(1, len(poly) - 1):
        if not nl.inside_pad(poly[k]) and \
                not legal_turn(turn(poly[k], poly[k - 1], poly[k + 1])):
            bad += 1
    return bad


def _body_reached(nl, a, b, w):
    from check_drc import point_to_pad_distance
    out = {id(v) for v in nl.vias
           if _pt_seg(v.x, v.y, *a, *b) <= v.size / 2.0 + w / 2.0}
    L = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(L / 0.025) + 1)
    r0 = w / 2.0
    for pd in nl.pad_grid.near(min(a[0], b[0]) - r0, min(a[1], b[1]) - r0,
                               max(a[0], b[0]) + r0, max(a[1], b[1]) + r0):
        r = max(pd.size_x, pd.size_y) + w
        if _pt_seg(pd.global_x, pd.global_y, *a, *b) > r:
            continue
        if any(point_to_pad_distance(a[0] + (b[0] - a[0]) * q / n,
                                     a[1] + (b[1] - a[1]) * q / n, pd) <= w / 2.0
               for q in range(n + 1)):
            out.add(id(pd))
    return out


def _keeps_custody(nl, pts, segs, i, j, path, width):
    """Every pad and via the replaced copper reached, at a vertex or along a
    track, is still reached: by the new path or by a vertex left in place."""
    gone_keys = {_key(*p) for p in pts[i + 1:j]}
    if _key(*path[0]) != _key(*pts[i]):
        gone_keys.add(_key(*pts[i]))
    if _key(*path[-1]) != _key(*pts[j]):
        gone_keys.add(_key(*pts[j]))
    old = set()
    for k in gone_keys:
        if k in nl.adj:
            old |= nl.reached(k)
    for s in segs[i:j]:
        old |= _body_reached(nl, (s.start_x, s.start_y), (s.end_x, s.end_y), s.width)
    if not old:
        return True
    custody = nl.custody()
    kept = {oid for oid in old if custody[oid] - gone_keys}
    need = old - kept
    if not need:
        return True
    for p in path:
        need -= nl.pads_reached(p, width) | nl.vias_reached(p, width)
    for a, b in zip(path, path[1:]):
        if not need:
            break
        need -= _body_reached(nl, a, b, width)
    return not need


def _clears_path(nl, ed, segs, i, j, path, width):
    old = {(_key(s.start_x, s.start_y), _key(s.end_x, s.end_y)) for s in segs[i:j]}
    for a, b in zip(path, path[1:]):
        ka, kb = _key(*a), _key(*b)
        if (ka, kb) in old or (kb, ka) in old:
            continue
        if not ed.clears(a[0], a[1], b[0], b[1], nl.layer, nl.net_id, width):
            return False
    return True


def _splits_pours(ed, nl, old, path, width):
    """Would the new copper split a foreign pour where the copper it replaces
    did not? The pass runs after the plane finalize, so nothing after it
    would rejoin a pour it cut. Judged on the pour's fill model around the
    edit as KiCad decides connectivity: the outline less every other net's
    copper at its clearance, opened by the zone's min_thickness (eroded by
    half of it and grown back, so a strip thinner than that carries no
    fill). The fill pieces there may not grow in number."""
    pours = ed.pours(nl.layer, nl.net_id)
    if not pours:
        return False
    from shapely.geometry import LineString, box as _box
    from shapely.ops import unary_union
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    own = ed.net_clearance(nl.net_id)
    for zone_net, poly, zclr, thick in pours:
        clr = max(zclr, own)
        bx0, by0, bx1, by1 = poly.bounds
        r = width / 2.0 + clr
        if max(xs) + r < bx0 or min(xs) - r > bx1 or max(ys) + r < by0 or min(ys) - r > by1:
            continue
        new = LineString(path).buffer(width / 2.0 + clr)
        was = unary_union([LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)])
                           .buffer(s.width / 2.0 + clr + _POUR_SLACK) for s in old])
        grown = new.difference(was).intersection(poly)
        if grown.area <= _POUR_AREA:
            continue
        x0, y0, x1, y1 = grown.bounds
        win = _box(x0 - _POUR_WINDOW, y0 - _POUR_WINDOW, x1 + _POUR_WINDOW, y1 + _POUR_WINDOW)
        mine = {id(s) for s in old}
        blocks = [g for nid, oid, g in ed.copper_near(nl.layer, win.bounds, zclr)
                  if nid != zone_net and oid not in mine]
        region = poly.intersection(win)
        rest = unary_union(blocks) if blocks else None

        def pieces(carve):
            fill = region.difference(carve if rest is None else rest.union(carve))
            if thick > 0:
                # KiCad keeps a strip exactly min_thickness wide
                k = thick / 2.0 - _THICK_SLACK
                fill = fill.buffer(-k).buffer(k)
            return sum(1 for g in getattr(fill, 'geoms', [fill])
                       if not g.is_empty and g.area > _POUR_AREA)
        if pieces(new) > pieces(was):
            return True
    return False


_POUR_WINDOW = 2.0
_MIN_THICKNESS = 0.25          # KiCad's default zone min_thickness
_THICK_SLACK = 0.001
_POUR_SLACK = 0.001
_POUR_AREA = 1e-6


def _clears_path(nl, ed, segs, i, j, path, width):
    old = {(_key(s.start_x, s.start_y), _key(s.end_x, s.end_y)): s.width
           for s in segs[i:j]}
    for a, b in zip(path, path[1:]):
        ka, kb = _key(*a), _key(*b)
        was = old.get((ka, kb), old.get((kb, ka)))
        if was is not None and was >= width:
            continue
        if not ed.clears(a[0], a[1], b[0], b[1], nl.layer, nl.net_id, width):
            return False
    return True


_MAX_WINDOW = 8
_MAX_EVALS = 1000
_MAX_TARGETS = 6
_LOCAL_WINDOW = 6
_GLOBAL_PROBES = 12
_LOCAL_PROBES = 24
_POUR_PROBES = 4


def _try_windows(nl, ed, pts, segs, lo, hi, pinned, start_targets=None,
                 end_targets=None, bound=None):
    """Re-lay a window [i, j] around pts[lo..hi] with an octolinear path the
    gate's rules allow, that clears foreign copper and grows no foreign
    pour's carve. First the paths that leave the chain fewest defects,
    straying least from the copper they replace; failing those, windows of
    a few segments around the defect, fewest defects left first. At most
    _MAX_WINDOW segments either side and _MAX_EVALS paths are weighed.
    Returns (i, j, path, new segments) or None."""
    n = len(segs)
    bad = [0] * (2 * n + 1)          # leg k at 2k+1, vertex k at 2k
    for k in range(max(0, lo - _MAX_WINDOW - 1), min(n, hi + _MAX_WINDOW + 1)):
        a, b = pts[k], pts[k + 1]
        bad[2 * k + 1] = int(math.hypot(b[0] - a[0], b[1] - a[1])
                             < MIN_TRACK_FRAGMENT - _EPS and not nl.entry(a, b))
        if k:
            bad[2 * k] = int(not nl.inside_pad(pts[k]) and
                             not legal_turn(turn(pts[k], pts[k - 1], pts[k + 1])))
    cum = [0]
    for v in bad:
        cum.append(cum[-1] + v)
    cands = []
    evals = 0
    q0, q1 = max(0, lo - _MAX_WINDOW), min(n, hi + _MAX_WINDOW)
    # another chain's edit may have landed on a vertex of this one
    held = {q for q in range(q0 + 1, q1) if nl.anchor(_key(*pts[q]))}
    for span in range(max(1, hi - lo), 2 * _MAX_WINDOW + hi - lo + 1):
        for i in range(max(0, hi - span), lo + 1):
            j = i + span
            if j < hi or j > n or i < lo - _MAX_WINDOW or j > hi + _MAX_WINDOW:
                continue
            window = segs[i:j]
            if any(not ed.editable(s) or id(s) in pinned for s in window):
                continue
            if any(i < q < j for q in held):
                continue
            starts = list(start_targets)[:_MAX_TARGETS] if (i == 0 and start_targets) else [pts[i]]
            ends = list(end_targets)[:_MAX_TARGETS] if (j == n and end_targets) else [pts[j]]
            for A in starts:
                for B in ends:
                    so, eo = A is not pts[i], B is not pts[j]
                    near = i >= lo - _LOCAL_WINDOW and j <= hi + _LOCAL_WINDOW
                    for path in octolinear_paths(_snap(A), _snap(B), z_paths=near):
                        evals += 1
                        entries = _window_geometry(nl, pts, segs, i, j, path, so, eo)
                        if entries is None:
                            continue
                        left = cum[-1] - (cum[2 * j + 1] - cum[2 * i])
                        if bound is not None and left > bound:
                            continue
                        tilted = sum(not exact_octolinear(b[0] - a[0], b[1] - a[1])
                                     for a, b in zip(path, path[1:]))
                        cands.append((left + tilted, entries,
                                      round(_deviation(path, pts[i:j + 1]), 6),
                                      int(so) + int(eo), j - i, i, j, path))
            if evals >= _MAX_EVALS:
                break
        if evals >= _MAX_EVALS:
            break
    ed.stats['paths_weighed'] += evals
    local = [c for c in cands if c[5] >= lo - _LOCAL_WINDOW and c[6] <= hi + _LOCAL_WINDOW]
    cands.sort(key=lambda c: (c[0], c[2], c[3], c[1], c[4]))
    local.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
    tried = set()
    pour_checks = 0
    for c in cands[:_GLOBAL_PROBES] + local[:_LOCAL_PROBES]:
        i, j, path = c[5], c[6], c[7]
        sig = (i, j, tuple(path))
        if sig in tried:
            continue
        tried.add(sig)
        window = segs[i:j]
        width = max(s.width for s in window)
        if _keeps_custody(nl, pts, segs, i, j, path, width) and \
                _clears_path(nl, ed, segs, i, j, path, width):
            if pour_checks >= _POUR_PROBES:
                continue
            pour_checks += 1
            if _splits_pours(ed, nl, window, path, width):
                ed.stats['pour_refused'] += 1
                continue
            return i, j, path, ed.replace(nl, window, path, width)
    return None


def _end_targets(nl, p, seg):
    """Where a lone, unsupported or off-centre end should land."""
    w = seg.width
    out = []
    v = nl.reaches_via(p, w)
    if v is not None and _key(v.x, v.y) != _key(*p):
        out.append((v.x, v.y))
    pd = nl.reaches_pad(p, w)
    if pd is not None and not nl.inside_pad(p):
        out.append((pd.global_x, pd.global_y))
        other = nl.far(seg, _key(*p))
        for step in (0.05, 0.1, 0.2, 0.3):
            L = math.hypot(p[0] - other[0], p[1] - other[1])
            if L > _EPS:
                q = _snap((p[0] + (p[0] - other[0]) / L * step,
                           p[1] + (p[1] - other[1]) / L * step))
                if nl.inside_pad(q):
                    out.append(q)
                    break
    if not out and not nl.supported(p, seg):
        for o in nl.near_segs(p, w):
            if o is seg:
                continue
            a, b = (o.start_x, o.start_y), (o.end_x, o.end_y)
            if _pt_seg(p[0], p[1], *a, *b) <= (o.width + w) / 2.0:
                for e in (a, b):
                    if math.hypot(e[0] - p[0], e[1] - p[1]) <= (o.width + w) / 2.0:
                        out.append(e)
                other = nl.far(seg, _key(*p))
                x = _line_cross(other, p, a, b)
                if x is not None and _pt_seg(x[0], x[1], *a, *b) <= _EPS \
                        and math.hypot(x[0] - p[0], x[1] - p[1]) <= (o.width + w) / 2.0:
                    out.append(_snap(x))
                out.append(_snap(_foot(p[0], p[1], *a, *b)))
    return out


def _pad_slides(nl, pts, segs):
    """Where a chain's lone start inside a pad may move within that pad: its
    own point, the pad's anchor, and the feet on the pad of the octolinear
    lines through the chain's next vertices."""
    p = pts[0]
    if len(nl.adj.get(_key(*p), ())) != 1 or not nl.inside_pad(p):
        return None
    out = [p]
    pd = nl.reaches_pad(p, segs[0].width)
    if pd is not None and nl.inside_pad((pd.global_x, pd.global_y)):
        out.append((pd.global_x, pd.global_y))
    for v in pts[1:7]:
        for d in ((1, 0), (0, 1), (1, 1), (1, -1)):
            q = _snap(_foot(p[0], p[1], v[0] - 50 * d[0], v[1] - 50 * d[1],
                            v[0] + 50 * d[0], v[1] + 50 * d[1]))
            if nl.inside_pad(q) and q not in out:
                out.append(q)
    return out


def _tee_slides(nl, pts, segs):
    """Where a chain's lone start landing on another track's body may slide
    along that track: its own point and where the octolinear lines through
    the chain's next vertices cross the host's centreline."""
    p = pts[0]
    if len(nl.adj.get(_key(*p), ())) != 1 or nl.inside_pad(p) or nl.in_via_disc(p):
        return None
    host = nl.body_host(p, segs[0])
    if host is None:
        return None
    a, b = (host.start_x, host.start_y), (host.end_x, host.end_y)
    out = [p]
    for v in pts[1:7]:
        for d in ((1, 0), (0, 1), (1, 1), (1, -1)):
            x = _line_cross(v, (v[0] + d[0], v[1] + d[1]), a, b)
            if x is None or _pt_seg(x[0], x[1], *a, *b) > _EPS:
                continue
            x = _snap(x)
            if x not in out and _key(*x) not in (_key(*a), _key(*b)):
                out.append(x)
    return out


def _repair_chain(ed, nl, pts, segs, failed):
    """Repair one chain in place; at most a number of edits proportional to
    its length. `failed` holds the defects no window fixed, across sweeps."""
    budget = 2 * len(segs) + 8
    while budget > 0 and segs:
        budget -= 1
        n = len(segs)
        if not any(ed.editable(s) for s in segs):
            return
        pinned = None
        now = _residual(nl, pts)
        done = False
        for at_start in (True, False):
            p = pts[0] if at_start else pts[-1]
            seg = segs[0] if at_start else segs[-1]
            if len(nl.adj.get(_key(*p), ())) != 1:
                continue
            memo = ('end', _key(*p), _key(*nl.far(seg, _key(*p))))
            if memo in failed:
                continue
            targets = _end_targets(nl, p, seg)
            if not targets:
                continue
            if pinned is None:
                pinned = _pinned(nl, segs)
            r_pts, r_segs = (pts[::-1], segs[::-1]) if at_start else (pts, segs)
            got = _try_windows(nl, ed, r_pts, r_segs, n, n, pinned,
                               end_targets=targets, bound=now)
            if got:
                i, j, path, new = got
                r_pts = r_pts[:i] + path + r_pts[j + 1:]
                r_segs = r_segs[:i] + new + r_segs[j:]
                pts, segs = (r_pts[::-1], r_segs[::-1]) if at_start else (r_pts, r_segs)
                ed.stats['ends_landed'] += 1
                done = True
                break
            failed.add(memo)
        if done:
            continue
        for kind, k in _chain_defects(nl, pts, segs):
            memo = (kind, _key(*pts[k]), _key(*pts[min(k + 1, n)]))
            if memo in failed:
                continue
            if pinned is None:
                pinned = _pinned(nl, segs)
            lo, hi = (k, k + 1) if kind == 'leg' else (k, k)
            targets_s = _pad_slides(nl, pts, segs) or _tee_slides(nl, pts, segs)
            targets_e = (_pad_slides(nl, pts[::-1], segs[::-1])
                         or _tee_slides(nl, pts[::-1], segs[::-1]))
            at_anchor = kind == 'vertex' and k in (0, n)
            got = _try_windows(nl, ed, pts, segs, lo, hi, pinned,
                               start_targets=targets_s, end_targets=targets_e,
                               bound=now if at_anchor else now - 1)
            if got:
                i, j, path, new = got
                pts = pts[:i] + path + pts[j + 1:]
                segs = segs[:i] + new + segs[j:]
                ed.stats['legs_relaid' if kind == 'leg' else 'corners_relaid'] += 1
                done = True
                break
            failed.add(memo)
        if not done:
            return


_SWEEPS = 3


def _repair_net_layer(ed, net_id, layer):
    nl = ed.view(net_id, layer)
    failed = set()
    for _sweep in range(_SWEEPS):
        edits = len(ed.added) + len(ed.removed)
        for pts, segs in nl.chains():
            _repair_chain(ed, nl, pts, segs, failed)
        if len(ed.added) + len(ed.removed) == edits:
            break
    return nl


def _tees_kept(nl, old, a, c, width):
    """Does every other track end that reached the old pieces' copper still
    reach the merged track's?"""
    mine = {id(x) for x in old}
    for s in old:
        for o in nl.seg_grid.near(*_seg_box(s, 0.5)):
            if id(o) in mine:
                continue
            for p in ((o.start_x, o.start_y), (o.end_x, o.end_y)):
                if _pt_seg(p[0], p[1], s.start_x, s.start_y, s.end_x, s.end_y) <= \
                        (s.width + o.width) / 2.0 and \
                        _pt_seg(p[0], p[1], *a, *c) > (width + o.width) / 2.0:
                    return False
    return True


def _merge_collinear(ed, nl):
    merged = 0
    todo = list(nl.adj)
    while todo:
        k = todo.pop()
        ss = nl.adj.get(k, ())
        if len(ss) != 2:
            continue
        s1, s2 = ss
        if not (ed.editable(s1) and ed.editable(s2)) or k in nl.via_keys:
            continue
        if nl.anchor(k, widths=False):
            continue
        a, c = nl.far(s1, k), nl.far(s2, k)
        p = (k[0] / 1e6, k[1] / 1e6)
        if _key(*a) == _key(*c):
            continue
        if (p[0] - a[0]) * (c[0] - p[0]) + (p[1] - a[1]) * (c[1] - p[1]) <= 0:
            continue
        dev = _pt_seg(p[0], p[1], *a, *c)
        if dev > _MERGE_DEVIATION:
            continue
        width = s1.width
        if s1.width != s2.width:
            # a width step shorter than a track piece may be takes the
            # width of the piece it steps from
            l1 = math.hypot(p[0] - a[0], p[1] - a[1])
            l2 = math.hypot(p[0] - c[0], p[1] - c[1])
            if min(l1, l2) >= MIN_TRACK_FRAGMENT - _EPS or \
                    nl.entry(*((a, p) if l1 < l2 else (p, c))):
                continue
            width = s1.width if l1 > l2 else s2.width
        if dev > _EPS or width > min(s1.width, s2.width):
            if not ed.clears(a[0], a[1], c[0], c[1], nl.layer, nl.net_id, width):
                continue
            if any(not joint_ok(e, nl.legs_at(_key(*e), exclude=(s1, s2)) + [f])
                   for e, f in ((a, c), (c, a))
                   if not nl.inside_pad(e)
                   and len(nl.legs_at(_key(*e), exclude=(s1, s2))) >= 1):
                continue
            if _splits_pours(ed, nl, [s1, s2], [a, c], width):
                continue
        if not _tees_kept(nl, [s1, s2], a, c, width):
            continue
        ed.replace(nl, [s1, s2], [a, c], width)
        merged += 1
        todo.extend((_key(*a), _key(*c)))
    return merged


def legalize_track_geometry(board, scope_net_ids, config, clears,
                            readonly_ids=frozenset()):
    """Repair the scope's track geometry on `board` in place (see module doc).

    `clears(x1, y1, x2, y2, layer, net_id, width)` judges new copper against
    foreign copper. Returns (removed, added, stats): the board's segments the
    pass took out and the ones it laid; each net's change is kept only when
    check_net_connectivity grades the net no worse, without pour credit.
    """
    from check_connected import check_net_connectivity
    stats = defaultdict(int)
    ed = _Editor(board, config, clears, set(readonly_ids), stats)

    def grade(nid):
        return check_net_connectivity(
            nid, [s for L in {k[1] for k in ed.by_nl if k[0] == nid}
                  for s in ed.by_nl[(nid, L)] if id(s) not in ed.gone]
            + [s for s in ed.fresh if s.net_id == nid],
            ed.vias_by_net.get(nid, []),
            board.pads_by_net.get(nid, []), [], pcb_data=board)

    def worse(before, after):
        return ((before.get('connected') and not after.get('connected')) or
                len(after.get('disconnected_pads') or []) > len(before.get('disconnected_pads') or []) or
                (after.get('num_components') or 1) > (before.get('num_components') or 1))

    layers_of = defaultdict(set)
    for s in board.segments:
        if s.net_id in scope_net_ids and ed.editable(s):
            layers_of[s.net_id].add(s.layer)
    trust = getattr(board, '_foreign_seg_arr_trust', False)
    board._foreign_seg_arr_cache = None
    board._foreign_seg_arr_trust = True
    try:
        for nid in sorted(layers_of):
            before = grade(nid)
            added0, removed0 = dict(ed.added), dict(ed.removed)
            for layer in sorted(layers_of[nid]):
                nl = _repair_net_layer(ed, nid, layer)
                stats['collinear_merged'] += _merge_collinear(ed, nl)
            if worse(before, grade(nid)):
                # the net's edits are all still pending (ed.sync folds them
                # in per net): dropping them restores the net
                ed.undo_net(added0, removed0)
                stats['nets_reverted'] += 1
            elif set(ed.added) != set(added0) or set(ed.removed) != set(removed0):
                stats['nets'] += 1
            ed.sync()
    finally:
        board._foreign_seg_arr_trust = trust
        board._foreign_seg_arr_cache = None
    return list(ed.removed.values()), list(ed.added.values()), dict(stats)
