"""The safeguards of the end-of-run track geometry pass, on synthetic boards
judged by independent geometry (shapely distances, segment lengths):

  1. a re-lay that would come inside clearance of foreign copper is refused;
  2. locked and sense-held copper is never edited;
  3. a net whose edit grades worse is restored whole (the custody guard
     forced off, so only the revert stands between the edit and an open);
  4. a re-lay that would split a foreign pour the copper it replaces left
     whole is refused;
  5. a chain is not re-laid across a width change at the wider width;
  6. the window search per defect is bounded: a 600-leg staircase weighs at
     most a fixed number of paths per search;
  7. the pour-split model on a corridor of GND fill between two squares,
     under KiCad's min_thickness 0.25 (each verdict checked against a
     kicad-cli refill when this was written): a strip thinner than that
     carries no fill; the moving net's own clearance widens the cut when
     larger than the pour's; and a second edit of a net is judged against
     the net's copper after the first.

    python3 tests/test_final_geometry_safeguards.py
"""
import os
import sys

from shapely.geometry import LineString, Polygon, box

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))

from synth import make_pad, make_seg, make_via, make_pcb, make_net  # noqa: E402
from kicad_parser import BoardInfo, Zone  # noqa: E402
from pcb_modification import copper_clearance_probe  # noqa: E402
import final_geometry as fg  # noqa: E402

failures = []
NET, FOE = 1, 2


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def board(segs, vias=(), pads=(), zones=(), nets=None):
    by_net = {}
    for p in pads:
        by_net.setdefault(p.net_id, []).append(p)
    return make_pcb(nets=nets or {NET: make_net(NET, 'SIG'), FOE: make_net(FOE, 'FOE')},
                    segments=list(segs), vias=list(vias), pads_by_net=by_net,
                    board_info=BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                                         copper_layers=['F.Cu', 'B.Cu']),
                    zones=list(zones))


def run(pcb, clearance=0.2, scope=(NET,)):
    clears = copper_clearance_probe(pcb, clearance, None, 0.0, None)
    return fg.legalize_track_geometry(pcb, set(scope), None, clears)


def staircase(x0, y0, steps, net=NET, w=0.2):
    pts = [(x0, y0)]
    x, y = x0, y0
    for _ in range(steps):
        x = round(x + 0.05, 6)
        pts.append((x, y))
        y = round(y + 0.05, 6)
        pts.append((x, y))
    return [make_seg(a[0], a[1], b[0], b[1], net_id=net, width=w) for a, b in zip(pts, pts[1:])]


def stair_board(extra_segs=(), zones=(), locked=False, held='', w=0.2):
    """J1 -- (3,5)..(5,5), a 20-step staircase to (6,6), (6,6)..(10,6) -- J2."""
    segs = ([make_seg(3, 5, 5, 5, width=w)] + staircase(5, 5, 20, w=w)
            + [make_seg(6, 6, 10, 6, width=w)])
    for s in segs:
        s.locked = locked
        s.held_by = held
    pads = [make_pad(NET, 3, 5, ref='J1', size_x=1, size_y=1),
            make_pad(NET, 10, 6, ref='J2', size_x=1, size_y=1)]
    return board(segs + list(extra_segs), pads=pads, zones=zones), segs


def sig(pcb):
    return sorted((round(s.start_x, 6), round(s.start_y, 6), round(s.end_x, 6),
                   round(s.end_y, 6), s.width) for s in pcb.segments if s.net_id == NET)


def main():
    print('1. clearance refusal')
    # SIG: via A -- y=0 to (3,0), a 0.05 mm jog, y=0.05 to (5,0.05), 45 deg to
    # via B. Every local re-lay of the jog runs the y=0 level on, or climbs,
    # into one of two foreign tracks; the jog itself clears both by 0.2 mm.
    segs = [make_seg(0, 0, 3, 0), make_seg(3, 0, 3, 0.05), make_seg(3, 0.05, 5, 0.05),
            make_seg(5, 0.05, 6, 1.05)]
    foes = [make_seg(3.2, -0.35, 4.8, -0.35, net_id=FOE), make_seg(3.2, 0.45, 4.8, 0.45, net_id=FOE)]
    pcb = board(segs + foes, vias=[make_via(0, 0, net_id=NET), make_via(6, 1.05, net_id=NET)])
    run(pcb)
    foe_shape = [LineString([(f.start_x, f.start_y), (f.end_x, f.end_y)]).buffer(0.1) for f in foes]
    gap = min(LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)]).buffer(s.width / 2)
              .distance(f) for s in pcb.segments if s.net_id == NET for f in foe_shape)
    check('no SIG track comes inside 0.2 mm of a foreign track', gap >= 0.2 - 1e-4,
          f'{gap:.4f} mm')

    print('2. locked and sense-held copper')
    for kind, kw in (('locked', dict(locked=True)), ('sense-held', dict(held='sense:J1.1'))):
        pcb, segs = stair_board(**kw)
        before = sig(pcb)
        removed, added, _st = run(pcb)
        check(f'{kind}: the staircase ships as it was',
              sig(pcb) == before and not removed and not added)

    print('3. a net that grades worse is restored whole')
    # 0.02 mm tracks and a third pad J3 on one outer staircase corner, which
    # the 45-degree re-lay passes 0.02 mm clear of
    pcb, segs = stair_board(w=0.02)
    pcb.pads_by_net[NET].append(make_pad(NET, 5.3, 5.25, ref='J3', size_x=0.01, size_y=0.01))
    before = sig(pcb)
    keep, anchor = fg._keeps_custody, fg._NetLayer.anchor
    fg._keeps_custody = lambda *a, **k: True
    fg._NetLayer.anchor = lambda self, k, widths=True: (len(self.adj.get(k, ())) != 2
                                           or k in self.via_keys)
    try:
        _r, _a, st = run(pcb)
    finally:
        fg._keeps_custody, fg._NetLayer.anchor = keep, anchor
    check('the net is reverted', st.get('nets_reverted') == 1, f'{st}')
    check('...and ships as it was', sig(pcb) == before)

    print('4. a foreign pour the re-lay would split')
    # the clearance case's jog with no foreign tracks, over a FOE pour of two
    # pads of fill joined by a neck between y=-0.52 and -0.25: the 0.05 mm
    # lower run (its carve ends at -0.25 under a 0.2 mm clearance) leaves the
    # neck its 0.27 mm, the run carried on at y=0 would leave 0.22 mm, under
    # the pour's 0.25 mm min_thickness
    from shapely.ops import unary_union
    gnd_poly = [(3.2, -1.5), (3.8, -1.5), (3.8, -0.52), (4.2, -0.52), (4.2, -1.5), (4.8, -1.5),
                (4.8, -0.25), (3.2, -0.25)]
    gnd = Zone(net_id=FOE, net_name='FOE', layer='F.Cu', polygon=gnd_poly, clearance=0.2,
               min_thickness=0.25)
    jog = [make_seg(0, 0, 3, 0), make_seg(3, 0, 3, 0.05), make_seg(3, 0.05, 5, 0.05),
           make_seg(5, 0.05, 6, 1.05)]
    pcb = board(jog, vias=[make_via(0, 0, net_id=NET), make_via(6, 1.05, net_id=NET)], zones=[gnd])

    def fill_pieces(p):
        carve = unary_union([LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)])
                             .buffer(s.width / 2 + 0.2) for s in p.segments if s.net_id == NET])
        fill = Polygon(gnd_poly).difference(carve).buffer(-0.124).buffer(0.124)
        return sum(1 for g in getattr(fill, 'geoms', [fill]) if g.area > 1e-6)
    n0 = fill_pieces(pcb)
    run(pcb)
    check('the pour stays in one piece', n0 == 1 and fill_pieces(pcb) == 1,
          f'{n0} -> {fill_pieces(pcb)} piece(s)')

    print('5. a width change is a chain boundary')
    # a 0.5 mm track necks to 0.2 mm at (6,5) and the narrow run carries a
    # 0.05 mm piece; joining the piece to the wide track would widen it
    wide = [make_seg(3, 5, 6, 5, width=0.5)]
    narrow = [make_seg(6, 5, 6.05, 5, width=0.2), make_seg(6.05, 5, 10, 5, width=0.2)]
    pads = [make_pad(NET, 3, 5, ref='J1', size_x=1, size_y=1),
            make_pad(NET, 10, 5, ref='J2', size_x=1, size_y=1)]
    pcb = board(wide + narrow, pads=pads)

    def wide_len(p):
        return sum(LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)]).length
                   for s in p.segments if s.net_id == NET and s.width > 0.2 + 1e-9)
    w0 = wide_len(pcb)
    run(pcb)
    check('no narrow copper is laid again at the wide width', wide_len(pcb) <= w0 + 1e-6,
          f'{w0:.4f} -> {wide_len(pcb):.4f} mm of 0.5 mm track')

    print('6. the window search is bounded')
    long = staircase(5, 5, 300)
    pads = [make_pad(NET, 5, 5, ref='J1', size_x=0.3, size_y=0.3),
            make_pad(NET, 20, 20, ref='J2', size_x=0.3, size_y=0.3)]
    pcb = board(long, pads=pads)
    counts = []
    weigh = fg._window_geometry
    try_w = fg._try_windows

    def counted(*a, **k):
        counts.append(0)
        return try_w(*a, **k)

    def geom(*a, **k):
        counts[-1] += 1
        return weigh(*a, **k)
    fg._try_windows, fg._window_geometry = counted, geom
    try:
        run(pcb)
    finally:
        fg._try_windows, fg._window_geometry = try_w, weigh
    check('the search ran', bool(counts))
    check('no search weighs more than 5000 paths', max(counts or [0]) <= 5000,
          f'max {max(counts or [0])} over {len(counts)} searches')

    pour_model()
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


def corridor(h):
    lo, hi = 5 - h / 2, 5 + h / 2
    return [(0, 0), (4, 0), (4, lo), (6, lo), (6, 0), (10, 0), (10, 10), (6, 10), (6, hi),
            (4, hi), (4, 10), (0, 10)]


def pour_model():
    print('7. the pour-split model')
    from types import SimpleNamespace

    def editor(h, segs, net_clearance=None):
        pcb = board(segs, zones=[Zone(net_id=FOE, net_name='FOE', layer='F.Cu', polygon=corridor(h),
                                      clearance=0.2, min_thickness=0.25)])
        cfg = SimpleNamespace(clearance=0.2,
                              net_clearances={NET: net_clearance} if net_clearance else {})
        ed = fg._Editor(pcb, cfg, lambda *a: True, set(), {})
        return ed, ed.view(NET, 'F.Cu')

    def splits(h, y, net_clearance=None):
        old = make_seg(4.5, 9, 5.5, 9)
        ed, nl = editor(h, [old], net_clearance)
        return fg._splits_pours(ed, nl, [old], [(4.5, y), (5.5, y)], 0.2)
    # a 0.2 mm track's carve at 0.2 mm clearance is 0.6 mm tall
    for h, y, want, why in ((1.0, 5.0, True, '0.2 mm strips either side'),
                            (1.0, 5.04, True, 'a 0.24 mm strip and a 0.16 mm one'),
                            (1.0, 5.06, False, 'a 0.26 mm strip'),
                            (1.4, 5.0, False, '0.4 mm strips')):
        check(f'{h} mm corridor, track at y={y} ({why}): {"splits" if want else "keeps"}',
              splits(h, y) == want)
    check('1.2 mm corridor: keeps it at the pour clearance (0.3 mm strips)', not splits(1.2, 5.0))
    check('...splits it at the net\'s 0.3 mm clearance (0.2 mm strips)', splits(1.2, 5.0, 0.3))
    # first edit: a SIG track at y=5.35 in a 1.4 mm corridor (fill left below
    # it, 4.3..5.05); then one at y=4.6, which leaves 4.9..5.05 = 0.15 mm
    first, second = make_seg(4.5, 9, 5.5, 9), make_seg(4.5, 8, 5.5, 8)
    ed, nl = editor(1.4, [first, second])
    fg._splits_pours(ed, nl, [first], [(4.5, 9.5), (5.5, 9.5)], 0.2)
    check('(the first edit alone keeps the corridor)',
          not fg._splits_pours(ed, nl, [first], [(4.5, 5.35), (5.5, 5.35)], 0.2))
    ed.replace(nl, [first], [(4.5, 5.35), (5.5, 5.35)], 0.2)
    check('a second edit is judged with the first in place',
          fg._splits_pours(ed, nl, [second], [(4.5, 4.6), (5.5, 4.6)], 0.2))


def test_final_geometry_safeguards():
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())
