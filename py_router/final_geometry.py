"""Track geometry a shipped board must hold, laid by construction.

Every track end lands exactly on what it joins (a via centre, inside a pad,
another track's vertex or centreline); no piece is shorter than
MIN_TRACK_FRAGMENT unless it enters a pad or a via; every corner turns by
0, 45 or 90 degrees, except on the net's own pad. `legalize_track_geometry`
repairs a board towards that by re-laying short runs of a track chain as
octolinear paths that clear foreign copper at the smoother's rules, removes
dangling dead ends, and keeps a net's edit only when the net is no less
connected. Locked, sense-held and read-only copper is never touched.
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


def octolinear_paths(A, B):
    """Candidate octolinear polylines from A to B: the straight run, the two
    diagonal+axis elbows, the two axis+axis elbows, and Z paths whose middle
    leg sits at a few offsets along the run."""
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


class _NetLayer:
    """One net's tracks on one layer, with what anchors them."""

    def __init__(self, board, net_id, layer, segs, vias, pads, copper_layers):
        from connectivity import via_copper_layers
        self.board, self.net_id, self.layer = board, net_id, layer
        self.segs = segs
        self.vias = [v for v in vias
                     if layer in via_copper_layers(v, copper_layers)]
        self.via_keys = {_key(v.x, v.y): v for v in self.vias}
        self.pads = [p for p in pads
                     if layer in p.layers or any('*' in L for L in p.layers)]
        self.adj = defaultdict(list)
        for s in segs:
            self.adj[_key(s.start_x, s.start_y)].append(s)
            self.adj[_key(s.end_x, s.end_y)].append(s)
        self._inside = {}
        self._custody = None

    def inside_pad(self, p):
        k = _key(*p)
        hit = self._inside.get(k)
        if hit is None:
            from check_drc import point_to_pad_distance
            hit = any(abs(p[0] - pd.global_x) <= max(pd.size_x, pd.size_y)
                      and abs(p[1] - pd.global_y) <= max(pd.size_x, pd.size_y)
                      and point_to_pad_distance(p[0], p[1], pd) <= _EPS
                      for pd in self.pads)
            self._inside[k] = hit
        return hit

    def _pads_reaching(self, p, w):
        from check_drc import point_to_pad_distance
        for pd in self.pads:
            r = max(pd.size_x, pd.size_y) + w
            if abs(p[0] - pd.global_x) > r or abs(p[1] - pd.global_y) > r:
                continue
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
        p = (k[0] / 1e6, k[1] / 1e6)
        w = max(s.width for s in self.adj[k])
        return self.pads_reached(p, w) | self.vias_reached(p, w)

    def custody(self):
        """pad or via id -> the vertex keys whose caps reach it."""
        if self._custody is None:
            self._custody = defaultdict(set)
            for k in self.adj:
                for oid in self.reached(k):
                    self._custody[oid].add(k)
        return self._custody

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
        for o in self.segs:
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
        for o in self.segs:
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

    def anchor(self, k):
        if len(self.adj.get(k, ())) != 2 or k in self.via_keys:
            return True
        custody = self.custody()
        return any(len(custody[oid]) == 1 for oid in self.reached(k))

    def chains(self):
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

    def editable(self, s):
        return (id(s) not in self.readonly_ids and not is_immutable_copper(s)
                and not getattr(s, 'graphic', False))

    def view(self, net_id, layer):
        segs = [s for s in self.board.segments
                if s.net_id == net_id and s.layer == layer]
        vias = [v for v in self.board.vias if v.net_id == net_id]
        pads = self.board.pads_by_net.get(net_id, [])
        return _NetLayer(self.board, net_id, layer, segs, vias, pads,
                         self.copper_layers)

    def replace(self, old, new_pts, width, layer, net_id):
        new = [Segment(start_x=a[0], start_y=a[1], end_x=b[0], end_y=b[1],
                       width=width, layer=layer, net_id=net_id)
               for a, b in zip(new_pts, new_pts[1:])
               if math.hypot(b[0] - a[0], b[1] - a[1]) >= _EPS]
        gone = {id(s) for s in old}
        self.board.segments = [s for s in self.board.segments
                               if id(s) not in gone] + new
        for s in old:
            if id(s) in self.added:
                del self.added[id(s)]
            else:
                self.removed[id(s)] = s
        for s in new:
            self.added[id(s)] = s
        if hasattr(self.board, '_foreign_seg_arr_cache'):
            self.board._foreign_seg_arr_cache = None
        return new


def _pinned(nl, segs):
    """Chain segments another same-net track touches away from their ends
    (pads and vias are held by _keeps_custody)."""
    pinned = set()
    mine = {id(s) for s in segs}
    others = [o for o in nl.segs if id(o) not in mine]
    for s in segs:
        a, b = (s.start_x, s.start_y), (s.end_x, s.end_y)
        ka, kb = _key(*a), _key(*b)
        for o in others:
            if o is s:
                continue
            for p in ((o.start_x, o.start_y), (o.end_x, o.end_y)):
                kp = _key(*p)
                if kp in (ka, kb):
                    continue
                if _pt_seg(p[0], p[1], *a, *b) <= s.width / 2.0 + _EPS:
                    pinned.add(id(s))
                    break
            if id(s) in pinned:
                break
            from geometry_utils import segments_intersect
            oa, ob = (o.start_x, o.start_y), (o.end_x, o.end_y)
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
        for pd in nl.pads:
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
    for pd in nl.pads:
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


_MAX_WINDOW = 40
_LOCAL_WINDOW = 6
_GLOBAL_PROBES = 30
_LOCAL_PROBES = 60


def _try_windows(nl, ed, pts, segs, lo, hi, pinned, start_targets=None,
                 end_targets=None, bound=None):
    """Re-lay a window [i, j] around pts[lo..hi] with an octolinear path the
    gate's rules allow and that clears foreign copper. First the paths that
    leave the chain fewest defects, straying least from the copper they
    replace; failing those, windows of a few segments around the defect,
    fewest defects left first. Returns the new segments or None."""
    n = len(segs)
    bad = [0] * (2 * n + 1)          # leg k at 2k+1, vertex k at 2k
    for k in range(n):
        a, b = pts[k], pts[k + 1]
        bad[2 * k + 1] = int(math.hypot(b[0] - a[0], b[1] - a[1])
                             < MIN_TRACK_FRAGMENT - _EPS and not nl.entry(a, b))
    for k in range(1, n):
        bad[2 * k] = int(not nl.inside_pad(pts[k]) and
                         not legal_turn(turn(pts[k], pts[k - 1], pts[k + 1])))
    cum = [0]
    for v in bad:
        cum.append(cum[-1] + v)
    cands = []
    for i in range(max(0, lo - _MAX_WINDOW), lo + 1):
        for j in range(max(hi, i + 1), min(n, i + _MAX_WINDOW) + 1):
            window = segs[i:j]
            if any(not ed.editable(s) or id(s) in pinned for s in window):
                continue
            starts = list(start_targets) if (i == 0 and start_targets) else [pts[i]]
            ends = list(end_targets) if (j == n and end_targets) else [pts[j]]
            for A in starts:
                for B in ends:
                    so, eo = A is not pts[i], B is not pts[j]
                    for path in octolinear_paths(_snap(A), _snap(B)):
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
    local = [c for c in cands if c[5] >= lo - _LOCAL_WINDOW and c[6] <= hi + _LOCAL_WINDOW]
    cands.sort(key=lambda c: (c[0], c[2], c[3], c[1], c[4]))
    local.sort(key=lambda c: (c[0], c[1], c[2], c[3], c[4]))
    tried = set()
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
            return ed.replace(window, path, width, nl.layer, nl.net_id)
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
        for o in nl.segs:
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


def _repair_net_layer(ed, net_id, layer):
    failed = set()
    for _round in range(2000):
        nl = ed.view(net_id, layer)
        progress = False
        for pts, segs in nl.chains():
            n = len(segs)
            if not any(ed.editable(s) for s in segs):
                continue
            pinned = None
            now = _residual(nl, pts)
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
                if _try_windows(nl, ed, r_pts, r_segs, n, n, pinned,
                                end_targets=targets, bound=now):
                    ed.stats['ends_landed'] += 1
                    progress = True
                    break
                failed.add(memo)
            if progress:
                break
            defects = _chain_defects(nl, pts, segs)
            for kind, k in defects:
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
                if _try_windows(nl, ed, pts, segs, lo, hi, pinned,
                                start_targets=targets_s, end_targets=targets_e,
                                bound=now if at_anchor else now - 1):
                    ed.stats['legs_relaid' if kind == 'leg' else 'corners_relaid'] += 1
                    progress = True
                    break
                failed.add(memo)
            if progress:
                break
        if not progress:
            return


def _merge_collinear(ed, net_id, layer):
    merged = 0
    while True:
        nl = ed.view(net_id, layer)
        done = False
        for k, ss in list(nl.adj.items()):
            if len(ss) != 2 or nl.anchor(k):
                continue
            s1, s2 = ss
            if not (ed.editable(s1) and ed.editable(s2)):
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
                short = min(l1, l2)
                if short >= MIN_TRACK_FRAGMENT - _EPS or nl.entry(*((a, p) if l1 < l2 else (p, c))):
                    continue
                width = s1.width if l1 > l2 else s2.width
            if dev > _EPS or width > min(s1.width, s2.width):
                if not ed.clears(a[0], a[1], c[0], c[1], layer, net_id, width):
                    continue
                if any(not joint_ok(e, nl.legs_at(_key(*e), exclude=(s1, s2)) + [f])
                       for e, f in ((a, c), (c, a))
                       if not nl.inside_pad(e)
                       and len(nl.legs_at(_key(*e), exclude=(s1, s2))) >= 1):
                    continue
            ed.replace([s1, s2], [a, c], width, layer, net_id)
            merged += 1
            done = True
            break
        if not done:
            return merged


def _in_own_zone(board, net_id, layer, p):
    from obstacle_map import point_in_polygon
    return any(z.net_id == net_id and z.layer == layer and len(z.polygon) >= 3
               and point_in_polygon(p[0], p[1], z.polygon)
               for z in (getattr(board, 'zones', None) or ()))


def _dangling_spurs(ed, net_id, layer):
    """Unsupported lone ends no landing reached: the chain back to its anchor."""
    nl = ed.view(net_id, layer)
    spurs = []
    for pts, segs in nl.chains():
        for at_start in (True, False):
            p = pts[0] if at_start else pts[-1]
            seg = segs[0] if at_start else segs[-1]
            if len(nl.adj.get(_key(*p), ())) != 1 or nl.supported(p, seg):
                continue
            if nl.reaches_pad(p, seg.width) or nl.reaches_via(p, seg.width):
                continue
            if _in_own_zone(ed.board, net_id, layer, p):
                continue
            if not all(ed.editable(s) for s in segs):
                continue
            far = pts[-1] if at_start else pts[0]
            if len(nl.adj.get(_key(*far), ())) == 1 and not nl.supported(far, segs[-1 if at_start else 0]):
                continue
            spurs.append(segs)
            break
    return spurs


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
            nid, [s for s in board.segments if s.net_id == nid],
            [v for v in board.vias if v.net_id == nid],
            board.pads_by_net.get(nid, []), [], pcb_data=board)

    def worse(before, after):
        return ((before.get('connected') and not after.get('connected')) or
                len(after.get('disconnected_pads') or []) > len(before.get('disconnected_pads') or []) or
                (after.get('num_components') or 1) > (before.get('num_components') or 1))

    def restore(segments, added, removed):
        board.segments = segments
        ed.added, ed.removed = added, removed
        board._foreign_seg_arr_cache = None

    layers_of = defaultdict(set)
    for s in board.segments:
        if s.net_id in scope_net_ids and ed.editable(s):
            layers_of[s.net_id].add(s.layer)
    trust = getattr(board, '_foreign_seg_arr_trust', False)
    board._foreign_seg_arr_cache = None
    board._foreign_seg_arr_trust = True
    try:
        for nid in sorted(layers_of):
            snapshot = (list(board.segments), dict(ed.added), dict(ed.removed))
            before = grade(nid)
            for layer in sorted(layers_of[nid]):
                _repair_net_layer(ed, nid, layer)
                stats['collinear_merged'] += _merge_collinear(ed, nid, layer)
            complete = before.get('connected') and not before.get('disconnected_pads')
            for layer in sorted(layers_of[nid]) if complete else ():
                for _round in range(50):
                    spurs = _dangling_spurs(ed, nid, layer)
                    if not spurs:
                        break
                    trial = (list(board.segments), dict(ed.added), dict(ed.removed))
                    g0 = grade(nid)
                    for chain in spurs:
                        ed.replace(chain, [], 0, layer, nid)
                    if worse(g0, grade(nid)):
                        restore(*trial)
                        break
                    stats['spurs_removed'] += len(spurs)
            if worse(before, grade(nid)):
                restore(*snapshot)
                stats['nets_reverted'] += 1
            elif set(ed.added) != set(snapshot[1]) or set(ed.removed) != set(snapshot[2]):
                stats['nets'] += 1
    finally:
        board._foreign_seg_arr_trust = trust
        board._foreign_seg_arr_cache = None
    return list(ed.removed.values()), list(ed.added.values()), dict(stats)
