"""Tables for the 20 MHz synthesis (firmware/main/fubk_synth20.c) + its exact integer model.

Two standards, chosen at boot by the firmware:
  PAL  : FuBK test card, 1280 samples per line (64.000 us), 625 lines, fsc 4433618.75 Hz
  NTSC : SMPTE colour bars, 1271 samples per line (63.55 us), 525 lines, fsc 3579545.45 Hz

Why 20 MHz: the APLL makes 80 MHz with integer coefficients only, so the clock has no
sigma-delta jitter; I2S sends 80 / 4 = 20 MS/s. 20 MHz is not a multiple of fsc, so chroma is
computed per sample:
    sample = Y + (U * S[n] + V * C[n] + 2^13) >> 14
S and C come from one table car[k] = 2^14 sin(2 pi STEP k / N) (STEP / N ~ fsc / fs), read at
per-line offsets: S = car[n + ms], C = car[n + mc]. The offsets give each line its exact phase
from a free-running sample counter (exact at mid-line, in 1/N cycle steps); for PAL mc also
carries the V-switch (cos or -cos).
  PAL : N = 1024, STEP = 227 (fsc / fs = 709379 / 3200000 within 0.0006 %): error < 0.5 deg.
  NTSC: N = 352, STEP = 63, exact (fsc / fs = 63 / 352).
The burst uses the same carrier with finer amplitude steps (1/64 LSB).
"""
import argparse
import os
import sys
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import gaussian_filter1d

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, '..', '..', 'ref'))
import fubk_data as fd      # noqa: E402
import pal_encode as pe     # noqa: E402

FS = 20e6
T0 = 0.8e-6                       # buffer start before 0H (16 samples)
LSB_MV = 5.0
BLANK = 64
B = 32                            # samples per block
CAR_Q = 14
BURST_Q = 6                       # burst components in 1/64 LSB
KINDS = ['N-', 'NE', 'EE', 'BB', 'BE', 'EB', 'E-']
IRE_MV = 714.3 / 100              # NTSC: 100 IRE = 714.3 mV


@dataclass
class Std:
    name: str
    prefix: str                   # C names: f20_* / F20_* (PAL), n20_* / N20_* (NTSC)
    spl: int
    lines: int
    a0: int
    nb: int
    bw0: int
    bwn: int
    car_n: int
    car_step: int
    ph_num: int                   # fsc / fs = ph_num / ph_den cycles per sample (exact)
    ph_den: int
    vswitch: bool
    data_frames: int              # 2 (FuBK anti-PAL frames A/B) or 1
    rows: int                     # data rows per frame
    period_frames: int            # frames until the subcarrier sequence repeats
    sync_mv: float
    widths: dict
    burst: tuple                  # (start after 0H s, cycles, amplitude mV, U dir, V dir)
    line_kind: object
    has_burst: object
    line_to_row: object
    extra: dict = field(default_factory=dict)
    long_period: int = 0          # NTSC: every long_period-th line pair is 1 sample longer per line
    burst_rot: int = 0            # burst phase against the picture, steps of 360 / car_n deg

    @property
    def burst_ofs(self):          # the same rotation as a carrier index offset
        return (self.burst_rot % self.car_n) * self.car_inv % self.car_n

    @property
    def na(self):
        return self.nb * B

    @property
    def car_inv(self):
        return pow(self.car_step, -1, self.car_n)

    @property
    def fsc(self):
        return FS * self.ph_num / self.ph_den


def _ntsc_kind(l):
    if l in (1, 2, 3, 7, 8, 9, 264, 265, 270, 271):
        return 'EE'
    if l in (4, 5, 6, 267, 268):
        return 'BB'
    if l == 263:
        return 'NE'
    if l == 266:
        return 'EB'
    if l == 269:
        return 'BE'
    if l == 272:
        return 'E-'
    return 'N-'


def _ntsc_row(l):
    if 22 <= l <= 261:
        return 2 * (l - 22)
    if 285 <= l <= 524:
        return 2 * (l - 285) + 1
    return None


PAL = Std('PAL', 'f20', 1280, 625, 216, 33, 112, 80, 1024, 227, 709379, 3200000, True, 2, fd.ROWS, 4,
          pe.SYNC_MV, {'N': pe.NORMAL, 'E': pe.SHORT, 'B': pe.BROAD},
          (5.6e-6, 10, pe.BURST_MV, -1 / np.sqrt(2), 1 / np.sqrt(2)),
          pe._line_kind, pe.has_burst, fd.line_to_row)
NTSC = Std('NTSC', 'n20', 1271, 525, 180, 34, 104, 76, 352, 63, 63, 352, False, 1, 480, 1,
           -40 * IRE_MV, {'N': 4.7e-6, 'E': 2.3e-6, 'B': 1271 / 2 / FS - 4.7e-6},
           (5.3e-6, 9, 20 * IRE_MV, -1.0, 0.0),
           _ntsc_kind, lambda l: 10 <= l <= 263 or 273 <= l <= 525, _ntsc_row)
# PAL burst rotated by -26 steps = -9.1 deg against the picture. The ESP32 DAC output shifts the
# picture chroma (higher levels) about 9 deg against the burst (near blanking); this undoes it.
# Without it the FuBK anti-PAL boxes were not grey on PAL-D TVs (plasma, reverse-camera monitor)
# and decoders without a full delay line (RetroScaler 2X, 77" OLED AV) showed moving Hanover bars.
# Found 2026-11-08 by eye with the FUBK_BURST_TEST firmware (best between -27 and -24 steps).
# NTSC looks right on screens and is left alone.
PAL.burst_rot = -26
NTSC.period_frames = 2           # (2 frames are enough for the checks)
# The exact NTSC line is 227.5 cycles = 227.5 * 352 / 63 = 11440 / 9 samples: 8 line pairs of
# 1271 samples and 1 pair of 1272 per 9 pairs. Average line rate exactly 15734.27 Hz (227.5 fsc
# cycles per line, 180 deg between lines); each line start is within 1 sample of its ideal time.
# Tested 2026-11-04: off. The 1-sample line-start steps made vertical bar edges wavy and a
# plasma TV showed snow, so every line is 1271 samples (227.48 cycles, exact fsc).
# NTSC.long_period = 9
assert 9 * 227.5 * NTSC.ph_den == (9 * NTSC.spl + 1) * NTSC.ph_num
assert PAL.ph_num * 2_000_000_000 == 443_361_875 * PAL.ph_den        # fsc = 4433618.75 Hz
assert abs(NTSC.fsc - 315e6 / 88) < 1e-6                              # fsc = 315/88 MHz


def t_line(std):
    return np.arange(std.spl) / FS - T0


# --- pictures ---------------------------------------------------------------------------------
def fubk_picture():
    """Y, U, V in mV on the 20 MHz PAL grid, (2, 576, 1280)."""
    y, by, ry = fd.load(os.path.join(here, '..', '..', 'data', 'fubk4x3.zip'))
    M = pe.resample_matrix(t_line(PAL), fd.FS_SRC, fd.COLS, pe.SRC_T0, fc=6.2e6)
    u = 0.493 * gaussian_filter1d(by, pe.CHROMA_SIGMA, axis=-1, mode='nearest')
    v = 0.877 * gaussian_filter1d(ry, pe.CHROMA_SIGMA, axis=-1, mode='nearest')
    return tuple(a @ M.T for a in (y, u, v))


def orange_picture():
    """PAL test field: flat orange (the 4.43 MHz patch colour) with 300 ns edges."""
    t = t_line(PAL)
    edge = pe._step(t, 10.5e-6, 300e-9) - pe._step(t, 62.5e-6, 300e-9)   # t is from 0H
    shape = (2, fd.ROWS, PAL.spl)
    return tuple(np.broadcast_to(c * edge, shape).copy() for c in (350.0, 0.493 * -589.8, 0.877 * 224.0))


SMPTE_EDGES = (9.4e-6, 62.05e-6)          # full active line after 0H (170M: 10.9 us blanking, 1.5 us front porch)


def smpte_values():
    """SMPTE EG-1 colour bars: list of bands (first row, [(width in bars, Y, U, V) in mV])."""
    def rgb(r, g, b, amp=0.75):
        r, g, b = r * amp, g * amp, b * amp
        y = 0.299 * r + 0.587 * g + 0.114 * b
        s = 92.5 * IRE_MV                                   # 7.5 IRE setup: 92.5 IRE span
        return (7.5 * IRE_MV + s * y, s * 0.493 * (b - y), s * 0.877 * (r - y))

    black = (7.5 * IRE_MV, 0.0, 0.0)
    c33, s33 = np.cos(np.radians(33)), np.sin(np.radians(33))

    def iq(i, q):                                           # I, Q in IRE on black
        return (7.5 * IRE_MV, (-i * s33 + q * c33) * IRE_MV, (i * c33 + q * s33) * IRE_MV)

    top = [rgb(1, 1, 1), rgb(1, 1, 0), rgb(0, 1, 1), rgb(0, 1, 0), rgb(1, 0, 1), rgb(1, 0, 0), rgb(0, 0, 1)]
    mid = [rgb(0, 0, 1), black, rgb(1, 0, 1), black, rgb(0, 1, 1), black, rgb(1, 1, 1)]
    bot = [(1.25, iq(-20, 0)), (1.25, (100 * IRE_MV, 0.0, 0.0)), (1.25, iq(0, 20)), (1.25, black),
           (1 / 3, (3.5 * IRE_MV, 0.0, 0.0)), (1 / 3, black), (1 / 3, (11.5 * IRE_MV, 0.0, 0.0)), (1.0, black)]
    return [(0, [(1.0, v) for v in top]), (320, [(1.0, v) for v in mid]), (360, bot)]


def smpte_picture():
    """Y, U, V in mV on the 20 MHz NTSC grid, (1, 480, 1271)."""
    t = t_line(NTSC)
    p0, p1 = SMPTE_EDGES
    wbar = (p1 - p0) / 7
    rows = np.zeros((3, 3, NTSC.spl))
    for bi, (_, segs) in enumerate(smpte_values()):
        x = p0
        for w, val in segs:
            a, b = x, x + w * wbar
            for c, rise in ((0, 150e-9), (1, 400e-9), (2, 400e-9)):
                rows[bi, c] += val[c] * (pe._step(t, a, rise) - pe._step(t, b, rise))
            x = b
        assert abs(x - p1) < 1e-12
    out = np.zeros((3, 1, NTSC.rows, NTSC.spl))
    starts = [s for s, _ in smpte_values()] + [NTSC.rows]
    for bi in range(3):
        out[:, 0, starts[bi]:starts[bi + 1]] = rows[bi][:, None, :]
    return tuple(out)


# --- tables ------------------------------------------------------------------------------------
def build(std, Y, U, V, flat=None):
    """flat: precompute flat-colour chroma (default: NTSC only; PAL FuBK has few flat blocks)."""
    if flat is None:
        flat = std is NTSC
    t = t_line(std)
    width = std.widths
    tmpl = np.zeros((len(KINDS), std.spl), np.int64)
    h2 = (std.spl / 2) / FS if std is NTSC else pe.H2
    for i, k in enumerate(KINDS):
        p = pe._pulse(t, 0.0, width[k[0]])
        if k[1] != '-':
            p = p + pe._pulse(t, h2, width[k[1]])
        tmpl[i] = np.round(BLANK + std.sync_mv * p / LSB_MV)
    assert tmpl.min() >= 0

    b0, cyc, amp, du, dv = std.burst
    fsc_nom = pe.FSC if std is PAL else 315e6 / 88
    env = pe._step(t, b0, 300e-9) - pe._step(t, b0 + cyc / fsc_nom, 300e-9)
    w = slice(std.bw0, std.bw0 + std.bwn)
    assert env[:std.bw0].max() < 1e-3 and env[std.bw0 + std.bwn:].max() < 1e-3, 'burst window too small'
    assert std.bw0 + std.bwn <= std.a0
    bs = amp / LSB_MV * (1 << BURST_Q)
    bu = np.round(du * bs * env[w]).astype(np.int64)
    bv = np.round(dv * bs * env[w]).astype(np.int64)

    k = np.arange(std.car_n + std.spl)
    car = np.round((1 << CAR_Q) * np.sin(2 * np.pi * ((std.car_step * k) % std.car_n) / std.car_n)).astype(np.int64)

    Yq = np.round(Y / LSB_MV).astype(np.int64)
    Uq = np.round(U / LSB_MV).astype(np.int64)
    Vq = np.round(V / LSB_MV).astype(np.int64)
    nd = Yq.shape[0]                                # data frames: 2 (FuBK A/B) or 1
    assert Yq.shape == (nd, std.rows, std.spl), Yq.shape
    outside = np.ones(std.spl, bool)
    outside[std.a0:std.a0 + std.na] = False
    assert std.a0 + std.na <= std.spl
    for a, n in ((Yq, 'Y'), (Uq, 'U'), (Vq, 'V')):
        assert not a[..., outside].any(), f'{n} outside the active window'
    assert (Yq + BLANK).min() >= 0 and (Yq + BLANK).max() <= 255
    assert np.abs(Uq).max() <= 127 and np.abs(Vq).max() <= 127

    # block pools: Y blocks (uint8, Y + BLANK), UV blocks (int8 interleaved). UV block 0 = zero.
    zero = np.zeros(2 * B, np.int64)
    ypool, uvpool, yid, uvid = [], [zero], {}, {zero.tobytes(): 0}
    nrows = nd * std.rows
    ymap = np.zeros((nrows, std.nb), np.int64)
    uvmap = np.zeros((nrows, std.nb), np.int64)
    for df in range(nd):
        for r in range(std.rows):
            for bi in range(std.nb):
                s = slice(std.a0 + bi * B, std.a0 + (bi + 1) * B)
                yb = Yq[df, r, s] + BLANK
                key = yb.tobytes()
                if key not in yid:
                    yid[key] = len(ypool)
                    ypool.append(yb)
                uvb = np.empty(2 * B, np.int64)
                uvb[0::2], uvb[1::2] = Uq[df, r, s], Vq[df, r, s]
                ukey = uvb.tobytes()
                if ukey not in uvid:
                    uvid[ukey] = len(uvpool)
                    uvpool.append(uvb)
                ymap[df * std.rows + r, bi] = yid[key]
                uvmap[df * std.rows + r, bi] = uvid[ukey]

    # per row: blocks that differ from the previous row of the same field (row - 2);
    # the first row of each field lists all blocks
    POS_BITS = 6
    Y_BITS = max(1, int(len(ypool) - 1).bit_length())
    UV_BITS = max(1, int(len(uvpool) - 1).bit_length())
    assert POS_BITS + Y_BITS + UV_BITS <= 32 and std.nb <= 1 << POS_BITS
    delta, row_start = [], [0]
    for df in range(nd):
        for r in range(std.rows):
            i = df * std.rows + r
            for bi in range(std.nb):
                if r < 2 or ymap[i, bi] != ymap[i - 2, bi] or uvmap[i, bi] != uvmap[i - 2, bi]:
                    delta.append(bi | (ymap[i, bi] << POS_BITS) | (uvmap[i, bi] << (POS_BITS + Y_BITS)))
            row_start.append(len(delta))
    assert row_start[-1] < 65536

    kind_of_line = [KINDS.index(std.line_kind(l)) for l in range(1, std.lines + 1)]
    rowmap = np.array([0xFFFF if std.line_to_row(l) is None else std.line_to_row(l)
                       for l in range(1, std.lines + 1)])
    info = []
    for l in range(1, std.lines + 1):
        kd = kind_of_line[l - 1]
        v = kd | (0x80 if std.has_burst(l) else 0)
        # bit 6: a sync pulse sits inside the picture (slow path; PAL line 623)
        if rowmap[l - 1] != 0xFFFF and not (tmpl[kd, std.a0:std.a0 + std.na] == BLANK).all():
            v |= 0x40
        info.append(v)
    # Flat-colour blocks (one U, V over the whole block) get their chroma as a precomputed
    # waveform over one carrier period (+ one block), so the synth does a table read instead of
    # two multiplies. Exact: the carrier repeats every car_n samples, and
    # car[n + mc] = car[(n + ms + qshift) mod car_n]. PAL needs one waveform per V-switch sign
    # (qshift for +V, 3 qshift for -V): colour i uses rows 2 (i - 1) and 2 (i - 1) + 1.
    flat_ix = np.zeros(len(uvpool), np.int64)
    wtab = []
    if flat:
        q = std.car_n // 4
        shifts = [(q * std.car_inv) % std.car_n]
        if std.vswitch:
            shifts.append((3 * q * std.car_inv) % std.car_n)
        kk = np.arange(std.car_n + B)
        for i, uvb in enumerate(uvpool):
            u, v = uvb[0::2], uvb[1::2]
            if i and (u == u[0]).all() and (v == v[0]).all():
                for sh in shifts:
                    wtab.append((u[0] * car[kk % std.car_n] + v[0] * car[(kk + sh) % std.car_n]
                                 + (1 << (CAR_Q - 1))) >> CAR_Q)
                flat_ix[i] = len(wtab) // len(shifts)
        assert len(wtab) // len(shifts) <= 255
    return dict(std=std, tmpl=tmpl, bu=bu, bv=bv, car=car, Yq=Yq, Uq=Uq, Vq=Vq, flat=flat_ix,
                wtab=np.array(wtab, np.int64).reshape(-1, std.car_n + B),
                ypool=np.array(ypool), uvpool=np.array(uvpool), ymap=ymap, uvmap=uvmap,
                delta=np.array(delta, np.int64), row_start=np.array(row_start),
                line_info=np.array(info), rowmap=rowmap, bits=(POS_BITS, Y_BITS, UV_BITS))


def line_offsets(std, acc, vs):
    """Carrier table offsets of one line. acc = phase numerator (mod ph_den) at its sample 0.
    The index is exact at mid-line. Same integer steps as fubk_synth20.c."""
    half = std.spl // 2
    mid = (acc + half * std.ph_num) % std.ph_den
    i_mid = (mid * std.car_n * 2 + std.ph_den) // (2 * std.ph_den)       # round(mid * N / den)
    idx0 = (i_mid - std.car_step * half) % std.car_n
    q = std.car_n // 4
    ms = (idx0 * std.car_inv) % std.car_n
    mc = ((idx0 + (3 * q if vs else q)) * std.car_inv) % std.car_n
    return ms, mc


def line_len(std, g):
    """Samples of global line g (0-based): NTSC lengthens every 9th line pair by one sample."""
    if std.long_period and (g // 2) % std.long_period == std.long_period - 1:
        return std.spl + 1
    return std.spl


def model(tb, n_frames=None):
    """Integer reference of fubk_synth20.c: the uint8 stream and the start of every line.
    tb can be a list: frame f uses tb[min(f, len - 1)] (pattern switches at frame starts)."""
    tbs = tb if isinstance(tb, list) else [tb]
    std = tbs[0]['std']
    n_frames = n_frames or (len(tbs) if len(tbs) > 1 else std.period_frames)
    n = np.arange(std.spl)
    a = slice(std.a0, std.a0 + std.na)
    w = slice(std.bw0, std.bw0 + std.bwn)
    nw = np.arange(std.bw0, std.bw0 + std.bwn) + std.burst_ofs      # burst: rotated carrier
    acc, vs, g = 0, 0, 0
    lines, offs, pos = [], [], 0
    for f in range(n_frames):
        tb = tbs[min(f, len(tbs) - 1)]
        for l in range(1, std.lines + 1):
            ms, mc = line_offsets(std, acc, vs)
            S, C = tb['car'][n + ms], tb['car'][n + mc]
            info = tb['line_info'][l - 1]
            line = tb['tmpl'][info & 0x3F].copy()
            if info & 0x80:
                line[w] += (tb['bu'] * tb['car'][nw + ms] + tb['bv'] * tb['car'][nw + mc] + (1 << (CAR_Q + BURST_Q - 1))) >> (CAR_Q + BURST_Q)
            row = tb['rowmap'][l - 1]
            if row != 0xFFFF:
                df = f % tb['Yq'].shape[0]
                y = tb['Yq'][df, row, a] + BLANK
                c = (tb['Uq'][df, row, a] * S[a] + tb['Vq'][df, row, a] * C[a] + (1 << (CAR_Q - 1))) >> CAR_Q
                line[a] = y + line[a] - BLANK + c if info & 0x40 else y + c
            ln = line_len(std, g)
            line = np.append(line, [BLANK] * (ln - std.spl))
            lines.append(line)
            offs.append(pos)
            pos += ln
            acc = (acc + ln * std.ph_num) % std.ph_den
            if std.vswitch:
                vs ^= 1
            g += 1
    out = np.concatenate(lines)
    assert out.min() >= 0 and out.max() <= 255, (out.min(), out.max())
    return out.astype(np.uint8), np.array(offs)


# --- C header ----------------------------------------------------------------------------------
def c_array(f, ctype, name, a, per_line=24, attr='FUBK_TABLE_ATTR'):
    a = np.asarray(a).ravel()
    f.write(f'static const {ctype} {attr} {name}[{len(a)}] = {{\n')
    for i in range(0, len(a), per_line):
        f.write('    ' + ', '.join(str(int(x)) for x in a[i:i + per_line]) + ',\n')
    f.write('};\n')


def write_header(path, tb, what):
    std = tb['std']
    p, P = std.prefix, std.prefix.upper()
    pb, yb, ub = tb['bits']
    with open(path, 'w') as f:
        f.write('// Generated by esp32/tools/make_tables20.py - do not edit.\n')
        f.write(f'// {std.name}: {what}\n#pragma once\n#include <stdint.h>\n\n')
        for k, v in [('SPL', std.spl), ('LINES', std.lines), ('A0', std.a0), ('NB', std.nb),
                     ('BW0', std.bw0), ('BWN', std.bwn), ('CAR_N', std.car_n), ('CAR_STEP', std.car_step),
                     ('CAR_INV', std.car_inv), ('PH_NUM', std.ph_num), ('PH_DEN', std.ph_den),
                     ('VSWITCH', int(std.vswitch)), ('DATA_FRAMES', std.data_frames), ('ROWS', std.rows),
                     ('POS_BITS', pb), ('Y_BITS', yb), ('NFLAT', len(tb['wtab'])),
                     ('WLEN', std.car_n + B), ('LONG_PERIOD', std.long_period),
                     ('BURST_OFS', std.burst_ofs)]:
            f.write(f'#define {P}_{k} {v}\n')
        f.write(f'\n#ifdef FUBK20_TABLES_DATA   // data only in fubk_synth20.c\n')
        c_array(f, 'uint8_t', f'{p}_tmpl', tb['tmpl'], 32)
        c_array(f, 'int16_t', f'{p}_bu', tb['bu'])
        c_array(f, 'int16_t', f'{p}_bv', tb['bv'])
        c_array(f, 'int16_t', f'{p}_car', tb['car'], 16)
        f.write('// line_info: bits 0-5 template kind, bit 6 sync inside picture, bit 7 burst\n')
        c_array(f, 'uint8_t', f'{p}_line_info', tb['line_info'])
        c_array(f, 'uint16_t', f'{p}_rowmap', tb['rowmap'])
        f.write('// per data row (frame A, then frame B): block changes in delta[row_start[i] ..]\n')
        c_array(f, 'uint16_t', f'{p}_row_start', tb['row_start'])
        f.write('// delta: block | Y block << POS_BITS | UV block << (POS_BITS + Y_BITS)\n')
        c_array(f, 'uint32_t', f'{p}_delta', tb['delta'], 8)
        c_array(f, 'uint8_t', f'{p}_ypool', tb['ypool'], 32)
        c_array(f, 'int8_t', f'{p}_uvpool', tb['uvpool'], 32)
        f.write('// flat[UV block]: 0, or 1 + its chroma waveform in wtab (index n + ms)\n')
        if len(tb['wtab']):
            c_array(f, 'uint8_t', f'{p}_flat', tb['flat'], 32)
            c_array(f, 'int8_t', f'{p}_wtab', tb['wtab'], 32)
        else:
            f.write(f'#define {p}_flat ((const uint8_t *)0)\n#define {p}_wtab ((const int8_t *)0)\n')
        f.write('#endif\n')


def write_patterns_header(path, items):
    """items: (std, tb, name) per extra pattern. Only the picture tables (in flash); timing, sync,
    burst and carrier come from the standard's main tables. Ends with the struct initialisers."""
    PAT_LIST = {'PAL': [], 'NTSC': []}
    SIZES = {'PAL': [], 'NTSC': []}
    with open(path, 'w') as f:
        f.write('// Generated by esp32/tools/make_tables20.py - do not edit.\n')
        f.write('// Extra patterns (pattern button): picture tables only, in flash.\n#pragma once\n\n')
        f.write('#ifdef FUBK20_TABLES_DATA   // data only in fubk_synth20.c\n')
        for i, (std, tb, name) in enumerate(items):
            k = len(PAT_LIST[std.name]) + 1
            q = f'{std.prefix}p{k:02d}'
            pb, yb, ub = tb['bits']
            f.write(f'\n// {std.name} pattern {k}: {name}\n')
            A = 'FUBK_PAT_ATTR'
            c_array(f, 'uint16_t', f'{q}_row_start', tb['row_start'], attr=A)
            c_array(f, 'uint32_t', f'{q}_delta', tb['delta'], 8, attr=A)
            c_array(f, 'uint8_t', f'{q}_ypool', tb['ypool'], 32, attr=A)
            c_array(f, 'int8_t', f'{q}_uvpool', tb['uvpool'], 32, attr=A)
            if len(tb['wtab']):
                c_array(f, 'uint8_t', f'{q}_flat', tb['flat'], 32, attr=A)
                c_array(f, 'int8_t', f'{q}_wtab', tb['wtab'], 32, attr=A)
                fl, wt = f'{q}_flat', f'{q}_wtab'
            else:
                fl, wt = '0', '0'
            nfl = len(tb['flat']) if len(tb['wtab']) else 0
            SIZES[std.name].append(f"{{{2 * tb['row_start'].size}, {4 * tb['delta'].size}, {tb['ypool'].size}, "
                                   f"{tb['uvpool'].size}, {nfl}, {tb['wtab'].size}}}")
            PAT_LIST[std.name].append(f'PAT({std.prefix}, {std.prefix.upper()}, "{std.name} {name}", {pb}, {yb}, '
                                 f'{q}_ypool, {q}_row_start, {q}_delta, {q}_uvpool, {fl}, {wt})')
        for std, macro in ((PAL, 'F20_PATTERNS'), (NTSC, 'N20_PATTERNS')):
            f.write(f'\n#define {macro} \\\n    ' + ', \\\n    '.join(PAT_LIST[std.name]) + '\n')
            f.write(f'// bytes of row_start, delta, ypool, uvpool, flat, wtab (for copying a pattern to RAM)\n')
            f.write(f'#define {macro}_SIZES ' + ', '.join(SIZES[std.name]) + '\n')
        n = {len(v) for v in PAT_LIST.values()}
        assert len(n) == 1
        f.write(f'#define PATTERNS20_COUNT {len(PAT_LIST["PAL"])}   // extra patterns per standard\n')
        f.write('#endif\n')
    return n.pop() + 1


def report(tb):
    std = tb['std']
    sizes = dict(tmpl=tb['tmpl'].size, burst=4 * std.bwn, car=2 * tb['car'].size,
                 line_info=std.lines * 3, row_start=2 * tb['row_start'].size, delta=4 * tb['delta'].size,
                 ypool=tb['ypool'].size, uvpool=tb['uvpool'].size, flat=tb['wtab'].size + len(tb['flat']))
    tot = sum(sizes.values())
    col = (tb['uvmap'] != 0).sum(1)
    print(f'{std.name}: {len(tb["wtab"])} flat colours; {tot} bytes ({tot / 1024:.1f} KB), {len(tb["ypool"])} Y / {len(tb["uvpool"])} UV blocks, '
          f'{len(tb["delta"])} changes; colour blocks per line: mean {col.mean():.1f}, max {col.max()} of {std.nb}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test-field', action='store_true', help='PAL: flat orange field instead of FuBK')
    a = ap.parse_args()
    outdir = os.path.join(here, '..', 'out')
    os.makedirs(outdir, exist_ok=True)
    main_dir = os.path.join(here, '..', 'firmware', 'main')

    if a.test_field:
        jobs = [(PAL, orange_picture(), 'fubk20_tables_test.h', 'model20_test_4frames.u8', 'flat orange TEST FIELD')]
    else:
        jobs = [(PAL, fubk_picture(), 'fubk20_tables.h', 'model20_4frames.u8', 'FuBK test card'),
                (NTSC, smpte_picture(), 'ntsc20_tables.h', 'model20_ntsc_2frames.u8', 'SMPTE colour bars')]
    base = {}
    for std, pic, hname, mname, what in jobs:
        tb = build(std, *pic)
        base[std.name] = tb
        report(tb)
        write_header(os.path.join(main_dir, hname), tb, what)
        m, offs = model(tb)
        m.tofile(os.path.join(outdir, mname))
        np.save(os.path.join(outdir, mname.replace('.u8', '_offsets.npy')), offs)
        print(f'  wrote firmware/main/{hname}, out/{mname} ({len(offs)} lines, {len(m)} samples)')
    if a.test_field:
        return

    # extra patterns: tables, a 1-frame model each, previews, and a switching sequence per standard
    import patterns20 as pt
    from PIL import Image
    items, tbs = [], {'PAL': [base['PAL']], 'NTSC': [base['NTSC']]}
    total = 0
    for std in (PAL, NTSC):
        for k, (name, fn) in enumerate(pt.PATTERNS, 1):
            pic = fn(std)
            tb = build(std, *pic, flat=True)
            items.append((std, tb, name))
            tbs[std.name].append(tb)
            sz = 2 * tb['row_start'].size + 4 * tb['delta'].size + tb['ypool'].size + tb['uvpool'].size \
                + tb['wtab'].size + (len(tb['flat']) if len(tb['wtab']) else 0)
            total += sz
            col = (tb['uvmap'] != 0).sum(1).max()
            print(f'  {std.name} pattern {k:2d} {name:24s} {sz / 1024:6.1f} KB, {len(tb["wtab"]) // (1 + std.vswitch):2d} flat colours, '
                  f'colour blocks per line max {col}')
            m, offs = model(tb, 1)
            tag = 'pal' if std is PAL else 'ntsc'
            m.tofile(os.path.join(outdir, f'model20_{tag}_pat{k:02d}.u8'))
            img, w = pt.to_rgb8(std, *pic)
            Image.fromarray(img).resize((w, img.shape[0]), Image.LANCZOS).save(
                os.path.join(outdir, f'preview_{tag}_pat{k:02d}.png'))
    npat = write_patterns_header(os.path.join(main_dir, 'patterns20_tables.h'), items)
    print(f'  wrote firmware/main/patterns20_tables.h: {npat - 1} extra patterns per standard, {total / 1024:.1f} KB in flash')
    # switching sequence (frames): PAL FuBK, crosshatch, bars, red, FuBK; NTSC SMPTE, bars, green, SMPTE
    for std, seq in ((PAL, [0, 1, 4, 7, 0]), (NTSC, [0, 4, 8, 0])):
        tag = 'pal' if std is PAL else 'ntsc'
        m, offs = model([tbs[std.name][k] for k in seq])
        m.tofile(os.path.join(outdir, f'model20_{tag}_switch.u8'))
        print(f'  out/model20_{tag}_switch.u8: patterns {seq}, one frame each')


if __name__ == '__main__':
    main()
