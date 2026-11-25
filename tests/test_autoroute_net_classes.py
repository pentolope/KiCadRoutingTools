#!/usr/bin/env python3
"""autoroute and the net classes a board declares above the chain clearance,
and the via and layer costs a caller gives the route steps.

1. A real run on a synthetic 2-layer board at the chain clearance 0.15
   (--fab-overrides, 0.15 tracks), with three channels 1.0 mm wide between
   walls: HV_A and HV_B, of a 0.2 mm class, must pass one to take the short
   way, SIG_C and SIG_D, of the Default class, the second, and HV_E and SIG_F
   the third. A channel holds two 0.15 tracks 0.15 apart, never 0.2.
   Measured on the routed copper (segments and vias, by layer, with this
   file's own geometry): HV_A and HV_B end at least 0.2 apart, SIG_C and
   SIG_D closer than 0.2 -- the class holds its own nets and no other --
   SIG_F, routed by a pass, at least 0.2 from HV_E's copper, and every net
   is connected. No step is given a clearance ceiling, and one route call
   routes exactly HV_A, HV_B and HV_E and every route pass none of them,
   read back by route.py's own selector.
2. The same board with --cap-net-classes --via-cost 40 --layer-costs 1.0
   3.0: HV_A and HV_B, and HV_E and SIG_F, pass their channels together,
   closer than 0.2; every route step is given the ceiling, the via cost and
   the layer costs, and
   AUTOROUTE_SUMMARY and the --json-out document state the costs by layer,
   with the later passes', and that the classes were capped.
3. The chain's other steps, for an analysis with a QFN fanned out and a
   diff pair: honoured, no fanout rung and no diff-pair step is given a
   clearance ceiling (the diff pairs --clearance for the Default class) and
   the decap step no --clearance, which place_fanout_clearance reads as a
   ceiling; capped, each is given the chain clearance as its ceiling.
4. A cost count that is not the board's copper layer count, a cost in
   (0, 1), past 1000 or not a number, a via cost that is negative or not
   whole or past the router's 32-bit cost at the board's grid, a cost list
   forbidding every layer, and --layer-costs with --plane-layer-cost are
   refused for their reason, exit 2, before any step; route.py refuses a via
   cost past its 32-bit cost.
5. With --nets HV_*, every net of the run is in the class call: no route
   pass runs, no step fails, and the class call's board ships graded, its
   route.py summary in --json-out.
"""
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
import autoroute as AP                                        # noqa: E402
from kicad_parser import parse_kicad_pcb                     # noqa: E402
from net_queries import expand_net_patterns                  # noqa: E402
from run_utils import check, evidence                        # noqa: E402
from synth import RoutingBoard, write_board                  # noqa: E402

RUN_ALL_TIMEOUT = 900
SCRIPT = os.path.join(ROOT, 'py_router', 'autoroute.py')
ROUTE = os.path.join(ROOT, 'py_router', 'route.py')
CLASS_CLEARANCE = 0.2
CHAIN_CLEARANCE = 0.15
TRACK = 0.15
HV = ('HV_A', 'HV_B')
SIG = ('SIG_C', 'SIG_D')
MIX = ('HV_E', 'SIG_F')
CLASS_NETS = ('HV_A', 'HV_B', 'HV_E')
DEFAULT_NETS = ('SIG_C', 'SIG_D', 'SIG_F')
FAILS = []


def expect(cond, what):
    print(('  ok   ' if cond else '  FAIL ') + what)
    if not cond:
        FAILS.append(what)


def channel(b, y, names, tag):
    """Two 10 x 7 mm walls at x 15..25 leaving y-0.35..y+0.65 open, and two
    two-pad nets whose straight runs each wall blocks, one from below the
    channel, one from above."""
    b.pad(f'W{tag}1', 20, y - 0.35 - 3.5, '', sx=10, sy=7)
    b.pad(f'W{tag}2', 20, y + 0.65 + 3.5, '', sx=10, sy=7)
    for i, (net, dy) in enumerate(zip(names, (-2, 2))):
        b.pad(f'{tag}{2 * i + 1}', 3, y + dy, net)
        b.pad(f'{tag}{2 * i + 2}', 37, y + dy, net)


def board(path):
    b = RoutingBoard(40, 60)
    channel(b, 10.0, HV, 'H')
    channel(b, 30.0, SIG, 'S')
    channel(b, 50.0, MIX, 'M')
    write_board(b.text(), path)
    base = path[:-len('.kicad_pcb')]
    default = {'name': 'Default', 'clearance': CHAIN_CLEARANCE, 'track_width': TRACK,
               'via_diameter': 0.6, 'via_drill': 0.3, 'priority': 2147483647}
    with open(base + '.kicad_pro', 'w') as fh:
        json.dump({'board': {'design_settings': {'rules': {'min_clearance': CHAIN_CLEARANCE,
                                                          'min_track_width': TRACK}}},
                   'meta': {'filename': os.path.basename(base) + '.kicad_pro', 'version': 1},
                   'net_settings': {
                       'classes': [default, dict(default, name='HV', priority=0,
                                                 clearance=CLASS_CLEARANCE)],
                       'meta': {'version': 3},
                       'netclass_patterns': [{'netclass': 'HV', 'pattern': 'HV_*'}]}},
                  fh, indent=1)
    with open(base + '.fab.txt', 'w') as fh:
        fh.write(f'clearance = {CHAIN_CLEARANCE}\ntrack_width = {TRACK}\n'
                 'via_diameter = 0.6\nvia_drill = 0.3\n')
    return path, base + '.fab.txt'


def _seg_dist(p, q, r, s):
    """Distance between segments pq and rs."""
    def pt_seg(a, b, c):
        dx, dy = c[0] - b[0], c[1] - b[1]
        L = dx * dx + dy * dy
        t = 0.0 if L == 0 else max(0.0, min(1.0, ((a[0] - b[0]) * dx + (a[1] - b[1]) * dy) / L))
        return math.hypot(a[0] - b[0] - t * dx, a[1] - b[1] - t * dy)

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if (cross(p, q, r) * cross(p, q, s) < 0) and (cross(r, s, p) * cross(r, s, q) < 0):
        return 0.0
    return min(pt_seg(p, r, s), pt_seg(q, r, s), pt_seg(r, p, q), pt_seg(s, p, q))


def copper(pcb, net):
    """[(layer or None for every layer, a, b, half width)] of NET's tracks and vias."""
    nid = next(i for i, n in pcb.nets.items() if n.name == net)
    out = [(s.layer, (s.start_x, s.start_y), (s.end_x, s.end_y), s.width / 2)
           for s in pcb.segments if s.net_id == nid]
    out += [(None, (v.x, v.y), (v.x, v.y), v.size / 2) for v in pcb.vias if v.net_id == nid]
    return out


def gap(pcb, a, b):
    """The smallest edge-to-edge distance between copper of nets A and B on
    one layer (a via is on every layer of this 2-layer board), or inf."""
    best = math.inf
    for la, p, q, ha in copper(pcb, a):
        for lb, r, s, hb in copper(pcb, b):
            if la is None or lb is None or la == lb:
                best = min(best, _seg_dist(p, q, r, s) - ha - hb)
    return best


def _values(args, flag):
    """The values following FLAG in ARGS, up to the next option."""
    if flag not in args:
        return []
    out = []
    for x in args[args.index(flag) + 1:]:
        if x.startswith('--'):
            break
        out.append(x)
    return out


def _summary(stdout):
    m = re.search(r'^AUTOROUTE_SUMMARY: (\{.*\})$', stdout, re.M)
    return json.loads(m.group(1)) if m else {}


def test_honoured(tmp):
    print("a real run: the 0.2 class honoured")
    n = len(FAILS)
    src, fab = board(os.path.join(tmp, 'board.kicad_pcb'))
    out = os.path.join(tmp, 'routed.kicad_pcb')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, src, out, '--fab-overrides', fab],
              accept=True)
    pcb = parse_kicad_pcb(evidence(out, 'routed board'))
    g_hv, g_sig, g_mix = gap(pcb, *HV), gap(pcb, *SIG), gap(pcb, *MIX)
    expect(g_hv >= CLASS_CLEARANCE - 0.005,
           f"HV_A and HV_B end {g_hv:.3f} mm apart, at least their class {CLASS_CLEARANCE}")
    expect(g_sig < CLASS_CLEARANCE - 0.02 and g_sig >= CHAIN_CLEARANCE - 0.005,
           f"SIG_C and SIG_D end {g_sig:.3f} mm apart, at the Default class, "
           f"under {CLASS_CLEARANCE}: the class holds no other net")
    expect(g_mix >= CLASS_CLEARANCE - 0.005,
           f"SIG_F, routed by a pass, ends {g_mix:.3f} mm from HV_E's copper, at "
           f"least HV_E's class {CLASS_CLEARANCE}")
    expect(_summary(r.stdout).get('open') == 0,
           f"every net is connected ({_summary(r.stdout).get('open')} open)")

    plan = json.load(open(evidence(out + '.autoroute/plan.json', 'plan.json')))
    src_pcb = parse_kicad_pcb(src)
    expect(plan['analysis'].get('class_clearances') == {n: CLASS_CLEARANCE for n in CLASS_NETS},
           f"the analysis names the class's nets ({plan['analysis'].get('class_clearances')})")
    for s in plan['steps']:
        expect('--clearance-ceiling' not in (s.get('argv') or []),
               f"{s['tag']}: no clearance ceiling")
    routes = [s for s in plan['steps'] if s['tool'] == 'route']
    tiers = [s for s in routes if s['tag'].startswith('route_class_')]
    expect(len(tiers) == 1 and sorted(expand_net_patterns(
        src_pcb, _values(tiers[0]['argv'], '--nets'), quiet=True)) == list(CLASS_NETS),
           f"one route call routes exactly {', '.join(CLASS_NETS)} "
           f"({[s['tag'] for s in tiers]})")
    passes = [s for s in routes if s not in tiers]
    for s in passes:
        sel = expand_net_patterns(src_pcb, _values(s['argv'], '--nets'), quiet=True)
        expect(not set(sel) & set(CLASS_NETS) and set(DEFAULT_NETS) <= set(sel),
               f"{s['tag']}: routes {', '.join(DEFAULT_NETS)} and no net of the class "
               f"({sel})")
    assert len(FAILS) == n, FAILS[n:]


def test_capped(tmp):
    print("a real run: --cap-net-classes, with --via-cost and --layer-costs")
    n = len(FAILS)
    src, fab = board(os.path.join(tmp, 'board.kicad_pcb'))
    out = os.path.join(tmp, 'capped.kicad_pcb')
    js = os.path.join(tmp, 'capped.json')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, src, out, '--fab-overrides', fab,
               '--cap-net-classes', '--via-cost', '40', '--layer-costs', '1.0', '3.0',
               '--json-out', js], accept=True)
    pcb = parse_kicad_pcb(evidence(out, 'routed board'))
    g_hv, g_mix = gap(pcb, *HV), gap(pcb, *MIX)
    expect(g_hv < CLASS_CLEARANCE - 0.02,
           f"HV_A and HV_B end {g_hv:.3f} mm apart, under their class: capped")
    expect(g_mix < CLASS_CLEARANCE - 0.02,
           f"SIG_F ends {g_mix:.3f} mm from HV_E, under HV_E's class: capped")
    plan = json.load(open(evidence(out + '.autoroute/plan.json', 'plan.json')))
    routes = [s for s in plan['steps'] if s['tool'] == 'route']
    expect(routes and not any(s['tag'].startswith('route_class_') for s in routes),
           "no route call of the class's own")
    for s in routes:
        expect(_values(s['argv'], '--clearance-ceiling') == [str(CHAIN_CLEARANCE)],
               f"{s['tag']}: --clearance-ceiling {CHAIN_CLEARANCE}")
        expect(_values(s['argv'], '--via-cost') == ['40'], f"{s['tag']}: --via-cost 40")
        want = ['1.0', '3.0']
        expect(_values(s['argv'], '--layer-costs') == want,
               f"{s['tag']}: --layer-costs {' '.join(want)} "
               f"({_values(s['argv'], '--layer-costs')})")
    want = {'via_cost': 40, 'layer_costs': {'F.Cu': 1.0, 'B.Cu': 3.0},
            'crossing_layer_costs': {'F.Cu': 1.0, 'B.Cu': 3.0},
            'from_caller': ['--via-cost', '--layer-costs']}
    doc = json.load(open(evidence(js, '--json-out document')))
    expect(doc.get('routing_costs') == want,
           f"--json-out states the costs ({doc.get('routing_costs')})")
    expect(_summary(r.stdout).get('routing_costs') == want,
           f"AUTOROUTE_SUMMARY states the costs ({_summary(r.stdout).get('routing_costs')})")
    expect(doc.get('net_classes', {}).get('mode') == 'capped'
           and doc['net_classes'].get('above_clearance')
           == {n: CLASS_CLEARANCE for n in CLASS_NETS},
           f"--json-out states the classes were capped ({doc.get('net_classes')})")
    assert len(FAILS) == n, FAILS[n:]


class Recorder(AP.ChainEvents):
    """Chain.run recorded; every step succeeds and every grade is clean."""

    def __init__(self):
        self.cur = 'IN'
        self.runs = []
        self.steps = []
        self.last_iterations = 10

    def run(self, kind, args, tag, **kw):
        self.runs.append((kind, tag, list(args)))
        self.steps.append({'tag': tag, 'ok': True, 'log': tag + '.log'})
        return f'OUT{len(self.runs)}', ({'escaped': 4, 'failed': 1}
                                        if kind in ('bga', 'qfn') else {}), {}

    def accept(self, out):
        if out:
            self.cur = out

    def grade(self, board):
        return {'open': 0, 'drc': 0, 'open_nets': []}

    def drc_nets(self, board):
        return set()

    def check_drc(self, board):
        return {'violations': 0, 'items': []}


def test_commands(tmp):
    print("the other steps' clearance arguments")
    n = len(FAILS)
    a = dict(layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'], clearance=0.15, track=0.15,
             via=0.5, drill=0.25, fine_via=0.45, fine_drill=0.2,
             planes=[('GND', 'In1.Cu')], plane_nets=['GND'], power_nets=['GND'],
             layer_costs=[1.0, -1.0, 1.0, 1.0], grid=0.05, congested=False, n_nets=10,
             pair_nets=['ETH_P', 'ETH_N'],
             globs={'GND': 'GN[D/]', 'ETH_P': 'ETH_[P/]', 'ETH_N': 'ETH_[N/]'},
             parts=[('U1', 'qfn', 0.5)],
             class_clearances={'ETH_N': 0.2, 'ETH_P': 0.2})
    for capped in (False, True):
        ch = Recorder()
        orig = AP.Chain
        AP.Chain = lambda *args, **kw: ch
        try:
            AP.run_plan('IN', 'OUT', dict(a, cap_net_classes=True) if capped else a, tmp)
        finally:
            AP.Chain = orig
        mode = 'capped' if capped else 'honoured'
        kinds = {k for k, _, _ in ch.runs}
        expect({'qfn', 'decap', 'diff', 'route'} <= kinds,
               f"{mode}: the chain runs a fanout, the decap, the diff pairs and routes "
               f"({sorted(kinds)})")
        for kind, tag, args in ch.runs:
            if kind == 'planes':
                continue
            if kind == 'decap':
                got = _values(args, '--clearance')
                want = ['0.15'] if capped else []
            else:
                got = _values(args, '--clearance-ceiling')
                want = ['0.15'] if capped else []
            expect(got == want, f"{mode} {tag}: "
                   + ('--clearance' if kind == 'decap' else '--clearance-ceiling')
                   + f" {want or 'absent'} ({got or 'absent'})")
            if kind == 'diff' and not capped:
                expect(_values(args, '--clearance') == ['0.15'],
                       f"{tag}: --clearance 0.15 for the Default class")
    assert len(FAILS) == n, FAILS[n:]


def test_refusals(tmp):
    print("--layer-costs / --via-cost refusals")
    n = len(FAILS)
    src, fab = board(os.path.join(tmp, 'board.kicad_pcb'))
    out = os.path.join(tmp, 'refused.kicad_pcb')
    js = os.path.join(tmp, 'refused.json')
    cases = [
        (['--layer-costs', '1.0', '2.0', '3.0'], 'give one per layer', 'three costs, two layers'),
        (['--layer-costs', '1.0'], 'give one per layer', 'one cost, two layers'),
        (['--layer-costs', '1.0', '0.5'], 'must be a cost in [1, 1000]', 'a cost of 0.5'),
        (['--layer-costs', '1.0', '1001'], 'must be a cost in [1, 1000]', 'a cost of 1001'),
        (['--layer-costs', '1.0', 'nan'], 'must be a cost in [1, 1000]', 'a cost of nan'),
        (['--via-cost', '-1'], 'must be a whole number >= 0', 'a via cost of -1'),
        (['--via-cost', '1.5'], 'must be a whole number >= 0', 'a via cost of 1.5'),
        (['--layer-costs', '1.0', '2.0', '--plane-layer-cost', '3'],
         'not both', '--layer-costs with --plane-layer-cost'),
        (['--layer-costs', '-1', '-1'], 'forbids tracks on every layer',
         'every layer forbidden'),
        (['--via-cost', str((2 ** 31 - 1) // 2000 + 1)], 'the most whose cost the router holds',
         'a via cost one past what the router holds at the 0.1 mm grid'),
    ]
    for extra, why, what in cases:
        check([sys.executable, '-X', 'utf8', SCRIPT, src, out, '--fab-overrides', fab,
               '--json-out', js, *extra], refuse=why, code=2, allow=('error: argument',))
        expect(not os.path.exists(out + '.autoroute') and not os.path.exists(js),
               f"{what}: refused for its reason, before any step")
    # The bound is the router's: one more via-cost unit at the 0.1 mm grid,
    # doubled for a pair, is past a 32-bit signed integer, the one before is not.
    top = (2 ** 31 - 1) // (2 * 1000)
    expect(getattr(AP, 'max_via_cost', lambda g: None)(0.1) == top,
           f"the via cost bound at 0.1 mm is {top}")
    check([sys.executable, '-X', 'utf8', ROUTE, src, out, '--nets', 'SIG_C',
           '--via-cost', '2147484', '--grid-step', '0.1'],
          refuse="past the router's 32-bit cost", code=2, allow=('error: argument',))
    expect(True, "route.py refuses a via cost past its 32-bit cost, by reason")
    assert len(FAILS) == n, FAILS[n:]


def test_empty_pass(tmp):
    print("a real run: --nets HV_*, every net in a class call")
    n = len(FAILS)
    src, fab = board(os.path.join(tmp, 'board.kicad_pcb'))
    out = os.path.join(tmp, 'hv.kicad_pcb')
    js = os.path.join(tmp, 'hv.json')
    r = check([sys.executable, '-X', 'utf8', SCRIPT, src, out, '--fab-overrides', fab,
               '--nets', 'HV_*', '--json-out', js], accept=True)
    summary = _summary(r.stdout)
    plan = json.load(open(evidence(out + '.autoroute/plan.json', 'plan.json')))
    expect(sorted(summary.get('open_nets') or ['?']) == sorted(DEFAULT_NETS)
           and summary.get('drc') == 0,
           f"the shipped board is graded: open only the nets --nets leaves out "
           f"({summary.get('open_nets')}), {summary.get('drc')} DRC")
    expect(all(s.get('ok', True) for s in plan['steps']),
           f"no step failed ({[(s['tag'], s.get('rc')) for s in plan['steps']]})")
    expect(not any(AP._PASS_TAG.fullmatch(s['tag']) and not s['tag'].startswith('route_class')
                   for s in plan['steps']),
           "no route pass runs with no net to route")
    expect((summary.get('final_tag') or '').startswith('route_class_')
           and plan['grade'] and not set(plan['grade']['open_nets']) & set(CLASS_NETS),
           f"the class call's board ships, graded ({summary.get('final_tag')})")
    doc = json.load(open(evidence(js, '--json-out document')))
    expect(doc.get('status') == 'route_pass' and doc.get('route_summary'),
           f"--json-out carries the class call's route.py summary ({doc.get('status')})")
    pcb = parse_kicad_pcb(evidence(out, 'routed board'))
    expect(gap(pcb, *HV) >= CLASS_CLEARANCE - 0.005,
           f"HV_A and HV_B end {gap(pcb, *HV):.3f} mm apart")
    assert len(FAILS) == n, FAILS[n:]


def main():
    for t in (test_commands, test_refusals, test_empty_pass, test_honoured, test_capped):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                t(tmp)
            except Exception:
                traceback.print_exc()
                FAILS.append(t.__name__)
    if FAILS:
        print(f"FAIL: {len(FAILS)} check(s)")
        return 1
    print("PASS: autoroute net classes and routing costs")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
