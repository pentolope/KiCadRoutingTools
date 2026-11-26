#!/usr/bin/env python3
"""A pad escape's launch cell joins its pad through the escape's own stub on
every routing path, and nothing else is drawn there.

The fixture is test_pad_escape_corner_channel's: a 0.5 mm pitch column of
three 1.5 x 0.35 roundrect pins, SRC between two foreign pins, a .kicad_dru
rule holding SRC's tracks 0.25 mm off foreign pads (and foreign tracks off
SRC's pads). Variants:

  multi_pw   SRC is a 0.5 mm power net with a second far pad: the pin is an
             endpoint of the multipoint Phase 1 edge.
  zone       ... plus an SRC zone, so the net is pour-backed.
  tap        the far pads are 4.6 mm apart, the pin 4 mm from one: Phase 1
             joins the far pads and the pin
             is reached by a Phase 3 tap.
  asym       a second far pad, and the neighbours stand 0.30 / 0.15 mm off the
             pin under a 0.30 mm rule, so the only channel runs 0.05 mm off
             the pin's centre line (a connector from the launch to the pin's
             centre would be slanted); the route runs with --strict-sizes.
  tap_asym   the tap layout with the asymmetric neighbours.
  reach      the neighbours reach 1 mm past the pin's tip, so the corners
             that close the channel lie beyond pad_escape.ESCAPE_REACH_MM:
             the pin stays open, and opens once the reach is raised past
             them (the bound is what stops it).
  off        the power net under --escalation off: no stub narrower than the
             net is laid.

Oracle: the routed board -- the pads joined (check_connected), every SRC
segment at least the 0.15 mm floor, no two SRC segments stacked on one span,
the pin's copper joined by segments whose every point keeps the rule (kicad-
cli) -- kicad-cli DRC (clearance, shorts, dangling track, solder-mask
bridge, unconnected) with zones refilled, and the copper each route's path
was converted to before cleanup: none of it may reach into the pin, whose
only copper is the escape's exact-clear stub. Unit rows: the stub width rule
(power, impedance, KICAD_IMPEDANCE_NECKDOWN, --escalation off) and a stub's
commit/uncommit against the launch registry.

    python3 tests/test_pad_escape_paths.py
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ('py_router', 'rust_router', 'tests'):
    sys.path.insert(0, os.path.join(ROOT, _d))

import test_pad_escape_corner_channel as base               # noqa: E402
from kicad_parser import parse_kicad_pcb, Segment            # noqa: E402
from run_utils import check as run_check, evidence, tool     # noqa: E402

RUN_ALL_TIMEOUT = 600
FLOOR = 0.15
#: kicad-cli types that are copper the route made wrong (base.COPPER plus
#: the dangling and mask-bridge findings a stray connector shows up as)
COPPER = base.COPPER | {'track_dangling', 'solder_mask_bridge'}

fails = []


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + f': {name}' + (f' -- {detail}' if detail else ''))
    if not ok:
        fails.append(name)


def _tp(ref, n, x, y):
    return (f' (footprint "fix:PAD" (layer "F.Cu") (uuid "f0000000-0000-0000-0000-00000000010{n}")\n'
            f'  (at {x} {y})\n'
            f'  (property "Reference" "{ref}" (at 0 -2 0) (layer "F.Fab") '
            f'(uuid "f1000000-0000-0000-0000-00000000010{n}")\n'
            '   (effects (font (size 1 1) (thickness 0.15))))\n'
            '  (attr smd)\n'
            + base._pad(n, 0, 0, 1, 'SRC', 0.8, 0.95).replace(
                '"a0000000-0000-0000-0000-00000000000', '"a0000000-0000-0000-0000-00000000010') +
            ' )\n')


ZONE = (' (zone (net 1) (net_name "SRC") (layer "F.Cu") (uuid "e1000000-0000-0000-0000-000000000001")'
        ' (hatch edge 0.5)\n  (connect_pads (clearance 0.2)) (min_thickness 0.15) (filled_areas_thickness no)\n'
        '  (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))\n'
        '  (polygon (pts (xy 105.5 104) (xy 107.5 104) (xy 107.5 104.8) (xy 105.5 104.8))))\n')


def board(variant):
    b = base.BOARD
    rule = '0.25'
    if variant in ('asym', 'tap_asym'):
        b = b.replace(base._pad(1, 0, -0.5, 2, 'TIA'), base._pad(1, 0, -0.65, 2, 'TIA'))
        rule = '0.30'
    if variant == 'reach':
        for n, y, net_id, net in ((1, -0.5, 2, 'TIA'), (3, 0.5, 3, 'TIB')):
            b = b.replace(base._pad(n, 0, y, net_id, net), base._pad(n, 0.5, y, net_id, net, 2.5, 0.35))
    extra = ''
    if variant in ('multi_pw', 'zone', 'off', 'asym'):
        extra = _tp('TP2', 5, 104, 103)
    if variant in ('tap', 'tap_asym'):
        extra = _tp('TP2', 5, 107, 103.5)
    if variant == 'zone':
        extra += ZONE
    i = b.rindex(' (gr_rect')
    return b[:i] + extra + b[i:], rule


def route(work, variant, extra_args=(), reach=None):
    os.makedirs(work)
    text, rule = board(variant)
    src = os.path.join(work, 'board.kicad_pcb')
    with open(src, 'w', encoding='utf-8') as fh:
        fh.write(text)
    with open(os.path.join(work, 'board.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(base.PRO, fh)
    with open(os.path.join(work, 'board.kicad_dru'), 'w', encoding='utf-8') as fh:
        fh.write(base.DRU.format(rule=rule))
    out = os.path.join(work, 'routed.kicad_pcb')
    argv = [src, out, '--nets', 'SRC', '--layers', 'F.Cu', '--track-width', '0.15',
            '--clearance', '0.15', '--grid-step', '0.05', '--via-size', '0.5',
            '--via-drill', '0.25'] + list(extra_args)
    if variant in ('multi_pw', 'zone', 'off'):
        argv += ['--power-nets', 'SRC', '--power-nets-widths', '0.5']
    drawn = os.path.join(work, 'drawn.jsonl')
    cmd = [sys.executable, '-X', 'utf8', '-c', SHIM % (os.path.join(ROOT, 'py_router'), reach,
                                                      drawn, tool('route.py'))] + argv
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    log = r.stdout + r.stderr
    check(f'{variant}: route.py ran to the end', 'Traceback' not in log and
          os.path.isfile(out), log[-600:])
    return src, out, log, r.returncode


#: route.py in-process, with the path conversion recorded: every segment a
#: route's grid path is converted to (connectors to the terminals included)
#: goes to a JSON line, before any cleanup can remove it -- the escape's
#: stubs are not path copper and never pass through here. `reach` (not
#: None) sets pad_escape.ESCAPE_REACH_MM.
SHIM = '''
import json, runpy, sys
sys.path.insert(0, %r)
reach, drawn, route = %r, %r, %r
import pad_escape, single_ended_routing as ser
if reach is not None:
    pad_escape.ESCAPE_REACH_MM = reach
_conv = ser._path_to_segments_vias
def _spy(*a, **k):
    segs, vias = _conv(*a, **k)
    with open(drawn, 'a') as fh:
        for s in segs:
            fh.write(json.dumps([s.start_x, s.start_y, s.end_x, s.end_y, s.width]) + chr(10))
    return segs, vias
ser._path_to_segments_vias = _spy
sys.argv = [route] + sys.argv[1:]
runpy.run_path(route, run_name='__main__')
'''


def connected(out):
    r = subprocess.run([sys.executable, '-X', 'utf8', tool('check_connected.py'), out,
                        '--nets', 'SRC'], capture_output=True, text=True, timeout=300)
    return r.returncode == 0 and 'ALL NETS FULLY CONNECTED' in r.stdout


def kicad_copper(out):
    """kicad-cli's copper findings on a refilled copy of `out`."""
    d = os.path.dirname(out)
    cp = os.path.join(d, 'graded.kicad_pcb')
    shutil.copy(out, cp)
    for ext in ('.kicad_pro', '.kicad_dru'):
        s = os.path.splitext(out)[0] + ext
        if os.path.isfile(s):
            shutil.copy(s, os.path.join(d, 'graded' + ext))
    rep = cp + '.drc.json'
    subprocess.run([base.CLI, 'pcb', 'drc', '--refill-zones', '--format', 'json', '--severity-all',
                    '--all-track-errors', '-o', rep, cp], capture_output=True, text=True, timeout=300)
    with open(evidence(rep, 'the kicad-cli report'), encoding='utf-8') as fh:
        rep = json.load(fh)
    return [v for v in rep.get('violations', []) + rep.get('unconnected_items', [])
            if v['type'] in COPPER]


def src_segments(out):
    pcb = parse_kicad_pcb(out)
    sid = next(n for n, net in pcb.nets.items() if net.name == 'SRC')
    pin = next(p for p in pcb.pads_by_net[sid] if p.component_ref == 'U1')
    return [s for s in pcb.segments if s.net_id == sid], pin


def stacked(segs):
    """Pairs of segments on one layer whose spans overlap along one line."""
    out = []
    for i, a in enumerate(segs):
        for b in segs[i + 1:]:
            if a.layer != b.layer:
                continue
            ax, ay = a.end_x - a.start_x, a.end_y - a.start_y
            la = math.hypot(ax, ay)
            if la < 1e-9:
                continue
            ux, uy = ax / la, ay / la
            off = [abs((px - a.start_x) * uy - (py - a.start_y) * ux)
                   for px, py in ((b.start_x, b.start_y), (b.end_x, b.end_y))]
            if max(off) > 1e-6:
                continue
            t = sorted(((px - a.start_x) * ux + (py - a.start_y) * uy)
                       for px, py in ((b.start_x, b.start_y), (b.end_x, b.end_y)))
            if min(la, t[1]) - max(0.0, t[0]) > 1e-6:
                out.append(((a.start_x, a.start_y, a.end_x, a.end_y),
                            (b.start_x, b.start_y, b.end_x, b.end_y)))
    return out


def on_pin(segs, pin):
    return [s for s in segs
            if any(abs(x - pin.global_x) <= pin.size_x / 2 and abs(y - pin.global_y) <= pin.size_y / 2
                   for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y)))]


def grade_routed(variant, out, log):
    check(f'{variant}: the escape was taken', 'Pad escape' in log)
    check(f'{variant}: SRC pads joined (check_connected)', connected(out))
    segs, pin = src_segments(out)
    check(f'{variant}: no SRC copper under the {FLOOR} mm floor',
          bool(segs) and min(s.width for s in segs) >= FLOOR - 1e-9,
          str(sorted({round(s.width, 4) for s in segs})))
    check(f'{variant}: no SRC segments stacked on one span', not stacked(segs), str(stacked(segs)))
    pin_segs = on_pin(segs, pin)
    check(f'{variant}: nothing reaches the pin centre (the stub ends inside the pin, at its tip)',
          pin_segs and not any(math.hypot(x - pin.global_x, y - pin.global_y) < 0.5
                               for s in pin_segs
                               for x, y in ((s.start_x, s.start_y), (s.end_x, s.end_y))),
          str([(s.start_x, s.start_y, s.end_x, s.end_y, s.width) for s in pin_segs]))
    bad = kicad_copper(out)
    check(f'{variant}: kicad-cli: no copper finding', not bad, json.dumps(bad)[:600])
    drawn = os.path.join(os.path.dirname(out), 'drawn.jsonl')
    rows = []
    if os.path.isfile(drawn):
        with open(drawn) as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    into = [r for r in rows
            if any(abs(x - pin.global_x) <= pin.size_x / 2 and abs(y - pin.global_y) <= pin.size_y / 2
                   for x, y in ((r[0], r[1]), (r[2], r[3])))]
    check(f'{variant}: no path copper drawn into the pin (only the stub joins it)',
          bool(rows) and not into, str(into))


def units():
    print('unit: stub width')
    import env_knobs
    import fab_tiers
    from routing_config import GridRouteConfig
    from pad_escape import stub_width
    prev = fab_tiers.get_escalation_policy()
    prev_imp = env_knobs.IMPEDANCE_NECKDOWN
    try:
        fab_tiers.set_escalation_policy('fab')
        plain = GridRouteConfig(track_width=0.15)
        power = GridRouteConfig(track_width=0.15, power_net_widths={1: 0.5})
        imp = GridRouteConfig(track_width=0.15, layer_widths={'F.Cu': 0.3})
        check('a plain net: its own width', stub_width(plain, 1, 'F.Cu') == 0.15)
        check('a power net: the layer width', stub_width(power, 1, 'F.Cu') == 0.15)
        env_knobs.IMPEDANCE_NECKDOWN = True
        check('an impedance net, neckdown allowed: the nominal width',
              stub_width(imp, 1, 'F.Cu') == 0.15)
        env_knobs.IMPEDANCE_NECKDOWN = False
        check('an impedance net, KICAD_IMPEDANCE_NECKDOWN=0: its own width',
              stub_width(imp, 1, 'F.Cu') == 0.3, str(stub_width(imp, 1, 'F.Cu')))
        fab_tiers.set_escalation_policy('off')
        check('--escalation off: no stub narrower than a power net',
              stub_width(power, 1, 'F.Cu') is None)
        check('--escalation off: an impedance net that may not neck keeps its width',
              stub_width(imp, 1, 'F.Cu') == 0.3)
        env_knobs.IMPEDANCE_NECKDOWN = True
        check('--escalation off: no stub narrower than an impedance net',
              stub_width(imp, 1, 'F.Cu') is None)
        check('--escalation off: a plain net keeps its stub', stub_width(plain, 1, 'F.Cu') == 0.15)
    finally:
        fab_tiers.set_escalation_policy(*prev)
        env_knobs.IMPEDANCE_NECKDOWN = prev_imp

    print('unit: stub commit / uncommit')
    from pad_escape import note_used, joins_launch
    from single_ended_routing import _uncommit_unblock_copper
    work = tempfile.mkdtemp(prefix='pad_escape_unit_')
    try:
        path = os.path.join(work, 'b.kicad_pcb')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(base.BOARD)
        pcb = parse_kicad_pcb(path)
        cfg = GridRouteConfig(track_width=0.15)
        stub = [Segment(start_x=100.7, start_y=100, end_x=100.9, end_y=100, width=0.15,
                        layer='F.Cu', net_id=1)]
        cell = (2018, 2000, 0)
        note_used(pcb, 1, {cell: stub})
        check('a launch whose stub is not on the board joins nothing',
              not joins_launch(pcb, 1, cell, cfg))
        pcb.segments.extend(stub)
        check('a committed stub joins its launch', joins_launch(pcb, 1, cell, cfg))
        check('... for its own net only', not joins_launch(pcb, 2, cell, cfg))
        _uncommit_unblock_copper(pcb, [], stub)
        check('an uncommitted stub joins nothing again', not joins_launch(pcb, 1, cell, cfg)
              and not any(s is stub[0] for s in pcb.segments))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    if not base.CLI:
        print('SKIP: no kicad-cli (the oracle is its DRC)')
        sys.exit(77)
    units()
    work = tempfile.mkdtemp(prefix='pad_escape_paths_')
    try:
        for variant in ('multi_pw', 'zone', 'tap', 'tap_asym'):
            print(variant)
            _src, out, log, _rc = route(os.path.join(work, variant), variant)
            grade_routed(variant, out, log)

        print('asym, --strict-sizes')
        _src, out, log, rc = route(os.path.join(work, 'asym'), 'asym', ['--strict-sizes'])
        grade_routed('asym', out, log)
        check('asym: --strict-sizes passes (nothing shipped or disclosed below a size)',
              rc == 0 and 'below the requested size' not in log, f'rc {rc}')

        print('reach: the closing corners lie past ESCAPE_REACH_MM')
        _src, out, log, _rc = route(os.path.join(work, 'reach'), 'reach')
        check('reach: no escape is laid', 'Pad escape' not in log)
        check('reach: the pin stays open', not connected(out))
        got = {v['type'] for v in kicad_copper(out)}
        check('reach: kicad-cli reports only the missing connection', got == {'unconnected_items'},
              str(got))
        _src, out, log, _rc = route(os.path.join(work, 'reach_wide'), 'reach', reach=1.2)
        check('reach raised past the corners: the escape is taken and the pads joined',
              'Pad escape' in log and connected(out))
        bad = kicad_copper(out)
        check('reach raised: kicad-cli: no copper finding', not bad, json.dumps(bad)[:600])

        print('off: the power net under --escalation off')
        _src, out, log, _rc = route(os.path.join(work, 'off'), 'off', ['--escalation', 'off'])
        check('off: no escape is laid', 'Pad escape' not in log)
        segs, _pin = src_segments(out)
        check('off: no SRC copper narrower than the net', all(s.width >= 0.5 - 1e-9 for s in segs),
              str(sorted({round(s.width, 4) for s in segs})))
        bad = [v for v in kicad_copper(out) if v['type'] != 'unconnected_items']
        check('off: kicad-cli: no copper finding but the open pin', not bad, json.dumps(bad)[:600])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    if fails:
        print(f"FAILED {len(fails)}: " + '; '.join(fails))
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
