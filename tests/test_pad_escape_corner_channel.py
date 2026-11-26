#!/usr/bin/env python3
"""A pin whose only exit is a channel exactly as wide as the rules allow is
routed out of it, and KiCad's DRC passes the copper.

Fixture (written here): F.Cu only, a 0.5 mm pitch column of three 1.5 x 0.35
roundrect pins (rratio 0.25) -- SRC between two TI pins -- and a lone SRC pad
4 mm east. A .kicad_dru rule holds every non-pad item of SRC 0.25 mm off any
pad (and any non-pad item off an SRC pad), so a 0.15 mm SRC track can leave
the middle pin only along its own centre line: 0.25 mm to each neighbour's
flat edge, a few microns more past their rounded corners. The grid map widens
every pad corner by half a cell, which closes that line at both pin tips.

Negative control: at a 0.30 mm rule no track the fab can make fits the channel, so
the pin stays open and KiCad reports nothing but the missing connection.

Oracle: the routed board itself -- the two SRC pads joined by copper (KRT's
connectivity check), copper leaving the middle pin -- and kicad-cli's DRC
reporting no clearance, short or unconnected item on it.

    python3 tests/test_pad_escape_corner_channel.py
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

RUN_ALL_TIMEOUT = 300
CLI = find_kicad_cli(warn=False)


def _pad(num, x, y, net_id, net, sx=1.5, sy=0.35):
    return (f'  (pad "{num}" smd roundrect (at {x} {y}) (size {sx} {sy}) '
            f'(layers "F.Cu" "F.Mask" "F.Paste") (roundrect_rratio 0.25) '
            f'(net {net_id} "{net}") (uuid "a0000000-0000-0000-0000-00000000000{num}"))\n')


BOARD = ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
         ' (general (thickness 1.6) (legacy_teardrops no))\n'
         ' (paper "A4")\n'
         ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (1 "F.Mask" user) (3 "B.Mask" user)\n'
         '  (13 "F.Paste" user) (25 "Edge.Cuts" user))\n'
         ' (setup (pad_to_mask_clearance 0))\n'
         ' (net 0 "") (net 1 "SRC") (net 2 "TIA") (net 3 "TIB")\n'
         ' (footprint "fix:PINS" (layer "F.Cu") (uuid "f0000000-0000-0000-0000-000000000001")\n'
         '  (at 100 100)\n'
         '  (property "Reference" "U1" (at 0 -2 0) (layer "F.Fab") (uuid "f1000000-0000-0000-0000-000000000001")\n'
         '   (effects (font (size 1 1) (thickness 0.15))))\n'
         '  (attr smd)\n'
         + _pad(1, 0, -0.5, 2, 'TIA') + _pad(2, 0, 0, 1, 'SRC') + _pad(3, 0, 0.5, 3, 'TIB') +
         ' )\n'
         ' (footprint "fix:PAD" (layer "F.Cu") (uuid "f0000000-0000-0000-0000-000000000002")\n'
         '  (at 104 100)\n'
         '  (property "Reference" "TP1" (at 0 -2 0) (layer "F.Fab") (uuid "f1000000-0000-0000-0000-000000000002")\n'
         '   (effects (font (size 1 1) (thickness 0.15))))\n'
         '  (attr smd)\n'
         + _pad(4, 0, 0, 1, 'SRC', 0.8, 0.95) +
         ' )\n'
         ' (gr_rect (start 95 95) (end 108 105) (stroke (width 0.05) (type default)) (fill no)'
         ' (layer "Edge.Cuts") (uuid "e0000000-0000-0000-0000-000000000001"))\n'
         ')\n')
PRO = {'board': {'design_settings': {'rules': {'min_clearance': 0.15, 'min_track_width': 0.15}}},
       'meta': {'filename': 'board.kicad_pro', 'version': 3},
       'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.15, 'track_width': 0.15,
                                     'via_diameter': 0.5, 'via_drill': 0.25}],
                        'meta': {'version': 4}, 'netclass_patterns': []}}
DRU = ('(version 1)\n'
       '(rule "pad spacing" (constraint clearance (min {rule}mm))\n'
       ' (condition "A.Type == \'Pad\' && B.Type != \'Pad\' && '
       '(A.NetName == \'SRC\' || B.NetName == \'SRC\')"))\n')
#: kicad-cli violation types that are copper the route made wrong
COPPER = {'clearance', 'shorting_items', 'tracks_crossing', 'copper_edge_clearance',
          'hole_clearance', 'unconnected_items', 'track_width', 'track_dangling',
          'solder_mask_bridge'}

fails = []


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f': {name}' + (f' -- {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def stage(work, rule='0.25'):
    work = os.path.join(work, rule)
    os.makedirs(work)
    path = os.path.join(work, 'board.kicad_pcb')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(BOARD)
    with open(os.path.join(work, 'board.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(PRO, fh)
    with open(os.path.join(work, 'board.kicad_dru'), 'w', encoding='utf-8') as fh:
        fh.write(DRU.format(rule=rule))
    return path


def kicad_drc(path):
    out = path + '.drc.json'
    subprocess.run([CLI, 'pcb', 'drc', '--format', 'json', '--severity-all', '-o', out, path],
                   capture_output=True, text=True, timeout=300)
    with open(evidence(out, 'the kicad-cli report'), encoding='utf-8') as fh:
        rep = json.load(fh)
    return rep.get('violations', []) + rep.get('unconnected_items', [])


def main():
    if not CLI:
        print('SKIP: no kicad-cli (the oracle is its DRC)')
        sys.exit(77)
    work = tempfile.mkdtemp(prefix='pad_escape_')
    try:
        src = stage(work)
        check('control: KiCad grades the unrouted fixture with SRC unconnected only',
              {v['type'] for v in kicad_drc(src) if v['type'] in COPPER} == {'unconnected_items'})
        out = os.path.join(work, 'routed.kicad_pcb')
        r = run_check([sys.executable, '-X', 'utf8', tool('route.py'), src, out,
                       '--nets', 'SRC', '--layers', 'F.Cu',
                       '--track-width', '0.15', '--clearance', '0.15',
                       '--grid-step', '0.05', '--via-size', '0.5', '--via-drill', '0.25'],
                      accept=True)
        evidence(out, 'the routed board')
        r = subprocess.run([sys.executable, '-X', 'utf8', tool('check_connected.py'), out,
                            '--nets', 'SRC'], capture_output=True, text=True, timeout=300)
        check('check_connected ran to a verdict',
              'ALL NETS FULLY CONNECTED' in r.stdout or 'Disconnected' in r.stdout,
              (r.stdout + r.stderr)[-400:])
        check('SRC pads joined by copper (check_connected)',
              r.returncode == 0 and 'ALL NETS FULLY CONNECTED' in r.stdout, r.stdout[-400:])
        pcb = parse_kicad_pcb(out)
        src_id = next(n for n, net in pcb.nets.items() if net.name == 'SRC')
        segs = [s for s in pcb.segments if s.net_id == src_id]
        pin = next(p for p in pcb.pads_by_net[src_id] if p.component_ref == 'U1')
        on_pin = [s for s in segs
                  if any(abs(x - pin.global_x) <= pin.size_x / 2 and abs(y - pin.global_y) <= pin.size_y / 2
                         for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y)))]
        check('copper leaves the boxed pin', bool(on_pin), f'{len(segs)} SRC segment(s)')
        check('the pin exit runs on its centre line',
              bool(on_pin) and all(abs(s.start_y - pin.global_y) < 1e-6 and abs(s.end_y - pin.global_y) < 1e-6
                  for s in on_pin), str([(s.start_x, s.start_y, s.end_x, s.end_y) for s in on_pin]))
        check('at the requested 0.15 mm, nothing narrowed',
              bool(segs) and min(s.width for s in segs) >= 0.15 - 1e-9,
              str(sorted({s.width for s in segs})))
        bad = [v for v in kicad_drc(out) if v['type'] in COPPER]
        check('kicad-cli DRC: no copper violation and nothing unconnected', not bad,
              json.dumps(bad)[:600])
        print('negative control: a 0.30 mm rule closes the channel')
        src = stage(work, '0.30')
        out = os.path.join(os.path.dirname(src), 'routed.kicad_pcb')
        run_check([sys.executable, '-X', 'utf8', tool('route.py'), src, out,
                   '--nets', 'SRC', '--layers', 'F.Cu',
                   '--track-width', '0.15', '--clearance', '0.15',
                   '--grid-step', '0.05', '--via-size', '0.5', '--via-drill', '0.25'],
                  accept=True)
        got = {v['type'] for v in kicad_drc(evidence(out, 'the routed board')) if v['type'] in COPPER}
        check('0.30 mm: the pin stays open and no rule is broken', got == {'unconnected_items'},
              str(got))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)}: " + '; '.join(fails))
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
