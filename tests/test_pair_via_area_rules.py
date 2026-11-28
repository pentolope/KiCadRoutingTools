"""route_diff's P/N via landing check holds a site at a rule area's clearance.

A `.kicad_dru` rule `A.insideArea('ROOM')` sets 0.3 mm against the class's
0.15 mm. Each site is a D_P via with one foreign AGND item (via, F.Cu track,
SMD pad, B.Cu track) 4 um inside or outside 0.3 mm of it; the ROOM drawn
over both, over the foreign item only, on F.Cu only, or not at all. The
oracle is kicad-cli DRC on the same board; `_pair_via_refusal` must refuse
exactly the sites kicad-cli reports.

A second row of sites holds a D_P via against one foreign item (gaps 4 um
either side of each value in play) where another rule meets the area's: a
pad with its own clearance above or below the area's, a net rule later or
earlier in the file than the area's, two areas whose rules come in the file
in the opposite order to the regions the router builds for them (a width
rule on the second area comes first), an area rule below a net class's
clearance, a later net rule with a `(layer "F.Cu")` clause (the area's rule
still holds B.Cu), and a later rule on `B.Type == 'Track'` beside copper
graphic (KiCad types it `Graphic`, so the area's rule holds it).

    python3 tests/test_pair_via_area_rules.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))
import run_utils  # noqa: E402
from synth import RoutingBoard  # noqa: E402

GRID, VIA, DRILL, CLR, AREA = 0.1, 0.5, 0.3, 0.15, 0.3
TRACK_W, PAD = 0.2, 0.6
DELTAS = (-0.004, 0.004)
#: kind -> distance from the D_P via's centre to the item's near copper edge
KINDS = ('via', 'track', 'smd', 'track_b')
#: mode -> the ROOM's layers and whether it covers the D_P via (None: no ROOM)
MODES = {'both': (('F.Cu', 'B.Cu'), True), 'foreign': (('F.Cu', 'B.Cu'), False),
         'f_only': (('F.Cu',), True), 'none': (None, False)}
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def room(b, layers, x0, y0, x1, y1, name='ROOM'):
    lay = ' '.join(f'"{l}"' for l in layers)
    b.items.append(
        f'  (zone (net 0) (net_name "") (layers {lay}) (uuid "{b._uid()}") (name "{name}") '
        f'(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)\n'
        f'    (keepout (tracks allowed) (vias allowed) (pads allowed) '
        f'(copperpour allowed) (footprints allowed))\n'
        f'    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n'
        f'    (polygon (pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))))')


RULES = (('wb', 'track_width (min 0.15mm) (opt 0.15mm)', "A.insideArea('RB')"),
         ('early', 'clearance (min 0.5mm)', "A.NetName == 'EARLY'"),
         ('room', f'clearance (min {AREA}mm)', "A.insideArea('ROOM')"),
         ('late', 'clearance (min 0.5mm)', "A.NetName == 'LATE'"),
         ('ra', 'clearance (min 0.25mm)', "A.insideArea('RA')"),
         ('rb', 'clearance (min 0.4mm)', "A.insideArea('RB')"),
         ('relax', 'clearance (min 0.2mm)', "A.insideArea('RELAX')"),
         ('lay', 'clearance (min 0.1mm)', "A.NetName == 'LAYR'", 'F.Cu'),
         ('trk', 'clearance (min 0.1mm)', "A.NetName == 'D_Q' && B.Type == 'Track'"))
#: scenario -> (foreign net, its pad clearance, None for a via or 'graphic' for
#: an F.Cu gr_line, the area over both items, the area over the foreign item
#: only, the values in play, the net of the landing via)
SCENARIOS = {'pad_override_high': ('AGND', 0.5, 'ROOM', None, (AREA, 0.5), 'D_P'),
             'pad_override_low': ('AGND', 0.2, 'ROOM', None, (0.2, AREA), 'D_P'),
             'later_net_rule': ('LATE', None, 'ROOM', None, (AREA, 0.5), 'D_P'),
             'earlier_net_rule': ('EARLY', None, 'ROOM', None, (AREA, 0.5), 'D_P'),
             'two_areas': ('AGND', None, 'RA', 'RB', (0.25, 0.4), 'D_P'),
             'relaxing_area': ('WIDE', None, 'RELAX', None, (0.2, 0.4), 'D_P'),
             'later_layer_rule': ('LAYR', None, 'ROOM', None, (CLR, AREA), 'D_P'),
             'graphic_later_track_rule': ('', 'graphic', 'ROOM', None, (0.1, AREA), 'D_Q')}


def scenario_sites(b, y, sites):
    k = 0
    for name, (net, pad_clr, over, beside, values, mover) in SCENARIOS.items():
        for value in values:
            for delta in DELTAS:
                x, gap = 5.0 + 6.0 * k, value + delta
                k += 1
                if pad_clr is None:
                    fy = y - VIA / 2 - gap - VIA / 2
                    b.via(x, fy, net, size=VIA, drill=DRILL, locked=True)
                    box = (x - VIA / 2, fy - VIA / 2, x + VIA / 2, fy + VIA / 2)
                elif pad_clr == 'graphic':
                    fy = y - VIA / 2 - gap - TRACK_W / 2
                    b.items.append(f'  (gr_line (start {x - 1.5} {fy}) (end {x + 1.5} {fy}) '
                                   f'(stroke (width {TRACK_W}) (type solid)) (layer "F.Cu") '
                                   f'(uuid "{b._uid()}"))')
                    box = (x - 1.5, fy - TRACK_W / 2, x + 1.5, fy + TRACK_W / 2)
                else:
                    fy = y - VIA / 2 - gap - PAD / 2
                    b.pad(f'K{k}', x, fy, net, sx=PAD, sy=PAD)
                    b.items[-1] = b.items[-1].replace('(net ', f'(clearance {pad_clr}) (net ', 1)
                    box = (x - PAD / 2, fy - PAD / 2, x + PAD / 2, fy + PAD / 2)
                if beside is None:
                    room(b, ('F.Cu', 'B.Cu'), x - 2.0, box[1] - 0.1, x + 2.0, y + 1.0, over)
                else:
                    room(b, ('F.Cu', 'B.Cu'), x - 2.0, y - VIA / 2 - 0.05, x + 2.0, y + 1.0, over)
                    room(b, ('F.Cu', 'B.Cu'), box[0] - 0.05, box[1] - 0.05, box[2] + 0.05,
                         box[3] + 0.05, beside)
                b.via(x, y, mover, size=VIA, drill=DRILL)
                sites[(name, f'{value:g}', delta)] = (x, y, mover)
    return k


def site_board(d):
    n = sum(len(v[4]) for v in SCENARIOS.values()) * len(DELTAS)
    b = RoutingBoard(w=10 + 6 * max(len(KINDS) * len(DELTAS), n), h=14 + 6 * len(MODES))
    sites = {}
    for r, (mode, (layers, covers)) in enumerate(MODES.items()):
        for k, kind in enumerate(KINDS):
            for m, delta in enumerate(DELTAS):
                x, y = 5.0 + 6.0 * (k * len(DELTAS) + m), 6.0 + 6.0 * r
                gap = AREA + delta
                if kind == 'via':
                    fy = y - VIA / 2 - gap - VIA / 2
                    b.via(x, fy, 'AGND', size=VIA, drill=DRILL, locked=True)
                    box = (x - VIA / 2, fy - VIA / 2, x + VIA / 2, fy + VIA / 2)
                elif kind in ('track', 'track_b'):
                    fy = y - VIA / 2 - gap - TRACK_W / 2
                    b.seg(x - 1.5, fy, x + 1.5, fy, 'AGND', w=TRACK_W,
                          layer='F.Cu' if kind == 'track' else 'B.Cu', locked=True)
                    box = (x - 1.5, fy - TRACK_W / 2, x + 1.5, fy + TRACK_W / 2)
                else:
                    fy = y - VIA / 2 - gap - PAD / 2
                    b.pad(f'J{r}{k}{m}', x, fy, 'AGND', sx=PAD, sy=PAD)
                    box = (x - PAD / 2, fy - PAD / 2, x + PAD / 2, fy + PAD / 2)
                if layers is not None:
                    if covers:
                        room(b, layers, x - 2.0, box[1] - 0.1, x + 2.0, y + 1.0)
                    else:
                        room(b, layers, box[0] - 0.1, box[1] - 0.1, box[2] + 0.1, box[3] + 0.1)
                b.via(x, y, 'D_P', size=VIA, drill=DRILL)
                sites[(mode, kind, delta)] = (x, y, 'D_P')
    scenario_sites(b, 6.0 + 6.0 * len(MODES), sites)
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    pro = {'board': {'design_settings': {'rules': {
               'min_clearance': CLR, 'min_track_width': 0.15, 'min_via_diameter': VIA,
               'min_through_hole_diameter': DRILL, 'min_hole_to_hole': 0.25,
               'min_hole_clearance': 0.25, 'min_copper_edge_clearance': 0.3},
               'rule_severities': {}}},
           'meta': {'filename': 'in.kicad_pro', 'version': 3},
           'net_settings': {'classes': [{'name': 'Default', 'clearance': CLR,
                                         'track_width': 0.15, 'via_diameter': VIA,
                                         'via_drill': DRILL},
                                        {'name': 'Wide', 'clearance': 0.4,
                                         'track_width': 0.15, 'via_diameter': VIA,
                                         'via_drill': DRILL}],
                            'netclass_patterns': [{'netclass': 'Wide', 'pattern': 'WIDE'}],
                            'meta': {'version': 4}}}
    with open(os.path.join(d, 'in.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(pro, fh)
    with open(os.path.join(d, 'in.kicad_dru'), 'w', encoding='utf-8') as fh:
        fh.write('(version 1)\n' + ''.join(
            f'(rule "{r[0]}"\n' + (f'  (layer "{r[3]}")\n' if len(r) > 3 else '')
            + f'  (constraint {r[1]})\n  (condition "{r[2]}"))\n' for r in RULES))
    return pcb, sites


def kicad_drc(pcb):
    d = os.path.dirname(pcb)
    g = os.path.join(d, 'graded')
    os.makedirs(g)
    for ext in ('kicad_pcb', 'kicad_pro', 'kicad_dru'):
        shutil.copy(os.path.join(d, f'in.{ext}'), os.path.join(g, f'b.{ext}'))
    report = os.path.join(g, 'drc.json')
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-error',
                    '-o', report, os.path.join(g, 'b.kicad_pcb')],
                   capture_output=True, text=True)
    with open(run_utils.evidence(report, 'kicad-cli report'), encoding='utf-8') as fh:
        return json.load(fh)


def main():
    if not shutil.which('kicad-cli'):
        print('SKIP: kicad-cli not found')
        return 77
    td = tempfile.mkdtemp(prefix='krt_pairvia_area_')
    try:
        from diff_pair_routing import _pair_via_refusal
        from kicad_dru import install_layer_clearances, install_track_clearances
        from kicad_parser import parse_kicad_pcb
        from list_nets import net_clearance_map_by_id
        from routing_config import GridRouteConfig
        pcb, sites = site_board(os.path.join(td, 'sites'))
        rep = kicad_drc(pcb)
        hits = {}
        for v in rep.get('violations', []):
            for it in v['items']:
                if 'D_P' in it['description'] or 'D_Q' in it['description']:
                    key = (round(it['pos']['x'], 3), round(it['pos']['y'], 3))
                    hits.setdefault(key, set()).add(v['type'])
        board = parse_kicad_pcb(pcb)
        cfg = GridRouteConfig(clearance=CLR, track_width=0.15, via_size=VIA, via_drill=DRILL,
                              hole_to_hole_clearance=0.25, board_edge_clearance=0.3,
                              grid_step=GRID, layers=['F.Cu', 'B.Cu'])
        names = {nid: n.name for nid, n in board.nets.items() if n.name}
        ids = {v.name: n for n, v in board.nets.items()}
        routed = [ids['D_P'], ids['D_Q']]
        cfg.set_net_clearances(net_clearance_map_by_id(pcb, names), routed_net_ids=routed)
        install_layer_clearances(cfg, None, pcb, board)
        install_track_clearances(cfg, None, pcb, board, routed_net_ids=routed)
        check('the ROOM rule is installed', getattr(cfg, 'area_clearance', None) is not None)
        board.vias = [v for v in board.vias if v.net_id not in routed]
        reported = 0
        for (mode, kind, delta), (x, y, mover) in sites.items():
            net = ids[mover]
            got = hits.get((round(x, 3), round(y, 3)), set())
            bad = 'clearance' in got
            reported += bad
            why = _pair_via_refusal(x, y, net, board, cfg)
            check(f'{mode} {kind} {delta:+.3f} mm: refused exactly when kicad-cli reports '
                  f'clearance ({"reported" if bad else "clean"})', bool(why) == bad,
                  f'kicad-cli {sorted(got)}, check: {why or "(accepted)"}')
        check('kicad-cli reports some sites and passes others (the fixture binds the rule)',
              0 < reported < len(sites), f'{reported} of {len(sites)}')
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
