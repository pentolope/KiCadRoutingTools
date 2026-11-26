#!/usr/bin/env python3
"""A clearance rule scoped to named parts' pads is routed to.

The pcbqa toolkit writes voltage-spacing rules of this shape:

    (rule "..." (constraint clearance (min 0.6mm))
      (condition "A.Type == 'Pad' && B.Type != 'Pad' && ((A.memberOfFootprint('TP6')
                  && A.NetName == 'C') || ...) && (B.NetName == 'A' || !(B.NetName != ''))"))

The scoped channel (#1491) left every memberOfFootprint() rule out, so the
route step laid net A's track at the class floor beside TP6 and its account
named the rule NOT honoured.

Board (F.Cu, class 0.2 mm, 1 mm pads, 0.2 mm tracks). Net A runs from A1 to
A2 through a 0.8 mm slot between TP8 and TP9 (net C, not named), then past
FID1 (no net, named), TP6 (net C, named) and a second part also referenced
TP6; net B runs past TP7 (net C, named, but B is not a listed net).

Checks:
  1. kicad-cli with straight tracks, FID1, both TP6 and TP7 0.3 mm off them:
     the rule binds A against FID1 and both TP6 and nothing else (the oracle
     for 3);
  2. the predicate: the rule's shape is taken, and a memberOfFootprint() rule
     whose part is not held to a pad, or that reads a pad number, is not;
  3. FID1, TP6 and TP7 on the straight lines; B routed alone, then A: each run
     names the rule among the scoped ones and not among the unsupported ones;
     B goes round TP7 closer than the rule; A goes through the slot and round
     FID1 and TP6 at the rule;
  4. kicad-cli on the routed board: no clearance violation;
  5. the via keep-out of the base map: a via of A sits 0.6 mm off FID1 and
     TP6 and closer to TP8;
  6. a through-hole TP6 under the rule with (layer "B.Cu"): A routed on B.Cu
     goes round it 0.6 mm off, on F.Cu closer; kicad-cli clean on both;
  7. route_diff, qfn_csi_underpad_diff (test_1526's staging): a rule holding
     a pair's partner pad on J1 off its partner's track, by the part and by
     the pad type; the run names it and kicad-cli finds no row of it (the
     pair price for the partner's pad stood in for the pad's own);
  8. a custom (polygon) pad is priced by the rule naming its part.

    python3 tests/test_footprint_pad_clearance_rules.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(TESTS, 'oracle'))
import run_utils  # noqa: E402
from constraint_agreement import find_kicad_cli, write_board  # noqa: E402

PY = [sys.executable, '-X', 'utf8', '-B']
FAILS = []

A, B, C = 1, 2, 3
PAD, WIDTH, GAP, RULE_MM, FLOOR = 1.0, 0.2, 0.3, 0.6, 0.2
OFF = PAD / 2 + WIDTH / 2 + GAP
ROW_A, ROW_B = 10.0, 25.0
# ref: (x, y, net id, net name, on the straight line when routed)
PADS = {'TP8': (11.0, ROW_A - OFF, C, 'C', False), 'TP9': (11.0, ROW_A + OFF, C, 'C', False),
        'FID1': (19.0, ROW_A, 0, '', True), 'TP6': (27.0, ROW_A, C, 'C', True),
        'TP6#2': (32.0, ROW_A, C, 'C', True), 'TP7': (20.0, ROW_B, C, 'C', True)}
NAME = 'voltage spacing, pad, bared: test'
LOOSE = 'part graphic: test'
COND = ("A.Type == 'Pad' && B.Type != 'Pad' && (((A.memberOfFootprint('TP6') && "
        "A.NetName == 'C') || ((A.memberOfFootprint('TP7') && A.NetName == 'C') || "
        "(A.memberOfFootprint('FID1') && !(A.NetName != '')))) && "
        "(B.NetName == 'A' || !(B.NetName != '')))")
DRU = (f'(rule "{NAME}" (constraint clearance (min {RULE_MM}mm)) (condition "{COND}"))\n'
       f'(rule "{LOOSE}" (constraint clearance (min 0.21mm)) '
       f'(condition "A.memberOfFootprint(\'TP8\') && B.NetName == \'B\'"))')


def ok(cond, msg, detail=''):
    print(f"  [{'ok' if cond else 'FAIL'}] {msg}{(': ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        FAILS.append(msg)


def where(ref, straight):
    x, y, _n, _nn, on_line = PADS[ref]
    return x, (y + OFF if straight and on_line else y)


def board(path, segments=(), straight=False):
    fps = [dict(ref='A1', x=5.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='A2', x=35.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='B1', x=5.0, y=ROW_B, net_id=B, net_name='B', size=PAD),
           dict(ref='B2', x=35.0, y=ROW_B, net_id=B, net_name='B', size=PAD)]
    for ref, (_x, _y, n, nn, _o) in PADS.items():
        x, y = where(ref, straight)
        fps.append(dict(ref=ref.split('#')[0], x=x, y=y, net_id=n, net_name=nn, size=PAD))
    write_board(path, footprints=fps, segments=segments, dru=DRU, edge=(0, 0, 40, 35))
    with open(path, encoding='utf-8') as fh:
        if fh.read().count('"Reference" "TP6"') != 2:
            raise AssertionError('BROKEN TEST: the board does not carry two parts referenced TP6')
    return run_utils.evidence(path, 'the board')


def gaps(path, net):
    """Edge-to-edge distance from `net`'s tracks to each pad of PADS."""
    from shapely.geometry import LineString, box
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(path)
    nid = next(i for i, n in pcb.nets.items() if n.name == net)
    segs = [s for s in pcb.segments if s.net_id == nid]
    out = {}
    for ref in PADS:
        x, y = where(ref, False)
        pad = box(x - PAD / 2, y - PAD / 2, x + PAD / 2, y + PAD / 2)
        out[ref] = min((LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)]).distance(pad)
                        - s.width / 2 for s in segs), default=None)
    return out


def clearance_rows(cli, path):
    out = path + '.drc.json'
    subprocess.run([cli, 'pcb', 'drc', '--format', 'json', '--severity-all',
                    '--all-track-errors', '--output', out, path],
                   capture_output=True, text=True, timeout=300)
    with open(run_utils.evidence(out, 'the kicad-cli report'), encoding='utf-8') as fh:
        rows = json.load(fh).get('violations')
    if not isinstance(rows, list):
        raise AssertionError(f'BROKEN TEST: kicad-cli wrote no violations list for {path}')
    return [(v.get('description', ''), [i.get('description', '') for i in v.get('items', [])])
            for v in rows if v.get('type') == 'clearance']


def oracle(cli, td):
    print('1. kicad-cli on straight tracks 0.3 mm from each pad')
    b = board(os.path.join(td, 'straight.kicad_pcb'), straight=True,
              segments=[(5.0, ROW_A, 35.0, ROW_A, WIDTH, 'F.Cu', A),
                        (5.0, ROW_B, 35.0, ROW_B, WIDTH, 'F.Cu', B)])
    rows = clearance_rows(cli, b)
    named = sorted(r for _d, items in rows for r in ('FID1', 'TP6', 'TP7', 'TP8', 'TP9')
                   if f' of {r}' in ' '.join(items))
    ok(named == ['FID1', 'TP6', 'TP6'] and all(NAME in d for d, _i in rows),
       'three clearance rows, all by the rule: A against FID1 and both TP6', rows)


def predicate():
    print('2. the predicate')
    from design_rules import parse_dru
    try:
        from kicad_dru import is_footprint_pad_rule, is_scoped_clearance_rule
    except ImportError as e:
        ok(False, 'kicad_dru reads a footprint-pad rule', e)
        return

    def rule(cond):
        return parse_dru(f'(rule "r" (constraint clearance (min 0.6mm)) (condition "{cond}"))')[0][0]
    ok(is_footprint_pad_rule(rule(COND)) and not is_scoped_clearance_rule(rule(COND)),
       "the toolkit's shape is a footprint-pad rule")
    for label, cond in (
            ('a part not held to a pad', "A.memberOfFootprint('TP6') && B.NetName == 'A'"),
            ('a pad guard on the other item',
             "B.Type == 'Pad' && A.memberOfFootprint('TP6') && B.NetName == 'A'"),
            ('a pad guard under an OR',
             "(A.Type == 'Pad' || A.NetName == 'C') && A.memberOfFootprint('TP6')"),
            ('both items scoped to parts',
             "A.Type == 'Pad' && A.memberOfFootprint('TP6') && B.memberOfFootprint('TP7')"),
            ('a pad number', "A.Type == 'Pad' && A.memberOfFootprint('TP6') && "
                             "A.Pad_Number == '1' && B.NetName == 'A'")):
        ok(not is_footprint_pad_rule(rule(cond)), f'{label}: not taken')


def route(src, out, net, summary):
    run_utils.check(PY + [os.path.join(REPO, 'py_router', 'route.py'), src, out,
                          '--nets', net, '--layers', 'F.Cu', '--track-width', str(WIDTH),
                          '--clearance', str(FLOOR), '--grid-step', '0.05',
                          '--escalation', 'off', '--stub-proximity-cost', '0',
                          '--json-out', summary], accept=True, timeout=900)
    with open(run_utils.evidence(summary, 'the summary'), encoding='utf-8') as fh:
        doc = json.load(fh)
    dr = doc.get('design_rules') or {}
    scoped = (dr.get('scoped_clearance') or {}).get('rules') or []
    unsupported = dr.get('unsupported_rules') or []
    ok(NAME in scoped and NAME not in unsupported,
       f'{net}: the run states the rule among the scoped ones, not the unsupported',
       dr.get('scoped_clearance'))
    ok(LOOSE in unsupported and LOOSE not in scoped,
       f'{net}: a rule it cannot apply exactly stays unsupported', unsupported)
    ok(doc.get('routed_single') == [net], f'{net} routes', doc.get('routed_single'))
    return run_utils.evidence(out, 'the routed board')


def routed(cli, td):
    print('3. routed: B alone, then A')
    src = board(os.path.join(td, 'in.kicad_pcb'))
    ob = route(src, os.path.join(td, 'b.kicad_pcb'), 'B', os.path.join(td, 'b.json'))
    out = route(ob, os.path.join(td, 'ba.kicad_pcb'), 'A', os.path.join(td, 'ba.json'))
    ga, gb = gaps(out, 'A'), gaps(out, 'B')
    ok(gb['TP7'] is not None and FLOOR - 1e-3 <= gb['TP7'] < RULE_MM - 0.1,
       'B, a net the rule does not list, goes round TP7 closer than the rule', gb)
    ok(all(ga[r] is not None and FLOOR - 1e-3 <= ga[r] < RULE_MM - 0.1 for r in ('TP8', 'TP9')),
       'A goes through the slot of TP8 and TP9, pads the rule does not name', ga)
    ok(all(ga[r] is not None and ga[r] >= RULE_MM - 1e-3 for r in ('FID1', 'TP6', 'TP6#2')),
       f'A goes round FID1 (no net) and both TP6 {RULE_MM} mm off', ga)
    print('4. kicad-cli on the routed board')
    ok(os.path.isfile(os.path.splitext(out)[0] + '.kicad_dru'), 'the rules file rides along')
    rows = clearance_rows(cli, out)
    ok(not rows, 'no clearance violation', rows)


def via_keepout(td):
    print('5. the via keep-out of the base map')
    import contextlib
    import io
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    from kicad_dru import install_layer_clearances, install_track_clearances
    from obstacle_map import build_base_obstacle_map
    b = board(os.path.join(td, 'vias.kicad_pcb'))
    pcb = parse_kicad_pcb(b)
    step, via = 0.05, 0.6
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], clearance=FLOOR, grid_step=step,
                          via_size=via, via_drill=0.3, track_width=WIDTH)
    with contextlib.redirect_stdout(io.StringIO()):
        install_layer_clearances(cfg, None, b, pcb)
        install_track_clearances(cfg, None, b, pcb, routed_net_ids=[A])
        obs = build_base_obstacle_map(pcb, cfg, [A])

    def first_open(ref):
        # the gap from the pad's east edge to the nearest via site east of it
        x, y = where(ref, False)
        gx, gy = round(x / step), round(y / step)
        for k in range(1, 60):
            if not obs.is_via_blocked(gx + k, gy):
                return round(k * step - PAD / 2 - via / 2, 4)
        return None
    got = {r: first_open(r) for r in ('TP8', 'FID1', 'TP6')}
    ok(all(got[r] is not None and got[r] >= RULE_MM - 1e-3 for r in ('FID1', 'TP6')),
       f'a via of A stands {RULE_MM} mm off FID1 and TP6', got)
    ok(got['TP8'] is not None and got['TP8'] < RULE_MM - 0.1, '...and closer to TP8', got)


THT = '(pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.5) (layers "*.Cu")'


def layered(cli, td):
    print('6. a through-hole TP6 under a rule with (layer "B.Cu")')
    from shapely.geometry import LineString, Point
    from kicad_parser import parse_kicad_pcb
    path = os.path.join(td, 'tht.kicad_pcb')
    fps = [dict(ref='A1', x=5.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='A2', x=35.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='TP6', x=20.0, y=ROW_A, net_id=C, net_name='C', size=PAD)]
    write_board(path, footprints=fps, dru=f'(rule "{NAME}" (layer "B.Cu") '
                f'(constraint clearance (min {RULE_MM}mm)) (condition "{COND}"))',
                edge=(0, 0, 40, 20))
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    if text.count('(pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu")') != 3:
        raise AssertionError('BROKEN TEST: the pads to make through-hole are not where expected')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text.replace('(pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu")', THT))
    for layer, bound in (('B.Cu', True), ('F.Cu', False)):
        out = os.path.join(td, f'tht_{layer[0]}.kicad_pcb')
        run_utils.check(PY + [os.path.join(REPO, 'py_router', 'route.py'), path, out,
                              '--nets', 'A', '--layers', layer, '--track-width', str(WIDTH),
                              '--clearance', str(FLOOR), '--grid-step', '0.05',
                              '--escalation', 'off', '--stub-proximity-cost', '0'],
                        accept=True, timeout=900)
        pcb = parse_kicad_pcb(run_utils.evidence(out, 'the routed board'))
        segs = [s for s in pcb.segments if s.net_id == A and s.layer == layer]
        gap = min((LineString([(s.start_x, s.start_y), (s.end_x, s.end_y)]).distance(
            Point(20.0, ROW_A).buffer(PAD / 2, 64)) - s.width / 2 for s in segs), default=None)
        ok(gap is not None and ((gap >= RULE_MM - 1e-3) if bound
                                else (FLOOR - 1e-3 <= gap < RULE_MM - 0.1)),
           f'on {layer} A goes round TP6 {"at the rule" if bound else "closer"}', gap)
        rows = clearance_rows(cli, out)
        ok(not rows, f'on {layer} kicad-cli finds no clearance violation', rows)


PAIR_RULES = {
    'by part': "A.memberOfFootprint('J1')",
    'by pad type': "A.Pad_Type == 'Through-hole'"}


def pair(cli, td):
    print('7. route_diff: the partner pad on J1 under a rule')
    import test_1526_scoped_pair_gap as t1526
    for label, term in PAIR_RULES.items():
        name = f'pair pad spacing {label}'
        rule = ('(version 1)\n(rule "csi intra gap" (constraint clearance (min 0.127mm))\n'
                "  (condition \"A.NetClass == 'CSI' && B.NetClass == 'CSI'\"))\n"
                f'(rule "{name}" (constraint clearance (min {RULE_MM}mm))\n'
                f"  (condition \"A.Type == 'Pad' && B.Type != 'Pad' && {term} && "
                "((A.NetName == 'CSI_D0_N' && B.NetName == 'CSI_D0_P') || "
                "(A.NetName == 'CSI_D0_P' && B.NetName == 'CSI_D0_N'))\"))\n")
        sub = os.path.join(td, 'pair_' + label.replace(' ', '_'))
        os.makedirs(sub)
        log, _pairs, out = t1526.route(sub, 'p', rule)
        line = next((ln for ln in log.splitlines() if ln.startswith('JSON_SUMMARY: ')), None)
        if line is None:
            raise AssertionError('BROKEN TEST: route_diff printed no JSON_SUMMARY')
        doc = json.loads(line[len('JSON_SUMMARY: '):])
        ok(name in ((doc.get('design_rules') or {}).get('scoped_clearance') or {}).get('rules', []),
           f'{label}: the run names the rule')
        rows = [d for d, _i in clearance_rows(cli, out) if name in d]
        ok(not rows, f'{label}: kicad-cli finds no row of it', rows)


CUSTOM = ('(pad "1" smd custom (at 0 0) (size 0.5 0.5) (layers "F.Cu") {net} '
          '(options (clearance outline) (anchor rect)) (primitives (gr_poly (pts '
          '(xy -0.5 -0.5) (xy 0.5 -0.5) (xy 0.5 0.5) (xy -0.5 0.5)) (width 0) (fill yes))))')


def custom(td):
    print('8. a custom (polygon) pad')
    import contextlib
    import io
    from kicad_parser import parse_kicad_pcb
    from routing_config import GridRouteConfig
    from kicad_dru import install_layer_clearances, install_track_clearances
    from single_ended_routing import _seg_foreign_pad_dist
    path = os.path.join(td, 'custom.kicad_pcb')
    fps = [dict(ref='A1', x=5.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='A2', x=35.0, y=ROW_A, net_id=A, net_name='A', size=PAD),
           dict(ref='TP6', x=15.0, y=ROW_A + OFF, net_id=C, net_name='C', size=PAD),
           dict(ref='TP8', x=25.0, y=ROW_A + OFF, net_id=C, net_name='C', size=PAD)]
    write_board(path, footprints=fps, dru=DRU, edge=(0, 0, 40, 20))
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    pad = '(pad "1" smd rect (at 0 0) (size 1.0 1.0) (layers "F.Cu") (net 3 "C"))'
    if text.count(pad) != 2:
        raise AssertionError('BROKEN TEST: the pads to make custom are not where expected')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text.replace(pad, CUSTOM.format(net='(net 3 "C")')))
    pcb = parse_kicad_pcb(path)
    if sum(1 for p in pcb.pads_by_net.get(C, []) if getattr(p, 'polygons', None)) != 2:
        raise AssertionError('BROKEN TEST: the parser did not read two custom pads')
    cfg = GridRouteConfig(layers=['F.Cu'], clearance=FLOOR, track_width=WIDTH)
    with contextlib.redirect_stdout(io.StringIO()):
        install_layer_clearances(cfg, None, path, pcb)
        install_track_clearances(cfg, None, path, pcb, routed_net_ids=[A])
    got = {}
    for ref, x in (('TP6', 15.0), ('TP8', 25.0)):
        d = _seg_foreign_pad_dist(pcb, A, x - 1.0, ROW_A, x + 1.0, ROW_A, 'F.Cu',
                                  base_clearance=FLOOR, config=cfg)
        got[ref] = round(d - WIDTH / 2, 4)
    ok(abs(got['TP6'] - (GAP - (RULE_MM - FLOOR))) < 1e-3,
       f'TP6 is priced at the rule: a {GAP} mm gap is {RULE_MM - FLOOR:g} mm short', got)
    ok(abs(got['TP8'] - GAP) < 1e-3, 'TP8 at the class', got)


def main():
    cli = find_kicad_cli()
    if not cli:
        print('SKIP: kicad-cli not found')
        return 77
    td = tempfile.mkdtemp(prefix='krt_fp_pad_')
    try:
        oracle(cli, td)
        predicate()
        routed(cli, td)
        via_keepout(td)
        layered(cli, td)
        pair(cli, td)
        custom(td)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if FAILS:
        print(f'\nFAILED ({len(FAILS)}):')
        for f in FAILS:
            print(f'  - {f}')
        return 1
    print('\nALL PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
