#!/usr/bin/env python3
"""route.py --route-first in the engine: the declared nets route ahead of
every reorder, Phase 3 too, no nested sub-run is given them, and the JSON
summary records what the run did.

1. route_first_record over hand-made orders: the declared nets first and in
   order in both loops is honoured; another net ahead of them, the declared
   ones swapped, or a Phase 3 net ahead of a declared one is not; a declared
   net not routed (already connected) does not count; nothing routed is None.
2. It wins over every reorder before it, each shown in force by a control run
   without the flag: the bus-first reorder (--ordering bus routes a detected
   bus group first), direct-first (#472: a BGA's bare-ball nets first) and
   the global plan's order file (KICAD_GLOBAL_PLAN=1 front-loads a listed
   net). With --route-first the declared net routes first and every other
   net keeps the control's relative order; the summary says it was honoured.
3. Phase 3: a multipoint net whose pending terminal sits among foreign pads
   is the boxed-in one, so Phase 3 routes its taps first without the flag.
   With --route-first naming the other multipoint net, that net's taps route
   first and the summary's `phase3` says so.
4. Reconciliation: on a one-layer board where a locked ring walls in SIG_X's
   pad, SIG_X fails and the end-of-run reconciliation retries it alone. With
   --route-first SIG_A that retry runs (no "final reconciliation pass
   failed"), the kwargs dump shows route_first on the outermost call only,
   the plane finalize's forwarded knobs do not include it, and the merged --json-out keeps the outermost run's record although the
   sub-run's JSON_SUMMARY is the last one printed.
"""
import json
import os
import re
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'tests'))
from run_utils import check, evidence                        # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

RUN_ALL_TIMEOUT = 600
ROUTE = os.path.join(ROOT, 'py_router', 'route.py')
FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def route(board, tag, *argv, env=None):
    """(the nets the run's progress lines start, in order, its JSON_SUMMARY
    route_first record or None, the whole log) for route.py on BOARD."""
    out = os.path.join(TMP, f'{tag}.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', ROUTE, board, out, *argv], accept=True,
              env=None if env is None else dict(os.environ, **env))
    seq = re.findall(r'^\[\d+/\d+[^\]]*\] Routing (.*) \(id=\d+\)$', r.stdout, re.M)
    rec = None
    for m in re.finditer(r'^JSON_SUMMARY: (\{.*\})$', r.stdout, re.M):
        rec = rec or json.loads(m.group(1)).get('route_first')
    return seq, rec, r.stdout


def test_record():
    from net_ordering import route_first_record as rec
    print("route_first_record")
    n = len(FAILS)
    for args, honoured, what in (
            ((['B', 'D'], ['B', 'D', 'A'], []), True, "first and in order"),
            ((['B', 'D'], ['A', 'B', 'D'], []), False, "another net ahead"),
            ((['B', 'D'], ['D', 'B', 'A'], []), False, "the declared two swapped"),
            ((['B', 'D'], ['B', 'A', 'D'], []), False, "another net between them"),
            ((['B', 'D'], ['D', 'A'], []), True, "B already connected"),
            ((['M'], ['M', 'A'], ['N', 'M']), False, "Phase 3 routed N's taps first"),
            ((['M'], ['M', 'A'], ['M', 'N']), True, "Phase 3 in order too"),
            ((['M'], [], []), None, "nothing routed")):
        got = rec(*args)
        expect(got['honoured'] is honoured and got['nets'] == args[0],
               f"{what}: honoured {honoured} ({got})")
    assert len(FAILS) == n, FAILS[n:]


def crossing_board(path):
    """Three parallel rows and A_X across them (test_autoplan_route_first)."""
    b = RoutingBoard(40, 40)
    b.pad('X1', 5, 5, 'A_X')
    b.pad('X2', 35, 35, 'A_X')
    for i, net in enumerate(('B_ROW', 'C_ROW', 'D_ROW')):
        b.pad(f'R{2 * i + 1}', 5, 12 + 8 * i, net)
        b.pad(f'R{2 * i + 2}', 35, 12 + 8 * i, net)
    return write_board(b.text(), path)


def bus_board(path):
    """Four 0.5 mm pads' worth of parallel nets 1 mm apart (a bus) and Z_NET."""
    b = RoutingBoard(40, 30)
    for i in range(4):
        b.pad(f'B{2 * i}', 5, 10 + i, f'BUS_{i}', sx=0.5, sy=0.5)
        b.pad(f'B{2 * i + 1}', 35, 10 + i, f'BUS_{i}', sx=0.5, sy=0.5)
    b.pad('Z1', 5, 25, 'Z_NET')
    b.pad('Z2', 35, 25, 'Z_NET')
    return write_board(b.text(), path)


def bga_board(path):
    """A 2x2 BGA whose balls are bare (no stub, no via), each ball's net with
    a partner pad, and A_DECL elsewhere."""
    b = RoutingBoard(40, 30)
    pads = []
    for k, (dx, dy) in enumerate([(-0.5, -0.5), (0.5, -0.5), (-0.5, 0.5), (0.5, 0.5)]):
        pads.append('(pad "%d" smd circle (at %s %s) (size 0.4 0.4) (layers "F.Cu" '
                    '"F.Mask") (net %d "BALL_%d") (uuid "bga-p%d"))'
                    % (k + 1, dx, dy, b.net(f'BALL_{k}'), k, k))
    b.items.append(
        '  (footprint "synth:BGA-4" (layer "F.Cu") (uuid "bga") (at 20 15)\n'
        '    (property "Reference" "U1" (at 0 -2 0) (layer "F.SilkS") (uuid "bga-r")\n'
        '      (effects (font (size 0.5 0.5) (thickness 0.1))))\n'
        '    %s\n  )' % '\n    '.join(pads))
    for k, (x, y) in enumerate([(5, 5), (35, 5), (5, 25), (35, 25)]):
        b.pad(f'R{k}', x, y, f'BALL_{k}')
    b.pad('Z1', 2, 15, 'A_DECL')
    b.pad('Z2', 2, 28, 'A_DECL')
    return write_board(b.text(), path)


def _wins(label, board, declared, flags=(), env=None, mark=None):
    plain, _, log = route(board, label + '_plain', *flags, env=env)
    expect(mark is None or mark in log, f"{label}: the reorder is in force ({mark!r})")
    first, rec, _ = route(board, label + '_first', *flags, '--route-first', declared,
                          env=env)
    want = [declared] + [x for x in plain if x != declared]
    expect(plain[:1] != [declared] and first == want,
           f"{label}: without the flag {plain}; with it {first} (want {want})")
    expect(rec and rec['honoured'] is True and rec['main_loop'] == [declared],
           f"{label}: the summary records it honoured ({rec})")


def test_wins_over_reorders():
    print("--route-first wins over bus-first, direct-first and the global plan")
    n = len(FAILS)
    _wins('bus', bus_board(os.path.join(TMP, 'bus.kicad_pcb')), 'Z_NET',
          flags=('--ordering', 'bus'), mark='Bus Detection: Found 1 bus group(s)')
    _wins('direct', bga_board(os.path.join(TMP, 'bga.kicad_pcb')), 'A_DECL',
          flags=('--no-bga-zones',), mark='Direct-first ordering (#472): 4 bare-ball')
    order = os.path.join(TMP, 'order.txt')
    with open(order, 'w') as fh:
        fh.write('C_ROW\n')
    _wins('plan', crossing_board(os.path.join(TMP, 'crossing.kicad_pcb')), 'D_ROW',
          env={'KICAD_GLOBAL_PLAN': '1', 'KICAD_GLOBAL_PLAN_ORDER_FILE': order},
          mark='Global plan order file: front-loaded 1/1')
    assert len(FAILS) == n, FAILS[n:]


def phase3_board(path):
    """Two three-pad nets: every M_A pad has a foreign pad 0.9 mm off it (the
    boxed-in terminal), M_B's pads stand clear."""
    b = RoutingBoard(50, 40)
    for i, (x, y) in enumerate([(5, 5), (45, 5), (25, 35)]):
        b.pad(f'A{i}', x, y, 'M_A')
        b.pad(f'F{i}a', x + 0.9, y + 0.9, f'F_{i}')
        b.pad(f'F{i}b', x + 8, y + 0.9, f'F_{i}')
    for i, (x, y) in enumerate([(5, 20), (45, 20), (25, 12)]):
        b.pad(f'B{i}', x, y, 'M_B')
    return write_board(b.text(), path)


def _phase3_order(log):
    tail = log.split('Multi-point Phase 3:', 1)[-1]
    return re.findall(r'^(M_[AB]) \(net \d+\):$', tail, re.M)


def test_phase3():
    print("Phase 3: the declared net's taps first")
    n = len(FAILS)
    board = phase3_board(os.path.join(TMP, 'p3.kicad_pcb'))
    _, _, plain = route(board, 'p3_plain')
    expect(_phase3_order(plain) == ['M_A', 'M_B'],
           f"without the flag the boxed-in M_A's taps route first "
           f"({_phase3_order(plain)})")
    seq, rec, log = route(board, 'p3_first', '--route-first', 'M_B')
    expect(seq[:1] == ['M_B'] and _phase3_order(log) == ['M_B', 'M_A'],
           f"with --route-first M_B: M_B first in the main loop ({seq}) and in "
           f"Phase 3 ({_phase3_order(log)})")
    expect(rec and rec['phase3'] == ['M_B'] and rec['honoured'] is True,
           f"the summary records Phase 3's order ({rec})")
    assert len(FAILS) == n, FAILS[n:]


def wall_board(path):
    """One layer: SIG_A clear, SIG_X's right pad inside a locked ring of WALL."""
    b = RoutingBoard(40, 30, layers=('F.Cu',))
    b.pad('P1', 5, 5, 'SIG_A')
    b.pad('P2', 35, 5, 'SIG_A')
    b.pad('P3', 5, 15, 'SIG_X')
    b.pad('P4', 35, 15, 'SIG_X')
    b.ring([(32, 12), (38, 12), (38, 18), (32, 18)], 'WALL', w=0.3, locked=True)
    b.pad('P5', 32, 25, 'WALL')
    b.pad('P6', 32, 18.0, 'WALL')
    return write_board(b.text(), path)


def test_reconciliation():
    print("reconciliation: the retry runs, without the route-first list")
    n = len(FAILS)
    board = wall_board(os.path.join(TMP, 'wall.kicad_pcb'))
    js = os.path.join(TMP, 'wall.json')
    dump = os.path.join(TMP, 'kwargs.jsonl')
    seq, rec, log = route(board, 'wall', '--nets', 'SIG_A', 'SIG_X', '--layers', 'F.Cu',
                          '--route-first', 'SIG_A', '--json-out', js,
                          env={'KICAD_DUMP_BATCH_KWARGS': dump,
                               'KICAD_DUMP_BATCH_KWARGS_CONTINUE': '1'})
    retry = log.split('Final reconciliation: retrying', 1)
    expect(len(retry) == 2 and re.search(r'^\[1/1\] Routing SIG_X ', retry[1], re.M),
           "the reconciliation retried SIG_X")
    expect('final reconciliation pass failed' not in log,
           "and did not fail" + ('' if 'final reconciliation pass failed' not in log else
                                 ': ' + log.split('final reconciliation pass failed', 1)[1][:120]))
    calls = [json.loads(x) for x in open(evidence(dump, 'kwargs dump'))]
    expect(len(calls) >= 2 and calls[0].get('route_first') == ['SIG_A']
           and all(c.get('route_first') is None for c in calls[1:]),
           f"route_first on the outermost call only "
           f"({[c.get('route_first') for c in calls]})")
    summaries = [json.loads(m) for m in re.findall(r'^JSON_SUMMARY: (\{.*\})$', log, re.M)]
    expect(summaries and 'route_first' not in summaries[-1]
           and summaries[-1].get('scope') == 'reconciliation-subset',
           "the last JSON_SUMMARY is the sub-run's, with no record")
    import route as _route
    expect('route_first' not in _route._FINALIZE_ROUTE_KNOBS,
           "the plane finalize's sub-runs (repair_planes, given "
           "_finalize_route_knobs) are not given it either")
    want = {'nets': ['SIG_A'], 'main_loop': ['SIG_A'], 'phase3': [], 'honoured': True}
    doc = json.load(open(evidence(js, '--json-out')))
    expect(rec == want and doc.get('route_first') == want,
           f"the outermost record, in JSON_SUMMARY and the merged --json-out "
           f"({rec}, {doc.get('route_first')})")
    assert len(FAILS) == n, FAILS[n:]


def main():
    global TMP
    with tempfile.TemporaryDirectory() as TMP:
        for t in (test_record, test_wins_over_reorders, test_phase3, test_reconciliation):
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
    print("PASS: route.py --route-first order")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
