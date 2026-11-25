#!/usr/bin/env python3
"""Route a board through a deterministic plan built from the board itself.

    python3 py_router/autoroute.py board.kicad_pcb routed.kicad_pcb
    python3 py_router/autoroute.py board.kicad_pcb routed.kicad_pcb --dry-run

The plan-pcb-routing skill has an agent read a board and choose a command
chain. This makes the same kinds of choices from board facts alone -- the
footprints, net names, layer count and the board's own design rules -- and
runs the chain, so routing without an agent gets the steps that remove most
congestion before the router starts:

  1. design rules  the fab clearance floor for every step (the Default
                   class routes at it, a net class declared above it at its
                   own), the fab track floor, the working via (fine via
                   for fine-pitch escapes), all from
                   list_nets.read_design_rules
  2. planes        on 4+ layer boards the fewest inner layers that put a
                   plane next to every signal layer (In1+In2 on 4 layers,
                   In1+In4 on 6, In1+In3+In6 on 8): GND, then the dominant
                   rail, then GND again; GND on B.Cu on 2-layer boards;
                   poured first, and signals kept off the inner planes
                   until the route step's later passes
  3. fanout        every grid array with interior depth or fine pitch
                   (bga_fanout) and every fine-pitch four-sided perimeter
                   package (qfn_fanout), and every dense mezzanine (a
                   CM4/CM5's 200+ pads at 0.4 mm, qfn_fanout under the
                   pads), read from the pads and their pitch
                   along a row -- never a connector, socket or magnetics --
                   plane nets excluded; a part that drops escapes or leaves
                   grazes is retried with the fine via (BGA), then with the
                   dogbone (BGA) or under-pad (QFN) method, keeping the attempt
                   that escaped most, then grazed least
  4. decaps        place_fanout_clearance once, after the fanouts; a part it
                   leaves shorted to a BGA's fanout copper is held in a re-fan
                   of that BGA (bga_fanout --immovable-refs, the BGA's whole
                   ladder, every later fanout replayed at its accepted rung),
                   then place_fanout_clearance again, keeping the board with
                   fewer shorts, then fewer dropped escapes
  5. diff pairs    name-detected pairs routed first with route_diff; a pair
                   net it leaves in a DRC violation is ripped and routed
                   again single-ended
  6. route         route.py over every net with the poured nets and rails as
                   power nets at RAIL_WIDTH (a net the caller's --power-nets
                   select at its width), a first pass that keeps signals off
                   the planes, a finer grid around fine-pitch parts, deeper rip-up on
                   congested boards, and leave to rip the stubs of any pair
                   route_diff could not couple; up to two further passes on the result
                   while nets are left open, which may cross the planes at
                   PLANE_FALLBACK_COST, shipping the best pass

--finish runs step 6 alone on a board another chain has already prepared and
routed (an agent's chain, say): the poured planes are read from the board's
pours, and the input board is itself a candidate, so a pass ships only if it
beats it. route.py works on the nets still open, but like any route step it
may rip and reroute existing copper and runs its plane finalize.

--keep-pours takes the planes from the board's own pours, as --finish does, in
a full run: step 2 pours nothing, so every pour the board has keeps its
outline, clearance, pad connection and priority, and the signals keep off
the inner layers those pours cover.

--power-nets PATTERN [...] with --power-nets-widths MM [...] are route.py's
own flags, given to every route step ahead of autoroute's rails: route.py
routes a net at the width of the first pattern that selects it (fnmatch), so
a net they select, a rail included, routes at their width, and every other
net keeps the choice above. One width per pattern, each finite and above 0,
or the run is refused before any step runs (exit 2).

--nets PATTERN [...] routes only the nets the patterns select, read as
route.py reads its own --nets (wildcards, a sheet's last component, a leading
'!' excluding): every fanout and route step is given the patterns in place
of '*', the diff-pair and planes steps only the pairs and planes among those
nets, and a pour, power net or pair outside them is left as the board has
it. The layer costs still keep signals off every plane layer. Patterns that
together select no net are refused before any step runs (exit 2).

--track-width MM is route.py's own --track-width for every route step, in
place of the chain's track floor; the fanouts and diff pairs keep the floor,
which their escapes need.

--quick LEVEL passes route.py's --quick LEVEL to every route step: the chain
judges a placement and is not shipped.

--route-first PATTERN [...] routes the nets the patterns select before every
other net in each route pass, in the order the patterns are given (one
pattern's nets by name): each pass is given route.py's own --route-first
with a pattern that selects each of them alone, in that order, and every
other net keeps route.py's mps order. The analysis states the nets in order
(`route_first`), each pass route.py's own JSON_SUMMARY record of them (the
nets its single-ended loop and Phase 3 routed, in order, and whether they came
first; `route_first` on the step), and AUTOROUTE_SUMMARY both for the pass
that ships. A wildcard counts only the nets the route steps route (--nets,
else every net route.py's '*' selects). A pattern that selects no net, an
exclusion, a wildcard that selects none of those nets and a name that selects
a net outside them (an unqualified name selects every sheet's net of that
leaf name) are refused before any step runs (exit 2).

A net class the board declares above the chain clearance keeps its own on
every step: no step is given a clearance ceiling, the decap step no
clearance (its --clearance is a ceiling), and the route steps --clearance
for the Default class. Its nets route in a route.py call of their own per
class clearance, widest first, ahead of the route passes, which leave them
out and take no rip authority over them, and again before a later pass and
once after the last, at the later passes' costs, while one is open (in the
effort budget): route.py prices every obstacle of a call at the widest
class the call routes. A plane net, whose pour the passes' plane finalize
serves, and a net no pattern selects alone route in the passes at their
class. With every net of the run in a class call no pass runs, and each
round's last class call is graded. --cap-net-classes instead caps every class at the chain clearance on
every step, as route.py --clearance-ceiling does. The analysis names the
nets (`class_clearances`), and AUTOROUTE_SUMMARY and --json-out the mode
and the calls (`net_classes`).

--layer-costs COST [...] (one per copper layer, board order) replaces the
chain's layer costs on every step that takes them, and --via-cost N is
route.py's and route_diff.py's own --via-cost on every route and diff-pair
step; a negative layer cost keeps tracks off that layer in the first pass,
and the later passes cross it at PLANE_FALLBACK_COST, as they cross a
plane. A count that is not the board's copper layer count, a cost outside
[1, 1000] that is not negative, every layer negative, a via cost that is
not a whole number >= 0 or past what the router's 32-bit cost holds at the
chain's grid (`max_via_cost`), and --layer-costs with --plane-layer-cost
are refused before any step runs (exit 2). AUTOROUTE_SUMMARY and --json-out give what the route steps ran
with (`routing_costs`).

--fab-tier, --fab-overrides and --escalation are the shared fab flags every
routing CLI takes. autoroute's own floors (step 1) come from them, and it
passes each one the caller gave to every step that takes them (planes,
fanout, diff pairs, route) and to the check_drc that grades each step.

Every step runs as its own process in <output>.autoroute/, so a step that
fails is skipped and the chain continues from the last good board. Each route
pass is graded: open nets as check_connected reports them (the KiCad
cross-check off, so the grade is deterministic) and real violations as
check_drc reports them. A pass is kept when it ranks above the best board so
far, completion first: fewer open nets, then fewer violations at the same open
count. The first pass that is not kept ends the passes. A further pass
starts only while the router iterations spent, plus the last pass's again,
stay within --effort-factor times the first pass's -- a forecast from counts
the router reproduces exactly, not a cap on the pass itself -- and not at all
when a pass reported no iterations. If no route pass succeeds, the best board
the chain has ships (with --finish, the input board itself). The chain and
every grade are written to <output>.autoroute/plan.json. --json-out FILE also
writes the chain, the step that produced the shipped board and, when that is
a route pass, the summary route.py's own --json-out wrote for it; when no
route pass shipped, its `status` says so and the summary is null.

Besides its prose the run prints machine-readable lines, one JSON object
each, which the plugin's Autorouter tab reads (and a script may):

  AUTOROUTE_ANALYSIS: {...}  what analyze() read off the board
  AUTOROUTE_PLAN: {...}      the steps it means to run, in order, each with
                             what it does and why; a row marked optional runs
                             only if the passes before it leave nets open
  AUTOROUTE_STEP: {...}      a step starting (state "running") and ending
                             (state "done" or "failed"), one of each per step
                             run, or a planned step that never ran (state
                             "skipped", with the reason). An ending carries
                             the step's seconds, exit code, a short result,
                             its own summary, and on failure the tail of its
                             log. Steps the run adds (a fanout's next rung, a
                             re-fan, the pair re-route) appear as they start,
                             with the planned row they belong to as `parent`
  AUTOROUTE_SUMMARY: {...}   the shipped board and its grade, last
"""
from __future__ import annotations

#: #937 registry: which door(s) show this tool, and whether it changes
#: the board. Read by krt_registry.py -- by AST, never imported.
KRT_TOOL = {'scope': ['routing'], 'kind': 'actor'}

import argparse
import fnmatch
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

#: The step tools that take the shared fab flags; the decap step does not.
FAB_STEPS = ('planes', 'bga', 'qfn', 'diff', 'route')

TOOLS = {
    'planes': os.path.join(HERE, 'route_planes.py'),
    'bga': os.path.join(HERE, 'bga_fanout.py'),
    'qfn': os.path.join(HERE, 'qfn_fanout.py'),
    'decap': os.path.join(REPO, 'py_placer', 'place_fanout_clearance.py'),
    'diff': os.path.join(HERE, 'route_diff.py'),
    'route': os.path.join(HERE, 'route.py'),
}

GRID_FINE_PITCH = 0.5        # a part at or under this pitch routes on 0.05
CONGESTED_NETS = 150         # skill: CONGESTED = fine BGA or > 150 nets
RAIL_WIDTH = 0.3
PLANE_LAYER_COST = -1.0      # inner planes stay solid; a positive price lets signals cross
PLANE_FALLBACK_COST = 3.0    # a later pass's price on a solid plane, the route step's in the plan skill
ROUTE_PASSES = 3             # the first pass plus up to two on its result
EFFORT_FACTOR = 2.0          # --effort-factor's default


def _num(x):
    return round(float(x), 4)


_DASH_DIGIT = re.compile(r'^-\d')


def _exact_ok(name):
    """A net name the tools' argparse accepts as an exact value: it may start
    with '-' only before a digit (cli_nets.pin_dash_digit_values)."""
    return not name.startswith('-') or bool(_DASH_DIGIT.match(name))


def _glob(name):
    """A pattern that selects exactly this net through every selector the
    chain's tools use, and that argparse passes as a value.

    The selectors (net_queries) match a pattern with no '/' against a net's
    last sheet component too ('GND' selects '/Analog/GND'), and read a leading
    '!' as an exclusion. A sheet-qualified name ('/Analog/GND') is already
    full-matched only. Any other name gets its LAST character as a class that
    also holds '/' -- a net name never ends in '/', so the class admits no
    other net -- and a leading '-' or '!' becomes a class that is not read as
    an option or an exclusion: '[--]' (only '-') and '[ -!]' (a space or '!';
    no net name starts with a space). 'GND' -> 'GN[D/]', '-BATT' ->
    '[--]BAT[T/]', '!RESET' -> '[ -!]RESE[T/]'. Glob metacharacters are
    bracketed. `_selects_exactly` still checks each pattern against the
    board's own nets."""
    def esc(c):
        return f'[{c}]' if c in '[]*?' else c

    def tail(c):
        return {']': '[]/]', '!': '[/!]'}.get(c, f'[{c}/]')

    if name.startswith('/'):
        return ''.join(esc(c) for c in name)
    head = {'-': '[--]', '!': '[ -!]'}.get(name[0])
    if len(name) == 1:
        return head or tail(name)
    return (head or esc(name[0])) + ''.join(esc(c) for c in name[1:-1]) + tail(name[-1])


def _selects_exactly(pattern, name, names):
    """True when PATTERN selects NAME and no other of NAMES, read the ways the
    chain's tools read it: net_queries.net_pattern_matches (--nets, the
    fanouts' filters and their '!' exclusions) and plain fnmatchcase
    (route.py --power-nets)."""
    from net_queries import net_pattern_matches
    if pattern.startswith(('!', '-')):
        return False
    return all(net_pattern_matches(n, pattern) == (n == name)
               and fnmatch.fnmatchcase(n, pattern) == (n == name) for n in names)


class Refused(ValueError):
    """A caller setting the board cannot take; refused before any step runs."""


# --------------------------------------------------------------------------
# Board analysis


#: Footprint-name fragments of parts that are never escape-routed: connectors,
#: sockets and headers (pins reached in place) and magnetics. Matched against
#: the upper-cased library:footprint name.
MEZZANINE_PADS = 200         # the plan skill's dense mezzanine (CM4/CM5): 200+ pads,
MEZZANINE_PITCH = 0.4        # in rows at 0.4 mm, escaped under the pads
NOT_FANOUT_NAMES = ('CONN', 'SOCKET', 'HEADER', 'PINHEADER', 'BUS_M.2', 'M.2_',
                    'USB', 'RJ45', 'HDMI', 'SFP', 'TRANSFORMER', 'TRANS_', 'XFMR',
                    'MAGJACK', 'MAGNETIC')


def _mode(values):
    """The most common value after rounding to 1 um, the smallest on a tie."""
    from collections import Counter
    c = Counter(round(v, 3) for v in values if v > 1e-3)
    return min(c, key=lambda v: (-c[v], v)) if c else None


def pad_shape(fp):
    """('grid', pitch, depth), ('perimeter', pitch, None) or None for FP's pads.

    Read in the footprint's own frame (local_x/local_y), from the copper pads
    (a paste-only aperture is not one; a pin drawn as several pads is its
    largest) left once each marker (a net-less pad unlike the commonest pad,
    such as a BGA's corner outline pads off the ball lattice), each exposed
    pad (a pad over 3x the median area) and anything inside one (its sub-pads)
    is set aside. An unused ball or pin is net-less too, but keeps the common
    size.

    grid: no exposed pad, >= 3 rows and >= 3 columns of 3 or more pads, at
    least one pad off the bounding edges, and the columns and rows each on a lattice of their
    own pitch: the commonest spacing between neighbouring pads of one row (x)
    or one column (y), the two within 2x of each other. The part's pitch is
    the finer of the two; depth is rows from the edge to the centre.
    perimeter (when not a grid): at least two pads on each of the four
    bounding edges away from its corners, and every other pad within 1.5
    pitches of an edge -- a second staggered ring (aQFN) or corner pads set
    in. The pitch is the median spacing between neighbours along a side.
    Anything else -- two rows (a connector, a DFN, a transformer), one row, a
    scatter -- is neither."""
    from collections import Counter
    pads = [p for p in fp.pads if p.pad_type != 'np_thru_hole'
            and any(l.endswith('.Cu') for l in p.layers)]
    dims = lambda p: tuple(sorted((round(p.size_x, 3), round(p.size_y, 3))))
    if pads:
        common = Counter(map(dims, pads)).most_common(1)[0][0]
        pads = [p for p in pads if p.net_id or dims(p) == common]
    if len(pads) < 6:
        return None
    area = sorted(p.size_x * p.size_y for p in pads)
    med = area[len(area) // 2]
    big = [p for p in pads if p.size_x * p.size_y > 3 * med]
    # Pad sizes are in board space; local_x/local_y in the footprint's frame.
    turned = round(fp.rotation / 90) % 2 == 1

    def copper(p):
        return {l for l in p.layers if l.endswith('.Cu')}

    def under(p, q):
        hx, hy = (q.size_y, q.size_x) if turned else (q.size_x, q.size_y)
        shared = '*.Cu' in copper(p) | copper(q) or copper(p) & copper(q)
        return (shared and abs(p.local_x - q.local_x) <= hx / 2
                and abs(p.local_y - q.local_y) <= hy / 2)
    kept = [p for p in pads if p not in big and not any(under(p, q) for q in big)]
    # One land per pin: a pin drawn as two overlapping pads is one pad, and
    # an exposed pad's fingers out to the edge (which carry its number) are
    # one pad, not a side. The number alone is no test: libraries number the
    # exposed pad like a pin (orangecrab's RGY-24 calls it 12).
    largest = {}
    for p in kept:
        if p.pad_number and (p.pad_number not in largest or
                             p.size_x * p.size_y > largest[p.pad_number].size_x * largest[p.pad_number].size_y):
            largest[p.pad_number] = p
    kept = [p for p in kept if not p.pad_number or largest[p.pad_number] is p]
    if len(kept) < 6:
        return None
    # The exposed pad is a big pad in the middle of the part; big pads at its
    # corners (a sensor's mounting lands) are not one.
    kx = [p.local_x for p in kept]
    ky = [p.local_y for p in kept]
    cx, cy = (min(kx) + max(kx)) / 2, (min(ky) + max(ky)) / 2
    exposed = any(abs(q.local_x - cx) <= (max(kx) - min(kx)) / 4
                  and abs(q.local_y - cy) <= (max(ky) - min(ky)) / 4 for q in big)
    tol = 0.02

    def snap(vals):
        # Library coordinates jitter by a micron or two (IMX283: -9.001 and
        # -9.0 are one column); within tol, a value joins the one before it.
        out = {}
        for v in sorted(vals):
            prev = out[max(out)] if out else None
            out[v] = prev if prev is not None and v - prev <= tol else v
        return out
    sx = snap({p.local_x for p in kept})
    sy = snap({p.local_y for p in kept})
    pts = sorted({(sx[p.local_x], sy[p.local_y]) for p in kept})
    if len(pts) < 6:
        return None
    xs = sorted({x for x, _ in pts})
    ys = sorted({y for _, y in pts})
    x0, x1, y0, y1 = xs[0], xs[-1], ys[0], ys[-1]

    def steps(vals):
        v = sorted(vals)
        return [b - a for a, b in zip(v, v[1:])]

    def on_lattice(vals, pitch):
        return all(round(d / pitch) >= 1 and abs(d / pitch - round(d / pitch)) < 0.1
                   for d in steps(vals))

    interior = [(x, y) for x, y in pts
                if x0 + tol < x < x1 - tol and y0 + tol < y < y1 - tol]
    rows = {}
    cols = {}
    for x, y in pts:
        rows.setdefault(y, []).append(x)
        cols.setdefault(x, []).append(y)
    # An exposed pad makes it a QFN, however many rings of pads surround it.
    if (len(xs) >= 3 and len(ys) >= 3 and interior and not exposed
            and sum(len(r) >= 3 for r in rows.values()) >= 3
            and sum(len(c) >= 3 for c in cols.values()) >= 3):
        # Each axis has its own pitch (LPDDR packages: 0.8 x 0.65 mm); the
        # finer one is the part's escape pitch.
        px = _mode([d for r in rows.values() for d in steps(r)])
        py = _mode([d for c in cols.values() for d in steps(c)])
        # Two parallel pad rows a module's width apart (a compute module's
        # board-to-board connectors) also sit on a lattice, just not a square one.
        if (px and py and max(px, py) <= 2 * min(px, py)
                and on_lattice(xs, px) and on_lattice(ys, py)):
            return ('grid', min(px, py), min(len(xs), len(ys)) // 2)
    # A side counts only the pads strictly between the two perpendicular
    # edges: a two-row part's end pads sit on a corner, not on a side.
    mid_x = lambda x: x0 + tol < x < x1 - tol
    mid_y = lambda y: y0 + tol < y < y1 - tol
    sides = {'l': [y for x, y in pts if abs(x - x0) <= tol and mid_y(y)],
             'r': [y for x, y in pts if abs(x - x1) <= tol and mid_y(y)],
             't': [x for x, y in pts if abs(y - y0) <= tol and mid_x(x)],
             'b': [x for x, y in pts if abs(y - y1) <= tol and mid_x(x)]}
    if any(len(v) < 2 for v in sides.values()):
        return None
    d = sorted(s for v in sides.values() for s in steps(v) if s > 1e-3)
    if not d:
        return None
    pitch = d[len(d) // 2]
    depth = max(min(x - x0, x1 - x, y - y0, y1 - y) for x, y in pts)
    return ('perimeter', round(pitch, 3), None) if depth <= 1.5 * pitch + tol else None


def mezzanine_pitch(fp):
    """The pitch of FP when it is a dense board-to-board mezzanine -- at least
    MEZZANINE_PADS surface pads on at most four lines of one axis (two
    connectors of two rows), each line at MEZZANINE_PITCH or finer -- else
    None. Read in the footprint's own frame."""
    pads = [p for p in fp.pads if p.pad_type != 'np_thru_hole'
            and any(l.endswith('.Cu') for l in p.layers)]
    if len(pads) < MEZZANINE_PADS or any((p.drill or 0) > 0 for p in pads):
        return None
    for across, along in (('local_x', 'local_y'), ('local_y', 'local_x')):
        lines = {}
        for p in pads:
            lines.setdefault(round(getattr(p, across), 2), []).append(getattr(p, along))
        if len(lines) > 4:
            continue
        steps = sorted(b - a for v in lines.values()
                       for a, b in zip(sorted(v), sorted(v)[1:]))
        if steps and steps[len(steps) // 2] <= MEZZANINE_PITCH + 1e-3:
            return round(steps[len(steps) // 2], 3)
    return None


def fanout_census(pcb):
    """{ref: (tool or None, pitch or None, why)} for every footprint: the
    decision fanout_parts takes on it and the reason, in words."""
    out = {}
    for ref, fp in pcb.footprints.items():
        name = (fp.footprint_name or '').upper()
        hit = next((k for k in NOT_FANOUT_NAMES if k in name), None)
        if hit:
            out[ref] = (None, None, f'a connector, socket or magnetics by name ({hit})')
            continue
        pads = [p for p in fp.pads if p.pad_type != 'np_thru_hole']
        th = sum(1 for p in pads if (p.drill or 0) > 0)
        if th > len(pads) - th and 'PGA' not in name:
            out[ref] = (None, None, 'mostly through-hole pins')
            continue
        shape = pad_shape(fp)
        if shape is None:
            pitch = mezzanine_pitch(fp)
            if pitch:
                out[ref] = ('mezz', pitch, f'a dense mezzanine ({MEZZANINE_PADS}+ pads '
                                          f'at {pitch:g} mm): escaped under the pads')
            else:
                out[ref] = (None, None, 'neither a grid array nor a four-sided package')
            continue
        kind, pitch, depth = shape
        if kind == 'grid':
            if pitch < 2.0 and (depth > 2 or pitch <= 0.5):
                out[ref] = ('bga', pitch, f'a grid array at {pitch:g} mm pitch, '
                                          f'{depth} rows deep')
            else:
                out[ref] = (None, pitch, f'a grid array at {pitch:g} mm pitch only '
                                         f'{depth} rows deep: routed from its pads')
        elif pitch < 1.27 and (pitch <= 0.5 or (len(pads) > 40 and pitch <= 0.8)):
            out[ref] = ('qfn', pitch, f'a four-sided package at {pitch:g} mm pitch')
        else:
            out[ref] = (None, pitch, f'a four-sided package at {pitch:g} mm pitch: '
                                     f'coarse enough to route from its pads')
    return out


def fanout_parts(pcb):
    """[(ref, tool, pitch)] for the parts the skill fans out (Step 3): a grid
    array (bga_fanout), a four-sided perimeter package (qfn_fanout) or a dense
    mezzanine ('mezz', qfn_fanout under the pads), read from the pads
    themselves -- never a connector or magnetics by name."""
    return sorted((ref, tool, pitch) for ref, (tool, pitch, _why)
                  in fanout_census(pcb).items() if tool)


#: The smallest part the plan names when it is NOT fanned out, with why: a
#: two-pad passive is never a candidate, and listing them all says nothing.
NOT_FANNED_MIN_PADS = 16


def plane_layers(n):
    """Indices of the fewest inner layers of an N-layer stack such that
    every other layer is next to one: each signal layer then has a reference
    plane. The first such set in order, so the same stack always gets the
    same planes."""
    from itertools import combinations
    for k in range(1, n - 1):
        for combo in combinations(range(1, n - 1), k):
            if all(i in combo or i - 1 in combo or i + 1 in combo for i in range(n)):
                return list(combo)
    return list(range(1, n - 1))


def analyze(board, finish=False, plane_cost=PLANE_LAYER_COST, caller_power=(),
            keep_pours=False, fab_args=(), only=(), route_track=None, route_first=(),
            length_groups=(), time_groups=(), length_tol=None, time_tol=None,
            impedance=(), diff_pairs=None, cap_net_classes=False, layer_costs=None,
            via_cost=None):
    from kicad_parser import parse_kicad_pcb, pour_zones
    from list_nets import (read_design_rules, effective_floors,
                           find_power_nets, find_differential_pairs,
                           power_pin_parts, power_net_order, net_clearance_map_by_id)

    pcb = parse_kicad_pcb(board)
    layers = list(pcb.board_info.copper_layers)
    n_layers = len(layers)
    rules = read_design_rules(board)
    eff = rules.get('effective') or effective_floors(rules.get('constraints') or {}, n_layers)

    # The Default class routes at the fab floor (#1522); a net class declared
    # above it keeps its own clearance unless the caller caps every class.
    clearance = _num(eff['drc_clearance'])
    track = _num(max(eff['fab_track_width'], 0.1))
    via, drill = _num(eff['working_via_diameter']), _num(eff['working_via_drill'])
    fine_via, fine_drill = _num(eff['fine_via_diameter']), _num(eff['fine_via_drill'])

    pairs = []
    if diff_pairs is not None:
        pairs = _explicit_pairs(diff_pairs, {n.name for n in pcb.nets.values() if n.name},
                                finish)
    for pr in (find_differential_pairs(pcb) or []) if diff_pairs is None else ():
        p, n = (pr[0], pr[1]) if isinstance(pr, (tuple, list)) else (pr.p, pr.n)
        pairs += [getattr(p, 'name', p), getattr(n, 'name', n)]

    # Name-matched grounds and supplies, not find_power_nets' pad-count
    # candidates: those include keyboard matrix rows and unconnected pins.
    gnd, vcc, _cand = find_power_nets(pcb)
    # A rail no pattern names (V+, /CM5_3V3, /M2_3v3) still carries power
    # pins of two or more parts; power pins of one part are that part's own
    # supply tap, an enable or a connector's misdeclared pins, and a diff
    # pair is a pair whatever its pins say.
    named = {n for n, _ in gnd + vcc} | set(pairs)
    vcc = vcc + [(n.name, len(n.pads)) for n in pcb.nets.values()
                 if n.name and n.name not in named
                 and len(power_pin_parts(n)) >= 2]
    real = lambda t: t[1] >= 3 and not t[0].lower().startswith('unconnected-')
    # ties by name, never by board file order (#1755)
    gnd = sorted(filter(real, gnd), key=power_net_order)
    rails = sorted(filter(real, vcc), key=power_net_order)
    planes = []                         # [(net, layer)]
    if gnd:
        if n_layers >= 4:
            nets = [gnd[0][0]] * len(plane_layers(n_layers))
            if rails and rails[0][1] >= 10 and len(nets) > 1:
                nets[1] = rails[0][0]
            planes = [(n, layers[i]) for n, i in zip(nets, plane_layers(n_layers))]
        else:
            planes.append((gnd[0][0], layers[-1]))
    # route_planes takes EXACT names: a name argparse cannot carry stays a
    # power net (passed as a pattern) instead of a plane.
    planes = [(n, l) for n, l in planes if _exact_ok(n)]
    plane_nets = list(dict.fromkeys(n for n, _ in planes))
    power_nets = plane_nets + [n for n, _ in rails if n not in plane_nets]
    power_nets += [n for n, _ in gnd[1:] if n not in power_nets]

    if finish or keep_pours:
        # The board's own pours: their nets are power nets, and an INNER
        # layer carrying one is a plane the signals should keep off. Teardrops
        # are zones too, but routing output on a signal net, not a pour.
        # With keep_pours a full run takes these and pours nothing.
        zones = [z for z in pour_zones(pcb.zones) if not z.in_footprint and z.net_name]
        planes = sorted({(z.net_name, z.layer) for z in zones})
        plane_nets = sorted({n for n, _ in planes})
        power_nets = plane_nets + [n for n, _ in rails if n not in plane_nets]
        power_nets += [n for n, _ in gnd if n not in power_nets]
    solid = {l for _, l in planes if l not in (layers[0], layers[-1])}
    caller_costs = layer_costs
    if caller_costs is not None:
        if len(caller_costs) != n_layers:
            raise Refused(f"--layer-costs gives {len(caller_costs)} value(s) for a board "
                          f"of {n_layers} copper layer(s) ({' '.join(layers)}); give one "
                          f"per layer, in that order")
        layer_costs = [float(c) for c in caller_costs]
        if all(c < 0 for c in layer_costs):
            raise Refused(f"--layer-costs {' '.join(f'{c:g}' for c in layer_costs)} "
                          f"forbids tracks on every layer; give at least one layer a "
                          f"cost in [1, 1000]")
    elif n_layers == 2:
        layer_costs = [1.0, 1.5]
    else:
        layer_costs = [plane_cost if l in solid else 1.0 for l in layers]

    census = fanout_census(pcb)
    parts = sorted((ref, tool, pitch) for ref, (tool, pitch, _w) in census.items() if tool)
    fanout_why = {ref: census[ref][2] for ref, _t, _p in parts}
    not_fanned = sorted(
        ({'ref': ref, 'pads': n, 'why': census[ref][2]}
         for ref, fp in pcb.footprints.items() if not census[ref][0]
         for n in [sum(1 for p in fp.pads if p.pad_type != 'np_thru_hole')]
         if n >= NOT_FANNED_MIN_PADS),
        key=lambda d: (-d['pads'], d['ref']))
    fine = any(p <= GRID_FINE_PITCH for _, _, p in parts)
    grid = 0.05 if fine else 0.1
    n_nets = sum(1 for n in pcb.nets.values() if n.name and len(n.pads) >= 2)
    congested = n_nets > CONGESTED_NETS or any(t == 'bga' and p <= 0.8 for _, t, p in parts)

    # Every net the chain names by pattern, with the pattern that selects it
    # alone on this board. A net no pattern isolates is left out of the
    # lists that name it (a diff pair goes as a pair) and reported.
    names = sorted({n.name for n in pcb.nets.values() if n.name})
    scope = None
    if only:
        # The nets the caller's patterns select, as route.py's own --nets
        # selects them, are the only ones any step routes: a pour, a power
        # net and a pair outside them are left as the board has them. The
        # layer costs above still keep signals off every plane layer.
        from net_queries import expand_net_patterns
        scope = sorted(set(expand_net_patterns(pcb, list(only), quiet=True)) & set(names))
        if not scope:
            raise Refused('--nets: the patterns select no net on this board')
        planes = [(n, l) for n, l in planes if n in scope]
        plane_nets = [n for n in plane_nets if n in scope]
        power_nets = [n for n in power_nets if n in scope]
        outside = [n for n in pairs if n not in scope] if diff_pairs is not None else []
        if outside:
            raise Refused(f"--diff-pair: {', '.join(outside)} not among the nets "
                          f"this run routes")
        pairs = [n for i in range(0, len(pairs) - 1, 2) for n in pairs[i:i + 2]
                 if pairs[i] in scope and pairs[i + 1] in scope]
    widths = None
    if caller_power:
        # Each net the caller's --power-nets give a width, as route.py's own
        # selector reads them (first matching pattern), among the routed nets.
        from net_queries import identify_power_nets
        ids = identify_power_nets(pcb, [p for p, _ in caller_power],
                                  [w for _, w in caller_power])
        widths = {pcb.nets[i].name: w for i, w in ids.items()
                  if scope is None or pcb.nets[i].name in scope}
    globs, unnamed = {}, []
    for n in dict.fromkeys(plane_nets + power_nets + pairs):
        g = _glob(n)
        if _selects_exactly(g, n, names):
            globs[n] = g
        else:
            unnamed.append(n)
    if diff_pairs is not None and any(n not in globs for n in pairs):
        raise Refused(f"--diff-pair: no pattern selects only "
                      f"{', '.join(n for n in pairs if n not in globs)} on this board")
    if unnamed:
        print(f"AUTOROUTE: no pattern selects only {', '.join(unnamed)} on this "
              f"board; left out of the power, exclusion and pair lists", flush=True)
    power_nets = [n for n in power_nets if n in globs]
    pairs = [n for i in range(0, len(pairs) - 1, 2) for n in pairs[i:i + 2]
             if pairs[i] in globs and pairs[i + 1] in globs]
    first = steps_scope = None
    declared = net_clearance_map_by_id(board, {nid: n.name for nid, n in pcb.nets.items()
                                               if n.name})
    above = {pcb.nets[nid].name: _num(c) for nid, c in declared.items()
             if c > clearance + 1e-9}
    if route_first or length_groups or time_groups or impedance or above:
        # Against the nets route.py's route steps route: the caller's --nets,
        # else '*' as route.py reads it (no single-pad unconnected-* net).
        from net_queries import expand_net_patterns
        steps_scope = scope if scope is not None else sorted(
            set(expand_net_patterns(pcb, ['*'], quiet=True)) & set(names))
    if route_first:
        first = _route_first(pcb, route_first, names, steps_scope)
    above = {n: c for n, c in above.items() if n in set(steps_scope or ())}
    for n in above:
        if _selects_exactly(_glob(n), n, names):
            globs.setdefault(n, _glob(n))
        elif n not in unnamed:
            unnamed.append(n)
    targets = _impedance_targets(pcb, impedance, steps_scope, pairs, names, finish,
                                 track, max(0.1, clearance)) if impedance else {}
    for n in targets:
        globs.setdefault(n, _glob(n))
    groups = _match_groups(
        [('length', g) for g in length_groups] + [('time', g) for g in time_groups],
        {'length': length_tol, 'time': time_tol}, steps_scope, pairs, targets,
        names, finish, tier_of=None if cap_net_classes else {
            n: f'route_class_{c:g}' for n, c in above.items()
            if _tiered(n, globs, plane_nets)}
    ) if (length_groups or time_groups) else []

    out = dict(layers=layers, clearance=clearance, track=track, via=via,
               drill=drill, fine_via=fine_via, fine_drill=fine_drill, planes=planes,
               plane_nets=plane_nets, power_nets=power_nets,
               layer_costs=layer_costs, parts=parts, grid=grid,
               congested=congested, n_nets=n_nets, pair_nets=pairs,
               globs=globs,
               # Why, for a reader (AUTOROUTE_PLAN, the plugin's tab): where
               # the rules came from, why each part is fanned out or not,
               # and the grounds and rails found by name and power pins.
               rules_source=(os.path.basename(rules['source']) if rules.get('source')
                             else None),
               fanout_why=fanout_why, not_fanned=not_fanned,
               grounds=[n for n, _ in gnd], rails=[n for n, _ in rails],
               unselectable=unnamed)
    if widths is not None:
        out['caller_power_nets'] = [[p, w] for p, w in caller_power]
        out['net_widths'] = widths
    if scope is not None:
        out['nets'] = scope
        out['net_patterns'] = list(only)
    if route_track is not None:
        out['route_track'] = route_track
    if first is not None:
        out['route_first'] = first
        out['route_first_patterns'] = list(route_first)
    if diff_pairs is not None:
        out['diff_pairs'] = [pairs[i:i + 2] for i in range(0, len(pairs), 2)]
    if targets:
        out['net_impedance'] = targets
    if groups:
        out['match_groups'] = groups
        for mode, tol in (('length', length_tol), ('time', time_tol)):
            if tol is not None:
                out[f'{mode}_match_tolerance'] = tol
    if keep_pours:
        out['keep_pours'] = True
    if fab_args:
        out['fab_args'] = list(fab_args)
    if above:
        out['class_clearances'] = dict(sorted(above.items()))
        out['tiers_cover_scope'] = all(_tiered(n, globs, plane_nets) if n in above
                                       else False for n in steps_scope)
    if cap_net_classes:
        out['cap_net_classes'] = True
    if caller_costs is not None:
        out['caller_layer_costs'] = list(layer_costs)
    if via_cost is not None:
        top = max_via_cost(grid)
        if via_cost > top:
            raise Refused(f"--via-cost {via_cost} is above {top}, the most whose "
                          f"cost the router holds at the {grid:g} mm grid (route_diff "
                          f"doubles it)")
        out['via_cost'] = via_cost
    return out


def max_via_cost(grid):
    """The largest --via-cost whose per-via cost (routing_config.
    via_cost_units), doubled as route_diff.py doubles it for a pair, is a
    32-bit signed integer, the router's cost type, at GRID mm."""
    from routing_config import REFERENCE_GRID_STEP
    return int((2 ** 31 - 1) // (2 * 1000 * REFERENCE_GRID_STEP / grid))


def _tiered(net, globs, plane_nets):
    """Whether a net of a class above the chain clearance routes in its
    class's own call: a pattern selects it alone, and it is no plane net,
    which the route passes route so that their plane finalize serves it."""
    return net in globs and net not in plane_nets


def honours_classes(a):
    """True when the chain routes the net classes A declares above its
    clearance at their own clearance: some class is, and the caller did not
    cap every class (--cap-net-classes). A class net outside every class
    call (`class_tiers`) routes in the passes, at its own clearance."""
    return bool(a.get('class_clearances')) and not a.get('cap_net_classes')


def class_tiers(a):
    """[(tag, clearance, nets)]: one route.py call per class clearance above
    the chain's, widest first, for the nets of that class a pattern selects,
    when the chain honours the classes; else []. route.py prices every
    obstacle at the widest class it routes in one call, so a net of a wider
    class routed with the others would hold every net of the call at its
    clearance; routed in a call of its own, each pair is priced at the wider
    of its two classes."""
    if not honours_classes(a):
        return []
    by = {}
    for n, c in a['class_clearances'].items():
        if _tiered(n, a['globs'], a['plane_nets']):
            by.setdefault(c, []).append(n)
    return [(f'route_class_{c:g}', c, by[c]) for c in sorted(by, reverse=True)]


def _route_first(pcb, patterns, names, scope):
    """The nets PATTERNS select among SCOPE, the route steps' nets, in the
    order route.py's --route-first reads them (net_ordering.
    resolve_route_first: each pattern's own nets by name, each net once, a
    wildcard's only inside SCOPE). A pattern that selects no net, an
    exclusion, a wildcard that selects none of SCOPE, a name that selects a
    net outside it and a net no pattern isolates are refused."""
    from net_ordering import resolve_route_first
    try:
        first = resolve_route_first(pcb, list(patterns), scope)
    except ValueError as exc:
        raise Refused(str(exc)) from None
    alone = [n for n in first if not _selects_exactly(_glob(n), n, names)]
    if alone:
        raise Refused(f"--route-first: no pattern selects only {', '.join(alone)} on this board")
    return first


def _explicit_pairs(given, names, finish):
    from net_queries import extract_diff_pair_base
    if finish:
        raise Refused('--diff-pair: --finish runs no diff-pair step')
    flat = []
    for p, n in given:
        for x in (p, n):
            if x not in names:
                raise Refused(f'--diff-pair {p} {n}: {x} is no net on this board')
            if x in flat:
                raise Refused(f'--diff-pair: {x} is in two pairs')
            flat.append(x)
        bp, bn = extract_diff_pair_base(p), extract_diff_pair_base(n)
        if not (bp and bn and bp[1] and not bn[1] and bp[0] == bn[0] and bp[2] == bn[2]):
            raise Refused(
                f'--diff-pair {p} {n}: route_diff.py pairs nets by their names only, '
                f'and these are not the positive and the negative half of one such '
                f'pair (P/N, +/-, _t/_c, DP/DM, positive first)')
    return flat


def _impedance_targets(pcb, given, scope, pairs, names, finish, track, pair_gap):
    from net_queries import expand_net_patterns
    if finish:
        raise Refused('--net-impedance: --finish routes only the nets still open, so '
                      'a routed net would keep its width')
    in_pair = {n: pairs[i ^ 1] for i, n in enumerate(pairs)}
    out = {}
    for pattern, ohms in given:
        label = f'--net-impedance {pattern} {ohms:g}'
        if pattern.startswith('!'):
            raise Refused(f'{label}: an exclusion selects no net')
        sel = sorted(set(expand_net_patterns(pcb, [pattern], quiet=True)) & set(scope))
        if not sel:
            raise Refused(f'{label}: selects none of the nets this run routes')
        if any(n in in_pair for n in sel) and any(n not in in_pair for n in sel):
            raise Refused(f'{label}: selects pair nets (a differential target) and '
                          f'single-ended nets (a single-ended target); give each '
                          f'kind its own pattern')
        for n in sel:
            if n in out and out[n]['ohms'] != ohms:
                raise Refused(f"--net-impedance: {n} is given {out[n]['ohms']:g} "
                              f"and {ohms:g} ohm")
            out[n] = {'ohms': ohms, 'differential': n in in_pair}
    for n, spec in sorted(out.items()):
        if spec['differential']:
            if (out.get(in_pair[n]) or {}).get('ohms') != spec['ohms']:
                raise Refused(f'--net-impedance: {n} and {in_pair[n]} are one pair, '
                              f'which route_diff.py routes at one differential '
                              f'target; give both halves the same ohms')
        elif not _selects_exactly(_glob(n), n, names):
            raise Refused(f'--net-impedance: no pattern selects only {n} on this board')
    if not pcb.board_info.stackup:
        raise Refused('--net-impedance: the board declares no stackup, so no width '
                      'can be solved for a target')
    from impedance import calculate_layer_widths_for_impedance
    layers = list(pcb.board_info.copper_layers)
    solved = {}
    for n, spec in out.items():
        key = (spec['ohms'], spec['differential'])
        if key not in solved:
            unsolved = []
            solved[key] = calculate_layer_widths_for_impedance(
                pcb, layers, spec['ohms'], spacing=pair_gap if key[1] else 0.0,
                is_differential=key[1], fallback_width=track, unsolved_report=unsolved)
            if unsolved:
                raise Refused(f"--net-impedance: the stackup solves no width for "
                              f"{spec['ohms']:g} ohm on {', '.join(unsolved)}")
        spec['widths'] = {l: round(w, 4) for l, w in solved[key].items()}
        if key[1]:
            spec['pair_gap'] = pair_gap
    return out


def _lm_pattern(name):
    s = name.replace('[', '[[]')
    return '[-]' + s[1:] if s.startswith('-') else s


def _owner_text(key):
    tool, ohms = key
    if tool == 'route_diff':
        return 'pairs by route_diff.py' + (f' at {ohms:g} ohm' if ohms else '')
    if tool.startswith('route_class_'):
        return f"single-ended by route.py's {tool} step"
    return ('single-ended by ' + (f"route.py's {ohms:g} ohm step" if ohms
                                  else "route.py's route passes"))


def _match_groups(groups, tols, scope, pairs, targets, names, finish, tier_of=None):
    from length_matching import find_nets_matching_patterns
    import routing_defaults as defaults
    in_pair = set(pairs)
    partner = {n: pairs[i ^ 1] for i, n in enumerate(pairs)}
    flag = {'length': '--length-match-group', 'time': '--time-match-group'}
    out, seen = [], {}
    for mode, g in groups:
        label = f"{flag[mode]} {' '.join(g)}"
        if any(p.lower() == 'auto' for p in g):
            raise Refused(f"{label}: 'auto' forms its DRAM groups inside a step, so "
                          f"autoroute cannot tell which step routes them; name the "
                          f"group's nets")
        for p in g:
            board = find_nets_matching_patterns(list(names), [p])
            if not board:
                raise Refused(f'{label}: {p} selects no net')
            outside = sorted(set(board) - set(scope))
            if outside:
                raise Refused(f"{label}: {p} selects {', '.join(outside)}, outside "
                              f"the nets this run routes")
        nets = sorted(find_nets_matching_patterns(list(scope), list(g)))
        if len(nets) < 2:
            raise Refused(f'{label}: selects {len(nets)} of the nets this run routes; '
                          f'a group needs two')
        for n in nets:
            if n in seen:
                raise Refused(f'{label}: {n} is in {seen[n]} too')
            seen[n] = label
        owners = {}
        for n in nets:
            ohms = (targets.get(n) or {}).get('ohms')
            key = ('route_diff' if n in in_pair else
                   (tier_of or {}).get(n, 'route') if ohms is None else 'route', ohms)
            owners.setdefault(key, []).append(n)
        if len(owners) > 1:
            split = '; '.join(f"{_owner_text(k)}: {', '.join(v)}"
                              for k, v in sorted(owners.items(), key=lambda kv: (
                                  kv[0][0] != 'route_diff', kv[0][1] or 0)))
            raise Refused(
                f'{label}: its nets are routed by different steps ({split}). A step '
                f'lengthens only the nets it routes -- copper an earlier step laid '
                f'sets the target but is never lengthened -- so the group cannot be '
                f'matched across steps; give each step\'s nets their own group'
                + ('' if not any(k[0].startswith('route_class_') for k in owners) else
                   ', or pass --cap-net-classes, which routes the class\'s nets in '
                   'the route passes with the others (capped at the chain clearance)'))
        (tool, ohms), = owners
        if tool == 'route_diff' and finish:
            raise Refused(f'{label}: --finish runs no diff-pair step, and the '
                          f'group\'s nets are pairs route_diff.py routes')
        if tool == 'route_diff':
            members = {min(n, partner[n]) for n in nets}
            if len(members) < 2:
                raise Refused(f'{label}: selects {len(members)} pair, which '
                              f'route_diff.py matches as one member; a group needs two')
        pats = [_lm_pattern(n) for n in nets]
        loose = [n for n, p in zip(nets, pats)
                 if find_nets_matching_patterns(list(names), [p]) != [n]]
        if loose:
            raise Refused(f"{label}: no pattern selects only {', '.join(loose)} "
                          f"through the length-match matcher on this board")
        tol = tols[mode]
        if tol is None:
            tol = (defaults.LENGTH_MATCH_TOLERANCE if mode == 'length'
                   else defaults.TIME_MATCH_TOLERANCE)
        out.append({'mode': mode, 'patterns': list(g), 'nets': nets, 'tolerance': tol,
                    'unit': 'mm' if mode == 'length' else 'ps', 'step': tool,
                    'impedance': ohms, 'step_patterns': pats})
    carried = {}
    for grp in out:
        if grp['step'] == 'route_diff':
            recv = [f"route_diff.py's call" + (f" at {grp['impedance']:g} ohm"
                                                if grp['impedance'] else '')]
        elif grp['step'] != 'route':
            recv = [f"route.py's {grp['step']} step"]
        else:
            recv = ["route.py's route passes"] + (
                [f"route.py's {grp['impedance']:g} ohm step"] if grp['impedance'] else [])
        for r in recv:
            carried.setdefault(r, {}).setdefault(grp['mode'], []).append(grp)
    for r, modes in carried.items():
        if len(modes) > 1:
            raise Refused(
                f"{r} would carry length group(s) "
                f"{'; '.join(' '.join(x['patterns']) for x in modes['length'])} and "
                f"time group(s) {'; '.join(' '.join(x['patterns']) for x in modes['time'])}, "
                f"and its --time-matching applies to every group of a call; match "
                f"them in separate runs")
    return out


def match_args(a, groups):
    if not groups:
        return []
    args = []
    for g in groups:
        args += ['--length-match-group', *g['step_patterns']]
    mode = groups[0]['mode']
    if mode == 'time':
        args.append('--time-matching')
    tol = a.get(f'{mode}_match_tolerance')
    if tol is not None:
        args += [f'--{mode}-match-tolerance', str(tol)]
    return args


# --------------------------------------------------------------------------
# Running steps


#: Prints the nets check_connected leaves incomplete (unrouted or broken),
#: one JSON list on its own line; run in a child so its output and state stay
#: out of this process.
_CONN_PROBE = (
    "import contextlib, io, json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from check_connected import run_connectivity_check\n"
    "with contextlib.redirect_stdout(io.StringIO()):\n"
    "    issues = run_connectivity_check(sys.argv[2], quiet=True)\n"
    "print('AUTOROUTE_OPEN: ' + json.dumps(sorted({i['net_name'] for i in issues"
    " if i.get('net_name')})))\n")


def _recorded_clearance(board):
    """The clearance BOARD's .kicad_pro records, or None. check_drc grades at
    the board's own recorded floors when there is one -- copper a step routed
    below the chain's clearance (a fine tap) is legal there -- and only
    otherwise at the chain's clearance, as the corpus grader does."""
    from fix_kicad_drc_settings import project_copper_clearance
    try:
        with open(board[:-len('.kicad_pcb')] + '.kicad_pro') as fh:
            return project_copper_clearance(json.load(fh))
    except (OSError, ValueError):
        return None


def _child_env():
    env = dict(os.environ)
    env.pop('REDO_MANIFEST', None)   # the parent records the one invocation
    return env


def emit(token, payload):
    """One machine-readable line: TOKEN, a colon, PAYLOAD as JSON."""
    print(f"{token}: " + json.dumps(payload, sort_keys=True, default=str), flush=True)


#: Lines of a failed step's log its ending event carries.
ERROR_TAIL_LINES = 15

#: Fault injection for the tests of a failed step (this file's and the
#: plugin tab's): the steps named in KRT_AUTOROUTE_FAIL_STEPS (tags, comma
#: separated) run a stand-in that prints FAIL_STEP_MESSAGE and exits 3, so
#: the chain's failure path runs on a real board. Unset, nothing changes.
FAIL_STEPS_ENV = 'KRT_AUTOROUTE_FAIL_STEPS'
FAIL_STEP_MESSAGE = 'step failed on purpose (KRT_AUTOROUTE_FAIL_STEPS)'


def _fail_steps():
    return {s.strip() for s in os.environ.get(FAIL_STEPS_ENV, '').split(',') if s.strip()}

#: place_fanout_clearance's own summary line (pinned byte for byte by its
#: tests); it prints a JSON_SUMMARY only when given an intent.
_DECAP_MOVED = re.compile(r'^Moved (\d+) cap\(s\); resolved (\d+)/(\d+) initial '
                          r'violations; (\d+) unresolved', re.M)


def _tail(text, n=ERROR_TAIL_LINES):
    lines = [ln for ln in (text or '').splitlines() if ln.strip()]
    return lines[-n:]


def step_summary(kind, text, summary, summary_min):
    """(summary dict, one-line result) for a finished step of tool KIND, from
    its log TEXT and the JSON_SUMMARY / JSON_SUMMARY_MIN it printed: the
    numbers a reader needs to judge the step, not the whole summary."""
    s = summary or {}
    if kind in ('bga', 'qfn'):
        d = {k: s[k] for k in ('requested', 'escaped', 'failed', 'escape_method',
                               'via_size', 'via_drill') if k in s}
        d['grazes'] = rung_grazes(s)
        d['unescaped_nets'] = len(s.get('unescaped_nets') or ())
        if not s:
            return d, 'no fanout summary printed'
        return d, (f"escaped {d.get('escaped', 0)}/{d.get('requested', 0)}, "
                   f"{d.get('failed', 0)} dropped, {d['grazes']} graze(s)")
    if kind == 'planes':
        zones = [f"{z.get('net')} on {z.get('layer')}" for z in s.get('plane_zones') or ()]
        d = {'zones': zones, 'status': s.get('status')}
        return d, ('poured ' + ', '.join(zones)) if zones else 'no zone poured'
    if kind == 'decap':
        m = _DECAP_MOVED.search(text or '')
        if not m:
            if 'No caps moved' in (text or ''):
                # its early exit: no fanout via on the board to clear caps off
                return {'moved': 0}, 'moved 0 caps: no fanout via to clear them off'
            return {}, 'no summary line'
        d = dict(zip(('moved', 'resolved', 'initial', 'unresolved'),
                     map(int, m.groups())))
        return d, (f"moved {d['moved']} cap(s), resolved {d['resolved']}/"
                   f"{d['initial']}, {d['unresolved']} unresolved")
    if kind == 'diff':
        if not s:
            if 'nothing to route' in (text or ''):
                return {}, 'nothing to route: every pair already connected'
            return {}, 'no summary printed'

        def n(k):
            return len(s.get(k) or ())
        d = {'routed': n('routed_diff_pairs'), 'failed': n('failed_diff_pairs'),
             'single_ended': n('single_ended_diff_pairs'),
             'uncoupled': n('uncoupled_diff_pairs'),
             'followup_nets': list(s.get('single_ended_followup_nets') or ()),
             'vias': s.get('total_vias')}
        return d, (f"{d['routed']} pair(s) routed, {d['failed']} failed"
                   + (f", {d['single_ended']} left to the route step"
                      if d['single_ended'] else ''))
    if kind == 'route':
        m = summary_min or {}
        d = {k: m[k] for k in ('routed', 'failed', 'vias', 'failed_single',
                               'open_single', 'multipoint_deficit') if k in m}
        if 'pad_pairs_open' in m:
            d['open_nets'] = list((m['pad_pairs_open'] or {}).get('nets') or ())
        if not m:
            return d, 'no route summary printed'
        return d, (f"routed {d.get('routed', 0)}, {d.get('failed', 0)} failed, "
                   f"{d.get('vias', 0)} via(s)")
    return {}, ''


class ChainEvents:
    """A chain's event side: the steps that started, the planned steps that
    will not and why, the decisions it made, and a step whose ending waits on
    its grade. Its state is made on first use, so a scripted chain in a test
    gets it by inheriting this class alone."""

    held = None                     # a step whose ending waits on its grade

    @property
    def started(self):              # tags with a `running` event
        return self.__dict__.setdefault('_started', set())

    @property
    def skipped(self):              # planned tag -> why it did not run
        return self.__dict__.setdefault('_skipped', {})

    @property
    def decisions(self):            # AUTOROUTE_DECISION payloads, in order
        return self.__dict__.setdefault('_decisions', [])

    def skip(self, tag, reason):
        """A planned step that will not run, and why (emitted by main)."""
        self.skipped.setdefault(tag, reason)

    def decide(self, tag, kept, why, **extra):
        """A choice the chain made about step TAG -- the fanout rung it kept,
        a re-fan or route pass kept or not -- and why: AUTOROUTE_DECISION."""
        self.end_step()
        d = dict(extra, tag=tag, kept=kept, why=why)
        self.decisions.append(d)
        emit('AUTOROUTE_DECISION', d)

    def end_step(self, result=None, **extra):
        """Emit the held step's ending, with RESULT and EXTRA (a grade)."""
        ev, self.held = self.held, None
        if ev is None:
            return
        if result is not None:
            ev['result'] = result
        ev.update(extra)
        self.steps[ev.pop('_index')].update(
            {k: ev[k] for k in ('result', 'kept') if k in ev})
        emit('AUTOROUTE_STEP', ev)


class Chain(ChainEvents):
    def __init__(self, board, work, clearance=None, baseline=None, fab_args=()):
        self.work = work
        self.fab_args = list(fab_args)
        self.cur = os.path.abspath(board)
        self.n = 0
        self.steps = []
        self.last_iterations = None
        self.clearance = clearance
        self.baseline = baseline

    def grade(self, board):
        """{'open': n, 'open_nets': [...], 'drc': n} for BOARD, or None when a
        checker could not grade it.

        open: the nets check_connected reports unrouted or broken, every one
        (no capped name list, no pad counts), with its KiCad cross-check off
        so the same board always grades the same. drc: check_drc's real
        violations at the board's recorded floors (else the chain's
        clearance), the chain's input as baseline."""
        env = _child_env()
        env['KICAD_NO_GRADE_RECONCILE'] = '1'
        r = subprocess.run([sys.executable, '-X', 'utf8', '-c', _CONN_PROBE, HERE, board],
                           capture_output=True, text=True, env=env)
        m = re.search(r'^AUTOROUTE_OPEN: (\[.*\])$', r.stdout, re.M)
        if r.returncode != 0 or not m:
            print(f"  grade: check_connected failed on {os.path.basename(board)}",
                  flush=True)
            return None
        open_nets = json.loads(m.group(1))
        d = self.check_drc(board)
        if d is None:
            return None
        return {'open': len(open_nets), 'open_nets': open_nets, 'drc': int(d['violations']),
                'drc_items': _named_items(d)}

    def check_drc(self, board):
        """check_drc's JSON report for BOARD at the board's recorded floors
        (else the chain's clearance), the chain's input as baseline; None when
        it could not grade."""
        fd, jpath = tempfile.mkstemp(suffix='.json', dir=self.work)
        os.close(fd)
        argv = [sys.executable, '-X', 'utf8', os.path.join(HERE, 'check_drc.py'),
                board, '--quiet', '--json', jpath]
        if self.clearance is not None and _recorded_clearance(board) is None:
            argv += ['--clearance', str(self.clearance)]
        if self.baseline:
            argv += ['--baseline', self.baseline]
        argv += self.fab_args
        subprocess.run(argv, capture_output=True, text=True, env=_child_env())
        try:
            with open(jpath) as fh:
                d = json.load(fh)
            int(d['violations'])
            return d
        except (OSError, ValueError, KeyError, TypeError):
            print(f"  grade: check_drc failed on {os.path.basename(board)}", flush=True)
            return None
        finally:
            os.remove(jpath)

    def drc_nets(self, board):
        """The nets named in BOARD's DRC violations that involve a track or
        via -- copper a routing step lays -- or None when ungraded. An item
        check_drc accepts (a protected via in a pad) is no violation."""
        d = self.check_drc(board)
        if d is None:
            return None
        return {n for it in d.get('items') or []
                if not it.get('accepted')
                and any(k in it.get('type', '') for k in ('segment', 'via', 'track'))
                for n in (it.get('net1'), it.get('net2')) if n}

    def run(self, kind, args, tag, parent=None, hold=False):
        """Run one tool from the current board to a fresh output; returns
        (output path or None, JSON_SUMMARY dict or None, JSON_SUMMARY_MIN).
        ``last_iterations`` is the router iterations its JSON_SUMMARY lines
        report, summed -- the same effort count the replay harness uses --
        or None when the run printed no JSON_SUMMARY at all. '{JSON}' in
        ARGS is <output stem>.summary.json.

        Emits the step's `running` event, and its ending -- unless HOLD and
        the step produced a board, when the caller ends it with end_step
        once it is graded. PARENT names the planned row a step the plan did
        not list belongs to (a fanout rung, a re-fan)."""
        self.end_step()
        self.n += 1
        out = os.path.join(self.work, f'step{self.n:02d}_{tag}.kicad_pcb')
        js = out[:-len('.kicad_pcb')] + '.summary.json'
        argv = [sys.executable, '-u', '-X', 'utf8', TOOLS[kind]] + \
            [a.replace('{IN}', self.cur).replace('{OUT}', out).replace('{JSON}', js)
             for a in args] + (self.fab_args if kind in FAB_STEPS else [])
        log = out[:-len('.kicad_pcb')] + '.log'
        ev = {'n': self.n, 'tag': tag, 'tool': kind, 'parent': parent,
              'cmd': [os.path.basename(TOOLS[kind])] + argv[5:], 'log': log,
              'board': out, 'input': self.cur}
        self.started.add(tag)
        emit('AUTOROUTE_STEP', dict(ev, state='running', t=round(time.time(), 3)))
        if tag in _fail_steps():
            argv = [sys.executable, '-c', 'import sys; print("' + FAIL_STEP_MESSAGE
                    + '", file=sys.stderr); sys.exit(3)']
        t0 = time.time()
        with open(log, 'w') as fh:
            rc = subprocess.run(argv, cwd=self.work, stdout=fh,
                                stderr=subprocess.STDOUT, env=_child_env()).returncode
        seconds = round(time.time() - t0, 1)
        text = open(log, errors='replace').read()
        summary = summary_min = None
        self.last_iterations = None
        for m in re.finditer(r'^JSON_SUMMARY: (\{.*\})$', text, re.M):
            try:
                summary = json.loads(m.group(1))
            except ValueError:
                continue
            self.last_iterations = ((self.last_iterations or 0)
                                    + int(summary.get('total_iterations') or 0))
        m = re.search(r'^JSON_SUMMARY_MIN: (\{.*\})$', text, re.M)
        if m:
            try:
                summary_min = json.loads(m.group(1))
            except ValueError:
                pass
        ok = rc == 0 and os.path.exists(out)
        compact, result = step_summary(kind, text, summary, summary_min)
        if not ok:
            # A failed step's numbers describe no board the chain keeps.
            compact = {}
            result = ((f"exit code {rc}" if rc else "exit code 0 but no output board")
                      + "; the chain continues from the board before it")
        self.steps.append({'tool': kind, 'tag': tag, 'rc': rc, 'ok': ok,
                           'iterations': self.last_iterations,
                           'argv': argv[4:], 'log': os.path.basename(log),
                           'seconds': seconds, 'summary': compact,
                           'result': result})
        print(f"  [{self.n:02d}] {tag}: rc={rc}{'' if ok else ' (skipped)'}",
              flush=True)
        end = dict(ev, state='done' if ok else 'failed', rc=rc, seconds=seconds,
                   summary=compact, result=result, iterations=self.last_iterations,
                   t=round(time.time(), 3))
        if not ok:
            end['board'] = None
            end['error_tail'] = _tail(text)
        if hold and ok:
            self.held = dict(end, _index=len(self.steps) - 1)
        else:
            emit('AUTOROUTE_STEP', end)
        return (out if ok else None), summary, summary_min

    def accept(self, out):
        if out:
            self.cur = out


def diff_calls(a):
    """[(tag, ohms or None, pair nets)]: one route_diff.py call per
    differential impedance target among the pairs, the pairs without one
    first, as run_plan runs them and AUTOROUTE_PLAN lists them."""
    imp = a.get('net_impedance') or {}
    calls = {}
    for n in a['pair_nets']:
        calls.setdefault((imp.get(n) or {}).get('ohms'), []).append(n)
    return [('diffpairs' + (f'_{o:g}ohm' if o else ''), o, calls[o])
            for o in sorted(calls, key=lambda k: (k is not None, k or 0))]


def impedance_steps(a):
    """[(tag, ohms, nets)]: one route.py --impedance step per single-ended
    impedance target, ahead of the route passes, as route_passes runs them
    and AUTOROUTE_PLAN lists them."""
    imp = {}
    for n, spec in sorted((a.get('net_impedance') or {}).items()):
        if not spec['differential']:
            imp.setdefault(spec['ohms'], []).append(n)
    return [(f'impedance_{o:g}ohm', o, imp[o]) for o in sorted(imp)]


def _matched(a, step, ohms=...):
    """'; length-matches GROUP ...' for the match groups STEP's call at
    OHMS carries (every STEP group when OHMS is not given: the route passes
    carry them all), for AUTOROUTE_PLAN's why; else ''."""
    gs = [g for g in a.get('match_groups') or ()
          if g['step'] == step and (ohms is ... or g['impedance'] == ohms)]
    return ('; ' + '; '.join(f"{g['mode']}-matches {' '.join(g['patterns'])} "
                             f"within {g['tolerance']:g} {g['unit']}" for g in gs)
            if gs else '')


def plan_steps(a, finish=False, effort_factor=2.0, quick=0):
    """AUTOROUTE_PLAN's payload: the design rules the chain routes at and the
    steps it means to run, in order, as {tag, tool, what, why, optional}. A
    step marked optional runs only if what comes before it calls for it;
    steps a run adds (a fanout's next rung, a re-fan, the pair re-route)
    are not listed, and appear in the events with their planned row as
    parent."""
    rows = []

    def row(tag, tool, what, why, optional=False):
        rows.append({'tag': tag, 'tool': tool, 'what': what, 'why': why,
                     'optional': optional})
    pours = ', '.join(f'{n} on {l}' for n, l in a['planes'])
    if finish:
        row('input', 'grade', 'grade the input board',
            'it is itself a candidate: a route pass ships only if it beats it')
    else:
        if a['planes'] and not a.get('keep_pours'):
            row('planes', 'planes', f'pour {pours}',
                'a GND pour on the bottom of a 2-layer board' if len(a['layers']) == 2
                else 'the fewest inner planes that put one next to every signal '
                     'layer; GND, then the dominant rail')
        for ref, tool, pitch in a['parts']:
            row(f'fanout_{ref}', 'bga' if tool == 'bga' else 'qfn',
                f"fan out {ref} with {'bga_fanout' if tool == 'bga' else 'qfn_fanout'}"
                + (' under the pads' if tool == 'mezz' else ''),
                a.get('fanout_why', {}).get(ref, '') + '; a rung that drops escapes '
                'or grazes is retried with the next')
        if a['parts']:
            row('decap', 'decap', 'move decoupling caps off the fanout copper '
                '(place_fanout_clearance)',
                'once, after the fanouts, when one escaped a ball; a part it leaves '
                'shorted to a BGA\'s copper re-fans that BGA holding the part',
                optional=True)
        for tag, ohms, nets in diff_calls(a):
            row(tag, 'diff',
                f"route {len(nets) // 2} differential pair(s) with route_diff"
                + (f' at {ohms:g} ohm (--net-impedance)' if ohms else ''),
                ('pairs given by --diff-pair' if 'diff_pairs' in a else 'pairs found by name')
                + ', routed before the single nets; a pair net left in a DRC violation '
                'is routed again single-ended' + _matched(a, 'route_diff', ohms))
        for tag, ohms, nets in impedance_steps(a):
            row(tag, 'route', f"route {', '.join(nets[:6])}"
                + (' ...' if len(nets) > 6 else '') + f' at {ohms:g} ohm (--net-impedance)',
                'a single-ended impedance target: route.py --impedance solves its '
                'widths from the stackup, on its own ahead of the route passes'
                + _matched(a, 'route', ohms))
    tiered = set()
    first = a.get('route_first') or []

    def firsts(nets):
        return (', first ' + ', '.join(nets[:6]) + (' ...' if len(nets) > 6 else '')
                + ' (--route-first)' if nets else '')
    for tag, clr, nets in class_tiers(a):
        tiered |= set(nets)
        row(tag, 'route', f"route {', '.join(nets[:6])}" + (' ...' if len(nets) > 6 else '')
            + f' at their net class clearance {clr:g} mm',
            'a net class declared above the chain clearance, routed in a call of its '
            'own ahead of the route passes, which leave its nets out, and again '
            'while one of them is open: route.py holds every net of a call at the '
            'widest class it routes' + firsts([n for n in first if n in nets])
            + _matched(a, tag))
    row('route', 'route', ('route the nets --nets selects' if a.get('nets') else
                           'route every net') + (' still open' if finish else ''),
        f"grid {a['grid']:g} mm"
        + (f", tracks at {a['route_track']:g} mm (--track-width)"
           if a.get('route_track') is not None else '')
        + (f', route.py --quick {quick}' if quick else '')
        + firsts([n for n in first if n not in tiered])
        + (', power nets ' + ', '.join(a['power_nets'][:6])
           + (' ...' if len(a['power_nets']) > 6 else '') + f' at {RAIL_WIDTH:g} mm'
           if a['power_nets'] else '')
        + (', ' + ', '.join(f'{p} at {w:g} mm' for p, w in a['caller_power_nets'])
           + ' (--power-nets, ahead of the rails)' if a.get('caller_power_nets') else '')
        + (', deeper rip-up (congested)' if a['congested'] else '')
        + ('; signals kept off the inner planes' if any(
            float(c) < 0 for c in a['layer_costs']) else '')
        + _matched(a, 'route'))
    for i in range(2, ROUTE_PASSES + 1):
        row(f'route_p{i}', 'route', f'route pass {i} on the best board so far',
            f'only while nets stay open, the last pass was kept, and the router '
            f'effort stays within --effort-factor {effort_factor:g} x the first '
            f"pass's; may cross the planes at {PLANE_FALLBACK_COST:g}", optional=True)
    rules = {k: a[k] for k in ('clearance', 'track', 'via', 'drill', 'fine_via',
                               'fine_drill', 'grid')}
    return {'mode': 'finish' if finish else 'full', 'rules': rules,
            'rules_source': a.get('rules_source'),
            'rules_why': ('the fab floor for every step: '
                          + ('each net class declared above it routes at its own '
                             'clearance; ' if honours_classes(a) else
                             'the steps cap every class at it (--cap-net-classes); '
                             if a.get('cap_net_classes') else
                             'a net class above it is aspirational, so the route '
                             'steps cap every class at it; ')
                          + 'read from ' + (a.get('rules_source') or
                                          "the fab defaults (the board has no .kicad_pro)")
                          + (' under the caller\'s ' + ' '.join(a['fab_args'])
                             if a.get('fab_args') else '')),
            'layers': a['layers'],
            'layer_costs': a['layer_costs'], 'planes': a['planes'],
            'power_nets': a['power_nets'], 'pair_nets': a['pair_nets'],
            'parts': a['parts'], 'not_fanned': a.get('not_fanned', []),
            'unselectable': a.get('unselectable', []),
            'congested': a['congested'], 'n_nets': a['n_nets'],
            'effort_factor': effort_factor, 'steps': rows}


def run_plan(board, output, a, work, finish=False, effort_factor=2.0, route_json=False,
             quick=0):
    ch = Chain(board, work, clearance=a['clearance'], baseline=os.path.abspath(board),
               fab_args=a.get('fab_args') or ())
    L = a['layers']
    costs = [str(c) for c in a['layer_costs']]
    honour = honours_classes(a)
    ceiling = [] if honour else ['--clearance-ceiling', str(a['clearance'])]
    if finish:
        return route_passes(ch, a, L, costs, effort_factor, finish=True,
                            route_json=route_json, quick=quick)

    # 2. planes, first -- unless the board's own pours are the planes
    if a['planes'] and not a.get('keep_pours'):
        out, _, _ = ch.run('planes', [
            '{IN}', '{OUT}', '--nets', *[n for n, _ in a['planes']],
            '--plane-layers', *[l for _, l in a['planes']],
            '--clearance', str(a['clearance'])], 'planes')
        ch.accept(out)

    # 3. fanout, each part, with a retry ladder keeping the best escape
    excl = [f"!{a['globs'][n]}" for n in a['plane_nets'] if n in a['globs']]
    scope = _scope_args(a)
    fanned = False
    fans = []
    for ref, tool, pitch in a['parts']:
        fine = pitch <= 0.65
        v, d = (a['fine_via'], a['fine_drill']) if fine else (a['via'], a['drill'])
        tw = str(a['track'])
        if tool == 'bga':
            base = ['{IN}', '--output', '{OUT}', '--component', ref,
                    '--nets', *scope, *excl, '--layers', *L, '--layer-costs', *costs,
                    '--track-width', tw, '--grid-step', str(a['grid']),
                    '--clearance', str(a['clearance']),
                    # the ceiling too, as on the route steps, unless the chain
                    # honours the classes: bga_fanout honours and keeps them
                    # without one (#1529)
                    *ceiling]
            # The working via, then the fine via (one rung when they are the
            # same via), then the fine via with dogbone escapes.
            vias = dict.fromkeys([(v, d), (a['fine_via'], a['fine_drill'])])
            ladder = [base + ['--via-size', str(sv), '--via-drill', str(sd)]
                      for sv, sd in vias]
            ladder.append(ladder[-1] + ['--escape-method', 'dogbone'])
            names = [f'{sv:g}/{sd:g} via' for sv, sd in vias]
            names.append(names[-1] + ', dogbone')
        else:
            base = ['{IN}', '--output', '{OUT}', '--component', ref,
                    '--nets', *scope, *excl, '--width', tw,
                    '--grid-step', str(a['grid']), '--clearance', str(a['clearance']),
                    # the ceiling too, as on the route steps, unless the chain
                    # honours the classes: qfn_fanout honours and keeps them
                    # without one (#1529)
                    *ceiling]
            ladder = [
                base,
                base + ['--escape-method', 'underpad',
                        '--via-size', str(a['fine_via']), '--via-drill', str(a['fine_drill']),
                        '--allow-via-in-pad'],
            ]
            names = ['stub escape', f"under-pad escape, {a['fine_via']:g}/"
                                    f"{a['fine_drill']:g} via in pad"]
            if tool == 'mezz':
                ladder, names = ladder[1:], names[1:]
        fan = {'ref': ref, 'tool': tool, 'ladder': ladder, 'input': ch.cur,
               'rung_names': names}
        best = fan_ladder(ch, fan)
        fan['best'] = best if best and best['escaped'] > 0 else None
        fans.append(fan)
        if fan['best']:
            ch.accept(best['out'])
            fanned = True
            ch.decide(f'fanout_{ref}', True,
                      f"kept rung {best['rung']} ({names[best['rung']]}): escaped "
                      f"{best['escaped']}, {best['failed']} dropped, "
                      f"{best['grazes']} graze(s)", board=best['out'])
        else:
            ch.decide(f'fanout_{ref}', False,
                      'no rung escaped a ball; the route step routes the part '
                      'from its pads' if best else
                      'no rung produced a board; the route step routes the part '
                      'from its pads')

    # 4. decaps, once, after the fanouts -- and again after a re-fan that
    # holds the parts it left shorted
    if fanned:
        # place_fanout_clearance's --clearance is a ceiling (#768): omitted, it
        # prices each pair at its classes and keeps them in the project
        decap = ['{IN}', '{OUT}'] + ([] if honour else ['--clearance', str(a['clearance'])])
        out, _, _ = ch.run('decap', decap, 'decap')
        ch.accept(out)
        if out:
            refan(ch, fans, decap)
    elif a['parts']:
        ch.skip('decap', 'no fanout escaped a ball, so there is no fanout copper '
                         'to clear the caps off')

    # 5. diff pairs first
    rip, reroute = [], []
    if a['pair_nets']:
        before = ch.drc_nets(ch.cur)
        calls = diff_calls(a)
        ran = False
        for tag, ohms, nets in calls:
            groups = [g for g in a.get('match_groups') or ()
                      if g['step'] == 'route_diff' and g['impedance'] == ohms]
            out, s, _ = ch.run('diff', [
                '{IN}', '{OUT}', '--nets', *[a['globs'][n] for n in nets],
                '--track-width', str(max(0.1, a['track'])),
                '--diff-pair-gap', str(max(0.1, a['clearance'])),
                '--clearance' if honour else '--clearance-ceiling', str(a['clearance']),
                '--via-size', str(a['via']), '--via-drill', str(a['drill']),
                '--layers', *L, '--layer-costs', *costs,
                '--grid-step', str(a['grid'])]
                + (['--via-cost', str(a['via_cost'])] if 'via_cost' in a else [])
                + (['--impedance', str(ohms)] if ohms else [])
                + match_args(a, groups), tag)
            ch.accept(out)
            # A pair it could not couple is left to the route step, with whatever
            # stubs it laid; those stubs block the partner unless they may be ripped.
            if out:
                ran = True
                left = [n for n in (s or {}).get('single_ended_followup_nets') or []
                        if n in a['globs']]
                ch.steps[-1]['single_ended_followup'] = left
                rip += [a['globs'][n] for n in left]
        if ran:
            # A pair net the steps left in a DRC violation is laid again, single-ended.
            after = ch.drc_nets(ch.cur)
            if None not in (before, after):
                reroute = sorted((after - before) & set(a['pair_nets']))
            ch.decide(calls[0][0], True,
                      '; '.join(x for x in (
                          f"{len(rip)} uncoupled pair net(s) the route step may rip"
                          if rip else '',
                          f"{len(reroute)} pair net(s) left in a DRC violation, "
                          f"routed again single-ended: {', '.join(reroute)}"
                          if reroute else '') if x) or 'every pair kept as routed',
                      rip=rip, reroute=reroute)

    return route_passes(ch, a, L, costs, effort_factor, rip=rip, reroute=reroute,
                        route_json=route_json, quick=quick)


def rung_grazes(summary):
    """The clearance rows a fanout rung's own copper left that no later step
    clears: its JSON_SUMMARY drc_grazes, less those on a part the decap step
    moves. A grade that errored or never ran counts none: it measured nothing."""
    g = (summary or {}).get('drc_grazes') or {}
    return int(g.get('total') or 0) - int(g.get('deferred_to_decap') or 0)


def fan_ladder(ch, fan, rungs=None, hold=(), tag=''):
    """Run FAN's fanout ladder (or the RUNGS of it named) until a rung escapes
    every ball and leaves no graze; returns the rung that escaped most, then
    grazed least (#1685), the earliest of equals, as {'escaped', 'failed',
    'grazes', 'out', 'rung'}, or None when no rung produced a board. HOLD names
    the parts the fanout keeps off as if locked (bga_fanout --immovable-refs)."""
    ref = fan['ref']
    best = None
    for i in (range(len(fan['ladder'])) if rungs is None else rungs):
        args = fan['ladder'][i] + (['--immovable-refs', *hold] if hold else [])
        step = f'fanout_{ref}' + (f'_r{i}' if i else '') + tag
        out, s, _ = ch.run('bga' if fan['tool'] == 'bga' else 'qfn', args, step,
                           parent='refan' if tag else (f'fanout_{ref}' if i else None))
        if not out:
            continue
        escaped = int((s or {}).get('escaped', 0))
        failed = int((s or {}).get('failed', 0) or 0)
        grazes = rung_grazes(s)
        if best is None or (escaped, -grazes) > (best['escaped'], -best['grazes']):
            best = {'escaped': escaped, 'failed': failed, 'grazes': grazes,
                    'out': out, 'rung': i}
        if s is not None and failed == 0 and grazes == 0:
            break
    return best


def _named_items(d, cap=20):
    """check_drc report D's real items, one short line each, so the run's last
    line says WHAT is left and not only how many (#1681): a part the clearance
    step could not clear ships as `R6.2 pad-via GND 0.07`."""
    names = []
    for it in d.get('items') or []:
        if it.get('accepted'):
            continue
        over = it.get('overlap_mm')
        names.append(' '.join(str(x) for x in (
            it.get('pad_ref') or it.get('net1'), it.get('type'), it.get('net2'),
            round(over, 3) if isinstance(over, (int, float)) else None,
            'SHORT' if it.get('short') else None) if x is not None))
    if len(names) > cap:
        return names[:cap] + [f'+{len(names) - cap} more']
    return names


def _shorts(d):
    """check_drc report D's shorts: the items it does not accept that put
    copper of two nets in contact (check_drc sets `short` on copper pairs
    only, never on a hole, edge or size item, #1558)."""
    return [it for it in d.get('items') or []
            if it.get('short') and not it.get('accepted')]


def held_parts(board, d, fans):
    """{BGA ref: [part refs]}: the parts BOARD's check_drc report D still
    shorts to a fanned BGA's copper, where the fanout laid its copper on the
    part because the clearance step moves such a part (an unlocked 2-pad
    C/R/FB, bga_fanout's movable passive) and that step could not.

    A short is the BGA's when the copper's net is on one of its balls (its
    escapes and plane drops alike); of two such BGAs, the one with a ball of
    that net nearest the copper."""
    from kicad_parser import parse_kicad_pcb
    from bga_fanout.geometry import movable_passive
    bgas = [f['ref'] for f in fans if f['tool'] == 'bga' and f['best']]
    items = [it for it in _shorts(d)
             if it.get('type') in ('pad-via', 'pad-segment') and it.get('pad_ref')]
    if not bgas or not items:
        return {}
    pcb = parse_kicad_pcb(board)
    balls = {}                          # net name -> [(BGA ref, x, y)]
    for ref in bgas:
        fp = pcb.footprints.get(ref)
        for p in (fp.pads if fp else ()):
            if p.net_name:
                balls.setdefault(p.net_name, []).append((ref, p.global_x, p.global_y))
    held = {}
    for it in items:
        part = it['pad_ref'].rsplit('.', 1)[0]
        fp = pcb.footprints.get(part)
        if fp is None or part in bgas:
            continue
        copper = [p for p in fp.pads if any(str(l).endswith('.Cu') for l in p.layers)]
        if not movable_passive(fp, pcb, copper):
            continue
        at = it.get('via_loc') or it.get('seg_loc') or it.get('pad_loc')
        x = sum(at[0::2]) / len(at[0::2])
        y = sum(at[1::2]) / len(at[1::2])
        near = min(balls.get(it.get('net2'), ()), default=None,
                   key=lambda b: math.hypot(b[1] - x, b[2] - y))
        if near:
            held.setdefault(near[0], set()).add(part)
    return {ref: sorted(parts) for ref, parts in held.items()}


def refan(ch, fans, decap):
    """The clearance step moves a part off the fanout's copper, so a fanout
    lays vias and escapes on such parts. A part it could not clear is still
    shorted to that copper, so the BGA is fanned out again from the board it
    started on, holding those parts (dropping a ball it cannot escape, which
    the route step routes from the bare ball), every later fanout replayed
    with the rung it was accepted at, and the clearance step run again. The
    board with fewer shorts, then fewer dropped escapes, is kept."""
    d = ch.check_drc(ch.cur)
    if d is None:
        return
    held = held_parts(ch.cur, d, fans)
    if not held:
        return
    before = (len(_shorts(d)), sum(f['best']['failed'] for f in fans if f['best']))
    print(f"  decap left {before[0]} short(s); re-fanning "
          + '; '.join(f"{r} holding {' '.join(p)}" for r, p in held.items()), flush=True)
    first = min(i for i, f in enumerate(fans) if f['ref'] in held)
    kept, ch.cur = ch.cur, fans[first]['input']
    failed = sum(f['best']['failed'] for f in fans[:first] if f['best'])
    for f in fans[first:]:
        if not f['best']:
            continue
        if f['ref'] in held:
            best = fan_ladder(ch, f, hold=held[f['ref']], tag='_held')
        else:
            best = fan_ladder(ch, f, rungs=[f['best']['rung']], tag='_refan')
        if not best or best['escaped'] == 0:
            ch.cur = kept
            ch.decide('refan', False, f"the re-fan of {f['ref']} escaped no ball; "
                                      f"the board before the re-fan is kept",
                      held=held)
            return
        ch.accept(best['out'])
        failed += best['failed']
    out, _, _ = ch.run('decap', decap, 'decap_refan', parent='refan')
    d2 = ch.check_drc(out) if out else None
    after = None if d2 is None else (len(_shorts(d2)), failed)
    ch.steps.append({'tool': 'refan', 'tag': 'refan', 'held': held,
                     'before': {'shorts': before[0], 'dropped': before[1]},
                     'after': after and {'shorts': after[0], 'dropped': after[1]},
                     'kept': bool(after and after < before)})
    if after and after < before:
        ch.accept(out)
        print(f"  re-fan kept: {after[0]} short(s), {after[1]} dropped escape(s) "
              f"(was {before[0]}, {before[1]})", flush=True)
    else:
        ch.cur = kept
        print(f"  re-fan not kept: "
              + (f"{after[0]} short(s), {after[1]} dropped escape(s)" if after else 'ungraded')
              + f" (was {before[0]}, {before[1]})", flush=True)
    ch.decide('refan', bool(after and after < before),
              (f"{after[0]} short(s), {after[1]} dropped escape(s)" if after
               else 'ungraded') + f" against {before[0]} short(s), {before[1]} "
              f"dropped before the re-fan; kept the "
              + ('re-fanned board' if after and after < before else 'board before it'),
              held=held, board=out if after and after < before else kept)


def _scope_args(a):
    """The --nets values of a step that may route any of the chain's nets:
    the caller's --nets patterns as given, else every net."""
    return list(a.get('net_patterns') or ['*'])


def _improves(g, best):
    """G ranks above BEST completion first: fewer open nets, then, at the same
    open count, fewer DRC violations. A pass that closes nets at the cost of
    new violations is kept, and its violations are disclosed by the grade."""
    return (g['open'], g['drc']) < (best['open'], best['drc'])


def route_passes(ch, a, L, costs, effort_factor=2.0, finish=False, rip=(), reroute=(),
                 route_json=False, quick=0):
    """6. route every net; up to two more passes while nets are open, each
    pass improves on the best board so far, and the effort stays within
    budget. Only the first pass keeps signals off the solid planes; the later
    ones may cross them at PLANE_FALLBACK_COST, so a plane carries only what
    could not route without it. The REROUTE nets are first ripped and routed
    again on their own. The nets of each class tier (`class_tiers`) route in
    a call of their own before the passes, which leave them out, again
    before a later pass and once after the last, at the later passes' costs,
    while one of them is open; the budget counts those calls, and with no
    net left for a pass each round's last class call is the graded board.
    Every step routes the power nets at RAIL_WIDTH, with
    the caller's --power-nets ahead of them, so a net those select routes at
    their width. Each pass is given the caller's --route-first nets as
    route.py's own --route-first, a pattern per net in their order, and records
    route.py's own account of them (`route_first_record`). With
    ROUTE_JSON each pass writes route.py's --json-out beside its board.
    Returns (chain, best board or None, its grade or None)."""
    def route_args(costs):
        return (base + ['--layers', *L, '--layer-costs', *costs]
                + (['--json-out', '{JSON}'] if route_json else []))
    tiers = class_tiers(a)
    tiered = {n for _, _, nets in tiers for n in nets}

    def shared_args(rip):
        shared = ['--no-bga-zones',
                  '--clearance' if honours_classes(a) else '--clearance-ceiling',
                  str(a['clearance']),
                  '--track-width', str(a.get('route_track', a['track'])),
                  '--via-size', str(a['via']), '--via-drill', str(a['drill']),
                  '--grid-step', str(a['grid'])]
        if 'via_cost' in a:
            shared += ['--via-cost', str(a['via_cost'])]
        caller = a.get('caller_power_nets') or []
        if caller or a['power_nets']:
            shared += ['--power-nets', *[p for p, _ in caller],
                       *[a['globs'][n] for n in a['power_nets']],
                       '--power-nets-widths', *[str(w) for _, w in caller],
                       *[str(RAIL_WIDTH)] * len(a['power_nets'])]
        if a['congested']:
            shared += ['--max-ripup', '5']
        if rip:
            shared += ['--rip-existing-nets', *rip]
        if quick:
            shared += ['--quick', str(quick)]
        return shared
    order = a.get('route_first') or []
    routed_groups = [g for g in a.get('match_groups') or () if g['step'] == 'route']
    # A tier's nets route in its own call; no other call is given rip
    # authority over them, which would hold it at their class.
    tier_globs = {a['globs'][n] for n in tiered}
    shared = shared_args([r for r in rip if r not in tier_globs])
    untiered_first = [n for n in order if n not in tiered]
    base = (['{IN}', '{OUT}', '--nets', *_scope_args(a),
             *[f"!{a['globs'][n]}" for n in sorted(tiered)]]
            + (['--route-first', *[_glob(n) for n in untiered_first]]
               if untiered_first else [])
            + shared + match_args(a, routed_groups))
    # Every net in scope is in a class call: no pass has a net to route, and
    # each round's board is the last class call's.
    no_pass = bool(tiers) and bool(a.get('tiers_cover_scope'))
    best = None                        # (grade, board)
    passes = ['route' + (f'_p{i + 1}' if i else '') for i in range(ROUTE_PASSES)]

    def skip_rest(i, why):
        for tag in passes[i:]:
            ch.skip(tag, why)
    if no_pass:
        skip_rest(0, 'every net this run routes is in a net class call of its own')
    if finish:
        ev = {'n': 0, 'tag': 'input', 'tool': 'grade', 'parent': None,
              'board': ch.cur, 'input': ch.cur, 'cmd': [], 'log': None}
        ch.started.add('input')
        emit('AUTOROUTE_STEP', dict(ev, state='running', t=round(time.time(), 3)))
        t0 = time.time()
        g = ch.grade(ch.cur)
        end = dict(ev, seconds=round(time.time() - t0, 1), t=round(time.time(), 3),
                   rc=0, summary={}, iterations=None)
        if g is None:
            emit('AUTOROUTE_STEP', dict(end, state='failed', board=None,
                                        result='a checker could not grade the input',
                                        error_tail=[]))
            print("  the input could not be graded: no route pass runs", flush=True)
            skip_rest(0, 'the input could not be graded')
            return ch, None, None
        emit('AUTOROUTE_STEP', dict(end, state='done', grade=g,
                                    result=f"{g['open']} open, {g['drc']} DRC"))
        ch.steps.append({'tool': 'grade', 'tag': 'input', 'grade': g})
        best = (g, ch.cur)
        print(f"  input: {g['open']} open, {g['drc']} DRC", flush=True)
        if g['open'] == 0:
            skip_rest(0, 'the input has no open net')
            return ch, ch.cur, g
    for tag, ohms, nets in impedance_steps(a):
        groups = [g for g in routed_groups if g['impedance'] == ohms]
        out, _, _ = ch.run('route', ['{IN}', '{OUT}', '--nets', *[a['globs'][n] for n in nets],
                                     '--impedance', str(ohms), *shared, '--layers', *L,
                                     '--layer-costs', *costs] + match_args(a, groups),
                           tag)
        ch.accept(out)
    if reroute:
        # A net whose new route lays no copper gets its old copper back, and
        # the passes below route whatever is left open.
        out, _, _ = ch.run('route', ['{IN}', '{OUT}', '--nets', *[a['globs'][n] for n in reroute],
                                     '--force-reroute', *shared, '--layers', *L,
                                     '--layer-costs', *costs], 'reroute_pairs',
                           parent=diff_calls(a)[0][0])
        ch.steps[-1]['nets'] = list(reroute)
        ch.accept(out)

    def run_tiers(costs, suffix='', open_nets=None, hold=False):
        """Each class's own call, at COSTS, only a class with a net in
        OPEN_NETS when that is given; HOLD keeps the last call's ending for
        its grade. Returns (the last call's board or None, the router
        iterations of the calls, or None when none ran)."""
        run = [(tag, nets) for tag, _clr, nets in tiers
               if open_nets is None or set(nets) & set(open_nets)]
        out, iters = None, None
        for k, (tag, nets) in enumerate(run):
            globs = [a['globs'][n] for n in nets]
            tier_first = [n for n in order if n in nets]
            last = hold and k == len(run) - 1
            out, _, _ = ch.run('route', ['{IN}', '{OUT}', '--nets', *globs]
                               + (['--route-first', *[_glob(n) for n in tier_first]]
                                  if tier_first else [])
                               + shared_args([r for r in rip if r in globs])
                               + match_args(a, [g for g in a.get('match_groups') or ()
                                                if g['step'] == tag])
                               + ['--layers', *L, '--layer-costs', *costs]
                               + (['--json-out', '{JSON}'] if route_json and last else []),
                               tag + suffix, parent=tag if suffix else None, hold=last)
            ch.steps[-1]['nets'] = list(nets)
            if tier_first:
                ch.steps[-1]['route_first'] = route_first_record(
                    os.path.join(ch.work, ch.steps[-1]['log']))
            iters = (iters or 0) + (ch.last_iterations or 0)
            ch.accept(out)
        return out, iters

    def judge(out, tag, last_round):
        """Grade OUT, the board of round TAG, keep it when it improves on the
        best; True when another round may follow."""
        nonlocal best
        g = ch.grade(out)
        ch.steps[-1]['grade'] = g
        if g is None:
            if best is None:           # still the routed board, just ungraded
                best = (None, out)
                ch.accept(out)
            ch.end_step(result='routed, but a checker could not grade it',
                        grade=None, kept=best[1] == out)
            skip_rest(last_round + 1, f'{tag} could not be graded')
            return False
        print(f"  {os.path.basename(out)}: {g['open']} open, {g['drc']} DRC", flush=True)
        if best is not None and best[0] is not None and not _improves(g, best[0]):
            print(f"  not kept: no better than {os.path.basename(best[1])} "
                  f"({best[0]['open']} open, {best[0]['drc']} DRC)", flush=True)
            ch.end_step(result=f"{g['open']} open, {g['drc']} DRC: not kept",
                        grade=g, kept=False)
            ch.decide(tag, False,
                      f"{g['open']} open, {g['drc']} DRC is no better than "
                      f"{os.path.basename(best[1])} ({best[0]['open']} open, "
                      f"{best[0]['drc']} DRC)")
            ch.cur = best[1]
            skip_rest(last_round + 1, f'{tag} was not kept, which ends the passes')
            return False
        best = (g, out)
        ch.accept(out)
        ch.end_step(result=f"{g['open']} open, {g['drc']} DRC: best so far",
                    grade=g, kept=True)
        if g['open'] == 0:
            skip_rest(last_round + 1, 'no open net left')
            return False
        return True

    def open_tier_nets():
        return (None if best is None or best[0] is None
                else sorted(set(best[0]['open_nets']) & tiered))
    crossing = [str(PLANE_FALLBACK_COST) if float(c) < 0 else c for c in costs]
    first = spent = last_round = last_tiers = None
    graded = False                     # the last board a call laid is graded
    for i in range(ROUTE_PASSES):
        if i:
            if last_round is None:
                print(f"  route pass {i + 1} not started: the last pass reported "
                      f"no iterations to budget from", flush=True)
                skip_rest(i, 'the last pass reported no router iterations to '
                             'budget the next from')
                break
            if spent + last_round > effort_factor * first:
                print(f"  route pass {i + 1} not started: {spent} iterations "
                      f"spent, the last pass took {last_round}, budget "
                      f"{effort_factor:g} x {first}", flush=True)
                skip_rest(i, f"effort budget: {spent} router iterations spent and "
                             f"the last pass took {last_round}, over "
                             f"--effort-factor {effort_factor:g} x the first "
                             f"pass's {first}")
                break
        want = None if (i == 0 and not finish) else open_tier_nets()
        if tiers and (want is None or want):
            out, last_tiers = run_tiers(crossing if i else costs,
                                        f'_p{i + 1}' if i else '', want, hold=no_pass)
            round_iters = last_tiers or 0
        elif no_pass:
            break
        else:
            out, round_iters = None, 0
        tag = ch.steps[-1]['tag'] if no_pass else passes[i]
        if not no_pass:
            out, _, _ = ch.run('route', route_args(crossing if i else costs),
                               passes[i], hold=True)
            if order:
                ch.steps[-1]['route_first'] = route_first_record(
                    os.path.join(ch.work, ch.steps[-1]['log']))
            round_iters = (None if ch.last_iterations is None
                           else round_iters + ch.last_iterations)
        last_round = round_iters
        if round_iters is not None:
            spent = (spent or 0) + round_iters
            if first is None:
                first = round_iters
        if not out:
            skip_rest(i + 1, f'{tag} failed')
            break
        graded = True
        if not judge(out, tag, i):
            break
    # A class net still open after the last pass gets one more class call on
    # the best board, at the later passes' costs, within the effort budget.
    rest = open_tier_nets() if not no_pass and graded else None
    if rest and (first is None or (spent or 0) + (last_tiers or 0)
                 <= effort_factor * first):
        ch.cur = best[1]
        out, iters = run_tiers(crossing, '_final', rest, hold=True)
        spent = (spent or 0) + (iters or 0)
        if out:
            judge(out, ch.steps[-1]['tag'], ROUTE_PASSES)
    elif rest:
        print(f"  class call after the last pass not started: {spent} iterations "
              f"spent, the last class call took {last_tiers}, budget "
              f"{effort_factor:g} x {first}", flush=True)
    ch.end_step()
    return ch, (best[1] if best else None), (best[0] if best else None)


def route_first_record(log):
    """The `route_first` record route.py's own JSON_SUMMARY gives in the
    route step's LOG -- the declared nets, those its single-ended loop and
    Phase 3 routed, in their order, and whether they came first
    (net_ordering.route_first_record) -- or None when the step printed none
    (it routed nothing). Only the outermost run carries one: no nested
    sub-run is given --route-first."""
    try:
        text = open(log, errors='replace').read()
    except OSError:
        return None
    for m in re.finditer(r'^JSON_SUMMARY: (\{.*\})$', text, re.M):
        try:
            rec = json.loads(m.group(1)).get('route_first')
        except ValueError:
            continue
        if rec is not None:
            return rec
    return None


def _member_value(pcb, net_id, mode):
    from net_queries import net_match_length, net_pads_joined, net_pad_to_die_delay
    if net_id is None:
        return None
    if mode == 'length':
        return net_match_length(pcb, net_id, mode='total')
    if not net_pads_joined(pcb, net_id):
        return None
    from impedance import calculate_route_propagation_time_ps
    segs = [s for s in pcb.segments if s.net_id == net_id]
    vias = [v for v in pcb.vias if v.net_id == net_id]
    return calculate_route_propagation_time_ps(segs, vias, pcb) + net_pad_to_die_delay(pcb, net_id)


def _single_ended_pair_nets(steps, board):
    stem = board and os.path.basename(board)[:-len('.kicad_pcb')] + '.log'
    upto = next((i for i, s in enumerate(steps) if s.get('log') == stem), len(steps) - 1)
    out = set()
    for s in steps[:upto + 1]:
        if s.get('ok', True):
            out.update(s.get('single_ended_followup') or ())
            if s.get('tag') == 'reroute_pairs':
                out.update(s.get('nets') or ())
    return out


def _width_account(pcb, net_id, spec):
    shipped = {}
    for s in pcb.segments:
        if s.net_id != net_id:
            continue
        row = shipped.setdefault(s.layer, {'at_target_mm': 0.0, 'off_target_mm': 0.0,
                                           'widths': set()})
        key = ('at_target_mm' if abs(s.width - spec['widths'].get(s.layer, -1)) <= 1e-3
               else 'off_target_mm')
        row[key] += math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
        row['widths'].add(round(s.width, 4))
    return {l: {'target': spec['widths'].get(l), 'widths': sorted(r['widths']),
                'at_target_mm': round(r['at_target_mm'], 4),
                'off_target_mm': round(r['off_target_mm'], 4)}
            for l, r in sorted(shipped.items())}


def match_account(board, a, steps=()):
    groups = a.get('match_groups') or []
    targets = a.get('net_impedance') or {}
    if not board or not (groups or targets):
        return None
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board)
    ids = {n.name: nid for nid, n in pcb.nets.items() if n.name}
    pairs = a.get('pair_nets') or []
    partner = {n: pairs[i ^ 1] for i, n in enumerate(pairs)}
    single = _single_ended_pair_nets(list(steps), board)
    out = {}
    if groups:
        rows = []
        for g in groups:
            vals = {n: _member_value(pcb, ids.get(n), g['mode']) for n in g['nets']}
            got = {n: v for n, v in vals.items() if v is not None}
            row = {k: g[k] for k in ('mode', 'patterns', 'nets', 'tolerance', 'unit',
                                     'step', 'impedance')}
            row['measured'] = {n: round(v, 4) for n, v in got.items()}
            row['unmeasured'] = sorted(n for n, v in vals.items() if v is None)
            spread = (max(got.values()) - min(got.values())
                      if got and not row['unmeasured'] else None)
            row['spread'] = None if spread is None else round(spread, 4)
            row['within_tolerance'] = None if spread is None else spread <= g['tolerance']
            if g['step'] == 'route_diff':
                legs = {}
                for n in g['nets']:
                    p = n if pairs.index(n) % 2 == 0 else partner[n]
                    legs[p] = (_member_value(pcb, ids.get(p), g['mode']),
                               _member_value(pcb, ids.get(partner[p]), g['mode']))
                row['pairs'] = {p: {'n': partner[p],
                                    'pair': None if None in v else round(max(v), 4),
                                    'skew': None if None in v else round(abs(v[0] - v[1]), 4)}
                                for p, v in sorted(legs.items())}
                vals2 = [r['pair'] for r in row['pairs'].values()]
                row['pair_spread'] = (None if None in vals2
                                      else round(max(vals2) - min(vals2), 4))
                row['rerouted_without_group'] = sorted(set(g['nets']) & single)
            rows.append(row)
        out['match_groups'] = rows
    if targets:
        from protected_nets import pro_path_for_board, read_impedance_specs
        recorded = read_impedance_specs(pro_path_for_board(board))
        imp = {}
        for n, s in sorted(targets.items()):
            widths = _width_account(pcb, ids.get(n), s)
            imp[n] = {'requested_ohms': s['ohms'], 'differential': s['differential'],
                      'recorded': recorded.get(n), 'widths': widths,
                      'at_target': bool(widths) and not any(
                          r['off_target_mm'] for r in widths.values())}
            if n in single:
                imp[n]['rerouted_single_ended'] = True
        out['impedance'] = imp
    return out


#: The tags route_passes gives its passes; the diff-pair reroute is a route
#: step but not a pass.
_PASS_TAG = re.compile(r'route(_class_[0-9.]+)?(_p\d+|_final)?')


def _step_of(ch, board):
    """The step of CH that wrote BOARD, or None (the input, or no board)."""
    stem = board and os.path.basename(board)[:-len('.kicad_pcb')]
    return next((s for s in ch.steps if stem and s.get('log') == stem + '.log'), None)


def shipped_record(ch, final):
    """The head of the --json-out document for FINAL, the board that ships
    (None when none does): the step that produced it and, when that step is
    a route pass, the summary route.py wrote for that pass with its own
    --json-out, as written. `status` says which: 'route_pass',
    'route_pass_without_summary', 'no_route_pass' or 'no_board', each but
    the first with `why`."""
    step = _step_of(ch, final)
    doc = {'status': None, 'shipped_board': final and os.path.basename(final),
           'shipped_step': step['tag'] if step else (final and 'input'),
           'route_summary': None}
    if step and step['tool'] == 'route' and _PASS_TAG.fullmatch(step['tag']):
        try:
            with open(final[:-len('.kicad_pcb')] + '.summary.json') as fh:
                doc['route_summary'] = json.load(fh)
            doc['status'] = 'route_pass'
        except (OSError, ValueError):
            doc['status'] = 'route_pass_without_summary'
            doc['why'] = (f"route.py wrote no readable --json-out for "
                          f"{step['tag']}, the pass that shipped")
    elif final:
        doc['status'] = 'no_route_pass'
        doc['why'] = ('no route pass shipped: the board is ' +
                      (f"from {step['tag']}, the last step that succeeded" if step
                       else 'the input, which no route pass improved on'))
    else:
        doc['status'] = 'no_board'
        doc['why'] = 'no step produced a board'
    return doc


def routing_costs(a):
    """What the route steps ran with for vias and layers: `via_cost` (the
    caller's, else route.py's own default), the first pass's `layer_costs`
    and the later passes' `crossing_layer_costs`, by layer, and
    `from_caller`, the flags the caller set."""
    from routing_defaults import VIA_COST
    costs = a['layer_costs']
    return {'via_cost': a.get('via_cost', VIA_COST),
            'layer_costs': dict(zip(a['layers'], costs)),
            'crossing_layer_costs': dict(zip(a['layers'], [
                PLANE_FALLBACK_COST if c < 0 else c for c in costs])),
            'from_caller': [f for f, k in (('--via-cost', 'via_cost'),
                                           ('--layer-costs', 'caller_layer_costs'))
                            if k in a]}


def net_class_record(a):
    """The net classes declared above the chain clearance, by net, and
    whether the chain routed them at their own (`honoured`, with the route
    call of each clearance, and `in_passes`, the class nets the route passes
    route at their own: plane nets, and nets no pattern selects alone) or
    capped every class (`capped`); `mode` is None when no class is declared
    above it."""
    calls = class_tiers(a)
    called = {n for _, _, nets in calls for n in nets}
    return {'clearance': a['clearance'],
            'above_clearance': a.get('class_clearances') or {},
            'mode': (None if not a.get('class_clearances') else
                     'capped' if a.get('cap_net_classes') else 'honoured'),
            'calls': [{'tag': t, 'clearance': c, 'nets': n} for t, c, n in calls],
            'in_passes': (sorted(set(a.get('class_clearances') or ()) - called)
                          if honours_classes(a) else [])}


def _effort_factor(text):
    v = float(text)
    if not math.isfinite(v) or v < 0:
        raise argparse.ArgumentTypeError(f'must be a finite number >= 0, not {text}')
    return v


def _plane_layer_cost(text):
    v = float(text)
    if not math.isfinite(v) or v == 0:
        raise argparse.ArgumentTypeError(f'must be a finite nonzero number, not {text}')
    return v


def _layer_cost(text):
    try:
        v = float(text)
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or (v >= 0 and not 1.0 <= v <= 1000):
        raise argparse.ArgumentTypeError(
            f'must be a cost in [1, 1000] or negative (no tracks on the layer), not {text}')
    return v


def _via_cost(text):
    try:
        v = int(text)
    except ValueError:
        v = -1
    if v < 0:
        raise argparse.ArgumentTypeError(f'must be a whole number >= 0, not {text}')
    return v


def _width(text):
    try:
        v = float(text)
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v <= 0:
        raise argparse.ArgumentTypeError(f'must be a finite width > 0, not {text}')
    return v


def _ship(src, dst):
    """Copy board SRC and its siblings to DST, removing any sibling DST has
    that SRC does not, so the shipped set is exactly SRC's."""
    from copy_board import copy_board, SIBLING_EXTS
    sb, db = src[:-len('.kicad_pcb')], dst[:-len('.kicad_pcb')]
    for ext in SIBLING_EXTS:
        if os.path.exists(db + ext) and not os.path.exists(sb + ext):
            os.remove(db + ext)
    copy_board(src, dst)


def main():
    from redo_record import record_invocation
    record_invocation()  # stress-test redo manifest (#132); no-op unless REDO_MANIFEST set
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('input_file')
    ap.add_argument('output_file')
    ap.add_argument('--dry-run', action='store_true',
                    help='print the analysis and exit')
    ap.add_argument('--finish', action='store_true',
                    help='only the final route passes, on a board already '
                         'prepared and routed by another chain')
    ap.add_argument('--effort-factor', type=_effort_factor, default=EFFORT_FACTOR,
                    help='a route pass after the first starts only while the '
                         'router iterations spent, plus the last pass\'s '
                         'again, stay within this many times the first '
                         'pass\'s (default 2). A forecast, not a cap: a pass '
                         'that starts runs to its end')
    ap.add_argument('--plane-layer-cost', type=_plane_layer_cost, default=None,
                    help='the --layer-costs entry of every inner plane layer in the '
                         'fanout, diff pair and first route steps. Negative (default -1) '
                         'keeps signals off the planes, so each stays a solid '
                         'reference, and only the later route passes may cross them '
                         'at 3.0; a positive price lets every step cross them '
                         '(the skill\'s route step uses 3.0), trading reference '
                         'integrity for routability')
    ap.add_argument('--layer-costs', type=_layer_cost, nargs='+', default=None,
                    metavar='COST',
                    help='route.py\'s --layer-costs for every step that takes them '
                         '(fanouts, diff pairs, route), one per copper layer in board '
                         'order, in place of the chain\'s own (1.0 and 1.5 on 2 layers, '
                         'the --plane-layer-cost on inner planes): each in [1, 1000], or '
                         'negative to keep tracks off the layer, which the later route '
                         'passes may cross at 3.0. Not with --plane-layer-cost')
    ap.add_argument('--via-cost', type=_via_cost, default=None, metavar='N',
                    help='route.py\'s and route_diff.py\'s --via-cost for every route and '
                         'diff-pair step (default: their own)')
    ap.add_argument('--cap-net-classes', action='store_true',
                    help='cap every net class at the chain clearance on every step '
                         '(route.py --clearance-ceiling). Without it a net class '
                         'declared above that clearance routes at its own, its nets '
                         'in a route call of their own')
    ap.add_argument('--power-nets', nargs='+', default=[], metavar='PATTERN',
                    help='route.py\'s --power-nets, given to every route step ahead '
                         'of autoroute\'s rails: a net routes at the width of the '
                         'first pattern that selects it (fnmatch), a rail included '
                         f'(in place of the rail width {RAIL_WIDTH:g}); the other '
                         'nets keep autoroute\'s choice')
    ap.add_argument('--power-nets-widths', nargs='+', type=_width, default=[],
                    metavar='MM',
                    help='the width of each --power-nets pattern, in order: one '
                         'per pattern, or the run is refused before any step runs')
    ap.add_argument('--keep-pours', action='store_true',
                    help='take the planes from the board\'s own pours, as --finish '
                         'does, and pour none: the planes step does not run, so '
                         'every pour keeps its outline, clearance, pad connection '
                         'and priority')
    ap.add_argument('--nets', nargs='+', default=[], metavar='PATTERN',
                    help='route only the nets these patterns select, read as '
                         'route.py reads its --nets (wildcards; a leading ! '
                         'excludes), in every step: a pour, power net or pair '
                         'outside them is left as the board has it. Patterns that '
                         'together select no net are refused before any step runs')
    ap.add_argument('--track-width', type=_width, default=None, metavar='MM',
                    help='route.py\'s --track-width for every route step, in place '
                         'of the chain\'s track floor; the fanout and diff-pair '
                         'steps keep the floor')
    ap.add_argument('--quick', type=int, choices=(1, 2), default=0, metavar='LEVEL',
                    help='every route step runs at route.py\'s --quick LEVEL: a '
                         'chain to judge a placement by, not to ship')
    ap.add_argument('--route-first', nargs='+', default=[], metavar='PATTERN',
                    help='route.py\'s --route-first for every route pass: the nets '
                         'these patterns select route first, in the order given (a '
                         'pattern\'s own nets by name), and every other net keeps '
                         'route.py\'s mps order. A pattern that selects no net, an '
                         'exclusion and a net outside --nets are refused before any '
                         'step runs')
    ap.add_argument('--length-match-group', action='append', nargs='+', default=[],
                    dest='length_match_groups', metavar='PATTERN',
                    help='a group of nets to length-match (repeatable), read as '
                         'route.py\'s --length-match-group reads its patterns: '
                         'given to the one step that routes all its nets -- '
                         'route_diff.py for pairs, route.py for single-ended '
                         'nets -- or refused before any step runs')
    ap.add_argument('--length-match-tolerance', type=_width, default=None, metavar='MM',
                    help='the steps\' --length-match-tolerance for every length '
                         'group (their default when omitted)')
    ap.add_argument('--time-match-group', action='append', nargs='+', default=[],
                    dest='time_match_groups', metavar='PATTERN',
                    help='a group of nets to match by propagation delay (repeatable): '
                         'given to its step as --length-match-group with '
                         '--time-matching. A step carrying both a length and a '
                         'time group is refused')
    ap.add_argument('--time-match-tolerance', type=_width, default=None, metavar='PS',
                    help='the steps\' --time-match-tolerance for every time group '
                         '(their default when omitted)')
    ap.add_argument('--net-impedance', action='append', nargs=2, default=[],
                    metavar=('PATTERN', 'OHMS'),
                    help='an impedance target for the nets PATTERN selects '
                         '(repeatable): differential ohms for pair nets, given to '
                         'their route_diff.py call as --impedance; single-ended '
                         'ohms for other nets, routed by a route.py --impedance '
                         'step of their own ahead of the route passes')
    ap.add_argument('--diff-pair', action='append', nargs=2, default=None,
                    dest='diff_pairs', metavar=('P', 'N'),
                    help='a differential pair (repeatable): route_diff.py routes '
                         'these pairs and no other, in place of the pairs found by '
                         'name. Each must be one pair by its names, which is how '
                         'route_diff.py finds it')
    ap.add_argument('--json-out', metavar='FILE',
                    help='also write FILE: the shipped route pass\'s route.py '
                         '--json-out summary, the step that produced the shipped '
                         'board, and the chain as in plan.json; when no route pass '
                         'shipped, its status says so and its summary is null')
    from fab_tiers import (add_fab_floor_args, fab_tier_from_args,
                           set_default_fab_tier, set_policy_from_args)
    add_fab_floor_args(ap)
    # A flag the caller gives is passed on even at its default value; the
    # defaults are read back for the ones it does not give.
    defaults = {d: ap.get_default(d) for d in ('fab_tier', 'escalation')}
    ap.set_defaults(**dict.fromkeys(defaults))
    from cli_nets import pin_dash_digit_values
    args = pin_dash_digit_values(ap).parse_args()
    json_out = args.json_out and os.path.abspath(args.json_out)
    if json_out and os.path.isfile(json_out):
        os.remove(json_out)          # a stale file must not read as this run's

    t_start = time.time()
    fab_args = []
    for flag, dest in (('--fab-tier', 'fab_tier'), ('--escalation', 'escalation')):
        if getattr(args, dest) is None:
            setattr(args, dest, defaults[dest])
        else:
            fab_args += [flag, getattr(args, dest)]
    if args.fab_overrides:
        if not os.path.isfile(args.fab_overrides):
            print(f"AUTOROUTE: refused: --fab-overrides {args.fab_overrides} is no file",
                  flush=True)
            return 2
        args.fab_overrides = os.path.abspath(args.fab_overrides)
        fab_args += ['--fab-overrides', args.fab_overrides]
    set_default_fab_tier(*fab_tier_from_args(args))
    set_policy_from_args(args, args.input_file)

    try:
        if args.layer_costs is not None and args.plane_layer_cost is not None:
            raise Refused('--layer-costs gives every layer\'s cost; '
                          'give it or --plane-layer-cost, not both')
        if args.plane_layer_cost is None:
            args.plane_layer_cost = PLANE_LAYER_COST
        if len(args.power_nets) != len(args.power_nets_widths):
            raise Refused(f"--power-nets gives {len(args.power_nets)} pattern(s) and "
                          f"--power-nets-widths {len(args.power_nets_widths)} "
                          f"width(s); give one width per pattern")
        impedance = []
        for pattern, ohms in args.net_impedance:
            try:
                impedance.append((pattern, _width(ohms)))
            except argparse.ArgumentTypeError:
                raise Refused(f'--net-impedance {pattern} {ohms}: the ohms must be a '
                              f'finite number > 0') from None
        a = analyze(args.input_file, finish=args.finish, plane_cost=args.plane_layer_cost,
                    caller_power=list(zip(args.power_nets, args.power_nets_widths)),
                    keep_pours=args.keep_pours, fab_args=fab_args, only=args.nets,
                    route_track=args.track_width, route_first=args.route_first,
                    length_groups=args.length_match_groups,
                    time_groups=args.time_match_groups,
                    length_tol=args.length_match_tolerance,
                    time_tol=args.time_match_tolerance,
                    impedance=impedance, diff_pairs=args.diff_pairs,
                    cap_net_classes=args.cap_net_classes, layer_costs=args.layer_costs,
                    via_cost=args.via_cost)
    except Refused as exc:
        print(f"AUTOROUTE: refused: {exc}", flush=True)
        return 2
    print("AUTOROUTE_ANALYSIS: " + json.dumps(a, sort_keys=True), flush=True)
    plan = plan_steps(a, finish=args.finish, effort_factor=args.effort_factor,
                      quick=args.quick)
    emit('AUTOROUTE_PLAN', plan)
    if args.dry_run:
        return 0
    out = os.path.abspath(args.output_file)
    work = out + '.autoroute'
    from copy_board import copy_board
    # Stage the input before clearing the work directory: it may live there
    # (a previous run's step board).
    stage = tempfile.mkdtemp(prefix='.autoroute_input_', dir=os.path.dirname(out))
    try:
        staged = os.path.join(stage, 'input.kicad_pcb')
        copy_board(os.path.abspath(args.input_file), staged)
        if os.path.isdir(work):
            shutil.rmtree(work)
        os.makedirs(work)
        src = os.path.join(work, 'input.kicad_pcb')
        copy_board(staged, src)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    ch, final, grade = run_plan(src, out, a, work, finish=args.finish,
                                effort_factor=args.effort_factor,
                                route_json=bool(json_out), quick=args.quick)
    ch.end_step()
    note = None
    if not final and (args.finish or ch.cur != src):
        # No route pass finished: ship the best board the chain has -- the
        # input itself for --finish, else the last step that succeeded.
        final = ch.cur
        note = 'no route pass finished; shipping the last board a step produced'
        print(f"AUTOROUTE: no route pass finished; shipping "
              f"{os.path.basename(final)}", flush=True)
    # Every planned step that never started, with why.
    for r in plan['steps']:
        if r['tag'] not in ch.started:
            emit('AUTOROUTE_STEP', {'tag': r['tag'], 'tool': r['tool'], 'parent': None,
                                    'state': 'skipped', 't': round(time.time(), 3),
                                    'reason': ch.skipped.get(r['tag'], 'not reached')})
    account = match_account(final, a, ch.steps)
    with open(os.path.join(work, 'plan.json'), 'w') as fh:
        json.dump({'analysis': a, 'plan': plan, 'steps': ch.steps,
                   'decisions': ch.decisions, 'final': final,
                   'grade': grade, 'match': account}, fh, indent=1)
    final_tag = None
    if final:
        final_tag = next((s['tag'] for s in ch.steps
                          if s.get('log') and final.endswith(
                              s['log'][:-len('.log')] + '.kicad_pcb')),
                         'input' if final == src else None)
    summary = {'final_step': os.path.basename(final) if final else None,
               'final_tag': final_tag, 'output': out if final else None,
               'work': work, 'note': note,
               'open': grade['open'] if grade else None,
               'open_nets': grade['open_nets'] if grade else None,
               'drc': grade['drc'] if grade else None,
               'drc_items': grade['drc_items'] if grade else None,
               'steps': len(ch.steps),
               'seconds': round(time.time() - t_start, 1),
               'skipped': [s['tag'] for s in ch.steps if not s.get('ok', True)]}
    if 'route_first' in a:
        # The declared order, and what the shipped pass's log says of it
        # (None when the board that ships is no route pass's).
        summary['route_first'] = {'nets': a['route_first'],
                                  'shipped': (_step_of(ch, final) or {}).get('route_first')}

    def write_json_out():
        if json_out:
            from route_summary import write_summary_file
            write_summary_file(json_out, {**shipped_record(ch, final),
                                          'routing_costs': routing_costs(a),
                                          'net_classes': net_class_record(a),
                                          'analysis': a,
                                          'plan': plan, 'steps': ch.steps,
                                          'decisions': ch.decisions, 'final': final,
                                          'grade': grade, 'match': account})
    summary['routing_costs'] = routing_costs(a)
    summary['net_classes'] = net_class_record(a)
    if account:
        summary.update(account)
    if not final:
        print("AUTOROUTE: no route pass produced a board", flush=True)
        summary['error'] = 'no route pass produced a board'
        write_json_out()
        print("AUTOROUTE_SUMMARY: " + json.dumps(summary), flush=True)
        return 1
    _ship(final, out)
    write_json_out()
    print("AUTOROUTE_SUMMARY: " + json.dumps(summary), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
