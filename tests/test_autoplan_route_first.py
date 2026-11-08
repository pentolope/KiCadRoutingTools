#!/usr/bin/env python3
"""autoplan --route-first (and route.py's own): the caller's nets route first
in every route pass, in the order given, every other net keeps route.py's mps
order, and the run says whether route.py did so.

1. Steps: for one analysis exercising every step, each route pass of a full
   chain and of --finish is given route.py's --route-first with a pattern per
   declared net, which route.py's own resolve_route_first reads back in the
   declared order; its --nets and every other argument, and every other step
   (the diff-pair reroute included), are the chain autoplan builds without
   the flag.
2. route_first_record is route.py's own JSON_SUMMARY record: the outermost
   run's, though a reconciliation sub-run's summary comes after it, and not
   the progress lines (a declared net routed only in a sub-run does not
   count); None when no summary carries one.
3. Analysis: the nets in pattern order, a pattern's own nets by name, each
   once; a pattern that selects no net, an exclusion and a net outside --nets
   are refused for their reason (exit 2) before any step runs; route.py
   refuses the same three for the same reasons. A wildcard keeps only the
   routed nets it selects, and is refused only when it selects none of them.
   Without --nets the scope is route.py's '*', so a single-pad unconnected-*
   net is refused before any step, as route.py refuses it.
4. A real run on a synthetic 2-layer board of four two-pad nets: with
   --route-first SIG_D SIG_B the route step's command line and route.py's own
   log put SIG_D, then SIG_B, ahead of SIG_A and SIG_C, and the analysis, the
   pass's record, the --json-out document and AUTOPLAN_SUMMARY say so. The
   same board without the flag routes SIG_A first, so the order came from it.
5. The rest keep mps order: on a board where one net (A_X) crosses three
   parallel rows, route.py's mps order routes the rows first and A_X last,
   unlike name order. With --route-first C_ROW, route.py (and autoplan's
   pass) route C_ROW first and then every other net in the same relative
   order as with no flag at all (B_ROW, D_ROW, A_X), not in name order.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
import autoplan as AP                                        # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

RUN_ALL_TIMEOUT = 400
SCRIPT = os.path.join(ROOT, 'py_router', 'autoplan.py')
ROUTE = os.path.join(ROOT, 'py_router', 'route.py')
NETS = ['SIG_A', 'SIG_B', 'SIG_C', 'SIG_D']
FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def synthetic_board(path, lone=None):
    """Four two-pad nets across a 40 x 30 mm two-layer board, one row each;
    LONE names a one-pad net too."""
    b = RoutingBoard(40, 30)
    for i, net in enumerate(NETS):
        b.pad(f'P{2 * i + 1}', 5, 5 + 5 * i, net)
        b.pad(f'P{2 * i + 2}', 35, 5 + 5 * i, net)
    if lone:
        b.pad('J1', 20, 27, lone)
    return write_board(b.text(), path)


def crossing_board(path):
    """Three parallel rows (B_ROW, C_ROW, D_ROW) and A_X, which runs corner
    to corner across all three: mps routes the rows before A_X, whose name
    sorts first."""
    b = RoutingBoard(40, 40)
    b.pad('X1', 5, 5, 'A_X')
    b.pad('X2', 35, 35, 'A_X')
    for i, net in enumerate(('B_ROW', 'C_ROW', 'D_ROW')):
        b.pad(f'R{2 * i + 1}', 5, 12 + 8 * i, net)
        b.pad(f'R{2 * i + 2}', 35, 12 + 8 * i, net)
    return write_board(b.text(), path)


class Recorder:
    """Chain.run recorded, the rest scripted: the diff step leaves D_N in a
    new DRC violation (rerouted) and the passes grade 5, 3 and 1 open nets
    (all three run). The logs it names do not exist."""

    def __init__(self, work, input_grade=None):
        self.work = work
        self.cur = 'IN'
        self.runs = []
        self.steps = []
        self.grades = [G(5), G(3), G(1)]
        self.input_grade = input_grade
        self.last_iterations = None

    def run(self, kind, args, tag):
        self.runs.append((kind, tag, list(args)))
        self.steps.append({'tag': tag, 'ok': True, 'log': tag + '.log'})
        self.last_iterations = 50
        s = {'escaped': 1, 'failed': 0} if kind in ('bga', 'qfn') else {}
        return f'OUT{len(self.runs)}', s, {}

    def accept(self, out):
        if out:
            self.cur = out

    def grade(self, board):
        return self.input_grade if board == 'IN' else self.grades.pop(0)

    def drc_nets(self, board):
        diff = [i for i, r in enumerate(self.runs) if r[0] == 'diff']
        return {'D_N'} if diff and board == f'OUT{diff[0] + 1}' else set()

    def check_drc(self, board):
        return {'violations': 0, 'items': []}


def G(n):
    return {'open': n, 'drc': 0, 'open_nets': []}


A = dict(layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'], clearance=0.09,
         track=0.1, via=0.45, drill=0.25, fine_via=0.25, fine_drill=0.15,
         planes=[('GND', 'In1.Cu'), ('+3V3', 'In2.Cu')], plane_nets=['GND', '+3V3'],
         power_nets=['GND', '+3V3'], layer_costs=[1.0, -1.0, -1.0, 1.0],
         grid=0.05, congested=True, n_nets=200, pair_nets=['D_P', 'D_N'],
         globs={'GND': 'GN[D/]', '+3V3': '+3V[3/]', 'D_P': 'D_[P/]', 'D_N': 'D_[N/]'},
         parts=[('U1', 'qfn', 0.4)])


def chain_runs(a, work, finish=False):
    ch = Recorder(work, input_grade=G(7) if finish else None)
    orig = AP.Chain
    AP.Chain = lambda *args, **k: ch
    try:
        AP.run_plan('IN', 'OUT', a, work, finish=finish, effort_factor=4.0)
    finally:
        AP.Chain = orig
    return ch.runs


def _values(args, flag):
    """The values after FLAG in ARGS, up to the next option."""
    if flag not in args:
        return []
    out = []
    for x in args[args.index(flag) + 1:]:
        if x.startswith('--'):
            break
        out.append(x)
    return out


def _without(args, flag, n):
    """ARGS with FLAG and its next N values removed."""
    if flag not in args:
        return list(args)
    i = args.index(flag)
    return args[:i] + args[i + 1 + n:]


def test_steps():
    from net_ordering import resolve_route_first
    print("--route-first: the route passes put the declared nets first, nothing else changes")
    n = len(FAILS)
    stub = parse_kicad_pcb(synthetic_board(os.path.join(TMP, 'stub.kicad_pcb')))
    order = ['SIG_D', 'SIG_B']
    for finish in (False, True):
        plain = chain_runs(dict(A), TMP, finish)
        ordered = chain_runs(dict(A, route_first=order), TMP, finish)
        label = '--finish' if finish else 'full chain'
        expect([(k, t) for k, t, _ in plain] == [(k, t) for k, t, _ in ordered],
               f"{label}: the same steps run ({[t for _, t, _ in ordered]})")
        passes = [t for k, t, _ in ordered if k == 'route' and AP._PASS_TAG.fullmatch(t)]
        expect(len(passes) == 3, f"{label}: three route passes ({passes})")
        for (k, t, base), (_, _, args) in zip(plain, ordered):
            if t not in passes:
                expect(args == base, f"{label} {t}: unchanged")
                continue
            read = resolve_route_first(stub, _values(args, '--route-first'))
            expect(read == order,
                   f"{label} {t}: route.py's --route-first reading is SIG_D, SIG_B ({read})")
            expect(_without(args, '--route-first', 2) == base,
                   f"{label} {t}: --nets, the ordering and every other argument are "
                   f"the default pass's")
    assert len(FAILS) == n, FAILS[n:]


def _summary_line(**kw):
    return 'JSON_SUMMARY: ' + json.dumps(dict({'successful': 1, 'failed': 0}, **kw))


def test_record():
    print("route_first_record: route.py's own record, not its progress lines")
    n = len(FAILS)
    rec = {'nets': ['/D'], 'main_loop': ['/D'], 'phase3': [], 'honoured': True}
    # The outermost run's summary carries the record; a reconciliation
    # sub-run then prints its own progress lines and a summary without one,
    # routing an undeclared net ahead of nothing and the declared /D again.
    log = '\n'.join([
        '[1/2] Routing /D (id=4)', '[2/2] Routing /A (id=1)',
        _summary_line(route_first=rec),
        'Final reconciliation: retrying 1 incomplete/custody net(s)',
        '[1/1] Routing /A (id=1)', _summary_line(scope='reconciliation-subset')])
    path = os.path.join(TMP, 'route.log')
    for text, want, what in (
            (log, rec, "the outermost summary's record, though the last "
                       "summary is the sub-run's"),
            # Progress lines that would read as another net first: route.py
            # recorded its own loop, and that is what counts.
            ('[1/1] Routing /A (id=1)\n' + _summary_line(route_first=rec)
             + '\n[1/1] Routing /D (id=4)\n', rec,
             "a declared net routed only in a sub-run's lines does not count"),
            (_summary_line() + '\n', None, "a summary with no record: none"),
            ('All nets are already fully connected\n', None, "no summary: none")):
        with open(path, 'w') as fh:
            fh.write(text)
        got = AP.route_first_record(path)
        expect(got == want, f"{what} ({got})")
    expect(AP.route_first_record(os.path.join(TMP, 'missing.log')) is None,
           "no log: none")
    assert len(FAILS) == n, FAILS[n:]


def _analysis(board, *argv):
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, os.devnull + '.kicad_pcb',
               '--dry-run', *argv], accept=True)
    m = re.search(r'^AUTOPLAN_ANALYSIS: (\{.*\})$', r.stdout, re.M)
    return json.loads(m.group(1)) if m else {}


def test_analysis():
    print("--route-first: the analysis, and the refusals")
    n = len(FAILS)
    board = synthetic_board(os.path.join(TMP, 'board.kicad_pcb'))
    a = _analysis(board, '--route-first', 'SIG_D', 'SIG_[CA]', 'SIG_D')
    expect(a.get('route_first') == ['SIG_D', 'SIG_A', 'SIG_C']
           and a.get('route_first_patterns') == ['SIG_D', 'SIG_[CA]', 'SIG_D'],
           f"the nets in pattern order, a pattern's own by name, each once "
           f"({a.get('route_first')})")
    expect('route_first' not in _analysis(board), "without the flag the analysis has no key")
    out = os.path.join(TMP, 'refused.kicad_pcb')
    for argv, why in (
            (('--route-first', 'SIG_A', 'NOPE'), '--route-first: NOPE selects no net'),
            (('--route-first', '!SIG_A'), '--route-first: !SIG_A is an exclusion'),
            (('--nets', 'SIG_A', 'SIG_B', '--route-first', 'SIG_C'),
             '--route-first: SIG_C not among the nets this run routes')):
        check([sys.executable, '-X', 'utf8', SCRIPT, board, out, *argv],
              refuse=f'AUTOPLAN: refused: {why}', code=2)
        expect(not os.path.exists(out + '.autoplan') and not os.path.exists(out),
               f"{why}: refused before any step runs")
        check([sys.executable, '-X', 'utf8', ROUTE, board, out, *argv],
              refuse=f'route.py: error: {why}', code=2)
        expect(not os.path.exists(out), f"route.py: {why}: refused, no board written")

    print("a wildcard counts only the nets the run routes; a name outside refuses")
    a = _analysis(board, '--nets', 'SIG_A', 'SIG_B', '--route-first', 'SIG_*')
    expect(a.get('route_first') == ['SIG_A', 'SIG_B'],
           f"--nets SIG_A SIG_B --route-first 'SIG_*': SIG_A, SIG_B; SIG_C and SIG_D "
           f"are outside and dropped ({a.get('route_first')})")
    why = "--route-first: SIG_[CD] selects none of the nets this run routes"
    for tool, prefix in ((SCRIPT, 'AUTOPLAN: refused: '), (ROUTE, 'route.py: error: ')):
        check([sys.executable, '-X', 'utf8', tool, board, out, '--nets', 'SIG_A', 'SIG_B',
               '--route-first', 'SIG_[CD]'], refuse=prefix + why, code=2)
        expect(not os.path.exists(out), f"{os.path.basename(tool)}: {why}")

    print("without --nets the scope is route.py's '*', which leaves out a "
          "single-pad unconnected-* net")
    lone = synthetic_board(os.path.join(TMP, 'lone.kicad_pcb'), lone='unconnected-(J1-Pad9)')
    why = "--route-first: unconnected-(J1-Pad9) not among the nets this run routes"
    check([sys.executable, '-X', 'utf8', SCRIPT, lone, out,
           '--route-first', 'unconnected-(J1-Pad9)'],
          refuse='AUTOPLAN: refused: ' + why, code=2)
    expect(not os.path.exists(out + '.autoplan') and not os.path.exists(out),
           f"{why}: refused before any step runs, as route.py refuses it")
    check([sys.executable, '-X', 'utf8', ROUTE, lone, out,
           '--route-first', 'unconnected-(J1-Pad9)'],
          refuse='route.py: error: ' + why, code=2)
    expect(True, "route.py refuses it for the same reason")
    assert len(FAILS) == n, FAILS[n:]


#: route.py's record of --route-first SIG_D SIG_B on the four-net board.
RF_DB = {'nets': ['SIG_D', 'SIG_B'], 'main_loop': ['SIG_D', 'SIG_B'], 'phase3': [],
         'honoured': True}


def _routing_order(log):
    """The nets route.py's log says it started routing, in that order."""
    return [line.split('] Routing ', 1)[1].rsplit(' (id=', 1)[0]
            for line in open(log, errors='replace') if '] Routing ' in line]


def test_run():
    from net_ordering import resolve_route_first
    print("a real run: four nets, --route-first SIG_D SIG_B")
    n = len(FAILS)
    board = synthetic_board(os.path.join(TMP, 'board.kicad_pcb'))
    out = os.path.join(TMP, 'routed.kicad_pcb')
    js = os.path.join(TMP, 'autoplan.json')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, out,
               '--route-first', 'SIG_D', 'SIG_B', '--json-out', js], accept=True)
    plan = json.load(open(evidence(out + '.autoplan/plan.json', 'plan.json')))
    passes = [s for s in plan['steps'] if s['tool'] == 'route']
    expect(passes and not any('--ordering' in s['argv'] for s in passes),
           f"no route pass is given an --ordering ({len(passes)} pass(es))")
    pcb = parse_kicad_pcb(evidence(board, 'input board'))
    for s in passes:
        read = resolve_route_first(pcb, _values(s['argv'], '--route-first'))
        expect(read == ['SIG_D', 'SIG_B'],
               f"{s['tag']}: route.py's --route-first reading is SIG_D, SIG_B ({read})")
        seq = _routing_order(evidence(os.path.join(out + '.autoplan', s['log']), 'route log'))
        expect(seq[:2] == ['SIG_D', 'SIG_B'] and sorted(seq) == NETS,
               f"{s['tag']}: route.py routed SIG_D, then SIG_B, then the rest ({seq})")
        expect(s.get('route_first') == RF_DB,
               f"{s['tag']}: the pass records it honoured ({s.get('route_first')})")
    expect(plan['analysis'].get('route_first') == ['SIG_D', 'SIG_B'],
           f"plan.json's analysis states the order ({plan['analysis'].get('route_first')})")
    doc = json.load(open(evidence(js, '--json-out document')))
    shipped = next((s for s in doc['steps'] if s['tag'] == doc['shipped_step']), {})
    expect(doc['analysis'].get('route_first') == ['SIG_D', 'SIG_B']
           and shipped.get('route_first', {}).get('honoured') is True,
           f"the --json-out document states the order and the shipped pass's record "
           f"({doc['shipped_step']}: {shipped.get('route_first')})")
    m = re.search(r'^AUTOPLAN_SUMMARY: (\{.*\})$', r.stdout, re.M)
    summary = json.loads(m.group(1)) if m else {}
    expect(summary.get('route_first') == {
        'nets': ['SIG_D', 'SIG_B'],
        'shipped': RF_DB},
        f"AUTOPLAN_SUMMARY states the order and that it was honoured "
        f"({summary.get('route_first')})")
    expect(summary.get('open') == 0, f"the board routes ({summary.get('open')} open)")

    print("the same board without --route-first")
    plain = os.path.join(TMP, 'plain.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, plain], accept=True)
    plan = json.load(open(evidence(plain + '.autoplan/plan.json', 'plan.json')))
    first = next(s for s in plan['steps'] if s['tool'] == 'route')
    seq = _routing_order(evidence(os.path.join(plain + '.autoplan', first['log']), 'route log'))
    expect(seq[:1] == ['SIG_A'] and '--ordering' not in first['argv']
           and 'route_first' not in first,
           f"route.py routes SIG_A first, no --ordering is passed, no record ({seq})")
    m = re.search(r'^AUTOPLAN_SUMMARY: (\{.*\})$', r.stdout, re.M)
    expect(m and 'route_first' not in json.loads(m.group(1)),
           "and AUTOPLAN_SUMMARY has no route_first")
    assert len(FAILS) == n, FAILS[n:]


def test_rest_keep_mps():
    print("the undeclared nets keep route.py's mps order")
    n = len(FAILS)
    board = crossing_board(os.path.join(TMP, 'crossing.kicad_pcb'))
    names = sorted(['A_X', 'B_ROW', 'C_ROW', 'D_ROW'])

    def routed(*argv, tag):
        out = os.path.join(TMP, f'{tag}.kicad_pcb')
        r = check([sys.executable, '-X', 'utf8', ROUTE, board, out, *argv], accept=True)
        log = os.path.join(TMP, f'{tag}.log')
        with open(log, 'w') as fh:
            fh.write(r.stdout)
        return _routing_order(log)

    plain = routed(tag='mps')
    expect(sorted(plain) == names and plain != names,
           f"route.py's own order on this board is not name order ({plain})")
    expect(plain and plain[-1] == 'A_X', f"mps routes A_X, the crossing net, last ({plain})")
    out = os.path.join(TMP, 'crossing_routed.kicad_pcb')
    check([sys.executable, '-X', 'utf8', SCRIPT, board, out, '--route-first', 'C_ROW'],
          accept=True)
    plan = json.load(open(evidence(out + '.autoplan/plan.json', 'plan.json')))
    p1 = next(s for s in plan['steps'] if s['tool'] == 'route')
    seq = _routing_order(evidence(os.path.join(out + '.autoplan', p1['log']), 'route log'))
    want = ['C_ROW'] + [x for x in plain if x != 'C_ROW']
    expect(seq == want, f"autoplan's first pass: C_ROW, then the rest in route.py's own "
                        f"order ({seq}, want {want})")
    expect(p1.get('route_first') == {'nets': ['C_ROW'], 'main_loop': ['C_ROW'],
                                     'phase3': [], 'honoured': True},
           f"and the pass records it honoured ({p1.get('route_first')})")
    first = routed('--route-first', 'C_ROW', tag='first')
    want = ['C_ROW'] + [x for x in plain if x != 'C_ROW']
    expect(first == want, f"route.py --route-first C_ROW: C_ROW, then the rest in their "
                          f"order without the flag ({first}, want {want})")
    both = routed('--route-first', 'D_ROW', 'A_X', tag='both')
    want = ['D_ROW', 'A_X'] + [x for x in plain if x not in ('D_ROW', 'A_X')]
    expect(both == want, f"--route-first D_ROW A_X: those two, then the rest as before "
                         f"({both}, want {want})")
    assert len(FAILS) == n, FAILS[n:]


def main():
    global TMP
    with tempfile.TemporaryDirectory() as TMP:
        for t in (test_steps, test_record, test_analysis, test_run, test_rest_keep_mps):
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
    print("PASS: autoplan --route-first")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
