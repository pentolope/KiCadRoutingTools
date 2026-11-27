"""autoroute's reroute_pairs step lifts a routed pair's protection.

route_diff records the pair it routes as protected in the sibling
.kicad_pro; the exact name of a protected net is the override that lets
--force-reroute strip it. autoroute names every net by a literal glob
(`autoroute._glob`: 'D_P' -> 'D_[P/]'), which selects that one net and
nothing else, so it must lift that net the way the exact name does, and no
other: a wildcard, a negated class or a '!' exclusion still lifts nothing.

    python3 tests/test_reroute_protected_globs.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))
import run_utils  # noqa: E402
from synth import RoutingBoard  # noqa: E402

PY = [sys.executable, '-X', 'utf8']
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def stage(d):
    b = RoutingBoard(w=30, h=20)
    b.pad('J1', 4, 9.65, 'D_P', sx=0.6, sy=0.3)
    b.pad('J2', 4, 10.35, 'D_N', sx=0.6, sy=0.3)
    b.pad('J3', 26, 9.65, 'D_P', sx=0.6, sy=0.3)
    b.pad('J4', 26, 10.35, 'D_N', sx=0.6, sy=0.3)
    b.pad('J5', 4, 15, 'D_PX', sx=0.6, sy=0.6)
    b.pad('J6', 26, 15, 'D_PX', sx=0.6, sy=0.6)
    b.seg(4, 15, 26, 15, 'D_PX', w=0.2)
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    return pcb


PAIR_NAME = r'\bD_[PN]\b'


def excluded(log, name):
    return any(re.search(name, line) for line in log.splitlines()
               if 'PROTECTED net(s) excluded' in line)


def run(args):
    r = subprocess.run(PY + args, capture_output=True, text=True, cwd=REPO)
    return r.returncode, r.stdout + r.stderr


def copper(path, name):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(path)
    nid = next(i for i, n in pcb.nets.items() if n.name == name)
    return sorted((round(s.start_x, 4), round(s.start_y, 4), round(s.end_x, 4),
                   round(s.end_y, 4), s.layer) for s in pcb.segments if s.net_id == nid)


def reroute(td, board, tag, nets):
    out = os.path.join(td, f'{tag}.kicad_pcb')
    rc, log = run([os.path.join(REPO, 'py_router', 'route.py'), board, out,
                   '--nets', *nets, '--force-reroute', '--clearance', '0.15',
                   '--track-width', '0.15'])
    if rc != 0:
        raise AssertionError(f'BROKEN TEST: route.py {tag} exited {rc}\n{log[-2000:]}')
    return run_utils.evidence(out, f'the {tag} board'), log


def main():
    from autoroute import _glob
    from kicad_parser import parse_kicad_pcb
    from protected_nets import rip_override_names, stash_rip_overrides
    td = tempfile.mkdtemp(prefix='krt_reroute_globs_')
    try:
        pcb = stage(os.path.join(td, 'in'))
        routed = os.path.join(td, 'pair.kicad_pcb')
        rc, log = run([os.path.join(REPO, 'py_router', 'route_diff.py'), pcb, routed,
                       '--nets', 'D_*', '!D_PX', '--track-width', '0.15',
                       '--diff-pair-gap', '0.15', '--clearance', '0.15', '--no-gnd-vias'])
        if rc != 0:
            raise AssertionError(f'BROKEN TEST: route_diff exited {rc}\n{log[-2000:]}')
        pro = os.path.splitext(run_utils.evidence(routed, 'the routed pair'))[0] + '.kicad_pro'
        with open(run_utils.evidence(pro, 'the routed project'), encoding='utf-8') as fh:
            doc = json.load(fh)
        prot = doc.setdefault('kicad_routing_tools', {}).setdefault('protected_nets', {})
        check('route_diff records D_P and D_N as protected', {'D_P', 'D_N'} <= set(prot),
              str(prot))
        prot['D_PX'] = 'length-matched group'
        with open(pro, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        base = {n: copper(routed, n) for n in ('D_P', 'D_N', 'D_PX')}
        check('the pair and D_PX carry copper', all(base.values()), str(base))

        globs = [_glob('D_P'), _glob('D_N')]
        print(f'1. --force-reroute --nets {" ".join(globs)} (autoroute reroute_pairs)')
        out, log = reroute(td, routed, 'globs', globs)
        check('the pair is stripped', '--force-reroute: stripped' in log, log[-1500:])
        check('the pair is not excluded as protected', not excluded(log, PAIR_NAME),
              log[-1500:])
        check('D_PX, which no glob names, stays protected', excluded(log, r'\bD_PX\b'),
              log[-1500:])
        check('D_PX keeps its copper', copper(out, 'D_PX') == base['D_PX'])

        print('2. --force-reroute --nets D_P D_N (the exact names)')
        _out, log = reroute(td, routed, 'exact', ['D_P', 'D_N'])
        check('the pair is stripped', '--force-reroute: stripped' in log, log[-1500:])

        for tag, nets in (('wildcard', ['D_[PN]*']), ('negated', ['D_[!X]'])):
            print(f'3. --force-reroute --nets {" ".join(nets)} ({tag}: no override)')
            out, log = reroute(td, routed, tag, nets)
            check('nothing is stripped', '--force-reroute: stripped' not in log, log[-1500:])
            check('the pair is excluded as protected', excluded(log, PAIR_NAME), log[-1500:])
            check('the pair keeps its copper',
                  all(copper(out, n) == base[n] for n in ('D_P', 'D_N')))

        print('4. the stashed overrides are the nets the literal globs name, no other')
        board = parse_kicad_pcb(routed)
        stash_rip_overrides(board, [_glob('D_P'), 'D_?', 'D_[!X]', '!' + _glob('D_N'),
                                    'D_[PN]*'])
        got = rip_override_names(board)
        check("only D_P ('D_?', a negated class, an exclusion and a wildcard lift none)",
              got == {'D_P'}, str(sorted(got)))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
