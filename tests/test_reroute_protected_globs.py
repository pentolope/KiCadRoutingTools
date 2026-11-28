"""autoroute's reroute_pairs step lifts a routed pair's protection.

route_diff records the pair it routes as protected in the sibling
.kicad_pro; the exact name of a protected net is the override that lets
--force-reroute strip it. autoroute names every net by a literal glob
(`autoroute._glob`: 'D_P' -> 'D_[P/]'), which selects that one net and
nothing else, so it must lift that net the way the exact name does, and no
other: a wildcard, a negated class or a '!' exclusion, even one that selects
a single net ('D_N*', 'D_?X', 'D_[!PX]', '!RS[T/]'), or a class that selects
several nets ('D_[PN]', 'SIG[0-7]') still lifts nothing; a bracketed '*' is
the character ('A[*][B/]' names 'A*B'). A net is stripped when none of its
input tracks (by uuid) is left on the output board.

In plain --nets only an exact name lifts protection (#521): a class-tier
call of autoroute (its nets by literal glob, an uncoupled pair in
--rip-existing-nets, the in-run rip of pre-existing copper on) leaves a
protected pair in its --nets as it is, though the pair walls a net of the
call in, while the same call naming the nets exactly rips the pair to free it.
So too where route.py takes this step's copper off a foreign pad: a
protected net over another net's pad keeps its copper under a glob in plain
--nets and loses it under its exact name.

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
    for k, n in enumerate(('SIG0', 'SIG1', 'SIG5', 'A*B', '!RST')):
        b.pad(f'S{k}', 4 + 3 * k, 18, n, sx=0.6, sy=0.6)
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
    """{uuid: (start, end, layer)} of NAME's tracks on the board at PATH."""
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(path)
    nid = next(i for i, n in pcb.nets.items() if n.name == name)
    return {s.uuid: (round(s.start_x, 4), round(s.start_y, 4), round(s.end_x, 4),
                     round(s.end_y, 4), s.layer) for s in pcb.segments if s.net_id == nid}


def stripped(base, out, names):
    return all(copper(out, n) and not set(copper(out, n)) & set(base[n]) for n in names)


def pad_board(d):
    """Protected D_P's track runs over net Y's pad K1."""
    b = RoutingBoard(w=30, h=20)
    for ref, x, y, n in (('J1', 5, 10, 'D_P'), ('J2', 25, 10, 'D_P'), ('J3', 5, 11, 'D_N'),
                         ('J4', 25, 11, 'D_N'), ('K1', 15, 10, 'Y'), ('K2', 15, 16, 'Y')):
        b.pad(ref, x, y, n, sx=0.6, sy=0.6)
    b.seg(5, 10, 25, 10, 'D_P', w=0.2)
    b.seg(5, 11, 25, 11, 'D_N', w=0.2)
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    with open(os.path.join(d, 'in.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump({'kicad_routing_tools': {'protected_nets': {
            'D_P': 'diff-pair', 'D_N': 'diff-pair'}}}, fh)
    return pcb


def tier_board(d):
    """D_P/D_N (protected) wall X's pad in on F.Cu; U_P (routed) and U_N are
    the uncoupled pair."""
    b = RoutingBoard(w=30, h=16)
    for ref, x, y, n in (('J1', 5, 10, 'D_P'), ('J2', 25, 10, 'D_P'), ('J3', 5, 11, 'D_N'),
                         ('J4', 25, 11, 'D_N'), ('J5', 15, 6, 'X'), ('J6', 15, 1, 'X'),
                         ('J7', 3, 14, 'U_P'), ('J8', 27, 14, 'U_P'), ('J9', 3, 15, 'U_N'),
                         ('J10', 27, 15, 'U_N')):
        b.pad(ref, x, y, n, sx=0.6, sy=0.6)
    for x1, y1, x2, y2 in ((5, 10, 5, 3), (5, 3, 25, 3), (25, 3, 25, 10)):
        b.seg(x1, y1, x2, y2, 'D_P', w=0.2)
    b.seg(5, 11, 25, 11, 'D_N', w=0.2)
    b.seg(3, 14, 27, 14, 'U_P', w=0.2)
    os.makedirs(d)
    pcb = os.path.join(d, 'in.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write(b.text())
    with open(os.path.join(d, 'in.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump({'kicad_routing_tools': {'protected_nets': {
            'D_P': 'diff-pair', 'D_N': 'diff-pair'}}}, fh)
    return pcb


def reroute(td, board, tag, nets):
    out = os.path.join(td, f'{tag}.kicad_pcb')
    rc, log = run([os.path.join(REPO, 'py_router', 'route.py'), board, out,
                   '--nets', *nets, '--force-reroute', '--clearance', '0.15',
                   '--track-width', '0.15'])
    if rc != 0:
        raise AssertionError(f'BROKEN TEST: route.py {tag} exited {rc}\n{log[-2000:]}')
    return run_utils.evidence(out, f'the {tag} board'), log


def tier_call(board, out, name):
    r = subprocess.run(PY + [os.path.join(REPO, 'py_router', 'route.py'), board, out, '--nets',
                             *[name(n) for n in ('D_P', 'D_N', 'U_P', 'U_N', 'X')],
                             '--rip-existing-nets', name('U_P'), name('U_N'),
                             '--layers', 'F.Cu', '--clearance', '0.15', '--track-width', '0.2'],
                       capture_output=True, text=True, cwd=REPO,
                       env=dict(os.environ, KICAD_RIP_PREEXISTING='1'))
    if r.returncode != 0:
        raise AssertionError(f'BROKEN TEST: route.py {out} exited {r.returncode}\n'
                             f'{(r.stdout + r.stderr)[-2000:]}')
    return run_utils.evidence(out, 'the tier board')


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
        check('the pair is stripped', stripped(base, out, ('D_P', 'D_N')), log[-1500:])
        check('the pair is not excluded as protected', not excluded(log, PAIR_NAME),
              log[-1500:])
        check('D_PX, which no glob names, stays protected', excluded(log, r'\bD_PX\b'),
              log[-1500:])
        check('D_PX keeps its copper', copper(out, 'D_PX') == base['D_PX'])

        print('2. --force-reroute --nets D_P D_N (the exact names)')
        out, log = reroute(td, routed, 'exact', ['D_P', 'D_N'])
        check('the pair is stripped', stripped(base, out, ('D_P', 'D_N')), log[-1500:])

        for tag, nets in (('wildcard', ['D_[PN]*']), ('negated', ['D_[!X]']),
                          ('two-net class', ['D_[PN]'])):
            print(f'3. --force-reroute --nets {" ".join(nets)} ({tag}: no override)')
            out, log = reroute(td, routed, tag, nets)
            check('nothing is stripped', '--force-reroute: stripped' not in log, log[-1500:])
            check('the pair is excluded as protected', excluded(log, PAIR_NAME), log[-1500:])
            check('the pair keeps its copper',
                  all(copper(out, n) == base[n] for n in ('D_P', 'D_N')))

        print('4. the stashed overrides are the nets the literal globs name, no other')
        board = parse_kicad_pcb(routed)
        stash_rip_overrides(board, [_glob('D_P'), 'D_?', 'D_[!X]', '!' + _glob('D_N'),
                                    'D_[PN]*', 'D_[PN]', 'SIG[0-7]', 'D_N*', 'D_?X',
                                    'D_[!PX]', '!RS[T/]', _glob('A*B')])
        got = rip_override_names(board)
        check("only D_P and A*B (wildcards, negated classes and exclusions, one-net "
              "ones too, and the classes 'D_[PN]' and 'SIG[0-7]' lift none)",
              got == {'D_P', 'A*B'}, str(sorted(got)))

        print('5. a class-tier call: --nets by literal glob, --rip-existing-nets the '
              'uncoupled pair, KICAD_RIP_PREEXISTING=1')
        tb = tier_board(os.path.join(td, 'tier'))
        tout = tier_call(tb, os.path.join(td, 'tier', 'globs.kicad_pcb'), _glob)
        check('the protected pair keeps every track as it was',
              all(copper(tout, n) == copper(tb, n) for n in ('D_P', 'D_N')),
              str({n: copper(tout, n) for n in ('D_P', 'D_N')}))
        check('and X, which only a rip of the pair could free, stays open',
              not copper(tout, 'X'), str(copper(tout, 'X')))

        print('6. the same call naming every net exactly in --nets (the #521 override)')
        tout = tier_call(tb, os.path.join(td, 'tier', 'exact.kicad_pcb'), lambda n: n)
        check('the protected pair is ripped', copper(tout, 'D_P') != copper(tb, 'D_P'),
              str(copper(tout, 'D_P')))
        check('and X routes', bool(copper(tout, 'X')), str(copper(tout, 'X')))

        print('7. a protected net over a foreign pad, plain --nets (pad contact)')
        pb = pad_board(os.path.join(td, 'pad'))
        for tag, nets, kept in (('glob', [_glob('D_P')], True), ('exact', ['D_P'], False)):
            pout = os.path.join(td, 'pad', f'{tag}.kicad_pcb')
            rc, log = run([os.path.join(REPO, 'py_router', 'route.py'), pb, pout, '--nets',
                           *nets, '--clearance', '0.15', '--track-width', '0.2'])
            if rc != 0:
                raise AssertionError(f'BROKEN TEST: route.py pad {tag} exited {rc}\n'
                                     f'{log[-2000:]}')
            same = copper(run_utils.evidence(pout, 'the pad board'), 'D_P') == copper(pb, 'D_P')
            check(f'--nets {" ".join(nets)}: D_P '
                  + ('keeps every track' if kept else 'loses its track off the pad'),
                  same == kept, str(copper(pout, 'D_P')))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
