"""Build tests/repro/toy_pcba.kicad_pcb: 2 layers, TQFP-32, two 2x8 headers,
6 caps, 8 resistors, 26 nets, header pin order reversed against the part's so
the airwires cross.

    python3 tests/repro/make_toy_pcba.py [out.kicad_pcb]
"""
from __future__ import annotations

import os
import sys

import pcbnew

MM = pcbnew.FromMM

BOARD_W = 40.0
BOARD_H = 28.0


def _vec(x_mm, y_mm):
    return pcbnew.VECTOR2I(MM(x_mm), MM(y_mm))


def _add_net(board, name, cache):
    if name in cache:
        return cache[name]
    ni = pcbnew.NETINFO_ITEM(board, name)
    board.Add(ni)
    cache[name] = ni
    return ni


def _smd_pad(fp, num, x_mm, y_mm, w_mm, h_mm, net):
    pad = pcbnew.PAD(fp)
    pad.SetNumber(str(num))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(pcbnew.VECTOR2I(MM(w_mm), MM(h_mm)))
    pad.SetPosition(_vec(x_mm, y_mm))
    lset = pcbnew.LSET()
    for lyr in (pcbnew.F_Cu, pcbnew.F_Paste, pcbnew.F_Mask):
        lset.AddLayer(lyr)
    pad.SetLayerSet(lset)
    if net is not None:
        pad.SetNet(net)
    fp.Add(pad)
    return pad


def _tht_pad(fp, num, x_mm, y_mm, net, diameter=1.7, drill=1.0):
    pad = pcbnew.PAD(fp)
    pad.SetNumber(str(num))
    pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(diameter), MM(diameter)))
    pad.SetDrillSize(pcbnew.VECTOR2I(MM(drill), MM(drill)))
    pad.SetPosition(_vec(x_mm, y_mm))
    lset = pcbnew.LSET.AllCuMask()
    for lyr in (pcbnew.F_Mask, pcbnew.B_Mask):
        lset.AddLayer(lyr)
    pad.SetLayerSet(lset)
    if net is not None:
        pad.SetNet(net)
    fp.Add(pad)
    return pad


def _new_fp(board, ref, x_mm, y_mm, value=""):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(value or ref)
    fp.SetPosition(_vec(x_mm, y_mm))
    board.Add(fp)
    return fp


def _courtyard(fp, cx_mm, cy_mm, w_mm, h_mm):
    """A closed F.CrtYd rectangle; the placer needs one to move the part."""
    hw, hh = w_mm / 2.0, h_mm / 2.0
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        seg = pcbnew.PCB_SHAPE(fp, pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_vec(cx_mm + x1, cy_mm + y1))
        seg.SetEnd(_vec(cx_mm + x2, cy_mm + y2))
        seg.SetLayer(pcbnew.F_CrtYd)
        seg.SetWidth(MM(0.05))
        fp.Add(seg)


def build(out_path: str) -> str:
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(2)
    nets = {}

    for (x1, y1, x2, y2) in [(0, 0, BOARD_W, 0), (BOARD_W, 0, BOARD_W, BOARD_H),
                             (BOARD_W, BOARD_H, 0, BOARD_H), (0, BOARD_H, 0, 0)]:
        seg = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(_vec(x1, y1))
        seg.SetEnd(_vec(x2, y2))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(MM(0.1))
        board.Add(seg)

    gnd = _add_net(board, "GND", nets)
    vcc = _add_net(board, "+3V3", nets)

    # ---- U1: TQFP-32, 0.8 mm pitch, 7x7 mm body, centred on the board -----
    ucx, ucy = BOARD_W / 2.0, BOARD_H / 2.0
    u1 = _new_fp(board, "U1", ucx, ucy, "TQFP-32")
    _courtyard(u1, ucx, ucy, 9.4, 9.4)
    pitch = 0.8
    span = (8 - 1) * pitch / 2.0          # 8 pads per side
    row = 3.7                             # pad-row offset from body centre
    u1_pads = {}
    n = 1
    # KiCad pin order: left side top->bottom, bottom left->right,
    # right side bottom->top, top right->left.
    for i in range(8):                     # 1..8  left
        u1_pads[n] = _smd_pad(u1, n, ucx - row, ucy - span + i * pitch,
                              1.5, 0.45, None); n += 1
    for i in range(8):                     # 9..16 bottom
        u1_pads[n] = _smd_pad(u1, n, ucx - span + i * pitch, ucy + row,
                              0.45, 1.5, None); n += 1
    for i in range(8):                     # 17..24 right
        u1_pads[n] = _smd_pad(u1, n, ucx + row, ucy + span - i * pitch,
                              1.5, 0.45, None); n += 1
    for i in range(8):                     # 25..32 top
        u1_pads[n] = _smd_pad(u1, n, ucx + span - i * pitch, ucy - row,
                              0.45, 1.5, None); n += 1

    # ---- J1 / J2: 2x8 through-hole headers on the left and right edges ----
    def header(ref, x0, y0):
        fp = _new_fp(board, ref, x0, y0, "Conn_02x08")
        _courtyard(fp, x0, y0, 2 * 2.54 + 1.0, 8 * 2.54 + 1.0)
        pads = {}
        p = 1
        for r in range(8):
            for c in range(2):
                pads[p] = _tht_pad(fp, p, x0 - 1.27 + c * 2.54,
                                   y0 - (8 - 1) * 2.54 / 2.0 + r * 2.54, None)
                p += 1
        return pads

    j1 = header("J1", 4.0, BOARD_H / 2.0)
    j2 = header("J2", BOARD_W - 4.0, BOARD_H / 2.0)

    # ---- 0402 two-pad parts ------------------------------------------------
    def part_0402(ref, cx, cy, value, vertical=False):
        fp = _new_fp(board, ref, cx, cy, value)
        _courtyard(fp, cx, cy, 1.5 if not vertical else 0.9,
                   0.9 if not vertical else 1.5)
        if vertical:
            a = _smd_pad(fp, 1, cx, cy - 0.48, 0.6, 0.5, None)
            b = _smd_pad(fp, 2, cx, cy + 0.48, 0.6, 0.5, None)
        else:
            a = _smd_pad(fp, 1, cx - 0.48, cy, 0.5, 0.6, None)
            b = _smd_pad(fp, 2, cx + 0.48, cy, 0.5, 0.6, None)
        return a, b

    caps = []
    cap_spots = [(ucx - 6.2, ucy - 3.0, True), (ucx - 6.2, ucy + 3.0, True),
                 (ucx + 6.2, ucy - 3.0, True), (ucx + 6.2, ucy + 3.0, True),
                 (ucx - 2.0, ucy - 6.4, False), (ucx + 2.0, ucy + 6.4, False)]
    for i, (cx, cy, vert) in enumerate(cap_spots, start=1):
        a, b = part_0402("C%d" % i, cx, cy, "100nF", vertical=vert)
        a.SetNet(vcc)
        b.SetNet(gnd)
        caps.append((a, b))

    res = []
    for i in range(8):
        cx = 9.0 + (i % 4) * 5.5
        cy = 3.0 if i < 4 else BOARD_H - 3.0
        a, b = part_0402("R%d" % (i + 1), cx, cy, "33R")
        res.append((a, b))

    # ---- Nets -------------------------------------------------------------
    for pin in (1, 9, 17, 25):
        u1_pads[pin].SetNet(gnd)
    for pin in (8, 16, 24, 32):
        u1_pads[pin].SetNet(vcc)
    j1[1].SetNet(gnd)
    j1[2].SetNet(vcc)
    j2[1].SetNet(gnd)
    j2[2].SetNet(vcc)

    # Header pin order is reversed against the part's, so the airwires cross.
    left_pins = [2, 3, 4, 5, 6, 7, 10, 11]
    right_pins = [18, 19, 20, 21, 22, 23, 26, 27]
    j_left = [3, 5, 7, 9, 11, 13, 15, 16]
    j_right = [3, 5, 7, 9, 11, 13, 15, 16]

    for i in range(8):
        net_a = _add_net(board, "SIG_L%d" % i, nets)
        net_b = _add_net(board, "SIG_LR%d" % i, nets)
        j1[j_left[i]].SetNet(net_a)
        rp = res[i]
        rp[0].SetNet(net_a)
        rp[1].SetNet(net_b)
        u1_pads[left_pins[7 - i]].SetNet(net_b)

        net_c = _add_net(board, "SIG_R%d" % i, nets)
        j2[j_right[i]].SetNet(net_c)
        u1_pads[right_pins[7 - i]].SetNet(net_c)

    board.BuildListOfNets()

    pcbnew.SaveBoard(out_path, board)
    return out_path


def main(argv):
    out = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "toy_pcba.kicad_pcb")
    build(out)
    b = pcbnew.LoadBoard(out)
    print("wrote %s" % out)
    print("  footprints: %d" % len(b.GetFootprints()))
    print("  pads:       %d" % len(b.GetPads()))
    print("  nets:       %d" % (b.GetNetCount() - 1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
