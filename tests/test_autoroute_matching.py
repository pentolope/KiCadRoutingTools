#!/usr/bin/env python3
import json
import math
import os
import re
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'stress'))
import autoroute as AP                                        # noqa: E402
from cli_argv_check import parsed_args                       # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

RUN_ALL_TIMEOUT = 400
SCRIPT = os.path.join(ROOT, 'py_router', 'autoroute.py')
FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def singles_board(path, ends=(42, 32, 30), stackup=False):
    b = RoutingBoard(50, 30)
    for i, (net, x2) in enumerate(zip(('SIG_A', 'SIG_B', 'SIG_C'), ends)):
        b.pad(f'P{2 * i + 1}', 6, 6 + 8 * i, net)
        b.pad(f'P{2 * i + 2}', x2, 6 + 8 * i, net)
    text = b.text()
    if stackup:
        text = text.replace('(setup (pad_to_mask_clearance 0))', (
            '(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) '
            '(layer "dielectric 1" (type "core") (thickness 0.2) (material "FR4") '
            '(epsilon_r 4.5) (loss_tangent 0.02)) '
            '(layer "B.Cu" (type "copper") (thickness 0.035))) '
            '(pad_to_mask_clearance 0))'))
    return write_board(text, path)


def pairs_board(path, singles=False):
    b = RoutingBoard(50, 50 if singles else 30)
    k = 0
    for i, (base, x2) in enumerate((('D', 42), ('E', 30))):
        for j, pol in enumerate('PN'):
            y = 8 + 10 * i + 0.6 * j
            for x in (6, x2):
                k += 1
                b.pad(f'U{k}', x, y, f'{base}_{pol}', sx=0.4, sy=0.4)
    if singles:
        for i, (net, x2) in enumerate((('SIG_A', 42), ('SIG_B', 32), ('SIG_C', 30))):
            b.pad(f'P{2 * i + 1}', 6, 28 + 8 * i, net)
            b.pad(f'P{2 * i + 2}', x2, 28 + 8 * i, net)
    return write_board(b.text(), path)


def copper(board):
    text = open(evidence(board, 'routed board'), encoding='utf-8').read()
    names = dict(re.findall(r'\(net (\d+) "([^"]*)"\)', text))
    out = {}
    for m in re.finditer(r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) '
                         r'([-\d.]+)\)\s+\(width ([\d.]+)\).*?\(net (\d+)\)', text, re.S):
        x1, y1, x2, y2, w = map(float, m.groups()[:5])
        length, widths, vias = out.get(names[m.group(6)], (0.0, [], 0))
        out[names[m.group(6)]] = (length + math.hypot(x2 - x1, y2 - y1), widths + [w], vias)
    for m in re.finditer(r'\(via\b.*?\(net (\d+)\)', text, re.S):
        length, widths, vias = out.get(names[m.group(1)], (0.0, [], 0))
        out[names[m.group(1)]] = (length, widths, vias + 1)
    return out


def spread(cu, nets):
    vals = [cu[n][0] for n in nets]
    return max(vals) - min(vals)


def _summary(stdout):
    m = re.search(r'^AUTOROUTE_SUMMARY: (\{.*\})$', stdout, re.M)
    return json.loads(m.group(1)) if m else {}


class Recorder(AP.ChainEvents):

    def __init__(self):
        self.cur = 'IN'
        self.runs = []
        self.steps = []
        self.grades = [{'open': n, 'drc': 0, 'open_nets': []} for n in (5, 3, 1)]
        self.last_iterations = None

    def run(self, kind, args, tag, **kw):
        self.runs.append((kind, tag, list(args)))
        self.steps.append({'tag': tag, 'ok': True, 'log': tag + '.log'})
        self.last_iterations = 50
        return f'OUT{len(self.runs)}', {}, {}

    def accept(self, out):
        if out:
            self.cur = out

    def grade(self, board):
        return self.grades.pop(0)

    def drc_nets(self, board):
        diff = [i for i, r in enumerate(self.runs) if r[0] == 'diff']
        return {'D_N'} if diff and board == f'OUT{len(self.runs)}' else set()

    def check_drc(self, board):
        return {'violations': 0, 'items': []}


def chain_runs(a):
    ch = Recorder()
    orig = AP.Chain
    AP.Chain = lambda *args, **k: ch
    try:
        AP.run_plan('IN', 'OUT', a, TMP, effort_factor=4.0)
    finally:
        AP.Chain = orig
    return ch.runs


def parsed(kind, args):
    argv = [AP.TOOLS[kind]] + [x.replace('{IN}', 'in.kicad_pcb').replace(
        '{OUT}', 'out.kicad_pcb').replace('{JSON}', 'out.json') for x in args]
    r = parsed_args(argv)
    assert r is not None and r['ok'], f'{os.path.basename(argv[0])} refuses {args}: {r}'
    return r['ns']


def test_steps():
    from length_matching import find_nets_matching_patterns
    from net_queries import expand_net_patterns, find_differential_pairs
    print("each group and target reaches the step that routes its nets")
    n = len(FAILS)
    board = pairs_board(os.path.join(TMP, 'steps.kicad_pcb'), singles=True)
    pcb = parse_kicad_pcb(board)
    names = sorted(x.name for x in pcb.nets.values() if x.name)
    a = AP.analyze(board, length_groups=[['E_P', 'E_N'], ['SIG_[AB]']], length_tol=0.5,
                   impedance=[('D_*', 85.0), ('SIG_C', 50.0)])
    runs = chain_runs(a)
    tags = [t for _, t, _ in runs]
    expect(tags == ['diffpairs', 'diffpairs_85ohm', 'impedance_50ohm', 'reroute_pairs',
                    'route', 'route_p2', 'route_p3'],
           f"the chain: two diff calls, the 50 ohm step, the reroute, three passes ({tags})")
    rows = [r['tag'] for r in AP.plan_steps(a)['steps']]
    expect(rows == [t for t in tags if t != 'reroute_pairs'],
           f"AUTOROUTE_PLAN lists each of them but the reroute, which the run adds ({rows})")

    def groups(ns):
        return [sorted(find_nets_matching_patterns(names, g))
                for g in ns.get('length_match_groups') or []]

    def pairs(ns):
        return sorted(p.p_net_name for p in find_differential_pairs(pcb, ns['nets']).values())
    for kind, tag, args in runs:
        ns = parsed(kind, args)
        got = (groups(ns), ns.get('impedance'), ns.get('length_match_tolerance'),
               ns.get('time_matching'))
        if tag == 'diffpairs':
            want = ([['E_N', 'E_P']], None, 0.5, False)
            expect(pairs(ns) == ['E_P'] and got == want,
                   f"{tag}: pair E with its group at 0.5 mm, no impedance "
                   f"({pairs(ns)}, {got})")
        elif tag == 'diffpairs_85ohm':
            expect(pairs(ns) == ['D_P'] and got[:2] == ([], 85.0),
                   f"{tag}: pair D at --impedance 85, no group ({pairs(ns)}, {got})")
        elif tag == 'impedance_50ohm':
            nets = sorted(expand_net_patterns(pcb, ns['nets'], quiet=True))
            expect(nets == ['SIG_C'] and got[:2] == ([], 50.0),
                   f"{tag}: SIG_C alone at --impedance 50, no group ({nets}, {got})")
        elif tag == 'reroute_pairs':
            expect(got[:2] == ([], None), f"{tag}: no group, no impedance ({got})")
        else:
            expect(got == ([['SIG_A', 'SIG_B']], None, 0.5, False),
                   f"{tag}: the SIG_A/SIG_B group at 0.5 mm, no impedance ({got})")

    plain = chain_runs(AP.analyze(board))
    flags = ('--length-match-group', '--length-match-tolerance', '--time-matching',
             '--impedance')
    expect([t for _, t, _ in plain] == ['diffpairs', 'reroute_pairs', 'route',
                                         'route_p2', 'route_p3']
           and not any(f in args for _, _, args in plain for f in flags),
           f"without the flags: one diff call, no impedance step, none of the step "
           f"flags ({[t for _, t, _ in plain]})")

    print("a time group reaches its step as --time-matching with its tolerance")
    a = AP.analyze(board, time_groups=[['SIG_A', 'SIG_B']], time_tol=3.0)
    for kind, tag, args in chain_runs(a):
        ns = parsed(kind, args)
        if kind == 'route' and tag.startswith('route'):
            expect(groups(ns) == [['SIG_A', 'SIG_B']] and ns.get('time_matching') is True
                   and ns.get('time_match_tolerance') == 3.0,
                   f"{tag}: SIG_A/SIG_B, --time-matching, 3 ps "
                   f"({groups(ns)}, {ns.get('time_matching')}, {ns.get('time_match_tolerance')})")
        else:
            expect(not groups(ns) and not ns.get('time_matching'),
                   f"{tag}: no group ({groups(ns)})")
    assert len(FAILS) == n, FAILS[n:]


def _analysis(board, *argv):
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, os.devnull + '.kicad_pcb',
               '--dry-run', *argv], accept=True)
    m = re.search(r'^AUTOROUTE_ANALYSIS: (\{.*\})$', r.stdout, re.M)
    return json.loads(m.group(1)) if m else {}


def test_refusals():
    print("what no step can do is refused for its reason, before any step runs")
    n = len(FAILS)
    board = pairs_board(os.path.join(TMP, 'refuse.kicad_pcb'), singles=True)
    out = os.path.join(TMP, 'refused.kicad_pcb')
    for argv, why in (
            (('--length-match-group', 'D_P', 'D_N', 'SIG_A'),
             "--length-match-group D_P D_N SIG_A: its nets are routed by different "
             "steps (pairs by route_diff.py: D_N, D_P; single-ended by route.py's "
             "route passes: SIG_A). A step lengthens only the nets it routes"),
            (('--net-impedance', 'SIG_A', '50', '--length-match-group', 'SIG_A', 'SIG_B'),
             "--length-match-group SIG_A SIG_B: its nets are routed by different steps "
             "(single-ended by route.py's route passes: SIG_B; single-ended by "
             "route.py's 50 ohm step: SIG_A)"),
            (('--length-match-group', 'SIG_A'),
             "--length-match-group SIG_A: selects 1 of the nets this run routes; a "
             "group needs two"),
            (('--length-match-group', 'auto'),
             "--length-match-group auto: 'auto' forms its DRAM groups inside a step"),
            (('--length-match-group', 'SIG_*', '--length-match-group', 'SIG_A', 'SIG_B'),
             "--length-match-group SIG_A SIG_B: SIG_A is in --length-match-group SIG_* too"),
            (('--diff-pair', 'SIG_A', 'SIG_B'),
             "--diff-pair SIG_A SIG_B: route_diff.py pairs nets by their names only"),
            (('--diff-pair', 'D_N', 'D_P'),
             "--diff-pair D_N D_P: route_diff.py pairs nets by their names only"),
            (('--net-impedance', '*_[PNC]', '90'),
             "--net-impedance *_[PNC] 90: selects pair nets (a differential target) and "
             "single-ended nets"),
            (('--net-impedance', 'D_P', '85'),
             "--net-impedance: D_P and D_N are one pair, which route_diff.py routes at "
             "one differential target"),
            (('--net-impedance', 'SIG_C', 'fifty'),
             "--net-impedance SIG_C fifty: the ohms must be a finite number > 0"),
            (('--finish', '--length-match-group', 'D_*'),
             "--length-match-group D_*: --finish runs no diff-pair step")):
        check([sys.executable, '-X', 'utf8', SCRIPT, board, out, *argv],
              refuse=f'AUTOROUTE: refused: {why}', code=2)
        expect(not os.path.exists(out + '.autoroute') and not os.path.exists(out),
               f"refused before any step runs: {why[:70]}")

    print("a length group and a time group on two different steps both run")
    a = _analysis(board, '--length-match-group', 'SIG_A', 'SIG_B',
                  '--time-match-group', 'D_*', 'E_*', '--time-match-tolerance', '2')
    expect([(g['mode'], g['step'], g['nets'], g['tolerance'])
            for g in a.get('match_groups', [])]
           == [('length', 'route', ['SIG_A', 'SIG_B'], 0.1),
               ('time', 'route_diff', ['D_N', 'D_P', 'E_N', 'E_P'], 2.0)],
           f"single-ended length group (default 0.1 mm) and pair time group (2 ps): "
           f"accepted ({a.get('match_groups')})")

    print("--diff-pair: the listed pairs and no other")
    a = _analysis(board, '--diff-pair', 'E_P', 'E_N')
    expect(a.get('pair_nets') == ['E_P', 'E_N'] and a.get('diff_pairs') == [['E_P', 'E_N']],
           f"--diff-pair E_P E_N: E alone, D left to route.py ({a.get('pair_nets')})")
    expect(_analysis(board).get('pair_nets') == ['D_P', 'D_N', 'E_P', 'E_N'],
           "without it both name-detected pairs")
    assert len(FAILS) == n, FAILS[n:]


def test_mode_conflict():
    print("a step that would carry both a length and a time group is refused")
    n = len(FAILS)
    b = RoutingBoard(50, 40)
    for i, net in enumerate(('SIG_A', 'SIG_B', 'SIG_C', 'SIG_D')):
        b.pad(f'P{2 * i + 1}', 6, 6 + 8 * i, net)
        b.pad(f'P{2 * i + 2}', 40, 6 + 8 * i, net)
    board = write_board(b.text(), os.path.join(TMP, 'four.kicad_pcb'))
    out = os.path.join(TMP, 'four_out.kicad_pcb')
    check([sys.executable, '-X', 'utf8', SCRIPT, board, out, '--length-match-group',
           'SIG_A', 'SIG_B', '--time-match-group', 'SIG_C', 'SIG_D'],
          refuse="AUTOROUTE: refused: route.py's route passes would carry length "
                 "group(s) SIG_A SIG_B and time group(s) SIG_C SIG_D, and its "
                 "--time-matching applies to every group of a call", code=2)
    expect(not os.path.exists(out + '.autoroute'), "refused before any step runs")
    check([sys.executable, '-X', 'utf8', SCRIPT, board, out, '--net-impedance', 'SIG_[CD]',
           '50', '--length-match-group', 'SIG_A', 'SIG_B', '--time-match-group', 'SIG_C',
           'SIG_D'],
          refuse="AUTOROUTE: refused: route.py's route passes would carry length "
                 "group(s) SIG_A SIG_B and time group(s) SIG_C SIG_D", code=2)
    expect(not os.path.exists(out + '.autoroute'), "refused before any step runs")
    assert len(FAILS) == n, FAILS[n:]


def _run(board, out, *argv):
    js = out[:-len('.kicad_pcb')] + '.json'
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, out, '--json-out', js, *argv],
              accept=True)
    return _summary(r.stdout), json.load(open(evidence(js, '--json-out document'))), \
        json.load(open(evidence(out + '.autoroute/plan.json', 'plan.json')))


def test_length_run():
    print("a real run: SIG_A and SIG_B matched to 0.5 mm, measured on the shipped copper")
    n = len(FAILS)
    board = singles_board(os.path.join(TMP, 'len.kicad_pcb'))
    summary, doc, plan = _run(board, os.path.join(TMP, 'len_routed.kicad_pcb'),
                              '--length-match-group', 'SIG_A', 'SIG_B',
                              '--length-match-tolerance', '0.5')
    cu = copper(os.path.join(TMP, 'len_routed.kicad_pcb'))
    s = spread(cu, ('SIG_A', 'SIG_B'))
    expect(summary.get('open') == 0, f"the board routes ({summary.get('open')} open)")
    expect(not any(cu[x][2] for x in cu), "no vias: the length is the track length")
    expect(s <= 0.5, f"SIG_A {cu['SIG_A'][0]:.3f} mm, SIG_B {cu['SIG_B'][0]:.3f} mm: "
                     f"spread {s:.3f} mm within 0.5")
    plain_s, _, _ = _run(board, os.path.join(TMP, 'len_plain.kicad_pcb'))
    pc = copper(os.path.join(TMP, 'len_plain.kicad_pcb'))
    ps = spread(pc, ('SIG_A', 'SIG_B'))
    expect(ps > 0.5 and ps > 10 * s, f"without the flag: spread {ps:.3f} mm")
    expect(abs(pc['SIG_C'][0] - cu['SIG_C'][0]) < 1e-6,
           f"SIG_C, outside the group, ships the same copper ({cu['SIG_C'][0]:.3f} mm)")
    expect('match_groups' not in plain_s,
           "without the flag the summary records no group")

    print("the account: the group as requested, and what shipped")
    rows = summary.get('match_groups') or [{}]
    row = rows[0]
    expect(len(rows) == 1 and {k: row.get(k) for k in ('mode', 'patterns', 'nets',
                                                       'tolerance', 'unit', 'step')}
           == {'mode': 'length', 'patterns': ['SIG_A', 'SIG_B'],
               'nets': ['SIG_A', 'SIG_B'], 'tolerance': 0.5, 'unit': 'mm',
               'step': 'route'},
           f"requested: SIG_A SIG_B, 0.5 mm, route.py ({row})")
    meas = row.get('measured') or {}
    expect(set(meas) == {'SIG_A', 'SIG_B'}
           and all(abs(meas[x] - cu[x][0]) < 1e-3 for x in meas)
           and abs(row.get('spread', -1) - s) < 1e-3
           and row.get('within_tolerance') is True and row.get('unmeasured') == [],
           f"achieved: the lengths and spread measured here, within tolerance ({row})")
    expect(doc.get('match', {}).get('match_groups') == rows
           and plan.get('match', {}).get('match_groups') == rows,
           "--json-out and plan.json carry the same account")
    expect(plan['analysis'].get('match_groups', [{}])[0].get('nets') == ['SIG_A', 'SIG_B'],
           "the analysis states the requested group")
    assert len(FAILS) == n, FAILS[n:]


def test_pair_run():
    print("a real run: pairs D and E matched by route_diff")
    n = len(FAILS)
    board = pairs_board(os.path.join(TMP, 'pairs.kicad_pcb'))
    nets = ('D_N', 'D_P', 'E_N', 'E_P')
    summary, _, plan = _run(board, os.path.join(TMP, 'pairs_routed.kicad_pcb'),
                            '--length-match-group', 'D_*', 'E_*',
                            '--length-match-tolerance', '1.0')
    cu = copper(os.path.join(TMP, 'pairs_routed.kicad_pcb'))
    diff = [s for s in plan['steps'] if s['tool'] == 'diff']
    expect(len(diff) == 1 and '--length-match-group' in diff[0]['argv'],
           "route_diff.py's call carries the group")
    expect(summary.get('open') == 0 and not any(cu[x][2] for x in nets),
           f"the board routes, no vias ({summary.get('open')} open)")
    s = spread(cu, nets)
    expect(s <= 1.0, f"the four nets within 1.0 mm: {s:.3f} "
                     f"({ {x: round(cu[x][0], 3) for x in nets} })")
    row = (summary.get('match_groups') or [{}])[0]
    expect(row.get('step') == 'route_diff' and abs(row.get('spread', -1) - s) < 1e-3
           and row.get('within_tolerance') is True,
           f"the account: route_diff.py, the spread measured here ({row.get('spread')})")
    _run(board, os.path.join(TMP, 'pairs_plain.kicad_pcb'))
    ps = spread(copper(os.path.join(TMP, 'pairs_plain.kicad_pcb')), nets)
    expect(ps > 10 * s, f"without the flag: {ps:.3f} mm")
    assert len(FAILS) == n, FAILS[n:]


def ipc2141_microstrip(w, h, t, er):
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * h / (0.8 * w + t))


def test_impedance_run():
    print("a real run: SIG_C at 50 ohm on a board with a stackup")
    n = len(FAILS)
    board = singles_board(os.path.join(TMP, 'imp.kicad_pcb'), stackup=True)
    summary, _, plan = _run(board, os.path.join(TMP, 'imp_routed.kicad_pcb'),
                            '--net-impedance', 'SIG_C', '50')
    cu = copper(os.path.join(TMP, 'imp_routed.kicad_pcb'))
    expect([s['tag'] for s in plan['steps'] if s['tool'] == 'route'][:1]
           == ['impedance_50ohm'], "SIG_C's own step runs first")
    rows = [r['tag'] for r in plan['plan']['steps']]
    expect('impedance_50ohm' in rows and 'route' in rows
           and rows.index('impedance_50ohm') < rows.index('route'),
           f"AUTOROUTE_PLAN lists that step, ahead of the route passes ({rows})")
    widths = set(cu['SIG_C'][1])
    z = [ipc2141_microstrip(w, 0.2, 0.035, 4.5) for w in widths]
    expect(summary.get('open') == 0 and z and all(45 <= x <= 55 for x in z),
           f"SIG_C's copper is a 50 ohm microstrip by IPC-2141: widths {sorted(widths)} "
           f"-> {[round(x, 1) for x in z]} ohm")
    expect(set(cu['SIG_A'][1]) == {0.1} and ipc2141_microstrip(0.1, 0.2, 0.035, 4.5) > 70,
           f"SIG_A keeps the track floor ({set(cu['SIG_A'][1])})")
    imp = summary.get('impedance') or {}
    expect(imp == {'SIG_C': {'requested_ohms': 50.0, 'differential': False,
                             'recorded': {'ohms': 50.0, 'differential': False,
                                          'coplanar_gap': 0.0, 'applied': True}}},
           f"the account: the target and the project's record of it ({imp})")
    assert len(FAILS) == n, FAILS[n:]


def test_unrouted_account():
    print("the account of a group not routed: unmeasured, never within tolerance")
    n = len(FAILS)
    board = singles_board(os.path.join(TMP, 'bare.kicad_pcb'))
    a = AP.analyze(board, length_groups=[['SIG_A', 'SIG_B']])
    row = AP.match_account(board, a)['match_groups'][0]
    expect(row['measured'] == {} and row['unmeasured'] == ['SIG_A', 'SIG_B']
           and row['spread'] is None and row['within_tolerance'] is None
           and row['tolerance'] == 0.1,
           f"no copper: unmeasured, no spread, the steps' default tolerance ({row})")
    expect(AP.match_account(board, AP.analyze(board)) is None, "nothing asked: no account")
    assert len(FAILS) == n, FAILS[n:]


def main():
    global TMP
    with tempfile.TemporaryDirectory() as TMP:
        for t in (test_steps, test_refusals, test_mode_conflict, test_length_run,
                  test_pair_run, test_impedance_run, test_unrouted_account):
            before = len(FAILS)
            try:
                t()
            except AssertionError as exc:
                if len(FAILS) == before:     # a check() refusal, not an expect
                    print(f"  FAIL {t.__name__}: {exc}")
                    FAILS.append(f"{t.__name__}: {exc}")
            except Exception:                                        # noqa: BLE001
                traceback.print_exc()
                FAILS.append(f"{t.__name__} raised")
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: autoroute matching, impedance and pair options")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
