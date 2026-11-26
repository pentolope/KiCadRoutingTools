"""route_diff's P/N transition vias clear foreign copper where they land.

    python3 tests/test_pair_via_landing_site.py
"""
import json
import math
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

PY = [sys.executable, '-X', 'utf8']
GRID, VIA, DRILL, CLR, H2H, HOLE_CLR, EDGE = 0.1, 0.5, 0.3, 0.15, 0.25, 0.25, 0.3
FOREIGN = (14.3, 9.037)
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def write(d, b):
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    pro = {'board': {'design_settings': {'rules': {
               'min_clearance': CLR, 'min_track_width': 0.15, 'min_via_diameter': VIA,
               'min_through_hole_diameter': DRILL, 'min_hole_to_hole': H2H,
               'min_hole_clearance': HOLE_CLR, 'min_copper_edge_clearance': EDGE},
               'rule_severities': {}}},
           'meta': {'filename': 'in.kicad_pro', 'version': 3},
           'net_settings': {'classes': [{'name': 'Default', 'clearance': CLR,
                                         'track_width': 0.15, 'via_diameter': VIA,
                                         'via_drill': DRILL}],
                            'meta': {'version': 4}}}
    with open(os.path.join(d, 'in.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(pro, fh)
    return pcb


def stage(d, foreign):
    b = RoutingBoard(w=30, h=20)
    b.pad('J1', 4, 9.65, 'D_P', sx=0.6, sy=0.3)
    b.pad('J2', 4, 10.35, 'D_N', sx=0.6, sy=0.3)
    b.pad('J3', 26, 9.65, 'D_P', sx=0.6, sy=0.3)
    b.pad('J4', 26, 10.35, 'D_N', sx=0.6, sy=0.3)
    b.seg(15, 1, 15, 19, 'WALL', w=0.3, locked=True)
    b.pad('J5', 15, 1, 'WALL', sx=0.6, sy=0.6)
    if foreign:
        b.via(*foreign, 'AGND', size=VIA, drill=DRILL, locked=True)
    return write(d, b)


def route(d, foreign):
    pcb = stage(d, foreign)
    out = os.path.join(d, 'out.kicad_pcb')
    r = subprocess.run(PY + [os.path.join(REPO, 'py_router', 'route_diff.py'), pcb, out,
                             '--nets', 'D_*', '--track-width', '0.15', '--diff-pair-gap', '0.15',
                             '--via-size', str(VIA), '--via-drill', str(DRILL),
                             '--grid-step', str(GRID), '--clearance', str(CLR), '--no-gnd-vias'],
                       capture_output=True, text=True, cwd=REPO)
    run_utils.evidence(out, 'routed board')
    return out, r.stdout + r.stderr


def vias(path):
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    names = dict(re.findall(r'^\s*\(net (\d+) "([^"]*)"\)', text, re.M))
    rows = []
    for m in re.finditer(r'\(via\s+\(at ([-\d.]+) ([-\d.]+)\)\s+\(size ([\d.]+)\)\s+'
                         r'\(drill ([\d.]+)\)[^()]*(?:\([^()]*\)\s*)*?\(net (\d+)\)', text):
        rows.append((float(m.group(1)), float(m.group(2)), float(m.group(3)),
                     float(m.group(4)), names.get(m.group(5))))
    return rows


def kicad_drc(path, pro):
    g = os.path.join(os.path.dirname(path), 'graded')
    os.makedirs(g, exist_ok=True)
    board = os.path.join(g, 'b.kicad_pcb')
    shutil.copy(path, board)
    shutil.copy(pro, os.path.join(g, 'b.kicad_pro'))
    report = os.path.join(g, 'drc.json')
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-error',
                    '-o', report, board], capture_output=True, text=True)
    with open(run_utils.evidence(report, 'kicad-cli report'), encoding='utf-8') as fh:
        return json.load(fh)


#: kind -> (centre distance at which a via just meets the rule, KiCad's row type)
KINDS = {'via': (VIA / 2 + CLR + VIA / 2, 'clearance'),
         'pth': (VIA / 2 + CLR + 0.3, 'clearance'),
         'smd': (VIA / 2 + CLR + 0.3, 'clearance'),
         'npth': (VIA / 2 + HOLE_CLR + 0.25, 'hole_clearance'),
         'edge': (VIA / 2 + EDGE, 'copper_edge_clearance')}
DELTAS = (-0.004, 0.004)


def site_board(d):
    b = RoutingBoard(w=40, h=20)
    sites = {}
    for k, (kind, (at, _row)) in enumerate(KINDS.items()):
        for m, delta in enumerate(DELTAS):
            x, y = 4.0 + 7.0 * k + 3.0 * m, 10.0
            uid = f's{k}{m}'
            if kind == 'edge':
                y = at + delta
            elif kind == 'via':
                b.via(x, y - at - delta, 'AGND', size=VIA, drill=DRILL, locked=True)
            elif kind == 'smd':
                b.pad('J%d%d' % (k, m), x, y - at - delta, 'AGND', sx=0.6, sy=0.6)
            else:
                n = b.net('AGND')
                pad = ('(pad "1" thru_hole circle (at 0 0) (size 0.6 0.6) (drill 0.3) '
                       '(layers "*.Cu" "*.Mask") (net %d "AGND") (uuid "%s-p"))' % (n, uid)
                       if kind == 'pth' else
                       '(pad "" np_thru_hole circle (at 0 0) (size 0.5 0.5) (drill 0.5) '
                       '(layers "*.Cu" "*.Mask") (uuid "%s-p"))' % uid)
                b.items.append('  (footprint "synth:T" (layer "F.Cu") (uuid "%s") (at %s %s) '
                               '(property "Reference" "H%d%d" (at 0 -2 0) (layer "F.SilkS") '
                               '(uuid "%s-r")) %s)' % (uid, x, y - at - delta, k, m, uid, pad))
            b.via(x, y, 'D_P', size=VIA, drill=DRILL)
            sites[(kind, delta)] = (x, y)
    return write(d, b), sites


def part3_site_check(td):
    print('3. the landing-site check against kicad-cli, just inside and just outside each rule')
    try:
        from diff_pair_routing import _pair_via_refusal
    except ImportError as exc:
        check('diff_pair_routing has the landing-site check', False, str(exc))
        return
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    cfg = GridRouteConfig(clearance=CLR, track_width=0.15, via_size=VIA, via_drill=DRILL,
                          hole_to_hole_clearance=H2H, board_edge_clearance=EDGE,
                          grid_step=GRID, layers=['F.Cu', 'B.Cu'])
    pcb, sites = site_board(os.path.join(td, 'sites'))
    rep = kicad_drc(pcb, os.path.join(os.path.dirname(pcb), 'in.kicad_pro'))
    hits = {}
    for v in rep.get('violations', []):
        for it in v['items']:
            if 'D_P' in it['description']:
                key = (round(it['pos']['x'], 3), round(it['pos']['y'], 3))
                hits.setdefault(key, set()).add(v['type'])
    board = parse_kicad_pcb(pcb)
    net = next(n for n, v in board.nets.items() if v.name == 'D_P')
    board.vias = [v for v in board.vias if v.net_id != net]
    for (kind, delta), (x, y) in sites.items():
        row = KINDS[kind][1]
        got = hits.get((round(x, 3), round(y, 3)), set())
        inside = delta < 0
        check(f'{kind} {delta:+.3f} mm: kicad-cli {"reports " + row if inside else "is clean"}',
              (row in got) if inside else not got, str(sorted(got)))
        why = _pair_via_refusal(x, y, net, board, cfg)
        check(f'{kind} {delta:+.3f} mm: the site is {"refused" if inside else "accepted"}',
              bool(why) == inside, why or '(accepted)')

def main():
    if not shutil.which('kicad-cli'):
        print('SKIP: kicad-cli not found')
        return 77
    td = tempfile.mkdtemp(prefix='krt_pairvia_')
    try:
        print('1. control: no foreign via, where the pair changes layer')
        ctrl, _ = route(os.path.join(td, 'control'), None)
        pv = sorted((x, y) for x, y, _s, _d, n in vias(ctrl) if n == 'D_P')
        nv = sorted((x, y) for x, y, _s, _d, n in vias(ctrl) if n == 'D_N')
        check('the pair changes layer twice, P above N', len(pv) == 2 and len(nv) == 2
              and all(p[1] < n[1] for p, n in zip(pv, nv)), f'{pv} {nv}')
        if failures:
            return 1
        (px, py), (nx, ny) = pv[0], nv[0]
        cx, cy = (px + nx) / 2, (py + ny) / 2
        off = math.hypot(px - cx, py - cy)
        grid_off = round(off / GRID) * GRID
        probe = (cx, cy - grid_off)
        need = VIA + CLR
        d_real = math.hypot(px - FOREIGN[0], py - FOREIGN[1])
        d_probe = math.hypot(probe[0] - FOREIGN[0], probe[1] - FOREIGN[1])
        check('the fixture via sits inside the clearance of the landed P via but clear '
              'of the grid site the offset rounds to',
              abs(px - FOREIGN[0]) < 1e-6 and d_real < need - 0.005 and d_probe > need + 0.005
              and abs(grid_off - off) > 0.01,
              f'P via {pv[0]} offset {off:.4f} grid {grid_off:.4f} real {d_real:.4f} '
              f'probe {d_probe:.4f} need {need:.4f}')

        print('2. the same board with a locked foreign via beside that P via')
        out, log = route(os.path.join(td, 'foreign'), FOREIGN)
        rows = vias(out)
        pair = [(x, y, s, dr) for x, y, s, dr, n in rows if n in ('D_P', 'D_N')]
        check('the pair still changes layer (4 vias)', len(pair) == 4, f'{pair}')
        worst_cu = min((math.hypot(x - FOREIGN[0], y - FOREIGN[1]) - s / 2 - VIA / 2
                        for x, y, s, _dr in pair), default=None)
        worst_h2h = min((math.hypot(x - FOREIGN[0], y - FOREIGN[1]) - dr / 2 - DRILL / 2
                         for x, y, _s, dr in pair), default=None)
        worst_hole = min((math.hypot(x - FOREIGN[0], y - FOREIGN[1]) - s / 2 - DRILL / 2
                          for x, y, s, _dr in pair), default=None)
        check(f'measured: every pair via copper >= {CLR} mm from the AGND via',
              worst_cu is not None and worst_cu >= CLR - 1e-6, f'{worst_cu}')
        check(f'measured: every pair via drill >= {H2H} mm from the AGND drill',
              worst_h2h is not None and worst_h2h >= H2H - 1e-6, f'{worst_h2h}')
        check(f'measured: every pair via copper >= {HOLE_CLR} mm from the AGND hole',
              worst_hole is not None and worst_hole >= HOLE_CLR - 1e-6, f'{worst_hole}')
        rep = kicad_drc(out, os.path.join(td, 'foreign', 'in.kicad_pro'))
        bad = [v for v in rep.get('violations', [])
               if v['type'] in ('clearance', 'hole_clearance', 'hole_to_hole')
               and any('D_P' in i['description'] or 'D_N' in i['description']
                       for i in v['items'])]
        check('kicad-cli: no clearance, hole_clearance or hole_to_hole error on the pair',
              not bad, '; '.join(v['description'] for v in bad[:3]))
        check('kicad-cli: nothing left unconnected', not rep.get('unconnected_items'),
              str(len(rep.get('unconnected_items') or [])))
        part3_site_check(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
