#!/usr/bin/env python3
"""autoplan --route-first: the caller's nets route first in every route pass,
in the order given, and the run says whether route.py did so.

1. Steps: for one analysis exercising every step, each route pass of a full
   chain and of --finish is given the declared nets at the head of its
   --nets, which route.py's own expand_net_patterns reads back in the
   declared order ahead of every other net, with --ordering original; every
   other step, the diff-pair reroute included, is the chain autoplan builds
   without the flag.
2. route_first_record against route.py's progress lines as single_ended_loop
   prints them: the declared nets first and in order is honoured; another net
   ahead of them, or the declared two swapped, is not; a declared net already
   connected (absent from the log) does not count against it; a log naming no
   routed net is unknown (None).
3. Analysis: the nets in pattern order, a pattern's own nets by name, each
   once; a pattern that selects no net, an exclusion and a net outside --nets
   are refused for their reason (exit 2) before any step runs.
4. A real run on a synthetic 2-layer board of four two-pad nets: with
   --route-first SIG_D SIG_B the route step's command line and route.py's own
   log put SIG_D, then SIG_B, ahead of SIG_A and SIG_C, and the analysis, the
   pass's record, the --json-out document and AUTOPLAN_SUMMARY say so. The
   same board without the flag routes SIG_A first, so the order came from it.
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
from net_queries import expand_net_patterns                  # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

RUN_ALL_TIMEOUT = 300
SCRIPT = os.path.join(ROOT, 'py_router', 'autoplan.py')
NETS = ['SIG_A', 'SIG_B', 'SIG_C', 'SIG_D']
FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def synthetic_board(path):
    """Four two-pad nets across a 40 x 30 mm two-layer board, one row each."""
    b = RoutingBoard(40, 30)
    for i, net in enumerate(NETS):
        b.pad(f'P{2 * i + 1}', 5, 5 + 5 * i, net)
        b.pad(f'P{2 * i + 2}', 35, 5 + 5 * i, net)
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
            nets = _values(args, '--nets')
            read = expand_net_patterns(stub, nets, quiet=True)
            expect(read == ['SIG_D', 'SIG_B', 'SIG_A', 'SIG_C'],
                   f"{label} {t}: route.py's --nets reading routes SIG_D, SIG_B, then "
                   f"the rest ({read})")
            expect(_values(args, '--ordering') == ['original'],
                   f"{label} {t}: --ordering original ({_values(args, '--ordering')})")
            rest = _without(args, '--ordering', 1)
            rest = rest[:rest.index('--nets') + 1] + rest[rest.index('--nets') + 3:]
            expect(rest == base, f"{label} {t}: every other argument is the default pass's")
    assert len(FAILS) == n, FAILS[n:]


def _log(*nets, extra=''):
    """A route log with single_ended_loop's progress line for each of NETS."""
    lines = [f"[{i}/{len(nets)}{extra if i > 1 else ''}] Routing {nm} (id={i + 7})"
             for i, nm in enumerate(nets, 1)]
    return 'Using original net order (no sorting)\n\n' + '\n\n'.join(lines) + '\n'


def test_record():
    print("route_first_record: what route.py's log says of the declared nets")
    n = len(FAILS)
    order = ['/B', 'Net-(U1-PA0)']
    for text, want, what in (
            (_log('/B', 'Net-(U1-PA0)', '/A', extra=' (1 failed) (2 ripped)'),
             {'routed': ['/B', 'Net-(U1-PA0)'], 'honoured': True},
             "first and in order"),
            (_log('/A', '/B', 'Net-(U1-PA0)'),
             {'routed': ['/B', 'Net-(U1-PA0)'], 'honoured': False},
             "another net routed ahead of them"),
            (_log('Net-(U1-PA0)', '/B', '/A'),
             {'routed': ['Net-(U1-PA0)', '/B'], 'honoured': False},
             "the declared two swapped"),
            (_log('/B', '/A', 'Net-(U1-PA0)'),
             {'routed': ['/B', 'Net-(U1-PA0)'], 'honoured': False},
             "in order, but another net between them"),
            (_log('Net-(U1-PA0)', '/A'),
             {'routed': ['Net-(U1-PA0)'], 'honoured': True},
             "/B already connected, so not routed"),
            ('All nets already connected\n', {'routed': [], 'honoured': None},
             "no net routed")):
        path = os.path.join(TMP, 'route.log')
        with open(path, 'w') as fh:
            fh.write(text)
        got = AP.route_first_record(path, order)
        expect(got == want, f"{what}: {want} ({got})")
    got = AP.route_first_record(os.path.join(TMP, 'missing.log'), order)
    expect(got == {'routed': [], 'honoured': None}, f"no log: unknown ({got})")
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
             '--route-first: SIG_C not among the nets --nets selects')):
        check([sys.executable, '-X', 'utf8', SCRIPT, board, out, *argv],
              refuse=f'AUTOPLAN: refused: {why}', code=2)
        expect(not os.path.exists(out + '.autoplan') and not os.path.exists(out),
               f"{why}: refused before any step runs")
    assert len(FAILS) == n, FAILS[n:]


def _routing_order(log):
    """The nets route.py's log says it started routing, in that order."""
    return [line.split('] Routing ', 1)[1].rsplit(' (id=', 1)[0]
            for line in open(log, errors='replace') if '] Routing ' in line]


def test_run():
    print("a real run: four nets, --route-first SIG_D SIG_B")
    n = len(FAILS)
    board = synthetic_board(os.path.join(TMP, 'board.kicad_pcb'))
    out = os.path.join(TMP, 'routed.kicad_pcb')
    js = os.path.join(TMP, 'autoplan.json')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, board, out,
               '--route-first', 'SIG_D', 'SIG_B', '--json-out', js], accept=True)
    plan = json.load(open(evidence(out + '.autoplan/plan.json', 'plan.json')))
    passes = [s for s in plan['steps'] if s['tool'] == 'route']
    expect(passes and all(_values(s['argv'], '--ordering') == ['original'] for s in passes),
           f"every route pass ran --ordering original ({len(passes)} pass(es))")
    pcb = parse_kicad_pcb(evidence(board, 'input board'))
    for s in passes:
        read = expand_net_patterns(pcb, _values(s['argv'], '--nets'), quiet=True)
        expect(read[:2] == ['SIG_D', 'SIG_B'],
               f"{s['tag']}: route.py's --nets reading starts SIG_D, SIG_B ({read})")
        seq = _routing_order(evidence(os.path.join(out + '.autoplan', s['log']), 'route log'))
        expect(seq[:2] == ['SIG_D', 'SIG_B'] and sorted(seq) == NETS,
               f"{s['tag']}: route.py routed SIG_D, then SIG_B, then the rest ({seq})")
        expect(s.get('route_first') == {'routed': ['SIG_D', 'SIG_B'], 'honoured': True},
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
        'shipped': {'routed': ['SIG_D', 'SIG_B'], 'honoured': True}},
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


def main():
    global TMP
    with tempfile.TemporaryDirectory() as TMP:
        for t in (test_steps, test_record, test_analysis, test_run):
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
