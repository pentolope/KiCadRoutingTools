#!/usr/bin/env python3
"""A scoped .kicad_dru pad rule holds only the nets its condition binds.

Fixture (written here): F.Cu only, a 0.4 mm pitch column of five 1.0 x 0.2
rect pins. FREE sits in the middle, with a test pad 3 mm west; the other
pins are single-pad nets. HV has two pads whose straight line runs 0.175 mm
off the pin tips. A .kicad_dru rule holds every non-pad
item of HV 0.25 mm off any pad (and any non-pad item off an HV pad); the
class clearance is 0.15. Both nets route in one call.

FREE's exit along its own centre line passes 0.30 mm from the neighbours'
flat edges: a 0.15 mm track keeps 0.225 there, where FREE needs the class
0.15 and HV would need 0.25 + 0.075. The router used to stamp every pad at
the widest requirement over the routed nets, so FREE's pin was sealed in
the main pass and only the own-requirement retry routed it.

Rule-area arm: the same board with a clearance rule scoped by a rule area
far from the column, and HV's pads moved so its straight line runs 0.175 mm
off the west edge of FREE's test pad (a routed net's pad, in the per-net
cache). The floor is never armed on such a board (the area composition
stamps pads through configs the raise does not compose over), so HV is held
0.25 mm off that pad: closed map cells 0.25 and 0.30 mm from its edge
(HV needs 0.325) and a DRC-clean route.

Oracles: hand-computed geometry for the map cells (open for FREE, closed for
HV while each routes), the router's summary (FREE routed in the main pass, at
the full 0.15 width), KRT's connectivity check, and kicad-cli's DRC
reporting no clearance, short or unconnected item on the routed board.

    python3 tests/test_scoped_pad_rule_per_net.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'rust_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

from kicad_oracle import find_kicad_cli                  # noqa: E402
from kicad_parser import parse_kicad_pcb                 # noqa: E402
from run_utils import check as run_check, evidence, tool  # noqa: E402

RUN_ALL_TIMEOUT = 600
CLI = find_kicad_cli(warn=False)

NETS = {1: 'FREE', 2: 'HV', 3: 'N1', 4: 'N2', 5: 'N3', 6: 'N4'}


def _pad(num, x, y, net_id, sx, sy, shape='rect'):
    return (f'  (pad "{num}" smd {shape} (at {x} {y}) (size {sx} {sy}) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") '
            f'(net {net_id} "{NETS[net_id]}") (uuid "a0000000-0000-0000-0000-0000000000{num:02d}"))\n')


def _fp(ref, n, x, y, pads):
    return (f' (footprint "fix:{ref}" (layer "F.Cu") (uuid "f0000000-0000-0000-0000-0000000000{n:02d}")\n'
            f'  (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 -3 0) (layer "F.Fab") '
            f'(uuid "f1000000-0000-0000-0000-0000000000{n:02d}")\n'
            '   (effects (font (size 1 1) (thickness 0.15))))\n'
            '  (attr smd)\n' + ''.join(pads) + ' )\n')


#: the column: pin 3 (FREE) at the footprint origin, neighbours 0.4 mm apart
COLUMN = [(1, -0.8, 3), (2, -0.4, 4), (3, 0.0, 1), (4, 0.4, 5), (5, 0.8, 6)]
ZONE = (' (zone (net 0) (net_name "") (layer "F.Cu") (uuid "40b3d710-7d00-43ca-8803-b4b5e6f64abb")\n'
        '  (name "TIGHT") (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25)\n'
        '  (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed)'
        ' (footprints allowed))\n'
        '  (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n'
        '  (polygon (pts (xy 104.5 104.5) (xy 105.5 104.5) (xy 105.5 105.5) (xy 104.5 105.5))))\n')
AREA_RULE = ('(rule "tight room" (constraint clearance (min 0.2mm))\n'
             ' (condition "A.insideArea(\'TIGHT\')"))\n')


def board(hv_x=100.75, zone=False):
    return ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
            ' (general (thickness 1.6) (legacy_teardrops no))\n'
            ' (paper "A4")\n'
            ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user) (3 "B.Mask" user)\n'
            '  (13 "F.Paste" user) (25 "Edge.Cuts" user))\n'
            ' (setup (pad_to_mask_clearance 0))\n'
            ' (net 0 "")' + ''.join(f' (net {i} "{n}")' for i, n in NETS.items()) + '\n'
            + _fp('U1', 1, 100, 100, [_pad(num, 0, y, nid, 1.0, 0.2) for num, y, nid in COLUMN])
            + _fp('TP1', 2, 97, 100, [_pad(6, 0, 0, 1, 0.6, 0.6)])
            + _fp('TP2', 3, hv_x, 97.5, [_pad(7, 0, 0, 2, 0.3, 0.3)])
            + _fp('TP3', 4, hv_x, 102.5, [_pad(8, 0, 0, 2, 0.3, 0.3)])
            + (ZONE if zone else '')
            + ' (gr_rect (start 94 94) (end 106 106) (stroke (width 0.05) (type default)) (fill no)'
              ' (layer "Edge.Cuts") (uuid "e0000000-0000-0000-0000-000000000001"))\n'
            ')\n')


PRO = {'board': {'design_settings': {'rules': {'min_clearance': 0.15, 'min_track_width': 0.15}}},
       'meta': {'filename': 'board.kicad_pro', 'version': 3},
       'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.15, 'track_width': 0.15,
                                     'via_diameter': 0.5, 'via_drill': 0.25}],
                        'meta': {'version': 4}, 'netclass_patterns': []}}
DRU = ('(version 1)\n'
       '(rule "hv pad spacing" (constraint clearance (min 0.25mm))\n'
       ' (condition "A.Type == \'Pad\' && B.Type != \'Pad\' && '
       '(A.NetName == \'HV\' || B.NetName == \'HV\')"))\n')
ROUTE = ['--nets', 'FREE', 'HV', '--layers', 'F.Cu', '--track-width', '0.15',
         '--clearance', '0.15', '--grid-step', '0.05', '--via-size', '0.5',
         '--via-drill', '0.25']
#: kicad-cli violation types that are copper the route made wrong
COPPER = {'clearance', 'shorting_items', 'tracks_crossing', 'copper_edge_clearance',
          'hole_clearance', 'unconnected_items', 'track_width', 'track_dangling'}

fails = []


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f': {name}' + (f' -- {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def stage(work, area=False):
    work = os.path.join(work, 'area' if area else 'plain')
    os.makedirs(work)
    path = os.path.join(work, 'board.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(board(96.45, True) if area else board())
    with open(os.path.join(work, 'board.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(PRO, fh)
    with open(os.path.join(work, 'board.kicad_dru'), 'w', encoding='utf-8') as fh:
        fh.write(DRU + (AREA_RULE if area else ''))
    return path


def kicad_drc(path):
    out = path + '.drc.json'
    subprocess.run([CLI, 'pcb', 'drc', '--format', 'json', '--severity-all', '-o', out, path],
                   capture_output=True, text=True, timeout=300)
    with open(evidence(out, 'the kicad-cli report'), encoding='utf-8') as fh:
        rep = json.load(fh)
    return rep.get('violations', []) + rep.get('unconnected_items', [])


def map_cells(src, points):
    """The run's own map (base and per-net caches for FREE and HV), set up as
    route.py sets it, as each net sees it while it routes, at `points`."""
    from routing_config import GridRouteConfig, GridCoord
    from kicad_dru import install_layer_clearances, install_track_clearances
    from obstacle_map import build_base_obstacle_map
    from obstacle_cache import precompute_all_net_obstacles, build_working_obstacle_map
    from obstacle_cache import remove_net_obstacles_from_cache, add_net_obstacles_from_cache
    from net_keepouts import stamp
    pcb = parse_kicad_pcb(src)
    ids = {n.name: i for i, n in pcb.nets.items()}
    routed = [ids['FREE'], ids['HV']]
    cfg = GridRouteConfig(track_width=0.15, clearance=0.15, grid_step=0.05,
                          via_size=0.5, via_drill=0.25, layers=['F.Cu'])
    cfg.set_net_clearances({}, routed)
    install_layer_clearances(cfg, None, src, pcb, net_keepouts=True)
    install_track_clearances(cfg, None, src, pcb, routed_net_ids=routed)
    arm = getattr(cfg, 'arm_scoped_pad_floor', None)
    if arm is not None:
        arm()
    else:
        cfg.scoped_pad_floor = True
    base = build_base_obstacle_map(pcb, cfg, routed)
    cache = precompute_all_net_obstacles(pcb, routed, cfg)
    work = build_working_obstacle_map(base, cache)
    coord = GridCoord(cfg.grid_step)
    out = {}
    for name in ('FREE', 'HV'):
        nid = ids[name]
        remove_net_obstacles_from_cache(work, cache[nid])
        with stamp(work, pcb, (nid,), cfg):
            out[name] = [work.is_blocked(*coord.to_grid(x, y), 0) for x, y in points]
        add_net_obstacles_from_cache(work, cache[nid])
    return out


def route(src, main_pass):
    """Route FREE and HV in one call on `src` and grade the shipped board."""
    work = os.path.dirname(src)
    out = os.path.join(work, 'routed.kicad_pcb')
    js = os.path.join(work, 'routed.json')
    r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), src, out] + ROUTE
                  + ['--json-out', js], accept=True)
    evidence(out, 'the routed board')
    if main_pass:
        with open(evidence(js, '--json-out document'), encoding='utf-8') as fh:
            doc = json.load(fh)
        retried = set(doc.get('own_requirement_retry') or {})
        if 'own-requirement retry: FREE' in (r.stdout or ''):
            retried.add('FREE')
        check('FREE is routed in the main pass, not by the own-requirement retry',
              'FREE' not in retried, str(sorted(retried)))
    r = subprocess.run([sys.executable, '-X', 'utf8', tool('check_connected.py'), out,
                        '--nets', 'FREE', 'HV'], capture_output=True, text=True, timeout=300)
    check('FREE and HV pads joined by copper (check_connected)',
          r.returncode == 0 and 'ALL NETS FULLY CONNECTED' in r.stdout, r.stdout[-400:])
    pcb = parse_kicad_pcb(out)
    free = next(i for i, n in pcb.nets.items() if n.name == 'FREE')
    segs = [s for s in pcb.segments if s.net_id == free]
    check('FREE at the requested 0.15 mm, nothing narrowed',
          bool(segs) and min(s.width for s in segs) >= 0.15 - 1e-9,
          str(sorted({s.width for s in segs})))
    bad = [v for v in kicad_drc(out) if v['type'] in COPPER]
    check('kicad-cli DRC: no copper violation and nothing unconnected (HV held at 0.25)',
          not bad, json.dumps(bad)[:600])


def main():
    if not CLI:
        print('SKIP: no kicad-cli (the oracle is its DRC)')
        sys.exit(77)
    work = tempfile.mkdtemp(prefix='scoped_pad_rule_')
    try:
        print('1. the column, no rule area')
        src = stage(work)
        check('control: KiCad grades the unrouted fixture with FREE and HV unconnected only',
              {v['type'] for v in kicad_drc(src) if v['type'] in COPPER} == {'unconnected_items'})
        # FREE's exit: on its centre line just inside the pin's west end,
        # 0.30 mm from both neighbours' flat edges
        cells = map_cells(src, [(99.6, 100.0), (99.8, 100.0)])
        check("FREE's exit cells are open while FREE routes (0.225 mm >= its 0.15)",
              cells['FREE'] == [False, False], str(cells))
        check("...and closed while HV routes (0.225 mm < the rule's 0.25)",
              cells['HV'] == [True, True], str(cells))
        route(src, main_pass=True)
        print('2. with a rule-area clearance rule')
        src = stage(work, area=True)
        check('control: KiCad grades the unrouted fixture with FREE and HV unconnected only',
              {v['type'] for v in kicad_drc(src) if v['type'] in COPPER} == {'unconnected_items'})
        # 0.30 and 0.25 mm west of FREE's test pad (its edge at x 96.7)
        cells = map_cells(src, [(96.4, 100.0), (96.45, 100.0)])
        check("cells 0.25 and 0.30 mm off FREE's test pad are closed while HV routes "
              "(it needs 0.25 + 0.075)", cells['HV'] == [True, True], str(cells))
        route(src, main_pass=False)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)}: " + '; '.join(fails))
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
