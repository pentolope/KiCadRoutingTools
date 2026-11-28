"""Rule-area clearances against other rules, on the router and the via nudge.

KiCad (kicad-cli 10.0.7) holds a pair at its last matching clearance rule in
file order; a rule with a `(layer ...)` clause binds on that layer only. The
oracle is kicad-cli DRC on the board each step writes.

1. route.py: a net S from (4,10) to (26,10) must cross a dam (a locked track
   of net W across the board) through a gap whose cap-to-track distance is
   `gap`; an area covers the gap.
     * two areas over the gap, whose rules come in the file in the opposite
       order to the regions the router builds (a width rule on the later one
       comes first): the later rule's 0.4 holds, the gap is 0.3;
     * a net rule after the area's (0.5 over 0.3), the gap 0.4;
     * an area rule after a net rule that relaxes it (0.2 after 0.5), the
       gap 0.3: S must route through;
     * a net rule after the area's with a `(layer "F.Cu")` clause (0.1):
       on B.Cu the area's 0.3 holds over a gap of 0.2, on F.Cu S routes.
   S lies in no clearance violation, and routes where a legal way exists.
2. nudge_grazing_vias: a via of this run a hair inside the clearance a
   later net rule, a later relaxing area or the area's rule on B.Cu beside
   a later F.Cu-only rule sets: the nudge clears it.

    python3 tests/test_area_rule_order_paths.py
"""
import json
import os
import re
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
from test_pair_via_area_rules import room  # noqa: E402

PY = [sys.executable, '-X', 'utf8']
CLR, VIA, DRILL, W = 0.15, 0.5, 0.3, 0.2
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


RULES = {'wb': ('track_width (min 0.15mm) (opt 0.2mm)', "A.insideArea('RB')", None),
         'ra': ('clearance (min 0.25mm)', "A.insideArea('RA')", None),
         'rb': ('clearance (min 0.4mm)', "A.insideArea('RB')", None),
         'room': ('clearance (min 0.3mm)', "A.insideArea('ROOM')", None),
         'late': ('clearance (min 0.5mm)', "A.NetName == 'LATE'", None),
         'early': ('clearance (min 0.5mm)', "A.NetName == 'EARLY'", None),
         'relax': ('clearance (min 0.2mm)', "A.insideArea('RELAX')", None),
         'lay': ('clearance (min 0.1mm)', "A.NetName == 'LAYR'", 'F.Cu')}


def write(d, b, rules):
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    pro = {'board': {'design_settings': {'rules': {
               'min_clearance': 0.1, 'min_track_width': 0.15, 'min_via_diameter': VIA,
               'min_through_hole_diameter': DRILL, 'min_hole_to_hole': 0.25,
               'min_hole_clearance': 0.25, 'min_copper_edge_clearance': 0.3},
               'rule_severities': {}}},
           'meta': {'filename': 'in.kicad_pro', 'version': 3},
           'net_settings': {'classes': [{'name': 'Default', 'clearance': CLR,
                                         'track_width': W, 'via_diameter': VIA,
                                         'via_drill': DRILL}],
                            'meta': {'version': 4}}}
    with open(os.path.join(d, 'in.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(pro, fh)
    text = '(version 1)\n'
    for name in rules:
        c, cond, layer = RULES[name]
        text += (f'(rule "{name}"\n' + (f'  (layer "{layer}")\n' if layer else '')
                 + f'  (constraint {c})\n  (condition "{cond}"))\n')
    with open(os.path.join(d, 'in.kicad_dru'), 'w', encoding='utf-8') as fh:
        fh.write(text)
    return pcb


def kicad_drc(pcb):
    d = os.path.dirname(pcb)
    g = os.path.join(d, 'graded')
    os.makedirs(g, exist_ok=True)
    for ext in ('kicad_pro', 'kicad_dru'):
        shutil.copy(os.path.join(d, f'in.{ext}'), os.path.join(g, f'b.{ext}'))
    shutil.copy(pcb, os.path.join(g, 'b.kicad_pcb'))
    report = os.path.join(g, 'drc.json')
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-error',
                    '-o', report, os.path.join(g, 'b.kicad_pcb')],
                   capture_output=True, text=True)
    with open(run_utils.evidence(report, 'kicad-cli report'), encoding='utf-8') as fh:
        return json.load(fh)


def clearance_hits(rep, net):
    return [v['description'] + ': ' + '; '.join(i['description'] for i in v['items'])
            for v in rep.get('violations', []) if v['type'] == 'clearance'
            and any(f'[{net}]' in i['description'] for i in v['items'])]


#: case -> (rules in file order, areas over the gap, dam net, gap, layer,
#: whether a legal way through exists)
ROUTES = {'two_areas_rule_order': (('wb', 'ra', 'rb'), ('RA', 'RB'), 'W', 0.3, 'F.Cu', False),
          'later_net_rule': (('room', 'late'), ('ROOM',), 'LATE', 0.4, 'F.Cu', False),
          'later_relaxing_area': (('early', 'relax'), ('RELAX',), 'EARLY', 0.3, 'F.Cu', True),
          'later_f_cu_rule_on_b_cu': (('room', 'lay'), ('ROOM',), 'LAYR', 0.2, 'B.Cu', False),
          'later_f_cu_rule_on_f_cu': (('room', 'lay'), ('ROOM',), 'LAYR', 0.2, 'F.Cu', True)}


def route_case(td, name, rules, areas, dam, gap, layer, through):
    b = RoutingBoard(w=30, h=20)
    for ref, x in (('J1', 4), ('J2', 26)):
        b.pad(ref, x, 10, 'S', sx=1.2, sy=1.2, tht=True)
    half = gap + W / 2 + W / 2
    b.seg(15, 0.7, 15, 10 - half, dam, w=W, layer=layer, locked=True)
    b.seg(15, 10 + half, 15, 19.3, dam, w=W, layer=layer, locked=True)
    for a in areas:
        room(b, ('F.Cu', 'B.Cu'), 12, 7, 18, 13, a)
    pcb = write(os.path.join(td, name), b, rules)
    out = os.path.join(td, name, 'out.kicad_pcb')
    r = subprocess.run(PY + [os.path.join(REPO, 'py_router', 'route.py'), pcb, out,
                             '--nets', 'S', '--layers', layer, '--clearance', str(CLR),
                             '--track-width', str(W)],
                       capture_output=True, text=True, cwd=REPO)
    if not os.path.isfile(out):
        raise AssertionError(f'BROKEN TEST: route.py {name} wrote no board (exit '
                             f'{r.returncode})\n{(r.stdout + r.stderr)[-2000:]}')
    rep = kicad_drc(out)
    hits = clearance_hits(rep, 'S')
    check(f'{name}: kicad-cli reports no clearance error on S', not hits, '; '.join(hits[:2]))
    if through:
        with open(out, encoding='utf-8') as fh:
            laid = len(re.findall(r'\(segment\b', fh.read())) > 2
        left = [u for u in rep.get('unconnected_items') or []
                if any('[S]' in i['description'] for i in u['items'])]
        check(f'{name}: S routes through the gap the rule allows', laid and not left,
              f'{len(left)} unconnected')


#: case -> (rules, area, foreign net, the clearance KiCad holds)
NUDGES = {'later_net_rule': (('room', 'late'), 'ROOM', 'LATE', 0.5),
          'later_relaxing_area': (('early', 'relax'), 'RELAX', 'EARLY', 0.2),
          'later_f_cu_rule': (('room', 'lay'), 'ROOM', 'LAYR', 0.3)}


def nudge_case(td, name, rules, area, foreign, need):
    from kicad_dru import install_layer_clearances, install_track_clearances
    from kicad_parser import parse_kicad_pcb
    from pcb_modification import nudge_grazing_vias
    from routing_config import GridRouteConfig
    b = RoutingBoard(w=12, h=12)
    x, y = 6.0, 6.0
    fy = y - VIA - (need - 0.01)
    b.via(x, fy, foreign, size=VIA, drill=DRILL, locked=True)
    b.via(x, y, 'D_P', size=VIA, drill=DRILL, uid='moving-via')
    room(b, ('F.Cu', 'B.Cu'), 3, fy - 1, 9, y + 1, area)
    pcb = write(os.path.join(td, 'nudge_' + name), b, rules)
    check(f'nudge {name}: kicad-cli reports the via before the nudge',
          bool(clearance_hits(kicad_drc(pcb), 'D_P')))
    board = parse_kicad_pcb(pcb)
    ids = {v.name: n for n, v in board.nets.items()}
    cfg = GridRouteConfig(clearance=CLR, track_width=W, via_size=VIA, via_drill=DRILL,
                          hole_to_hole_clearance=0.25, grid_step=0.1, layers=['F.Cu', 'B.Cu'])
    cfg.set_net_clearances({}, [ids['D_P']])
    install_layer_clearances(cfg, None, pcb, board)
    install_track_clearances(cfg, None, pcb, board, routed_net_ids=[ids['D_P']])
    mine = next(v for v in board.vias if v.net_id == ids['D_P'])
    moved, _nets, moves = nudge_grazing_vias(
        [{'new_vias': [mine], 'new_segments': []}], board, clearance=CLR,
        hole_to_hole=0.25, max_shift=0.05, config=cfg)
    with open(pcb, encoding='utf-8') as fh:
        text = fh.read()
    for _nid, _ox, _oy, nx, ny in moves:
        text = re.sub(r'\(via \(at [-\d.]+ [-\d.]+\)(?=[^\n]*moving-via)',
                      f'(via (at {nx:.6f} {ny:.6f})', text)
    out = os.path.join(td, 'nudge_' + name, 'out.kicad_pcb')
    with open(out, 'w', encoding='utf-8') as fh:
        fh.write(text)
    hits = clearance_hits(kicad_drc(out), 'D_P')
    check(f'nudge {name}: the via is moved clear ({moved} moved)', moved == 1 and not hits,
          '; '.join(hits[:2]))


def main():
    if not shutil.which('kicad-cli'):
        print('SKIP: kicad-cli not found')
        return 77
    td = tempfile.mkdtemp(prefix='krt_area_order_')
    try:
        print('1. route.py through a dam gap')
        for name, args in ROUTES.items():
            route_case(td, name, *args)
        print('2. nudge_grazing_vias')
        for name, args in NUDGES.items():
            nudge_case(td, name, *args)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
