"""A track arc's linearised pieces are immutable only where the file writer
cannot strip them. Read from a FILE (the CLI), a piece is copper no pass may
rewrite or remove, since the file holds one (arc ...) block and the writer
strips (segment ...) blocks; read from the live board (the GUI, pcbnew), the
plugin maps a piece back to its PCB_ARC and removes the arc whole, so
--force-reroute still strips an arc-routed net there, arcs included, and
applying a result that dropped those pieces removes the live arc whole.

Needs KiCad's pcbnew for the GUI half; exits 77 without it.

    python3 tests/test_arc_piece_fronts.py
"""
import os
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, os.path.join(REPO, 'py_router'))

failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


BOARD = ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
         '  (general (thickness 1.6) (legacy_teardrops no))\n  (paper "A4")\n'
         '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))\n'
         '  (setup (pad_to_mask_clearance 0))\n  (net 0 "") (net 1 "SIG")\n'
         '  (segment (start 3 5) (end 5 5) (width 0.2) (layer "F.Cu") (net 1) '
         '(uuid "11111111-0000-0000-0000-000000000001"))\n'
         '  (arc (start 5 5) (mid 5.5 3.5) (end 6 5) (width 0.2) (layer "F.Cu") (net 1) '
         '(uuid "33333333-0000-0000-0000-000000000001"))\n'
         '  (segment (start 6 5) (end 10 5) (width 0.2) (layer "F.Cu") (net 1) '
         '(uuid "11111111-0000-0000-0000-000000000002"))\n)\n')


def stripped(pcb):
    from rip_up_reroute import select_force_reroute_nets
    ripped = select_force_reroute_nets(pcb, [('SIG', 1)], ['SIG'])
    segs, _vias = ripped.get(1, ([], []))
    return len([s for s in segs if s.arc]), len([s for s in pcb.segments if s.arc]), len(segs)


def main():
    from kicad_parser import parse_kicad_pcb, is_immutable_copper
    td = tempfile.mkdtemp(prefix='krt_arcfront_')
    path = os.path.join(td, 'b.kicad_pcb')
    open(path, 'w', encoding='utf-8').write(BOARD)

    print('the file front (text parser)')
    pcb = parse_kicad_pcb(path)
    arcs, total, n = stripped(pcb)
    check('the arc is read as pieces', total > 1, f'{total}')
    check('every piece is immutable', all(is_immutable_copper(s) for s in pcb.segments if s.arc))
    check('--force-reroute strips the two tracks and no arc piece', arcs == 0 and n == 2,
          f'{arcs} piece(s), {n} item(s)')

    print('the live-board front (pcbnew)')
    try:
        import pcbnew
    except ImportError:
        print('SKIP: no pcbnew')
        return 77
    from kicad_parser import build_pcb_data_from_board
    pcb = build_pcb_data_from_board(pcbnew.LoadBoard(path))
    arcs, total, n = stripped(pcb)
    check('the arc is read as pieces', total > 1, f'{total}')
    check('no piece is immutable', not any(is_immutable_copper(s) for s in pcb.segments if s.arc))
    check('--force-reroute strips the arc whole with the tracks', arcs == total and n == 2 + total,
          f'{arcs} of {total} piece(s), {n} item(s)')

    # what the GUI does with the strip: the result without the net's copper,
    # applied to the live board, removes the arc as one item
    sys.path.insert(0, REPO)
    from kicad_routing_plugin.result_apply import apply_copper
    result = os.path.join(td, 'r.kicad_pcb')
    open(result, 'w', encoding='utf-8').write(
        '\n'.join(ln for ln in BOARD.split('\n') if not ln.lstrip().startswith(('(arc', '(segment'))
                  and 'uuid "1111' not in ln and 'uuid "3333' not in ln))
    out = apply_copper(pcbnew.LoadBoard(path), parse_kicad_pcb(result), pcbnew, dry_run=True)
    check('applying the stripped result removes the live arc whole',
          out.get('arcs_removed') == 1 and out.get('removed') == 3, f'{out}')

    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


def test_arc_piece_fronts():
    rc = main()
    if rc == 77:
        import pytest
        pytest.skip('no pcbnew')
    assert rc == 0


if __name__ == '__main__':
    sys.exit(main())
