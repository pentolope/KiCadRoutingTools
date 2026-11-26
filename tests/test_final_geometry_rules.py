"""The end-of-run track geometry pass (final_geometry) holds the board's own
rules and leaves alone the copper that is not its to change, on both the
"nothing to route" return and a run that routes:

  (a) a .kicad_dru net keep-out ('disallow track' in a rule area);
  (b) a .kicad_dru net-pair clearance rule above the board's clearance;
  (c) a track arc: the file's (arc ...) block stays, nothing bypasses it
      (on a run that routes, the in-run smoother used to);
  (d) copper a KiCad tuning pattern owns;
  (e) a deliberate open stub: a one-pad trace antenna (on a run that
      routes, the in-run dead-end sweep took it and the smoother re-laid it);
  (f) a protected net (.kicad_pro protected_nets) and a net a .kicad_dru
      length rule binds;
  (g) a guide-corridor step keeps its drawn geometry (GUI front);
  (h) the file is left untouched when a track chosen for replacement is not
      found in it;
  (i) a loop of a track arc and a straight track between the same points:
      the strict collapse removes the straight track, never the arc's
      pieces, which no (segment ...) block in the file holds;
  (j) the atomic write keeps the file's mode, writes through a symlink to
      its target, and syncs the bytes before the swap;
  (k) a shipped board whose net ids differ from the run's (an id on one
      and not the other) is not edited, and the log says why;
  (l) a user track a pass split at a tee (#1217) stays the user's: both
      pieces are read-only to the pass, with or without a chain's start
      board, while the branch the run laid stays editable.

Each oracle reads the written file with its own regexes and shapely.

    python3 tests/test_final_geometry_rules.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from shapely.geometry import LineString, Polygon, box

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS)
sys.path.insert(0, TESTS)
import run_utils  # noqa: E402

RUN_ALL_TIMEOUT = 900
PY = [sys.executable, '-B', '-X', 'utf8']
PRO = os.path.join(TESTS, 'fixtures', 'sense_branch', 'sense_star.kicad_pro')
failures = []


def check(label, ok, detail=''):
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


HEAD = ('(kicad_pcb (version 20241229) (generator "pcbnew") (generator_version "9.0")\n'
        '  (general (thickness 1.6) (legacy_teardrops no))\n  (paper "A4")\n'
        '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user) '
        '(5 "F.SilkS" user "F.Silkscreen") (1 "F.Mask" user) (13 "F.Paste" user) '
        '(7 "B.SilkS" user "B.Silkscreen") (3 "B.Mask" user) (15 "B.Paste" user))\n'
        '  (setup (pad_to_mask_clearance 0))\n')
TAIL = ('  (gr_rect (start 0 0) (end 30 20) (stroke (width 0.1) (type default)) '
        '(fill no) (layer "Edge.Cuts") (uuid "22222222-0000-0000-0000-000000000001"))\n)\n')


def pad(ref, x, y, net, name, s=1.0):
    return (f'  (footprint "T:P" (layer "F.Cu") (at {x} {y})\n'
            f'    (property "Reference" "{ref}" (at 0 -2 0) (layer "F.SilkS") '
            f'(effects (font (size 1 1))))\n'
            f'    (pad "1" smd rect (at 0 0) (size {s} {s}) (layers "F.Cu" "F.Paste" '
            f'"F.Mask") (net {net} "{name}")))\n')


def seg(a, b, net, w=0.2, uid=None):
    seg.n += 1
    uid = uid or f"11111111-0000-0000-0000-{seg.n:012d}"
    return (f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) '
            f'(layer "F.Cu") (net {net}) (uuid "{uid}"))\n')


seg.n = 0


def nets(*names):
    return '  (net 0 "")' + ''.join(f' (net {i + 1} "{n}")' for i, n in enumerate(names)) + '\n'


def stair_net(net):
    """A pad-to-pad net whose 2:1 run the pass re-lays octolinearly."""
    return [pad('J1', 3, 5, net, 'STAIR'), pad('J2', 10, 5.5, net, 'STAIR'),
            seg((3, 5), (5, 5), net), seg((5, 5), (7, 5.5), net), seg((7, 5.5), (10, 5.5), net)]


def unrouted(net, name):
    return [pad('J11', 3, 15, net, name), pad('J12', 12, 15, net, name)]


def run(td, body, net_args, dru=None, pro_edit=None):
    src = os.path.join(td, 'in.kicad_pcb')
    open(src, 'w', encoding='utf-8').write(HEAD + ''.join(body) + TAIL)
    pro = json.load(open(PRO, encoding='utf-8'))
    if pro_edit:
        pro_edit(pro)
    json.dump(pro, open(os.path.join(td, 'in.kicad_pro'), 'w', encoding='utf-8'))
    if dru:
        open(os.path.join(td, 'in.kicad_dru'), 'w', encoding='utf-8').write(dru)
    # the tracks as an earlier chain step's, so the pass may re-lay them
    start = os.path.join(td, 'start.kicad_pcb')
    open(start, 'w', encoding='utf-8').write(HEAD + ''.join(
        b for b in body if not b.lstrip().startswith(('(segment', '(arc'))) + TAIL)
    out = os.path.join(td, 'out.kicad_pcb')
    r = subprocess.run(PY + [os.path.join('py_router', 'route.py'), src, out, '--nets'] + net_args
                       + ['--tidy-copper-from', start],
                       capture_output=True, text=True, cwd=REPO)
    run_utils.evidence(out)
    return open(out, encoding='utf-8').read(), r.stdout


def tracks(text, net):
    out = []
    for m in re.finditer(r'\(segment\s+\(start ([-\d.]+) ([-\d.]+)\)\s+\(end ([-\d.]+) ([-\d.]+)\)'
                         r'\s+\(width ([\d.]+)\)\s+\(layer "([^"]+)"\)\s+\(net (\d+)\)', text, re.S):
        if int(m.group(7)) == net:
            out.append(((float(m.group(1)), float(m.group(2))),
                        (float(m.group(3)), float(m.group(4))), float(m.group(5))))
    return out


def shape(t):
    return LineString([t[0], t[1]]).buffer(t[2] / 2.0)


def same(a, b):
    def key(ts):
        return sorted((tuple(sorted((tuple(round(v, 6) for v in s), tuple(round(v, 6) for v in e))))
                       , w) for s, e, w in ts)
    return key(a) == key(b)


def both_paths(label, body, routed_args, dru=None, pro_edit=None, judge=None, routes=True):
    for path, extra in (('nothing to route', []), ('a run that routes', ['RR']))[:2 if routes else 1]:
        td = tempfile.mkdtemp(prefix='krt_fgr_')
        try:
            b = body + (unrouted(9, 'RR') if extra else [])
            text, log = run(td, b, routed_args + extra, dru=dru, pro_edit=pro_edit)
            judge(f'{label}, {path}', text, log)
        finally:
            shutil.rmtree(td, ignore_errors=True)


KO = Polygon([(5.3, 4.7), (5.6, 4.7), (5.6, 4.93), (5.3, 4.93)])
KO_ZONE = ('  (zone (net 0) (net_name "") (layer "F.Cu") (uuid "55555555-0000-0000-0000-000000000001") '
           '(name "KO") (hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.25) '
           '(keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) '
           '(footprints allowed)) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
           '(polygon (pts (xy 5.3 4.7) (xy 5.6 4.7) (xy 5.6 4.93) (xy 5.3 4.93))))\n')
KO_DRU = ('(version 1)\n(rule "ko"\n  (constraint disallow track via)\n'
          '  (condition "A.intersectsArea(\'KO\') && A.NetName == \'STAIR\'"))\n')


def in_process():
    sys.path.insert(0, os.path.join(REPO, 'py_router'))
    from synth import make_pad, make_seg, make_pcb, make_net
    from kicad_parser import BoardInfo
    from routing_config import GridRouteConfig
    import final_geometry as fg
    from route import _late_final_geometry
    NET = 1

    print('(g) a guide-corridor step (GUI front)')
    segs = [make_seg(3, 5, 5, 5), make_seg(5, 5, 7, 5.5), make_seg(7, 5.5, 10, 5.5)]
    pads = [make_pad(NET, 3, 5, ref='J1', size_x=1, size_y=1),
            make_pad(NET, 10, 5.5, ref='J2', size_x=1, size_y=1)]
    pcb = make_pcb(nets={NET: make_net(NET, 'SIG')}, segments=segs, pads_by_net={NET: pads},
                   board_info=BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                                        copper_layers=['F.Cu', 'B.Cu']))
    before = [(x.start_x, x.start_y, x.end_x, x.end_y) for x in pcb.segments]
    for guided in (False, True):
        cfg = GridRouteConfig()
        cfg.guide_corridor_enabled = guided
        pcb.segments = list(segs)
        rd = {'results': [{'new_segments': list(segs), 'new_vias': []}]}
        _late_final_geometry(pcb, None, True, rd,
                             lambda r: ({NET: [x for res in r['results'] for x in res['new_segments']]}, {}),
                             {}, {'SIG'}, False, cfg, authority={'chain': {}, 'nets': set()})
        after = [(x.start_x, x.start_y, x.end_x, x.end_y) for x in pcb.segments]
        if guided:
            check('a guided step ships as it was drawn', after == before)
        else:
            check('(control: an unguided step is re-laid)', after != before)

    print('(h) a replacement whose track is not in the file')
    td = tempfile.mkdtemp(prefix='krt_fgr_')
    path = os.path.join(td, 'b.kicad_pcb')
    text = ('(kicad_pcb (version 20241229) (generator "pcbnew")\n  (layers (0 "F.Cu" signal) '
            '(2 "B.Cu" signal) (25 "Edge.Cuts" user))\n  (net 0 "") (net 1 "SIG")\n'
            '  (segment (start 3 5) (end 5 5) (width 0.2) (layer "F.Cu") (net 1) '
            '(uuid "11111111-0000-0000-0000-000000000001"))\n)\n')
    open(path, 'w', encoding='utf-8').write(text)
    phantom = make_seg(7, 7, 8, 8)
    real = fg.legalize_track_geometry

    def fake(board, *a, **k):
        return [board.segments[0], phantom], [make_seg(3, 5, 6, 5)], {'nets': 1}
    fg.legalize_track_geometry = fake
    try:
        _late_final_geometry(make_pcb(nets={NET: make_net(NET, 'SIG')}), path, False, None,
                             None, {}, {'SIG'}, False, GridRouteConfig())
    finally:
        fg.legalize_track_geometry = real
        got = open(path, encoding='utf-8').read()
        shutil.rmtree(td, ignore_errors=True)
    check('the file is left byte for byte', got == text)

    print('(j) the atomic write')
    from route import _write_atomically
    td = tempfile.mkdtemp(prefix='krt_fgr_')
    try:
        target = os.path.join(td, 'real.kicad_pcb')
        link = os.path.join(td, 'link.kicad_pcb')
        open(target, 'w', encoding='utf-8').write('old')
        os.chmod(target, 0o644)
        os.symlink(target, link)
        order = []
        fsync, replace = os.fsync, os.replace
        os.fsync = lambda fd: (order.append('fsync'), fsync(fd))[1]
        os.replace = lambda a, b: (order.append('replace'), replace(a, b))[1]
        try:
            _write_atomically(link, 'new')
        finally:
            os.fsync, os.replace = fsync, replace
        check('the link is still a link to the target', os.path.islink(link)
              and os.path.realpath(link) == os.path.realpath(target))
        check('the target holds the new text', open(target, encoding='utf-8').read() == 'new')
        check('the file keeps mode 0644', (os.stat(target).st_mode & 0o777) == 0o644,
              oct(os.stat(target).st_mode & 0o777))
        check('the bytes are synced before the swap', order == ['fsync', 'replace'], f'{order}')
        plain = os.path.join(td, 'plain.kicad_pcb')
        open(plain, 'w', encoding='utf-8').write('old')
        os.chmod(plain, 0o644)
        _write_atomically(plain, 'new')
        check('a plain file keeps mode 0644', (os.stat(plain).st_mode & 0o777) == 0o644,
              oct(os.stat(plain).st_mode & 0o777))
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('(k) a shipped board whose net ids are not the run\'s')
    import contextlib
    import io
    td = tempfile.mkdtemp(prefix='krt_fgr_')
    try:
        path = os.path.join(td, 'b.kicad_pcb')
        text2 = text.replace('(net 0 "") (net 1 "SIG")', '(net 0 "") (net 1 "SIG") (net 2 "FOO")')
        open(path, 'w', encoding='utf-8').write(text2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _late_final_geometry(make_pcb(nets={0: make_net(0, ''), NET: make_net(NET, 'SIG')}),
                                 path, False, None, None, {}, {'SIG'}, False, GridRouteConfig(),
                                 authority={'chain': {}, 'nets': set()})
        said = buf.getvalue()
        check('the file is left as it was', open(path, encoding='utf-8').read() == text2)
        check('the log names the mismatch', "net 2 is 'FOO' on the shipped board" in said, said)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print('(l) a user track split at a tee')
    from improvement_gate import copper_signature
    td = tempfile.mkdtemp(prefix='krt_fgr_')
    try:
        path = os.path.join(td, 'b.kicad_pcb')
        segs3 = ''.join(f'  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.2) '
                        f'(layer "F.Cu") (net 1) (uuid "11111111-0000-0000-0000-00000000000{i}"))\n'
                        for i, (a, b) in enumerate((((3, 5), (5, 5)), ((5, 5), (10, 5)),
                                                    ((5, 5), (5, 8)))))
        open(path, 'w', encoding='utf-8').write(
            '(kicad_pcb (version 20241229) (generator "pcbnew")\n  (layers (0 "F.Cu" signal) '
            '(2 "B.Cu" signal) (25 "Edge.Cuts" user))\n  (net 0 "") (net 1 "SIG")\n' + segs3 + ')\n')
        user = copper_signature([make_seg(3, 5, 10, 5, net_id=NET)], [], {NET: 'SIG'}.get)
        seen = {}

        def capture(board, scope, config, clears, readonly_ids=frozenset()):
            seen['ro'] = sorted((x.start_x, x.start_y, x.end_x, x.end_y)
                                for x in board.segments if id(x) in readonly_ids)
            return [], [], {}
        fg.legalize_track_geometry = capture
        try:
            for label, authority in (('no chain authority', None),
                                     ('the chain started with it', {'chain': user, 'nets': set()})):
                seen.clear()
                _late_final_geometry(make_pcb(nets={0: make_net(0, ''), NET: make_net(NET, 'SIG')}),
                                     path, False, None, None, user, {'SIG'}, False,
                                     GridRouteConfig(), authority=authority)
                check(f'{label}: both pieces read-only, the branch editable',
                      seen.get('ro') == [(3, 5, 5, 5), (5, 5, 10, 5)], f'{seen}')
        finally:
            fg.legalize_track_geometry = real
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    print('(a) a .kicad_dru keep-out for STAIR over rule area KO')

    def ko(label, text, log):
        inside = [t for t in tracks(text, 1) if shape(t).intersects(KO)]
        check(f'{label}: no STAIR track enters KO', not inside, f'{inside}')
    both_paths('keep-out', [nets('STAIR', 'FOO', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')]
               + stair_net(1) + [KO_ZONE], ['STAIR'], dru=KO_DRU, judge=ko)

    print('(b) a 0.35 mm STAIR-FOO rule over a 0.2 mm board clearance')
    foo = [pad('J9', 5.6, 1, 2, 'FOO'), pad('J10', 5.6, 4.45, 2, 'FOO', s=0.3),
           seg((5.6, 1), (5.6, 4.45), 2)]
    foo_copper = [box(5.1, 0.5, 6.1, 1.5), box(5.45, 4.3, 5.75, 4.6),
                  LineString([(5.6, 1), (5.6, 4.45)]).buffer(0.1)]
    dru = ('(version 1)\n(rule "stair_foo"\n  (condition "A.NetName == \'STAIR\' && '
           'B.NetName == \'FOO\'")\n  (constraint clearance (min 0.35mm)))\n')

    def pair(label, text, log):
        gap = min(shape(t).distance(f) for t in tracks(text, 1) for f in foo_copper)
        check(f'{label}: STAIR keeps 0.35 mm from FOO', gap >= 0.35 - 1e-4, f'{gap:.4f} mm')
    both_paths('pair rule', [nets('STAIR', 'FOO', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')]
               + stair_net(1) + foo, ['STAIR'], dru=dru, judge=pair)

    print('(c) a net carrying a track arc')
    arc = ('  (arc (start 5 5) (mid 5.5 3.5) (end 6 5) (width 0.2) (layer "F.Cu") (net 1) '
           '(uuid "33333333-0000-0000-0000-000000000001"))\n')
    sig = [pad('J1', 3, 5, 1, 'SIG'), pad('J2', 10, 5, 1, 'SIG'),
           seg((3, 5), (5, 5), 1), arc, seg((6, 5), (10, 5), 1)]

    def arcs(label, text, log):
        check(f'{label}: the arc block ships as it was', arc.strip() in text)
        check(f'{label}: its tracks ship as they were',
              same(tracks(text, 1), [((3, 5), (5, 5), 0.2), ((6, 5), (10, 5), 0.2)]),
              f'{tracks(text, 1)}')
    both_paths('arc', [nets('SIG', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')] + sig, ['SIG'],
               judge=arcs)

    print('(d) copper a tuning pattern owns')
    tuned = [pad('J1', 3, 5, 1, 'STAIR'), pad('J2', 10, 5.5, 1, 'STAIR'),
             seg((3, 5), (5, 5), 1, uid='44444444-0000-0000-0000-000000000001'),
             seg((5, 5), (7, 5.5), 1, uid='44444444-0000-0000-0000-000000000002'),
             seg((7, 5.5), (10, 5.5), 1, uid='44444444-0000-0000-0000-000000000003'),
             '  (generated (uuid "66666666-0000-0000-0000-000000000001") (type tuning_pattern) '
             '(name "Tuning Pattern") (layer "F.Cu") (members "44444444-0000-0000-0000-000000000001" '
             '"44444444-0000-0000-0000-000000000002" "44444444-0000-0000-0000-000000000003"))\n']

    def tune(label, text, log):
        check(f'{label}: the tuned tracks ship as they were',
              same(tracks(text, 1), [((3, 5), (5, 5), 0.2), ((5, 5), (7, 5.5), 0.2),
                                     ((7, 5.5), (10, 5.5), 0.2)]), f'{tracks(text, 1)}')
    both_paths('tuning pattern', [nets('STAIR', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')]
               + tuned, ['STAIR'], judge=tune)

    print('(e) a one-pad trace antenna')
    ant = [((3, 5), (8, 5), 0.5), ((8, 5), (8, 2), 0.5), ((8, 2), (14, 2), 0.5)]
    stubs = [pad('J1', 3, 5, 1, 'ANT')] + [seg(a, b, 1, w) for a, b, w in ant]

    def keep(label, text, log):
        check(f'{label}: the antenna ships whole', same(tracks(text, 1), ant), f'{tracks(text, 1)}')
    both_paths('antenna', [nets('ANT', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')] + stubs,
               ['ANT'], judge=keep)

    print('(i) a loop of an arc and a straight track')
    loop = [pad('J1', 3, 5, 1, 'SIG'), pad('J2', 10, 5, 1, 'SIG'),
            seg((3, 5), (5, 5), 1), arc, seg((5, 5), (6, 5), 1), seg((6, 5), (10, 5), 1)]

    def loops(label, text, log):
        check(f'{label}: the arc block ships as it was', arc.strip() in text)
        check(f'{label}: the straight branch of the loop is removed',
              same(tracks(text, 1), [((3, 5), (5, 5), 0.2), ((6, 5), (10, 5), 0.2)]),
              f'{tracks(text, 1)}')
    both_paths('arc loop', [nets('SIG', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')] + loop,
               ['SIG'], judge=loops, routes=False)

    print('(f) a protected net and a length-ruled net')
    orig = [((3, 5), (5, 5), 0.2), ((5, 5), (7, 5.5), 0.2), ((7, 5.5), (10, 5.5), 0.2)]

    def untouched(label, text, log):
        check(f'{label}: STAIR ships as it was', same(tracks(text, 1), orig), f'{tracks(text, 1)}')

    def protect(pro):
        pro.setdefault('kicad_routing_tools', {})['protected_nets'] = {'STAIR': 'diff-pair'}
    body = [nets('STAIR', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'RR')] + stair_net(1)
    both_paths('protected', body, ['STAIR'], pro_edit=protect, judge=untouched)
    length = ('(version 1)\n(rule "len"\n  (condition "A.NetName == \'STAIR\'")\n'
              '  (constraint length (min 7mm) (max 8mm)))\n')
    both_paths('length rule', body, ['STAIR'], dru=length, judge=untouched)

    in_process()
    if failures:
        print(f'FAIL: {len(failures)}: ' + '; '.join(failures))
        return 1
    print('ALL PASS')
    return 0


def test_final_geometry_rules():
    assert main() == 0


if __name__ == '__main__':
    sys.exit(main())
