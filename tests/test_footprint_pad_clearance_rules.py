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
FID1 (no net, named) and TP6 (net C, named); net B runs past TP7 (net C,
named, but B is not a listed net).

Checks:
  1. kicad-cli with straight tracks, FID1, TP6 and TP7 0.3 mm off them, one
     track per pad (kicad-cli 10.0.7 reports one clearance row per track): the
     rule binds A against TP6 and FID1 and nothing else (the oracle for 3);
  2. the predicate: the rule's shape is taken, and a memberOfFootprint() rule
     whose part is not held to a pad, or that reads a pad number, is not;
  3. FID1, TP6 and TP7 on the straight lines; B routed alone, then A: each run
     names the rule among the scoped ones and not among the unsupported ones;
     B goes round TP7 closer than the rule; A goes through the slot and round
     FID1 and TP6 at the rule;
  4. kicad-cli on the routed board: no clearance violation.

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
        'TP7': (20.0, ROW_B, C, 'C', True)}
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
        fps.append(dict(ref=ref, x=x, y=y, net_id=n, net_name=nn, size=PAD))
    write_board(path, footprints=fps, segments=segments, dru=DRU, edge=(0, 0, 40, 35))
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
                    '--output', out, path], capture_output=True, text=True, timeout=300)
    with open(run_utils.evidence(out, 'the kicad-cli report'), encoding='utf-8') as fh:
        rows = json.load(fh).get('violations')
    if not isinstance(rows, list):
        raise AssertionError(f'BROKEN TEST: kicad-cli wrote no violations list for {path}')
    return [(v.get('description', ''), [i.get('description', '') for i in v.get('items', [])])
            for v in rows if v.get('type') == 'clearance']


def oracle(cli, td):
    print('1. kicad-cli on straight tracks 0.3 mm from each pad')
    b = board(os.path.join(td, 'straight.kicad_pcb'), straight=True,
              segments=[(5.0, ROW_A, 15.0, ROW_A, WIDTH, 'F.Cu', A),
                        (15.0, ROW_A, 23.0, ROW_A, WIDTH, 'F.Cu', A),
                        (23.0, ROW_A, 35.0, ROW_A, WIDTH, 'F.Cu', A),
                        (5.0, ROW_B, 35.0, ROW_B, WIDTH, 'F.Cu', B)])
    rows = clearance_rows(cli, b)
    named = sorted(r for _d, items in rows for r in PADS if f' of {r} ' in ' '.join(items))
    ok(named == ['FID1', 'TP6'] and len(rows) == 2 and all(NAME in d for d, _i in rows),
       'two clearance rows, both by the rule: A against TP6 and FID1', rows)


def predicate():
    print('2. the predicate')
    from design_rules import parse_dru
    from kicad_dru import is_footprint_pad_rule, is_scoped_clearance_rule

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
    ok(all(ga[r] is not None and ga[r] >= RULE_MM - 1e-3 for r in ('FID1', 'TP6')),
       f'A goes round FID1 (no net) and TP6 {RULE_MM} mm off', ga)
    print('4. kicad-cli on the routed board')
    ok(os.path.isfile(os.path.splitext(out)[0] + '.kicad_dru'), 'the rules file rides along')
    rows = clearance_rows(cli, out)
    ok(not rows, 'no clearance violation', rows)


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
