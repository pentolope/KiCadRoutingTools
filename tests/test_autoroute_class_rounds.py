#!/usr/bin/env python3
"""autoroute's net class calls in the route rounds, on a scripted chain.

1. Which nets get a class call: a class net a pattern selects alone, never a
   plane net. On a board whose GND, poured on B.Cu, is in a 0.2 class, GND
   gets no class call and every route pass routes it (route.py's own
   selector), so the pass's plane finalize serves the pour; the record names
   it among the nets the passes route at their class.
2. Rounds: a class with a net open on the best board is called again before
   each later pass, at that pass's costs, and once more after the last pass,
   graded and shipped when it improves; a class with no open net is not.
3. The effort budget counts the class calls' iterations: the reason a pass
   is not started names them in what was spent.
4. With every net of the run in a class call, no route pass runs (route.py
   refuses an empty --nets), and each round's last class call is graded and
   ships.
5. --finish calls only a class with a net open on the input.
6. No impedance, pair reroute or pass call is given rip authority over a
   class net; the class call is, over its own.
7. The plan names each --route-first net on the call that routes it, the
   class call records route.py's route-first record, and rules_why says the
   route steps cap every class when they do.
8. A length group of a class net and a Default net is refused, naming
   --cap-net-classes.
9. With every net in a class call and the last class call of a round
   failing, the board an earlier class call laid is graded and ships.
10. A pass that reports no router iterations leaves nothing to budget a
   later call from: no class call follows the last pass, and the class
   calls' own iterations still count.
11. The record gives the clearance route.py holds the passes at for the
   class nets they route.
"""
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
import autoroute as AP                                        # noqa: E402
import test_autoroute_net_classes as NC                      # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from net_queries import expand_net_patterns                  # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


A = dict(layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'], clearance=0.15, track=0.15,
         via=0.5, drill=0.25, fine_via=0.45, fine_drill=0.2,
         planes=[('GND', 'In1.Cu')], plane_nets=['GND'], power_nets=['GND'],
         layer_costs=[1.0, -1.0, 1.0, 1.0], grid=0.05, congested=False, n_nets=10,
         pair_nets=['ETH_P', 'ETH_N'],
         globs={'GND': 'GN[D/]', 'ETH_P': 'ETH_[P/]', 'ETH_N': 'ETH_[N/]',
                'CLK': 'CL[K/]', 'SIG': 'SI[G/]'},
         parts=[], route_first=['CLK', 'SIG'],
         class_clearances={'CLK': 0.25, 'ETH_N': 0.2, 'ETH_P': 0.2, 'GND': 0.2})


class Recorder(AP.ChainEvents):
    """Chain.run recorded; a step's iterations are ITERS[its kind of call],
    a board's grade GRADES[the tag of the step that laid it] (open nets),
    every other board fully routed."""

    def __init__(self, work, grades=(), iters=None, input_open=None, fail=()):
        self.work = work
        self.cur = 'IN'
        self.runs = []
        self.steps = []
        self.boards = {}
        self.grades = dict(grades)
        self.iters = iters or {'class': 100, 'pass': 10}
        self.input_open = input_open
        self.fail = set(fail)
        self.last_iterations = None

    def run(self, kind, args, tag, **kw):
        self.runs.append((kind, tag, list(args)))
        self.steps.append({'tag': tag, 'tool': kind, 'ok': tag not in self.fail,
                           'log': tag + '.log'})
        if tag in self.fail:
            self.last_iterations = None
            return None, {}, {}
        out = f'OUT{len(self.runs)}'
        self.boards[out] = tag
        self.last_iterations = self.iters['class' if tag.startswith('route_class_')
                                          else 'pass']
        return out, {}, {}

    def accept(self, out):
        if out:
            self.cur = out

    def grade(self, board):
        nets = (self.input_open if board == 'IN'
                else self.grades.get(self.boards.get(board), []))
        return {'open': len(nets), 'open_nets': list(nets), 'drc': 0}

    def drc_nets(self, board):
        return set()

    def check_drc(self, board):
        return {'violations': 0, 'items': []}


def _values(args, flag):
    return NC._values(args, flag)


def tags(ch):
    return [t for _, t, _ in ch.runs]


def test_which_nets(tmp):
    print("class calls: selectable non-plane class nets; a classed pour stays in the passes")
    n = len(FAILS)
    expect(AP.class_tiers(A) == [('route_class_0.25', 0.25, ['CLK']),
                                 ('route_class_0.2', 0.2, ['ETH_N', 'ETH_P'])],
           f"CLK, then ETH_N and ETH_P; GND is a plane ({AP.class_tiers(A)})")
    rec = AP.net_class_record(A)
    expect(rec['in_passes'] == ['GND'], f"the record names GND in the passes ({rec})")

    b = RoutingBoard(30, 20)
    for i in range(4):
        b.pad(f'G{i}', 4 + 6 * i, 4, 'GND')
    b.pad('S1', 4, 14, 'SIG')
    b.pad('S2', 26, 14, 'SIG')
    path = os.path.join(tmp, 'pour.kicad_pcb')
    write_board(b.text(), path)
    import json
    default = {'name': 'Default', 'clearance': 0.15, 'track_width': 0.15,
               'via_diameter': 0.6, 'via_drill': 0.3}
    with open(path[:-len('.kicad_pcb')] + '.kicad_pro', 'w') as fh:
        json.dump({'board': {'design_settings': {'rules': {'min_clearance': 0.15}}},
                   'meta': {'filename': 'pour.kicad_pro', 'version': 1},
                   'net_settings': {'classes': [default, dict(default, name='Power',
                                                              clearance=0.2)],
                                    'meta': {'version': 3},
                                    'netclass_patterns': [{'netclass': 'Power',
                                                           'pattern': 'GND'}]}}, fh)
    fab = os.path.join(tmp, 'fab.txt')
    with open(fab, 'w') as fh:
        fh.write('clearance = 0.15\ntrack_width = 0.15\n')
    a = AP.analyze(path, fab_args=['--fab-overrides', fab])
    expect(a['plane_nets'] == ['GND'] and a.get('class_clearances') == {'GND': 0.2},
           f"GND is the pour, in its 0.2 class ({a['plane_nets']}, {a.get('class_clearances')})")
    expect(AP.class_tiers(a) == [], f"GND gets no class call ({AP.class_tiers(a)})")
    ch = Recorder(tmp)
    orig = AP.Chain
    AP.Chain = lambda *args, **kw: ch
    try:
        AP.run_plan(path, 'out', a, tmp)
    finally:
        AP.Chain = orig
    pcb = parse_kicad_pcb(path)
    passes = [args for kind, tag, args in ch.runs if kind == 'route']
    expect(passes and all('GND' in expand_net_patterns(pcb, _values(x, '--nets'), quiet=True)
                          for x in passes),
           f"every route step routes GND ({len(passes)} step(s))")
    expect(all('--clearance-ceiling' not in x for _, _, x in ch.runs),
           "and the class is honoured: no step is given a ceiling")
    assert len(FAILS) == n, FAILS[n:]


def test_rounds(tmp):
    print("rounds: an open class is called again before each later pass and after the last")
    n = len(FAILS)
    grades = {'route': ['CLK', 'SIG', 'X'], 'route_p2': ['CLK', 'SIG'],
              'route_p3': ['CLK'], 'route_class_0.25_final': []}
    ch = Recorder(tmp, grades)
    ch2, final, g = AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'],
                                    effort_factor=10)
    want = ['route_class_0.25', 'route_class_0.2', 'route', 'route_class_0.25_p2',
            'route_p2', 'route_class_0.25_p3', 'route_p3', 'route_class_0.25_final']
    expect(tags(ch) == want, f"the calls, in order ({tags(ch)})")
    later = [args for _, t, args in ch.runs if t.startswith('route_class_0.25_')]
    expect(all(_values(x, '--layer-costs') == ['1.0', '3.0', '1.0', '1.0'] for x in later),
           "a later class call takes the later passes' costs")
    expect(final == f'OUT{len(want)}' and g and g['open'] == 0,
           f"the call after the last pass is graded and ships ({final}, {g})")

    ch = Recorder(tmp, {'route': ['SIG'], 'route_p2': []})
    AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'], effort_factor=10)
    expect(tags(ch) == ['route_class_0.25', 'route_class_0.2', 'route', 'route_p2'],
           f"no class call once its nets are routed ({tags(ch)})")
    assert len(FAILS) == n, FAILS[n:]


def test_budget(tmp):
    print("the effort budget counts the class calls")
    n = len(FAILS)
    ch = Recorder(tmp, {'route': ['CLK', 'SIG']})
    AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'], effort_factor=1.0)
    why = ch.skipped.get('route_p2', '')
    expect('210 router iterations spent' in why and 'took 210' in why,
           f"two class calls of 100 and a pass of 10 are the round's 210 ({why!r})")
    expect(tags(ch) == ['route_class_0.25', 'route_class_0.2', 'route'],
           f"and no class call after the passes past the budget ({tags(ch)})")
    assert len(FAILS) == n, FAILS[n:]


def test_empty_pass(tmp):
    print("every net in a class call: no route pass, each class round graded")
    n = len(FAILS)
    a = dict(A, plane_nets=[], planes=[], power_nets=[], pair_nets=[],
             class_clearances={'CLK': 0.25, 'ETH_N': 0.2, 'ETH_P': 0.2},
             route_first=[], tiers_cover_scope=True)
    ch = Recorder(tmp, {'route_class_0.2': ['CLK'], 'route_class_0.25_p2': []})
    _, final, g = AP.route_passes(ch, a, a['layers'], ['1.0', '-1.0', '1.0', '1.0'],
                                  effort_factor=10)
    expect(tags(ch) == ['route_class_0.25', 'route_class_0.2', 'route_class_0.25_p2'],
           f"class calls only ({tags(ch)})")
    expect(final == 'OUT3' and g and g['open'] == 0,
           f"the last class call is graded and ships ({final}, {g})")
    expect(all('every net this run routes' in ch.skipped.get(t, '')
               for t in ('route', 'route_p2', 'route_p3')),
           f"the planned passes say why they did not run ({ch.skipped})")
    assert len(FAILS) == n, FAILS[n:]


def test_finish(tmp):
    print("--finish: only a class with a net open on the input")
    n = len(FAILS)
    ch = Recorder(tmp, input_open=['SIG'])
    AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'], finish=True)
    expect(tags(ch) == ['route'], f"no class call ({tags(ch)})")
    ch = Recorder(tmp, input_open=['ETH_N', 'SIG'])
    AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'], finish=True)
    expect(tags(ch) == ['route_class_0.2', 'route'], f"the 0.2 class only ({tags(ch)})")
    assert len(FAILS) == n, FAILS[n:]


def test_rip(tmp):
    print("rip authority over a class net only in its class call")
    n = len(FAILS)
    a = dict(A, pair_nets=['ETH_P', 'ETH_N', 'USB_P', 'USB_N'],
             globs=dict(A['globs'], USB_P='USB_[P/]', USB_N='USB_[N/]'),
             net_impedance={'SIG': {'ohms': 50.0, 'differential': False}})
    rip = ['ETH_[P/]', 'ETH_[N/]', 'USB_[P/]', 'USB_[N/]']
    ch = Recorder(tmp)
    AP.route_passes(ch, a, a['layers'], ['1.0', '-1.0', '1.0', '1.0'], rip=rip,
                    reroute=['ETH_P', 'USB_P'])
    got = {t: _values(x, '--rip-existing-nets') for _, t, x in ch.runs}
    for t in ('impedance_50ohm', 'reroute_pairs', 'route'):
        expect(got.get(t) == ['USB_[P/]', 'USB_[N/]'],
               f"{t}: the USB pair only ({got.get(t)})")
    expect(got.get('route_class_0.2') == ['ETH_[P/]', 'ETH_[N/]'],
           f"route_class_0.2: its own ETH pair ({got.get('route_class_0.2')})")
    assert len(FAILS) == n, FAILS[n:]


def test_plan_text(tmp):
    print("the plan's route-first and rules text")
    n = len(FAILS)
    plan = AP.plan_steps(A)
    rows = {r['tag']: r for r in plan['steps']}
    expect('first CLK' in rows['route_class_0.25']['why'],
           f"the 0.25 class call names CLK first ({rows['route_class_0.25']['why']!r})")
    expect('first SIG (--route-first)' in rows['route']['why']
           and 'CLK' not in rows['route']['why'],
           f"the pass names SIG and not CLK ({rows['route']['why']!r})")
    ch = Recorder(tmp)
    AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'])
    step = next(s for s in ch.steps if s['tag'] == 'route_class_0.25')
    expect('route_first' in step, f"the class call keeps a route-first record ({step})")
    plain = dict(A)
    plain.pop('class_clearances')
    expect('cap every class at it' in AP.plan_steps(plain)['rules_why'],
           f"capped by default: {AP.plan_steps(plain)['rules_why']!r}")
    expect('--cap-net-classes' in AP.plan_steps(dict(A, cap_net_classes=True))['rules_why'],
           "capped by the caller: says --cap-net-classes")
    expect('routes at its own' in plan['rules_why'], f"honoured: {plan['rules_why']!r}")
    assert len(FAILS) == n, FAILS[n:]


def test_match_group(tmp):
    print("a length group across a class call and the passes")
    n = len(FAILS)
    src, fab = NC.board(os.path.join(tmp, 'board.kicad_pcb'))
    try:
        AP.analyze(src, fab_args=['--fab-overrides', fab], length_groups=[['HV_A', 'SIG_C']])
        why = None
    except AP.Refused as exc:
        why = str(exc)
    expect(why is not None and '--cap-net-classes' in why,
           f"refused, naming --cap-net-classes ({why})")
    assert len(FAILS) == n, FAILS[n:]


def test_failed_last_call(tmp):
    print("every net in a class call, the round's last class call fails")
    n = len(FAILS)
    a = dict(A, plane_nets=[], planes=[], power_nets=[], pair_nets=[],
             class_clearances={'CLK': 0.25, 'ETH_N': 0.2, 'ETH_P': 0.2},
             route_first=[], tiers_cover_scope=True)
    ch = Recorder(tmp, {'route_class_0.25': ['ETH_N', 'ETH_P']}, fail={'route_class_0.2'})
    _, final, g = AP.route_passes(ch, a, a['layers'], ['1.0', '-1.0', '1.0', '1.0'],
                                  effort_factor=10)
    step = next(s for s in ch.steps if s['tag'] == 'route_class_0.25')
    expect((step.get('grade') or {}).get('open_nets') == ['ETH_N', 'ETH_P'],
           f"the 0.25 call's board, the round's, is graded ({step.get('grade')})")
    expect(tags(ch) == ['route_class_0.25', 'route_class_0.2', 'route_class_0.2_p2']
           and final == 'OUT3' and g is not None and g['open'] == 0,
           f"the next round calls the open class again and its board ships "
           f"({tags(ch)}, {final}, {g})")
    ch = Recorder(tmp, {}, fail={'route_class_0.2'})
    _, final, g = AP.route_passes(ch, a, a['layers'], ['1.0', '-1.0', '1.0', '1.0'],
                                  effort_factor=10)
    expect(final == 'OUT1' and g is not None and g['open'] == 0,
           f"with no net left open, the 0.25 call's board ships, graded ({final}, {g})")
    assert len(FAILS) == n, FAILS[n:]


def test_pass_without_iterations(tmp):
    print("a pass that reports no iterations")
    n = len(FAILS)
    ch = Recorder(tmp, {'route': ['CLK', 'SIG']}, iters={'class': 100, 'pass': None})
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        AP.route_passes(ch, A, A['layers'], ['1.0', '-1.0', '1.0', '1.0'], effort_factor=10)
    expect(tags(ch) == ['route_class_0.25', 'route_class_0.2', 'route'],
           f"no later pass and no class call after it ({tags(ch)})")
    expect('no router iterations to budget it from' in buf.getvalue(),
           "and it says why the class call did not run")
    assert len(FAILS) == n, FAILS[n:]


def test_held_record(tmp):
    print("the clearance the passes are held at")
    n = len(FAILS)
    rec = AP.net_class_record(A)
    expect(rec.get('passes_held_at') == 0.2,
           f"GND, a plane in the passes, holds them at its 0.2 ({rec.get('passes_held_at')})")
    a = dict(A, class_clearances={'CLK': 0.25})
    rec = AP.net_class_record(a)
    expect('passes_held_at' in rec and rec['passes_held_at'] is None,
           f"no class net in the passes, no clearance ({rec.get('passes_held_at', 'absent')})")
    assert len(FAILS) == n, FAILS[n:]


def main():
    for t in (test_which_nets, test_rounds, test_budget, test_empty_pass, test_finish,
              test_rip, test_plan_text, test_match_group, test_failed_last_call,
              test_pass_without_iterations, test_held_record):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                t(tmp)
            except Exception:
                traceback.print_exc()
                FAILS.append(t.__name__)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: autoroute class rounds")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
