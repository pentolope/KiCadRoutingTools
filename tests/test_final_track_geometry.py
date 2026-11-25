"""route.py ships track geometry clean by construction: a grid staircase
becomes the 45-degree run it approximates, a track end that stops just
outside a via lands on the via centre, a sub-0.1 mm sliver between two
(almost) collinear pieces merges into one track, and a sub-0.1 mm width
step joins a neighbour. Run on an already-routed
board, so route.py takes its "nothing to route" return and only the end of
a run's cleanups touch the copper.

    python3 tests/test_final_track_geometry.py
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
import run_utils  # noqa: E402

RUN_ALL_TIMEOUT = 600
PY = [sys.executable, '-B', '-X', 'utf8']
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def pad_fp(ref, x, y, net, name, layer='F.Cu'):
    side = 'B' if layer == 'B.Cu' else 'F'
    return (f'  (footprint "T:P" (layer "{layer}") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0) (layer "{side}.SilkS") '
            f'(effects (font (size 1 1))))\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "{layer}" "{side}.Paste" '
            f'"{side}.Mask") (net {net} "{name}")))\n')


def seg(a, b, net, layer='F.Cu', w=0.2):
    seg.n += 1
    return (f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) '
            f'(layer "{layer}") (net {net}) (uuid "11111111-0000-0000-0000-{seg.n:012d}"))\n')


seg.n = 0

STAIR_START, STAIR_END = (5.0, 5.0), (6.0, 6.0)


def staircase():
    pts = [STAIR_START]
    x, y = STAIR_START
    for _ in range(20):
        x = round(x + 0.05, 4)
        pts.append((x, y))
        y = round(y + 0.05, 4)
        pts.append((x, y))
    return pts


def board_text():
    out = ['(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
           '  (general (thickness 1.6) (legacy_teardrops no))\n  (paper "A4")\n'
           '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user) '
           '(5 "F.SilkS" user "F.Silkscreen") (1 "F.Mask" user) (13 "F.Paste" user) '
           '(7 "B.SilkS" user "B.Silkscreen") (3 "B.Mask" user) (15 "B.Paste" user))\n'
           '  (setup (pad_to_mask_clearance 0))\n'
           '  (net 0 "") (net 1 "STAIR") (net 2 "LAND") (net 3 "SLIVER") (net 4 "STEP")\n']
    out.append(pad_fp('J1', 3, 5, 1, 'STAIR'))
    out.append(pad_fp('J2', 10, 6, 1, 'STAIR'))
    out.append(seg((3, 5), STAIR_START, 1))
    pts = staircase()
    for a, b in zip(pts, pts[1:]):
        out.append(seg(a, b, 1))
    out.append(seg(STAIR_END, (10, 6), 1))
    # LAND: an F.Cu track stopping 0.255 mm from a 0.5 mm via's centre
    out.append(pad_fp('J3', 3, 12, 2, 'LAND'))
    out.append(pad_fp('J4', 12, 12, 2, 'LAND', layer='B.Cu'))
    out.append(seg((3, 12), (8.0, 12), 2))
    out.append('  (via (at 8.255 12) (size 0.5) (drill 0.25) (layers "F.Cu" "B.Cu") (net 2) '
               '(uuid "44444444-0000-0000-0000-000000000001"))\n')
    out.append(seg((8.255, 12), (12, 12), 2, layer='B.Cu'))
    # SLIVER: a 0.05 mm piece between two runs it is collinear with to 0.5 um
    out.append(pad_fp('J5', 3, 16, 3, 'SLIVER'))
    out.append(pad_fp('J6', 10, 16, 3, 'SLIVER'))
    out.append(seg((3, 16), (6, 16), 3))
    out.append(seg((6, 16), (6.05, 16.0005), 3))
    out.append(seg((6.05, 16.0005), (10, 16), 3))
    # STEP: a 0.05 mm, 0.4 mm wide step between a 0.5 mm and a 0.25 mm track
    out.append(pad_fp('J7', 3, 18.5, 4, 'STEP'))
    out.append(pad_fp('J8', 10, 18.5, 4, 'STEP'))
    out.append(seg((3, 18.5), (6, 18.5), 4, w=0.5))
    out.append(seg((6, 18.5), (6.05, 18.5), 4, w=0.4))
    out.append(seg((6.05, 18.5), (10, 18.5), 4, w=0.25))
    out.append('  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.1) (type default)) '
               '(fill no) (layer "Edge.Cuts") (uuid "22222222-0000-0000-0000-000000000001"))\n)\n')
    return ''.join(out)


def tracks(path):
    text = open(path, encoding='utf-8').read()
    found = {}
    for m in re.finditer(r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
                         r'\s+\(width ([\d.]+)\)\s+\(layer "([^"]+)"\)\s+\(net (\d+)', text, re.S):
        a = (round(float(m.group(1)), 6), round(float(m.group(2)), 6))
        b = (round(float(m.group(3)), 6), round(float(m.group(4)), 6))
        found.setdefault(int(m.group(7)), set()).add((m.group(6), frozenset((a, b))))
        widths[(int(m.group(7)), frozenset((a, b)))] = float(m.group(5))
    return found


widths = {}


def as_set(*pairs, layer='F.Cu'):
    return {(layer, frozenset(p)) for p in pairs}


def gui_front():
    print('(e) the same pass on the GUI front (write model)')
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    from synth import make_pad, make_seg, make_via, make_pcb, make_net
    from improvement_gate import copper_signature
    from routing_config import GridRouteConfig
    from kicad_parser import BoardInfo
    from route import _late_final_geometry

    def case(keep_input):
        pads = [make_pad(7, 3, 12, ref='J3', size_x=1.0, size_y=1.0)]
        track = make_seg(3, 12, 8.0, 12, net_id=7, width=0.2)
        via = make_via(8.255, 12, net_id=7, size=0.5)
        pcb = make_pcb(nets={7: make_net(7, 'LAND')}, segments=[track], vias=[via],
                       pads_by_net={7: pads},
                       board_info=BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                                            copper_layers=['F.Cu', 'B.Cu']))
        sig = copper_signature([track], [via], {7: 'LAND'}.get)
        rd = {'results': []}

        def write_model(r):
            drop = {id(x) for x in r.get('segments_to_remove') or []}
            segs = [x for x in (track,) if id(x) not in drop]
            segs += [x for res in r['results'] for x in res['new_segments']]
            return {7: segs}, {7: [via]}

        _late_final_geometry(pcb, None, True, rd, write_model, sig, {'LAND'},
                             keep_input, GridRouteConfig())
        return rd, track, write_model

    rd, track, wm = case(False)
    shipped = [((x.start_x, x.start_y), (x.end_x, x.end_y)) for x in wm(rd)[0][7]]
    check('the input track joins segments_to_remove', track in (rd.get('segments_to_remove') or []))
    check('the track that ships ends on the via centre',
          [sorted(p) for p in shipped] == [[(3, 12), (8.255, 12)]], f'{shipped}')
    rd, track, wm = case(True)
    check('--keep-input-copper: the input track stays as it was',
          not rd.get('segments_to_remove') and wm(rd)[0][7] == [track])


def main():
    td = tempfile.mkdtemp(prefix='krt_geom_')
    try:
        src = os.path.join(td, 'in.kicad_pcb')
        open(src, 'w', encoding='utf-8').write(board_text())
        shutil.copy(os.path.join(TESTS, 'fixtures', 'sense_branch', 'sense_star.kicad_pro'),
                    os.path.join(td, 'in.kicad_pro'))
        pro = json.load(open(os.path.join(td, 'in.kicad_pro'), encoding='utf-8'))
        pro['board']['design_settings']['rules']['min_track_width'] = 0.2
        json.dump(pro, open(os.path.join(td, 'in.kicad_pro'), 'w', encoding='utf-8'))
        out = os.path.join(td, 'out.kicad_pcb')
        r = subprocess.run(PY + [os.path.join('py_router', 'route.py'), src, out,
                                 '--nets', 'STAIR', 'LAND', 'SLIVER', 'STEP'],
                           capture_output=True, text=True, cwd=REPO)
        run_utils.evidence(out)
        check('route.py takes the "nothing to route" return', 'nothing to route' in r.stdout,
              r.stdout[-600:] + r.stderr[-600:])
        got = tracks(out)

        print('(a) a 0.05 mm grid staircase from (5,5) to (6,6)')
        check('ships as the one 45-degree track it approximates',
              got.get(1) == as_set(((3, 5), STAIR_START), (STAIR_START, STAIR_END),
                                   (STAIR_END, (10, 6))),
              f'{sorted((L, tuple(sorted(p))) for L, p in got.get(1, ()))}')

        print('(b) an F.Cu end 0.255 mm from a radius-0.25 via')
        check('lands on the via centre',
              got.get(2) == as_set(((3, 12), (8.255, 12)))
              | as_set(((8.255, 12), (12, 12)), layer='B.Cu'),
              f'{sorted((L, tuple(sorted(p))) for L, p in got.get(2, ()))}')

        print('(c) a 0.05 mm sliver collinear with its neighbours to 0.5 um')
        check('merges into one track', got.get(3) == as_set(((3, 16), (10, 16))),
              f'{sorted((L, tuple(sorted(p))) for L, p in got.get(3, ()))}')

        print('(d) a 0.05 mm width step between a 0.5 mm and a 0.25 mm track')
        step = sorted((widths[(4, p)], tuple(sorted(p))) for _L, p in got.get(4, ()))
        check('joins a neighbour: a 0.5 mm track from J7 meets a 0.25 mm one to J8 '
              'where the step was',
              len(step) == 2 and step[0][0] == 0.25 and step[1][0] == 0.5
              and step[0][1][1] == (10, 18.5) and step[1][1][0] == (3, 18.5)
              and step[0][1][0] == step[1][1][1] and step[0][1][0] in ((6, 18.5), (6.05, 18.5)),
              f'{step}')

        con = subprocess.run(PY + [os.path.join('py_router', 'check_connected.py'), out],
                             capture_output=True, text=True, cwd=REPO).stdout
        check('every net stays connected', 'ALL NETS FULLY CONNECTED' in con, con[-400:])
    finally:
        shutil.rmtree(td, ignore_errors=True)
    gui_front()
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


def test_final_track_geometry():
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())
