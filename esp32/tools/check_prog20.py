#!/usr/bin/env python3
"""Check the non-interlaced 256-colour image page (pattern 15) from the synth output.

usage: check_prog20.py pal|ntsc <out.u8>   (whole fields of the image page only, from field 0)
       check_prog20.py pal|ntsc --noburst <pattern 14 .u8> <pattern 11 .u8>
         multiburst without colour burst: the same as the multiburst everywhere except the
         burst window, which is plain blanking on every line.

1. Fields: the stream is whole 313 / 262-line fields, all with the same sync; PAL: the V-switch
   of each line flips from field to field (as in interlaced PAL).
2. Sync: every pulse is measured (start, width) and each half line classed as normal, equalising
   or broad; the sequence must be the non-interlaced one (no half-line offset between fields).
3. Burst: on which lines, amplitude, phase (+V / -V lines).
4. Picture: each sample is Y + U sin + V cos of the true subcarrier (from the sample counter,
   V sign switched per line in PAL). For each palette colour, Y, U and V are fitted (least
   squares) over every sample that shows it, and compared with the colour converted from RGB.
"""
import os
import sys

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import make_tables20 as mt  # noqa: E402
import patterns20 as pt  # noqa: E402


def fail(msg):
    print('FAIL:', msg)
    sys.exit(1)


def pulses(line_mv):
    """(start, width) in samples of every sync pulse: below -150 mV for at least 1 us (saturated
    dark colours also dip below -150 mV, for a sample or two)."""
    low = line_mv < -150
    edges = np.diff(np.concatenate([[0], low.astype(int), [0]]))
    st, en = np.nonzero(edges == 1)[0], np.nonzero(edges == -1)[0]
    return [(int(s), int(e - s)) for s, e in zip(st, en) if e - s >= 20]


def check_noburst(name, p14, p11):
    std = mt.PAL if name == 'pal' else mt.NTSC
    a = np.fromfile(p14, np.uint8).reshape(-1, std.lines, std.spl)
    b = np.fromfile(p11, np.uint8).reshape(-1, std.lines, std.spl)
    w = slice(std.bw0, std.bw0 + std.bwn)
    other = np.ones(std.spl, bool)
    other[w] = False
    if a.shape != b.shape or not np.array_equal(a[..., other], b[..., other]):
        fail('multiburst without burst differs outside the burst window')
    bl = np.array([std.has_burst(l) for l in range(1, std.lines + 1)])
    # the window starts on the end of the sync's rising edge; the burst itself 5.6 / 5.3 us after 0H
    b0 = int(round((mt.T0 + std.burst[0]) * mt.FS)) - 4          # 4 samples before the burst
    edge = slice(std.bw0, b0)
    w = slice(b0, std.bw0 + std.bwn)
    if not np.array_equal(a[..., edge], b[..., edge]):
        fail('sync edge in the burst window differs')
    if (a[:, bl][..., w] != mt.BLANK).any():
        fail('a burst (or something) left in the burst window')
    if not np.array_equal(a[:, ~bl], b[:, ~bl]):                 # vertical sync lines: unchanged
        fail('lines without burst differ')
    nb = int(bl.sum())
    print(f'{name.upper()} multiburst without colour burst: {a.shape[0]} frames; burst window blank on the {nb} lines '
          f'that carry a burst in the normal multiburst, everything else identical\n  OK')


def main():
    if sys.argv[2] == '--noburst':
        return check_noburst(sys.argv[1], sys.argv[3], sys.argv[4])
    name, path = sys.argv[1], sys.argv[2]
    std = mt.PAL_PROG if name == 'pal' else mt.NTSC_PROG
    spl, L = std.spl, std.lines
    lens = np.array([mt.line_len(std, 0, l) for l in range(1, L + 1)])
    fsz = int(lens.sum())                                # samples per field
    d = np.fromfile(path, np.uint8).astype(np.int64)
    if d.size % fsz:
        fail('not whole fields')
    nf = d.size // fsz
    starts = (np.arange(nf)[:, None] * fsz + np.concatenate([[0], np.cumsum(lens)[:-1]])[None, :])
    W = int(lens.max())
    lines = np.full((nf, L, W), mt.BLANK, np.int64)      # shorter lines padded with blanking
    for f in range(nf):
        for l in range(L):
            lines[f, l, :lens[l]] = d[starts[f, l]:starts[f, l] + lens[l]]
    mv = (lines - mt.BLANK) * mt.LSB_MV
    cyc = fsz * std.ph_num / std.ph_den
    print(f'{name.upper()} image page: {nf} fields of {L} lines, {fsz} samples = {cyc:.2f} subcarrier cycles '
          f'({mt.FS / fsz:.3f} Hz field rate)')
    if not std.vswitch:
        # NTSC: whole cycles per field, so every field is the same signal (colour pattern still)
        if cyc != int(cyc) or any(not np.array_equal(lines[f], lines[0]) for f in range(1, nf)):
            fail('NTSC fields differ (the colour pattern moves from field to field)')
        print('  every field identical: the colour pattern stands still')
    else:
        # PAL: the phase starts again at every field, so fields 2 apart are the same signal
        if nf < 3 or any(not np.array_equal(lines[f], lines[f - 2]) for f in range(2, nf)):
            fail('PAL fields 2 apart differ (the colour pattern moves)')
        print(f'  fields 2 apart identical: the colour pattern stands still (V-switch flips per field; '
              f'{(cyc % 1) * 360:.1f} deg phase step at the field start)')

    # sync: same in every field, the expected kinds, line start at 0H
    widths = {k: w * mt.FS for k, w in std.widths.items()}
    h2 = spl / 2 if std.name == 'NTSC' else mt.pe.H2 * mt.FS
    syncs = [[pulses(mv[f, l]) for l in range(L)] for f in range(nf)]
    for f in range(1, nf):
        if syncs[f] != syncs[0]:
            fail(f'field {f}: sync differs from field 0')
    for l in range(1, L + 1):
        exp = std.line_kind(l)
        got = ''
        for half, t0 in ((0, mt.T0 * mt.FS), (1, mt.T0 * mt.FS + h2)):
            p = [(s, w) for s, w in syncs[0][l - 1] if abs(s - t0) < 8]
            if not p:
                got += '-'
                continue
            w = p[0][1]
            got += min(widths, key=lambda k: abs(widths[k] - w))
        if got != exp:
            fail(f'line {l}: sync {got}, expected {exp}')
    print(f'  sync: every line as expected ({sum(1 for l in range(1, L + 1) if "B" in std.line_kind(l))} lines with '
          f'broad pulses), no half lines, identical in all fields')

    # absolute subcarrier phase of every sample (cycles), V sign per line
    n = np.arange(W)
    g = np.arange(nf * L).reshape(nf, L)
    pos = starts - (np.arange(nf)[:, None] * fsz if std.ph_reset else 0)   # phase from each field start
    ph = ((pos[..., None] + n) * std.ph_num % std.ph_den) / std.ph_den
    sn, cs = np.sin(2 * np.pi * ph), np.cos(2 * np.pi * ph)
    vsgn = (1 - 2 * (g & 1))[..., None] if std.vswitch else np.ones_like(g)[..., None]

    # burst: fit U, V over its middle
    b0 = int(round((mt.T0 + std.burst[0]) * mt.FS)) + 8
    bw = slice(b0, b0 + int(round((std.burst[1] - 2) / std.fsc * mt.FS)))
    blines = [l for l in range(1, L + 1) if np.ptp(mv[0, l - 1, bw]) > 100]
    exp_lines = [l for l in range(1, L + 1) if std.has_burst(l)]
    if blines != exp_lines:
        fail(f'burst on lines {blines[:3]}..{blines[-3:]}, expected {exp_lines[:3]}..{exp_lines[-3:]}')
    angs, amps = [], []
    for f in range(nf):
        for l in exp_lines:
            A = np.stack([sn[f, l - 1, bw], vsgn[f, l - 1] * cs[f, l - 1, bw]], 1)
            u, v = np.linalg.lstsq(A, mv[f, l - 1, bw], rcond=None)[0]
            amps.append(np.hypot(u, v))
            angs.append((np.degrees(np.arctan2(v, u)) % 360, vsgn[f, l - 1, 0]))
    if std.vswitch:
        vs = vsgn[..., 0]
        if nf < 2 or (vs[1:] == vs[:-1]).any():
            fail('PAL: the V-switch of a line does not flip from field to field')
        print('  V-switch of every line flips from field to field')
    rot = std.burst_rot * 360 / std.car_n
    a_p = np.array([a for a, s in angs if s > 0])
    a_m = np.array([a for a, s in angs if s < 0])
    if std.vswitch:
        # with the V switch undone, -V lines mirror the (true) rotation: 135 + rot / 135 - rot
        want = ((a_p, 135 + rot, '+V'), (a_m, 135 - rot, '-V'))
    else:
        want = ((a_p, 180.0, 'all'),)
    for a, w, nm in want:
        if np.abs(a - w).max() > 1.5:
            fail(f'burst phase on {nm} lines {a.min():.1f}..{a.max():.1f}, want {w:.1f}')
    if abs(min(amps) - std.burst[2]) > 0.05 * std.burst[2] or abs(max(amps) - std.burst[2]) > 0.05 * std.burst[2]:
        fail(f'burst amplitude {min(amps):.1f}..{max(amps):.1f} mV, want {std.burst[2]:.1f}')
    print(f'  burst: lines {exp_lines[0]}..{exp_lines[-1]}, amplitude {min(amps):.1f}..{max(amps):.1f} mV '
          f'(want {std.burst[2]:.1f}), phase ' +
          ', '.join(f'{nm} lines {a.min():.1f}..{a.max():.1f} deg (want {w:.1f})' for a, w, nm in want))

    # picture: per palette colour, fit Y U V over all its samples
    idx, rgb = pt.image_256(std)
    pal = mt.palette_yuv(std, rgb)                       # Y + BLANK, U, V in DAC steps (exact)
    j = np.arange(mt.IMG_W * 13 // 4)
    pix = (j // 13) * 4 + np.array(mt.IMG_MAP13)[j % 13]
    cols = std.img_x0 + j
    rows = [(l, std.line_to_row(l)) for l in range(1, L + 1) if std.line_to_row(l) is not None]
    if len(rows) != std.rows:
        fail('picture rows')
    X, Yv, C, G = [], [], [], []
    for f in range(nf):
        for l, r in rows:
            X.append(mv[f, l - 1, cols])
            C.append(idx[r, pix])
            G.append(np.full(len(cols), vsgn[f, l - 1, 0]))
            Yv.append(np.stack([np.ones(len(cols)), sn[f, l - 1, cols], vsgn[f, l - 1] * cs[f, l - 1, cols]], 1))
    X, C, Yv, G = np.concatenate(X), np.concatenate(C), np.concatenate(Yv), np.concatenate(G)
    # expected colours: the exact ones, with the DAC correction (make_tables20.palette_entries)
    # where it is on: chroma raised by the DAC's loss and turned ahead by its lag; on -V lines
    # (V switched back) that turn is the other way
    want = {}
    Yd, Ud, Vd = pal.T
    A = np.hypot(Ud, Vd)
    lag, k = np.zeros_like(A), np.ones_like(A)
    if mt.DAC_CORR:
        lag, gain = mt.dac_correction(std, (Yd - mt.BLANK) * mt.LSB_MV, A * mt.LSB_MV)
        room = np.minimum(253.5 - Yd, Yd - 1.5)
        k = np.where(A > 0, np.minimum(A / gain, np.maximum(room, A)) / np.maximum(A, 1e-9), 1.0)
    for sg in (1, -1):
        r = np.radians(lag) * sg
        want[sg] = np.stack([Yd - mt.BLANK, k * (Ud * np.cos(r) - Vd * np.sin(r)),
                             k * (Vd * np.cos(r) + Ud * np.sin(r))], 1) * mt.LSB_MV
    worst = np.zeros(3)
    used = np.unique(C)
    for c in used:
        for sg in np.unique(G):
            m = (C == c) & (G == sg)
            y, u, v = np.linalg.lstsq(Yv[m], X[m], rcond=None)[0]
            worst = np.maximum(worst, np.abs(np.array([y, u, v]) - want[sg][c]))
    print(f'  picture: {len(used)} colours used on {std.rows} rows x {mt.IMG_W} pixels; worst fitted error '
          f'Y {worst[0]:.2f} / U {worst[1]:.2f} / V {worst[2]:.2f} mV (vs the exact colour' + (' with the DAC correction' if mt.DAC_CORR else '') + f'; 1 DAC step = 5 mV)')
    if worst.max() > mt.LSB_MV:
        fail('a colour is off by more than 1 DAC step')
    print('  OK')


if __name__ == '__main__':
    main()
