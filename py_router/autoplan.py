#!/usr/bin/env python3
"""Route a board through a deterministic plan built from the board itself.

    python3 py_router/autoplan.py board.kicad_pcb routed.kicad_pcb
    python3 py_router/autoplan.py board.kicad_pcb routed.kicad_pcb --dry-run

The plan-pcb-routing skill has an agent read a board and choose a command
chain. This makes the same kinds of choices from board facts alone -- the
footprints, net names, layer count and the board's own design rules -- and
runs the chain, so routing without an agent gets the steps that remove most
congestion before the router starts:

  1. design rules  the board's Default class clearance as a ceiling, the fab
                   track floor, the working via (fine via for fine-pitch
                   escapes), all from list_nets.read_design_rules
  2. planes        GND on In1.Cu and the dominant rail on In2.Cu on 4+ layer
                   boards, GND on B.Cu on 2-layer boards; poured first
  3. fanout        every BGA/PGA/LGA with interior depth or fine pitch
                   (bga_fanout) and every fine-pitch QFN/QFP (qfn_fanout),
                   plane nets excluded; a part that drops escapes is retried
                   at the fab clearance with the fine via, then with the
                   under-pad method, keeping the attempt that escaped most
  4. decaps        place_fanout_clearance once, after the fanouts
  5. diff pairs    name-detected pairs routed first with route_diff
  6. route         route.py over every net with the poured nets and rails as
                   power nets, layer costs that keep signals off the planes,
                   a finer grid around fine-pitch parts and deeper rip-up on
                   congested boards; up to two further passes on the result
                   while nets are left open, shipping the best pass

Every step runs as its own process in <output>.autoplan/, so a step that
fails or does not improve is skipped and the chain continues from the last
good board. The chain is written to <output>.autoplan/plan.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

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
ROUTE_PASSES = 3             # the first pass plus up to two on its result


def _num(x):
    return round(float(x), 4)


# --------------------------------------------------------------------------
# Board analysis


def _grid_depth(fp):
    """Rows of a grid array from its edge to its centre (columns // 2)."""
    xs = {round(p.local_x, 2) for p in fp.pads}
    ys = {round(p.local_y, 2) for p in fp.pads}
    return min(len(xs), len(ys)) // 2


def fanout_parts(pcb):
    """[(ref, tool, pitch)] for the parts the skill fans out (Step 3)."""
    from kicad_parser import detect_package_type, detect_bga_pitch
    out = []
    for ref, fp in pcb.footprints.items():
        pads = [p for p in fp.pads if p.pad_type != 'np_thru_hole']
        if len(pads) < 6:
            continue
        kind = detect_package_type(fp)
        if kind not in ('BGA', 'QFN', 'QFP'):
            continue
        th = sum(1 for p in pads if (p.drill or 0) > 0)
        name = (fp.footprint_name or '').upper()
        if th > len(pads) - th and 'PGA' not in name:
            continue
        pitch = round(detect_bga_pitch(fp), 3)
        if not pitch:
            continue
        if kind == 'BGA':
            if pitch >= 2.0:
                continue
            if _grid_depth(fp) > 2 or pitch <= 0.5:
                out.append((ref, 'bga', pitch))
        else:
            if pitch >= 1.27:
                continue
            if pitch <= 0.5 or (len(pads) > 40 and pitch <= 0.8):
                out.append((ref, 'qfn', pitch))
    return sorted(out)


def analyze(board):
    from kicad_parser import parse_kicad_pcb
    from list_nets import (read_design_rules, effective_floors,
                           find_power_nets, find_differential_pairs,
                           board_default_netclass_clearance)

    pcb = parse_kicad_pcb(board)
    layers = list(pcb.board_info.copper_layers)
    n_layers = len(layers)
    rules = read_design_rules(board)
    eff = effective_floors(rules.get('constraints') or {}, n_layers)

    clearance = board_default_netclass_clearance(board) or eff['drc_clearance']
    clearance = _num(max(clearance, eff['drc_clearance']))
    track = _num(max(eff['fab_track_width'], 0.1))
    via, drill = _num(eff['working_via_diameter']), _num(eff['working_via_drill'])
    fine_via, fine_drill = _num(eff['fine_via_diameter']), _num(eff['fine_via_drill'])

    # Name-matched grounds and supplies only: find_power_nets' pad-count
    # candidates include keyboard matrix rows and unconnected pins.
    gnd, vcc, _cand = find_power_nets(pcb)
    real = lambda t: t[1] >= 3 and not t[0].lower().startswith('unconnected-')
    gnd = sorted(filter(real, gnd), key=lambda t: -t[1])
    rails = sorted(filter(real, vcc), key=lambda t: -t[1])
    planes = []                         # [(net, layer)]
    if gnd:
        if n_layers >= 4:
            planes.append((gnd[0][0], layers[1]))
            if rails and rails[0][1] >= 10:
                planes.append((rails[0][0], layers[2]))
        else:
            planes.append((gnd[0][0], layers[-1]))
    plane_nets = [n for n, _ in planes]
    power_nets = plane_nets + [n for n, _ in rails if n not in plane_nets]
    power_nets += [n for n, _ in gnd[1:] if n not in power_nets]

    solid = {l for _, l in planes}
    if n_layers == 2:
        layer_costs = [1.0, 1.5]
    else:
        layer_costs = [3.0 if l in solid else 1.0 for l in layers]

    parts = fanout_parts(pcb)
    fine = any(p <= GRID_FINE_PITCH for _, _, p in parts)
    grid = 0.05 if fine else 0.1
    n_nets = sum(1 for n in pcb.nets.values() if n.name and len(n.pads) >= 2)
    congested = n_nets > CONGESTED_NETS or any(t == 'bga' and p <= 0.8 for _, t, p in parts)

    pairs = []
    for pr in find_differential_pairs(pcb) or []:
        p, n = (pr[0], pr[1]) if isinstance(pr, (tuple, list)) else (pr.p, pr.n)
        pairs += [getattr(p, 'name', p), getattr(n, 'name', n)]

    return dict(layers=layers, clearance=clearance, track=track, via=via,
                drill=drill, fine_via=fine_via, fine_drill=fine_drill,
                fab_clearance=_num(eff['drc_clearance']), planes=planes,
                plane_nets=plane_nets, power_nets=power_nets,
                layer_costs=layer_costs, parts=parts, grid=grid,
                congested=congested, n_nets=n_nets, pair_nets=pairs)


# --------------------------------------------------------------------------
# Running steps


class Chain:
    def __init__(self, board, work):
        self.work = work
        self.cur = os.path.abspath(board)
        self.n = 0
        self.steps = []

    def run(self, kind, args, tag):
        """Run one tool from the current board to a fresh output; returns
        (output path or None, JSON_SUMMARY dict or None, JSON_SUMMARY_MIN)."""
        self.n += 1
        out = os.path.join(self.work, f'step{self.n:02d}_{tag}.kicad_pcb')
        argv = [sys.executable, '-u', '-X', 'utf8', TOOLS[kind]] + \
            [a.replace('{IN}', self.cur).replace('{OUT}', out) for a in args]
        log = out[:-len('.kicad_pcb')] + '.log'
        env = dict(os.environ)
        env.pop('REDO_MANIFEST', None)
        with open(log, 'w') as fh:
            rc = subprocess.run(argv, cwd=self.work, stdout=fh,
                                stderr=subprocess.STDOUT, env=env).returncode
        text = open(log, errors='replace').read()
        summary = summary_min = None
        for m in re.finditer(r'^JSON_SUMMARY: (\{.*\})$', text, re.M):
            try:
                summary = json.loads(m.group(1))
            except ValueError:
                pass
        m = re.search(r'^JSON_SUMMARY_MIN: (\{.*\})$', text, re.M)
        if m:
            try:
                summary_min = json.loads(m.group(1))
            except ValueError:
                pass
        ok = rc == 0 and os.path.exists(out)
        self.steps.append({'tool': kind, 'tag': tag, 'rc': rc, 'ok': ok,
                           'argv': argv[4:], 'log': os.path.basename(log)})
        print(f"  [{self.n:02d}] {tag}: rc={rc}{'' if ok else ' (skipped)'}",
              flush=True)
        return (out if ok else None), summary, summary_min

    def accept(self, out):
        if out:
            self.cur = out


def _open_count(summary_min):
    if not summary_min:
        return None
    ppo = summary_min.get('pad_pairs_open') or {}
    return (len(summary_min.get('failed_single') or [])
            + len(summary_min.get('open_single') or [])
            + int(summary_min.get('multipoint_deficit') or 0)
            + int(ppo.get('count', 0) if isinstance(ppo, dict) else 0))


def run_plan(board, output, a, work):
    ch = Chain(board, work)
    L = a['layers']
    costs = [str(c) for c in a['layer_costs']]

    # 2. planes, first
    if a['planes']:
        out, _, _ = ch.run('planes', [
            '{IN}', '{OUT}', '--nets', *[n for n, _ in a['planes']],
            '--plane-layers', *[l for _, l in a['planes']],
            '--clearance', str(a['clearance'])], 'planes')
        ch.accept(out)

    # 3. fanout, each part, with a retry ladder keeping the best escape
    excl = [f'!{n}' for n in a['plane_nets']]
    fanned = False
    for ref, tool, pitch in a['parts']:
        fine = pitch <= 0.65
        v, d = (a['fine_via'], a['fine_drill']) if fine else (a['via'], a['drill'])
        tw = str(min(0.15, a['track']) if pitch > 0.5 else a['track'])
        if tool == 'bga':
            base = ['{IN}', '--output', '{OUT}', '--component', ref,
                    '--nets', '*', *excl, '--layers', *L,
                    '--track-width', tw, '--grid-step', str(a['grid'])]
            ladder = [
                base + ['--clearance', str(a['clearance']),
                        '--via-size', str(v), '--via-drill', str(d)],
                base + ['--clearance', str(a['fab_clearance']),
                        '--via-size', str(a['fine_via']), '--via-drill', str(a['fine_drill'])],
                base + ['--clearance', str(a['fab_clearance']),
                        '--via-size', str(a['fine_via']), '--via-drill', str(a['fine_drill']),
                        '--escape-method', 'dogbone'],
            ]
        else:
            base = ['{IN}', '--output', '{OUT}', '--component', ref,
                    '--nets', '*', *excl, '--width', tw,
                    '--grid-step', str(a['grid'])]
            ladder = [
                base + ['--clearance', str(a['clearance'])],
                base + ['--clearance', str(a['fab_clearance'])],
                base + ['--clearance', str(a['fab_clearance']),
                        '--escape-method', 'underpad',
                        '--via-size', str(a['fine_via']), '--via-drill', str(a['fine_drill']),
                        '--allow-via-in-pad'],
            ]
        best = None                    # (escaped, out)
        for i, args in enumerate(ladder):
            out, s, _ = ch.run(tool, args, f'fanout_{ref}' + (f'_r{i}' if i else ''))
            if not out:
                continue
            escaped = int((s or {}).get('escaped', 0))
            if best is None or escaped > best[0]:
                best = (escaped, out)
            if s is not None and int(s.get('failed', 0) or 0) == 0:
                break
        if best and best[0] > 0:
            ch.accept(best[1])
            fanned = True

    # 4. decaps, once, after the fanouts
    if fanned:
        out, _, _ = ch.run('decap', ['{IN}', '{OUT}', '--clearance',
                                     str(a['clearance'])], 'decap')
        ch.accept(out)

    # 5. diff pairs first
    if a['pair_nets']:
        out, s, _ = ch.run('diff', [
            '{IN}', '{OUT}', '--nets', *a['pair_nets'],
            '--track-width', str(max(0.1, a['track'])),
            '--diff-pair-gap', str(max(0.1, a['clearance'])),
            '--clearance-ceiling', str(a['clearance']),
            '--via-size', str(a['via']), '--via-drill', str(a['drill']),
            '--layers', *L, '--layer-costs', *costs,
            '--grid-step', str(a['grid'])], 'diffpairs')
        ch.accept(out)

    # 6. route every net; up to two more passes while nets are open
    args = ['{IN}', '{OUT}', '--nets', '*', '--no-bga-zones',
            '--clearance-ceiling', str(a['clearance']),
            '--track-width', str(a['track']),
            '--via-size', str(a['via']), '--via-drill', str(a['drill']),
            '--layers', *L, '--layer-costs', *costs,
            '--grid-step', str(a['grid'])]
    if a['power_nets']:
        args += ['--power-nets', *a['power_nets'], '--power-nets-widths',
                 *[str(RAIL_WIDTH)] * len(a['power_nets'])]
    if a['congested']:
        args += ['--max-ripup', '5']
    best = None                        # (open, out)
    for i in range(ROUTE_PASSES):
        out, _, smin = ch.run('route', args, 'route' + (f'_p{i + 1}' if i else ''))
        if not out:
            break
        n_open = _open_count(smin)
        if best is None or (n_open is not None and n_open <= best[0]):
            best = (n_open if n_open is not None else 1 << 30, out)
        ch.accept(out)
        if not n_open:
            break
    return ch, (best[1] if best else None), (best[0] if best else None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('input_file')
    ap.add_argument('output_file')
    ap.add_argument('--dry-run', action='store_true',
                    help='print the analysis and exit')
    args = ap.parse_args()

    a = analyze(args.input_file)
    print("AUTOPLAN_ANALYSIS: " + json.dumps(a, sort_keys=True), flush=True)
    if args.dry_run:
        return 0
    out = os.path.abspath(args.output_file)
    work = out + '.autoplan'
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work)
    from copy_board import copy_board
    src = os.path.join(work, 'input.kicad_pcb')
    copy_board(os.path.abspath(args.input_file), src)
    ch, final, n_open = run_plan(src, out, a, work)
    with open(os.path.join(work, 'plan.json'), 'w') as fh:
        json.dump({'analysis': a, 'steps': ch.steps, 'final': final,
                   'open': n_open}, fh, indent=1)
    if not final:
        print("AUTOPLAN: no route pass produced a board", flush=True)
        return 1
    copy_board(final, out)
    print("AUTOPLAN_SUMMARY: " + json.dumps(
        {'final_step': os.path.basename(final), 'open': n_open,
         'steps': len(ch.steps),
         'skipped': [s['tag'] for s in ch.steps if not s['ok']]}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
