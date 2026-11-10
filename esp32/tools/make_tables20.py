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
from dataclasses import dataclass, field, replace

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
UVS = 2 * B + 2                   # bytes per UV block: U, V per sample, then the carrier offset
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
    prog: bool = False            # non-interlaced: every field the same rows 0 .. rows - 1
    img_x0: int = 0               # image pages: first sample of the 320-pixel line
    short_line: int = 0           # this line (1..lines) is short_by samples shorter (0 = none)
    short_by: int = 0
    ext_every: int = 0            # lines ext_every, 2 ext_every, .. ext_n ext_every: 2 samples longer
    ext_n: int = 0
    ph_reset: bool = False        # subcarrier phase back to 0 at every field start

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
# 2026-11-09 the scope map (DAC_LAG below) showed the shift depends on the level: with the DAC
# correction on (default) the picture chroma is corrected per block instead and the burst stays
# at its standard phase. FUBK_DAC_CORR=0 in the environment gives the old fixed rotation.
DAC_CORR = os.environ.get('FUBK_DAC_CORR', '1') != '0'
PAL.burst_rot = 0 if DAC_CORR else -26
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


# Non-interlaced timing (as home computers and consoles): every field is the same, with whole
# lines only (no half line), so a field is a "frame" to the synth. Used by the image page.
#   PAL : 313 lines (49.92 Hz): 1-2 broad, 3 broad + equalising, 4-5 and 311-313 equalising,
#         picture on lines 23..310 (288 rows), burst on 7..310. An odd number of lines, so the
#         PAL V-switch of a screen line flips from field to field as in interlaced PAL: with 312
#         lines a small phase error stood still as Hanover bars (seen on the picture's sky).
#   NTSC: 262 lines (60.06 Hz): 1-3 / 7-9 equalising, 4-6 broad, picture 22..261 (240 rows),
#         burst on 10..262
def _pal_prog_kind(l):
    if l in (1, 2):
        return 'BB'
    if l == 3:
        return 'BE'
    if l in (4, 5, 311, 312, 313):
        return 'EE'
    return 'N-'


def _ntsc_prog_kind(l):
    if l in (1, 2, 3, 7, 8, 9):
        return 'EE'
    if l in (4, 5, 6):
        return 'BB'
    return 'N-'


PAL.img_x0 = 16 + 210            # 10.5 us after 0H: 1040 samples = 52 us (FuBK's picture width)
NTSC.img_x0 = 16 + 194           # 9.7 us after 0H, centred in the SMPTE picture (9.4 .. 62.05 us)
# PAL: 313 x 1280 samples = 88814.25 subcarrier cycles, so the colour pattern would move by a
# quarter cycle every field (crawling dots on fine colour detail). Lines 14, 28, .. 308 are 2
# samples longer (2, so every 2-line DMA buffer stays whole 32-bit words): 400684 samples =
# 88824.0048 cycles, and the phase starts again from 0 at every field, so the 1.7 deg left over
# is a step in the vertical sync. Every second field is then the same signal (the V-switch still
# flips per field).
PAL_PROG = replace(PAL, lines=313, rows=288, data_frames=1, period_frames=1, prog=True,
                   line_kind=_pal_prog_kind, has_burst=lambda l: 7 <= l <= 310,
                   line_to_row=lambda l: l - 23 if 23 <= l <= 310 else None,
                   ext_every=14, ext_n=22, ph_reset=True)
_c = ((313 * PAL.spl + 44) * PAL.ph_num % PAL.ph_den) / PAL.ph_den
assert min(_c, 1 - _c) < 0.01, _c
# NTSC: 262 x 1271 samples = 59600.36 subcarrier cycles, so the colour pattern would move by 0.36
# cycle every field (a plasma's comb filter showed it as restless). Line 262 (blank) is 10
# samples shorter: 332992 samples = exactly 59600 cycles, so the pattern stands still from field
# to field, as from a game console.
NTSC_PROG = replace(NTSC, lines=262, rows=240, data_frames=1, period_frames=1, prog=True,
                    line_kind=_ntsc_prog_kind, has_burst=lambda l: l >= 10,
                    line_to_row=lambda l: l - 22 if 22 <= l <= 261 else None,
                    short_line=262, short_by=10)
assert (262 * NTSC.spl - 10) * NTSC.ph_num % NTSC.ph_den == 0
# PAL with the subcarrier locked to the frame (the phase test pattern): 625 x 1280 samples =
# 177344.75 cycles; lines 8, 16, .. 600 are 2 samples longer (+150 samples: 0.8 deg left over,
# a step in the vertical sync) and the phase restarts at every frame, so a scope on one line can
# average (frames 2 apart identical; with V = 0 in the picture, all frames but the burst)
PAL_LOCK = replace(PAL, ext_every=8, ext_n=75, ph_reset=True)
_c = ((625 * PAL.spl + 150) * PAL.ph_num % PAL.ph_den) / PAL.ph_den
assert min(_c, 1 - _c) < 0.003, _c
IMG_W = 320                       # image pixels per line: 4 pixels = 13 samples (3.25 each)
IMG_MAP13 = [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3]   # pixel (of 4) of each of 13 samples


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
# --- DAC correction -------------------------------------------------------------------------
# Measured 2026-11-09 on the scope (75 ohm load, 2 GS/s, 20 MHz bandwidth limit) with the phase
# test pattern (PAL, -U chroma on luma steps, correction off; 9 x 8 points): the chroma lag against the burst (deg at 4.43 MHz)
# and its amplitude against the burst's, per luma level (mV above blanking) and chroma amplitude
# (mV peak, as sent). The ESP32 DAC output gets slower and weaker near the top of its range and
# with bigger swings; the burst (150 mV at blanking) sits at about 0 lag. None = not measured
# (out of the range of real colour signals).
DAC_MAP_Y = (0.0, 100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0)
DAC_MAP_A = (50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 350.0, 400.0, 450.0)
DAC_LAG = ((-2.78, -2.58, -2.42, 0.13, 0.20, 1.70, 5.32, 18.35),
           (-1.37, -1.25, -0.89, 0.56, 1.57, 3.29, 7.54, 20.16),
           (0.12, 0.18, 0.69, 2.08, 3.29, 4.99, 10.00, 21.41),
           (2.15, 1.84, 2.68, 3.62, 5.16, 7.45, 13.45, 21.21),
           (None, 4.00, 4.54, 5.41, 7.03, 10.44, 16.33, None),
           (None, 6.48, 6.61, 7.57, 9.54, 13.47, 18.39, None),
           (None, None, 8.98, 10.03, 12.04, 16.61, None, None),
           (None, None, 11.14, 12.08, 14.91, 19.03, None, None),
           (None, None, None, 14.68, 17.74, None, None, None))
DAC_GAIN = ((1.004, 1.009, 1.027, 0.970, 0.990, 1.001, 0.963, 0.835),
            (1.005, 1.015, 1.016, 0.978, 0.970, 0.987, 0.946, 0.810),
            (1.000, 1.010, 1.008, 0.987, 0.974, 0.980, 0.931, 0.776),
            (1.009, 1.006, 1.002, 0.992, 0.977, 0.964, 0.916, 0.729),
            (None, 1.001, 0.994, 0.987, 0.973, 0.947, 0.881, None),
            (None, 0.996, 0.989, 0.982, 0.968, 0.933, 0.833, None),
            (None, None, 0.983, 0.976, 0.958, 0.913, None, None),
            (None, None, 0.982, 0.972, 0.947, 0.884, None, None),
            (None, None, None, 0.963, 0.934, None, None, None))
DAC_TOP_MV = (254 - BLANK) * LSB_MV           # the DAC's range above / below blanking (1 step spare)
DAC_BOTTOM_MV = (BLANK - 1) * LSB_MV


def _dac_grid(rows):
    a = np.array([[np.nan if v is None else v for v in r] for r in rows])
    for r in a:                                   # unmeasured corners: the nearest step of the band
        ok = np.nonzero(~np.isnan(r))[0]
        r[:] = np.interp(np.arange(len(r)), ok, r[ok])
    return a


def dac_correction(std, y, amp):
    """Chroma lag (deg at the standard's fsc) and gain of the DAC at luma y (mV above blanking)
    and chroma amplitude amp (mV peak); bilinear in the measured map, clamped at its edges. NTSC:
    the PAL lag scaled by fsc (assumes the lag is a delay)."""
    from scipy.interpolate import RegularGridInterpolator
    pts = np.stack([np.clip(amp, DAC_MAP_A[0], DAC_MAP_A[-1]), np.clip(y, DAC_MAP_Y[0], DAC_MAP_Y[-1])], -1)
    lag = RegularGridInterpolator((DAC_MAP_A, DAC_MAP_Y), _dac_grid(DAC_LAG))(pts)
    gain = RegularGridInterpolator((DAC_MAP_A, DAC_MAP_Y), _dac_grid(DAC_GAIN))(pts)
    if std.name == 'NTSC':
        lag = lag * (315e6 / 88) / pe.FSC
    return lag, gain


def build(std, Y, U, V, flat=None):
    """flat: precompute flat-colour chroma (default: NTSC only; PAL FuBK has few flat blocks)."""
    if flat is None:
        flat = std.name == 'NTSC'
    t = t_line(std)
    width = std.widths
    tmpl = np.zeros((len(KINDS), std.spl), np.int64)
    h2 = (std.spl / 2) / FS if std.name == 'NTSC' else pe.H2
    for i, k in enumerate(KINDS):
        p = pe._pulse(t, 0.0, width[k[0]])
        if k[1] != '-':
            p = p + pe._pulse(t, h2, width[k[1]])
        tmpl[i] = np.round(BLANK + std.sync_mv * p / LSB_MV)
    assert tmpl.min() >= 0

    b0, cyc, amp, du, dv = std.burst
    fsc_nom = pe.FSC if std.name == 'PAL' else 315e6 / 88
    env = pe._step(t, b0, 300e-9) - pe._step(t, b0 + cyc / fsc_nom, 300e-9)
    w = slice(std.bw0, std.bw0 + std.bwn)
    assert env[:std.bw0].max() < 1e-3 and env[std.bw0 + std.bwn:].max() < 1e-3, 'burst window too small'
    assert std.bw0 + std.bwn <= std.a0
    bs = amp / LSB_MV * (1 << BURST_Q)
    bu = np.round(du * bs * env[w]).astype(np.int64)
    bv = np.round(dv * bs * env[w]).astype(np.int64)

    k = np.arange(std.car_n + std.spl)
    car = np.round((1 << CAR_Q) * np.sin(2 * np.pi * ((std.car_step * k) % std.car_n) / std.car_n)).astype(np.int64)

    # DAC correction: chroma gain per sample; its lag as a carrier offset per block (below)
    amp = np.hypot(U, V)
    lag = np.zeros_like(amp)
    if DAC_CORR and amp.any():
        lag, gain = dac_correction(std, Y, amp)
        U, V = U / gain, V / gain
        # the gain must not take the signal out of the DAC's range: there it keeps what fits
        room = np.minimum(DAC_TOP_MV - Y, Y + DAC_BOTTOM_MV)
        a2 = np.hypot(U, V)
        k = np.where(a2 > room, np.maximum(room, amp) / np.maximum(a2, 1e-9), 1.0)
        U, V = U * k, V * k
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

    # block pools: Y blocks (uint8, Y + BLANK), UV blocks (int8 interleaved, then the block's
    # carrier offset d as 2 bytes, low first: the synth reads its carrier at index + d, which
    # advances the chroma by the DAC lag on +V and -V lines alike). UV block 0 = zero.
    zero = np.zeros(UVS, np.int64)
    ypool, uvpool, yid, uvid = [], [zero], {}, {zero.tobytes(): 0}
    step = 360.0 / std.car_n
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
                uvb = np.zeros(UVS, np.int64)
                uvb[0:2 * B:2], uvb[1:2 * B:2] = Uq[df, r, s], Vq[df, r, s]
                wgt = amp[df, r, s] ** 2
                if uvb[:2 * B].any() and wgt.sum() > 0:  # amplitude-weighted lag of the block
                    k = int(round(float((wgt * lag[df, r, s]).sum() / wgt.sum()) / step)) % std.car_n
                    d = k * std.car_inv % std.car_n
                    uvb[2 * B], uvb[2 * B + 1] = ((d & 0xFF) ^ 0x80) - 0x80, d >> 8
                ukey = uvb.tobytes()
                if ukey not in uvid:
                    uvid[ukey] = len(uvpool)
                    uvpool.append(uvb)
                ymap[df * std.rows + r, bi] = yid[key]
                uvmap[df * std.rows + r, bi] = uvid[ukey]

    # per row: blocks that differ from the previous row of the same field (row - 2; non-interlaced:
    # row - 1); the first row of each field lists all blocks
    POS_BITS = 6
    Y_BITS = max(1, int(len(ypool) - 1).bit_length())
    UV_BITS = max(1, int(len(uvpool) - 1).bit_length())
    assert POS_BITS + Y_BITS + UV_BITS <= 32 and std.nb <= 1 << POS_BITS
    delta, row_start = [], [0]
    ref = 1 if std.prog else 2
    for df in range(nd):
        for r in range(std.rows):
            i = df * std.rows + r
            for bi in range(std.nb):
                if r < ref or ymap[i, bi] != ymap[i - ref, bi] or uvmap[i, bi] != uvmap[i - ref, bi]:
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
            u, v, d = uvb[0:2 * B:2], uvb[1:2 * B:2], uv_ofs(uvb)
            if i and (u == u[0]).all() and (v == v[0]).all():
                for sh in shifts:
                    wtab.append((u[0] * car[(kk + d) % std.car_n] + v[0] * car[(kk + d + sh) % std.car_n]
                                 + (1 << (CAR_Q - 1))) >> CAR_Q)
                flat_ix[i] = len(wtab) // len(shifts)
        assert len(wtab) // len(shifts) <= 255
    return dict(std=std, tmpl=tmpl, bu=bu, bv=bv, car=car, Yq=Yq, Uq=Uq, Vq=Vq, flat=flat_ix,
                wtab=np.array(wtab, np.int64).reshape(-1, std.car_n + B),
                ypool=np.array(ypool), uvpool=np.array(uvpool), ymap=ymap, uvmap=uvmap,
                delta=np.array(delta, np.int64), row_start=np.array(row_start),
                line_info=np.array(info), rowmap=rowmap, bits=(POS_BITS, Y_BITS, UV_BITS))


def uv_ofs(uvb):
    """The carrier offset stored at the end of a UV block."""
    return (int(uvb[2 * B]) & 0xFF) | (int(uvb[2 * B + 1]) & 0xFF) << 8


def palette_yuv(std, rgb):
    """256 x (R', G', B') 0..255 -> Y + BLANK, U, V in DAC steps (float, not rounded)."""
    if std.name == 'PAL':
        black, span = 0.0, 700.0
    else:
        black, span = 7.5 * IRE_MV, 92.5 * IRE_MV
    r, g, b = (np.asarray(rgb, float).T / 255.0)
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return np.stack([BLANK + (black + span * y) / LSB_MV,
                     span * 0.493 * (b - y) / LSB_MV, span * 0.877 * (r - y) / LSB_MV], 1)


IMG_SHIFT = 20                    # image pages: sample = (y + a * car[k]) >> 20 (car is 2^14 sin)


def palette_entries(std, yuv):
    """Palette as fubk20_pal_t: chroma U sin + V cos = A sin(phase + theta), so a colour is
    y (Y + BLANK, with the rounding, << 20), a (A in 1/64 DAC steps) and the carrier table offset
    k that shifts the phase by theta: car[k0 + k] = sin(phase + theta) with k = t * car_inv mod N
    for theta = t / N cycles. k[0] for +V lines (C = +cos), k[1] for -V lines (V negated)."""
    Y, U, V = yuv.T
    A = np.hypot(U, V)
    lag = np.zeros_like(A)
    if DAC_CORR:                                    # DAC correction (as build()): gain, lag
        lag, gain = dac_correction(std, (Y - BLANK) * LSB_MV, A * LSB_MV)
        room = np.minimum(253.5 - Y, Y - 1.5)        # DAC steps, as the assert below
        A = np.minimum(np.where(A > 0, A / gain, 0), np.maximum(room, A))
    assert ((Y - A).min() >= 0.5 and (Y + A).max() <= 254.5), 'palette colour clips the DAC'
    ent = np.zeros((len(yuv), 4), np.int64)
    ent[:, 0] = np.round(Y * (1 << IMG_SHIFT)).astype(np.int64) + (1 << (IMG_SHIFT - 1))
    ent[:, 1] = np.round(A * (1 << (IMG_SHIFT - CAR_Q))).astype(np.int64)
    for j, sv in enumerate((1, -1 if std.vswitch else 1)):
        t = np.round((np.arctan2(sv * V, U) + np.radians(lag)) / (2 * np.pi) * std.car_n).astype(np.int64) % std.car_n
        ent[:, 2 + j] = t * std.car_inv % std.car_n
    assert ent[:, 1].max() < 1 << 15
    return ent


def build_image(std, idx, rgb):
    """Image page: idx (rows, IMG_W) palette indices, rgb (256, 3). Timing tables from build()
    (empty picture); model() renders the image from tb['img'] and tb['pal'] exactly as
    fubk_synth20.c: pixel p of each group of 4 covers the samples IMG_MAP13 says."""
    assert idx.shape == (std.rows, IMG_W) and len(rgb) <= 256
    z = np.zeros((1, std.rows, std.spl))
    tb = build(std, z, z, z)
    yuv = np.zeros((256, 3))
    yuv[:, 0] = BLANK
    yuv[:len(rgb)] = palette_yuv(std, rgb)
    j = np.arange(IMG_W * 13 // 4)
    assert std.a0 <= std.img_x0 and std.img_x0 + j[-1] < std.a0 + std.na
    tb['img'] = np.asarray(idx, np.uint8)
    tb['pal'] = palette_entries(std, yuv)
    tb['pal_yuv'] = yuv
    tb['img_pix'] = (j // 13) * 4 + np.array(IMG_MAP13)[j % 13]
    return tb


def image_line(tb, row, ms, vs):
    """Samples of an image page line (same integer steps as fubk_synth20.c)."""
    std = tb['std']
    pal = tb['pal'][tb['img'][row, tb['img_pix']]]
    i = std.img_x0 + np.arange(len(tb['img_pix']))
    k = (ms + pal[:, 2 + vs]) % std.car_n + i
    return (pal[:, 0] + pal[:, 1] * tb['car'][k]) >> IMG_SHIFT


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


def line_len(std, g, l=0):
    """Samples of global line g (0-based), line number l of its frame: NTSC can lengthen every
    9th line pair by one sample (off); a pattern can have one short line (short_line)."""
    if std.long_period and (g // 2) % std.long_period == std.long_period - 1:
        return std.spl + 1
    if std.short_line and l == std.short_line:
        return std.spl - std.short_by
    if std.ext_every and l % std.ext_every == 0 and l // std.ext_every <= std.ext_n:
        return std.spl + 2
    return std.spl


TEXT_STATE = {}                   # font, lut, texts for model() (set by main / setup_text)


def setup_text():
    if not TEXT_STATE:
        chars, glyphs, lut = text_tables()
        TEXT_STATE.update(chars=chars, font=glyphs[:, :, :].astype(int), lut=lut, texts=text_default())
    return TEXT_STATE


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
        if tb['std'].ph_reset:
            acc = 0
        for l in range(1, tb['std'].lines + 1):
            ms, mc = line_offsets(std, acc, vs)
            S, C = tb['car'][n + ms], tb['car'][n + mc]
            info = tb['line_info'][l - 1]
            line = tb['tmpl'][info & 0x3F].copy()
            if info & 0x80:
                line[w] += (tb['bu'] * tb['car'][nw + ms] + tb['bv'] * tb['car'][nw + mc] + (1 << (CAR_Q + BURST_Q - 1))) >> (CAR_Q + BURST_Q)
            row = tb['rowmap'][l - 1]
            if row != 0xFFFF and 'img' in tb:
                ts = tb['std']
                line[ts.img_x0:ts.img_x0 + len(tb['img_pix'])] = image_line(tb, row, ms, vs)
            elif row != 0xFFFF:
                df = f % tb['Yq'].shape[0]
                y = tb['Yq'][df, row, a] + BLANK
                # each block's carrier offset (DAC correction), as fubk_synth20.c
                d = np.repeat([uv_ofs(tb['uvpool'][u]) for u in tb['uvmap'][df * tb['std'].rows + row]], B)
                ia = np.arange(std.a0, std.a0 + std.na)
                Sa = tb['car'][(ms + d) % std.car_n + ia]
                Ca = tb['car'][(mc + d) % std.car_n + ia]
                c = (tb['Uq'][df, row, a] * Sa + tb['Vq'][df, row, a] * Ca + (1 << (CAR_Q - 1))) >> CAR_Q
                line[a] = y + line[a] - BLANK + c if info & 0x40 else y + c
                if 'text' in tb:
                    text_overlay(line, tb, row, TEXT_STATE['texts'], TEXT_STATE['font'], TEXT_STATE['lut'])
            ln = line_len(tb['std'], g, l)
            line = np.append(line, [BLANK] * (ln - std.spl))[:ln]
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


# --- text in the test cards (PM5644 boxes, FuBK name box) --------------------------------------
# PM8503 characters (9 x 18 dots, tools/pm8503_font.py). A dot is 2 pixels at the PM5644's
# 13.5 MHz (80/27 samples at 20 MS/s) and 2 frame lines (one per field), so a character is
# 1.33 us x 36 lines: 8 fit in the upper PM5644 box, 12 in the lower one.
TEXT_DOT_STEP = 27                # sample to sample: 27/80 dot
TEXT_DOT = 80
TEXT_ROWS = 2 * 18                # frame rows per text line
TEXT_MAX = 13
TEXT_LEN = (8, 12, 13)            # PM5644 upper, PM5644 lower, FuBK name box
TEXT_DEFAULT = ('', '', '')     # (centred in their boxes)
TEXT_CAPS = (4, 16)               # the capitals' dot rows: centred in the box, but no higher
TEXT_INK = (1, 16)                # than keeps all ink (the accents start at dot row 1) inside it
# glyph TEXT_HALF (after the font's characters) is the "half space": only in a line's last place,
# it moves the whole line half a character (4.5 dots) to the right, to centre any length


def text_lut():
    """lut[pos][prev, cur, next dot]: DAC steps above black at 1/80-dot position pos of the
    current dot: white dots shaped for a 150 ns rise, as the other patterns' luma."""
    from scipy.special import erf
    sig = 150e-9 / 2.563 / (2 / 13.5e6)          # in dots
    def box(v):                                   # a dot over [0, 1), smoothed, at v
        return 0.5 * (erf((v) / (np.sqrt(2) * sig)) - erf((v - 1) / (np.sqrt(2) * sig)))
    lut = np.zeros((TEXT_DOT, 8), np.int64)
    for pos in range(TEXT_DOT):
        v = pos / TEXT_DOT
        for combo in range(8):
            prv, cur, nxt = combo >> 2 & 1, combo >> 1 & 1, combo & 1
            lut[pos, combo] = round(700 / LSB_MV * (prv * box(v + 1) + cur * box(v) + nxt * box(v - 1)))
    return lut


def find_black_boxes(Y, black_mv, min_w=80, min_h=16):
    """Black rectangles in a picture (rows x samples, mV): (row0, row1, x0, x1), inclusive."""
    groups = []
    for r in range(Y.shape[0]):
        blk = Y[r] < black_mv + 25
        e = np.diff(np.concatenate([[0], blk.astype(int), [0]]))
        for a, b in zip(np.nonzero(e == 1)[0], np.nonzero(e == -1)[0] - 1):
            if b - a < min_w:
                continue
            for g in groups:
                if r - g[1] <= 2 and abs(a - g[2]) < 12 and abs(b - g[3]) < 12:
                    g[1] = r
                    break
            else:
                groups.append([r, r, a, b])
    return [tuple(g) for g in groups if g[1] - g[0] + 1 >= min_h]


def text_box(row0, row1, xc, n, text, Y=None, black=0.0, bx=None):
    """A text line of n characters centred on sample xc and rows row0..row1. Y (the card): the
    samples where the box is not black (FuBK: the centre line) are covered by a character's
    cell (cx0..cx1); elsewhere only the letters are drawn (the box is black anyway)."""
    h = row1 - row0 + 1
    assert h >= TEXT_ROWS, (row0, row1)
    width = n * 9 * TEXT_DOT / TEXT_DOT_STEP
    caps = 2 * (TEXT_CAPS[1] - TEXT_CAPS[0] + 1)          # capital height in frame rows (26)
    top = row0 + (h - caps) // 2 - 2 * TEXT_CAPS[0]           # capitals centred
    top = max(top, row0 - 2 * TEXT_INK[0])                    # accents inside the box
    assert top + 2 * (TEXT_INK[1] + 1) <= row1 + 1, 'text ink does not fit the box'
    out = dict(row0=top, x0=int(round(xc - width / 2)), n=n, text=text,
                nsamp=((n * 9 + 1) * TEXT_DOT + TEXT_DOT_STEP - 1) // TEXT_DOT_STEP,
                ylo=row0, yhi=row1, cx0=0, cx1=0)    # drawn only inside the box
    if Y is not None:                                # inside the box (bx: its first, last sample)
        a0, a1 = max(out['x0'], bx[0] + 4), min(out['x0'] + out['nsamp'], bx[1] - 3)
        bright = np.nonzero((Y[row0:row1 + 1, a0:a1] > black + 10).any(axis=0))[0]
        if len(bright):
            assert bright[-1] - bright[0] < 24, 'more than one bright strip in a text box'
            out['cx0'], out['cx1'] = a0 + bright[0] - 1, a0 + bright[-1] + 2
    return out


def fubk_text_boxes(Y):
    """FuBK: its own text line (13 characters) in the black name box in the middle of the card.
    The middle character sits on the centre line: a space there keeps the line visible (e.g.
    "YLE TV1" centred shows "YLE | TV1")."""
    b = [g for g in find_black_boxes(Y[0], 0.0)
         if g[1] - g[0] + 1 < 60 and g[2] > PAL.a0 + 100 and g[3] < PAL.a0 + PAL.na - 100]
    assert len(b) == 2 and b[0][:2] == b[1][:2], b               # the two halves of the box
    xc = (b[0][3] + b[1][2]) / 2                                    # the centre line
    return [text_box(b[0][0], b[0][1], xc, 13, 2, Y[0], 0.0, (b[0][2], b[1][3]))]


def pm5644_text_boxes(std, Y):
    """PM5644: upper text (8) in the box at the top of the circle, lower (12) at the bottom."""
    black = 0.0 if std.name == 'PAL' else 7.5 * IRE_MV
    b = sorted((g for g in find_black_boxes(Y[0], black)                  # inside the picture,
                if g[2] > std.a0 + 100 and g[3] < std.a0 + std.na - 100 and 100 < g[3] - g[2] < 400),
               key=lambda g: g[0])
    assert len(b) == 2, b
    up, lo = b[0], b[-1]
    assert up[3] - up[2] < lo[3] - lo[2]
    return [text_box(up[0], up[1], (up[2] + up[3]) / 2, 8, 0, Y[0], black, up[2:]),
            text_box(lo[0], lo[1], (lo[2] + lo[3]) / 2, 12, 1, Y[0], black, lo[2:])]


def text_default():
    """TEXT_DEFAULT as glyph indices, centred ('\u00bd' = the half space, last place only)."""
    import pm8503_font as pf
    half = len(pf.ORDER)
    out = []
    for t, n in zip(TEXT_DEFAULT, TEXT_LEN):
        if len(t) < n:
            pad = (n - len(t)) // 2
            t = ' ' * pad + t + ' ' * (n - len(t) - pad)
        assert len(t) == n and '\u00bd' not in t[:-1]
        out.append([half if ch == '\u00bd' else pf.encode(ch)[0] for ch in t])
    return out


def text_overlay(line, tb, row, texts, font, lut):
    """Draw the text lines of tb on a line (data row `row`), as fubk_synth20.c."""
    black = round((0.0 if tb['std'].name == 'PAL' else 7.5 * IRE_MV) / LSB_MV)
    half = len(font) - 1                      # the half space (last glyph, blank)
    for b in tb.get('text', []):
        if not (b['row0'] <= row < b['row0'] + TEXT_ROWS and b['ylo'] <= row <= b['yhi']):
            continue
        dr = (row - b['row0']) >> 1
        t = texts[b['text']]
        sh = t[b['n'] - 1] == half            # shifted by 4.5 dots: 5 empty dots, start at 1/2
        dots = [0] * (6 if sh else 1)
        opq = list(dots)
        for c in range(b['n']):
            g = t[c]
            dots += list(font[g, dr])
            opq += [1 if g and g != half else 0] * 9   # a character's cell is drawn whole
        dots += [0, 0]
        opq += [0, 0]
        d, pos = (1, 40) if sh else (1, 0)      # sample x0: 4.5 dots before the first character
        for k in range(b['nsamp']):
            v = lut[pos, dots[d - 1] << 2 | dots[d] << 1 | dots[d + 1]]
            x = b['x0'] + k
            if v or (opq[d] and b['cx0'] <= x < b['cx1']):
                line[x] = BLANK + black + v
            pos += TEXT_DOT_STEP
            if pos >= TEXT_DOT:
                pos -= TEXT_DOT
                d += 1


def text_tables():
    """The font's characters plus the half space (blank, last)."""
    import pm8503_font as pf
    chars, g = pf.glyphs()
    g = np.concatenate([g, np.zeros((1,) + g.shape[1:], bool)])
    return chars + ['\u00bd'], g.astype(np.int64), text_lut()


def write_text_header(path, chars, glyphs, lut, boxes):
    """boxes: {C name: [box, ...]}"""
    with open(path, 'w') as f:
        f.write('// Generated by esp32/tools/make_tables20.py - do not edit.\n')
        f.write('// Text in the test cards: PM8503 font (9 x 18 dots, tools/pm8503_font.py), the edge\n')
        f.write('// shaping table and the text boxes. Only in fubk_synth20.c.\n#pragma once\n#include <stdint.h>\n\n')
        f.write(f'#define FUBK20_NGLYPH {len(chars)}\n#define FUBK20_TEXT_DOT_STEP {TEXT_DOT_STEP}\n')
        f.write(f'#define FUBK20_TEXT_DOT {TEXT_DOT}\n#define FUBK20_TEXT_ROWS {TEXT_ROWS}\n')
        f.write(f'#define F20_TXT_BLACK 0\n#define N20_TXT_BLACK {round(7.5 * IRE_MV / LSB_MV)}\n')
        dflt = text_default()
        f.write('// default text: glyph indices (' + ' / '.join(repr(t) for t in TEXT_DEFAULT) + ', centred)\n')
        f.write('#define FUBK20_TEXT_DEFAULT { ' + ', '.join('{' + ', '.join(map(str, d)) + '}' for d in dflt) + ' }\n')
        f.write(f'#define FUBK20_HALF {len(chars) - 1}   // the half space (last place of a line only)\n')
        f.write('#ifdef FUBK20_TABLES_DATA\n')
        f.write('// characters in glyph order (UTF-8; the last is the half space): ' + ''.join(chars) + '\n')
        f.write('// glyph rows: bit 8 = leftmost dot\n')
        rows = np.array([[int(sum(int(v) << (8 - j) for j, v in enumerate(r))) for r in gl] for gl in glyphs])
        c_array(f, 'uint16_t', 'fubk20_font', rows, 18)
        f.write('// [pos in the dot, 1/80][prev, cur, next dot]: DAC steps above black\n')
        c_array(f, 'uint8_t', 'fubk20_text_lut', lut, 8)
        for name, bl in boxes.items():
            f.write(f'static const fubk20_textbox_t FUBK_TABLE_ATTR {name}[{len(bl)}] = {{\n')
            for b in bl:
                f.write(f'    {{{b["row0"]}, {b["x0"]}, {b["nsamp"]}, {b["ylo"]}, {b["yhi"]}, {b["cx0"]}, {b["cx1"]}, {b["n"]}, {b["text"]}}},\n')
            f.write('};\n')
        f.write('#endif\n')


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
                     ('BURST_OFS', std.burst_ofs), ('IMG_X0', std.img_x0)]:
            f.write(f'#define {P}_{k} {v}\n')
        # text lines of the built-in card (FuBK: the name box; boxes in text20_tables.h)
        if 'text' in tb:
            f.write(f'#define {P}_TEXT {p}_text\n#define {P}_NTEXT {len(tb["text"])}\n')
        else:
            f.write(f'#define {P}_TEXT ((const fubk20_textbox_t *)0)\n#define {P}_NTEXT 0\n')
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
        c_array(f, 'int8_t', f'{p}_uvpool', tb['uvpool'], 32, attr='FUBK_TABLE_ATTR __attribute__((aligned(4)))')
        f.write('// flat[UV block]: 0, or 1 + its chroma waveform in wtab (index n + ms)\n')
        if len(tb['wtab']):
            c_array(f, 'uint8_t', f'{p}_flat', tb['flat'], 32)
            c_array(f, 'int8_t', f'{p}_wtab', tb['wtab'], 32)
        else:
            f.write(f'#define {p}_flat ((const uint8_t *)0)\n#define {p}_wtab ((const int8_t *)0)\n')
        f.write('#endif\n')


def write_patterns_header(path, items):
    """items: (std, tb, name) per pattern in the button's order; tb None = the standard's own test
    card (main tables, always in RAM). Only the picture tables (in flash); sync, burst and carrier
    come from the standard's main tables, and the line timing too unless the pattern has its own
    (tb['own_timing']: no burst, or non-interlaced). Ends with the struct initialisers."""
    PAT_LIST = {'PAL': [], 'NTSC': []}
    SIZES = {'PAL': [], 'NTSC': []}
    with open(path, 'w') as f:
        f.write('// Generated by esp32/tools/make_tables20.py - do not edit.\n')
        f.write('// Extra patterns (pattern button): picture tables only, in flash.\n#pragma once\n\n')
        f.write('#ifdef FUBK20_TABLES_DATA   // data only in fubk_synth20.c\n')
        MAIN = {}
        for i, (std, tb, name) in enumerate(items):
            k = len(PAT_LIST[std.name])
            if tb is None:                          # the built-in card: MAIN in the list
                PAT_LIST[std.name].append('MAIN')
                SIZES[std.name].append('{0}')
                MAIN[std.name] = k
                continue
            q = f'{std.prefix}p{k:02d}'
            p, P = std.prefix, std.prefix.upper()
            ts = tb['std']                          # its timing (non-interlaced pages: own)
            pb, yb, ub = tb['bits']
            f.write(f'\n// {std.name} pattern {k}: {name}\n')
            A = 'FUBK_PAT_ATTR'
            c_array(f, 'uint16_t', f'{q}_row_start', tb['row_start'], attr=A)
            c_array(f, 'uint32_t', f'{q}_delta', tb['delta'], 8, attr=A)
            c_array(f, 'uint8_t', f'{q}_ypool', tb['ypool'], 32, attr=A)
            c_array(f, 'int8_t', f'{q}_uvpool', tb['uvpool'], 32, attr=A + ' __attribute__((aligned(4)))')
            if len(tb['wtab']):
                c_array(f, 'uint8_t', f'{q}_flat', tb['flat'], 32, attr=A)
                c_array(f, 'int8_t', f'{q}_wtab', tb['wtab'], 32, attr=A)
                fl, wt = f'{q}_flat', f'{q}_wtab'
            else:
                fl, wt = '0', '0'
            nfl = len(tb['flat']) if len(tb['wtab']) else 0
            if tb.get('own_timing'):
                f.write('// own line timing (line_info: bits 0-5 template kind, bit 6 sync in picture, bit 7 burst)\n')
                c_array(f, 'uint8_t', f'{q}_line_info', tb['line_info'], attr=A)
                c_array(f, 'uint16_t', f'{q}_rowmap', tb['rowmap'], attr=A)
                li, rm, nli, nrm = f'{q}_line_info', f'{q}_rowmap', ts.lines, 2 * ts.lines
                lines, rows = str(ts.lines), str(ts.rows)
            else:
                li, rm, nli, nrm = f'{p}_line_info', f'{p}_rowmap', 0, 0
                lines, rows = f'{P}_LINES', f'{P}_ROWS'
            short = f'{ts.short_line}, {ts.short_by}, {ts.ext_every}, {ts.ext_n}, {int(ts.ph_reset)}'
            text = f'{q}_text, {len(tb["text"])}' if 'text' in tb else '0, 0'
            vbi = '0' if ts.prog else f'{P}_VBI'
            if 'img' in tb:
                f.write(f'// image: {ts.rows} rows x {IMG_W} palette indices; palette (y, a, carrier offsets +V / -V)\n')
                c_array(f, 'uint8_t', f'{q}_img', tb['img'], 32, attr=A)
                f.write(f'static const uint8_t *const {A} {q}_img_rows[{ts.rows}] = {{\n')
                for r0 in range(0, ts.rows, 8):
                    f.write('    ' + ', '.join(f'{q}_img + {r * IMG_W}' for r in range(r0, min(r0 + 8, ts.rows))) + ',\n')
                f.write('};\n')
                f.write(f'static const fubk20_pal_t {A} {q}_pal[256] = {{\n')
                for r0 in range(0, 256, 4):
                    f.write('    ' + ', '.join('{%d, %d, {%d, %d}}' % tuple(int(x) for x in tb['pal'][r])
                                               for r in range(r0, r0 + 4)) + ',\n')
                f.write('};\n')
                ir, ip, npal = f'{q}_img_rows', f'{q}_pal', 12 * 256
            else:
                ir, ip, npal = '0', '0', 0
            SIZES[std.name].append(f"{{{2 * tb['row_start'].size}, {4 * tb['delta'].size}, {tb['ypool'].size}, "
                                   f"{tb['uvpool'].size}, {nfl}, {tb['wtab'].size}, {nli}, {nrm}, {npal}}}")
            PAT_LIST[std.name].append(f'PAT({p}, {P}, "{std.name} {name}", {pb}, {yb}, '
                                      f'{q}_ypool, {q}_row_start, {q}_delta, {q}_uvpool, {fl}, {wt}, '
                                      f'{lines}, {rows}, {li}, {rm}, {vbi}, {ir}, {ip}, {short}, {text})')
        for std, macro in ((PAL, 'F20_PATTERNS'), (NTSC, 'N20_PATTERNS')):
            f.write('\n// the button order; MAIN = the built-in test card of the standard (main tables)\n')
            f.write(f'#define {macro}(MAIN) \\\n    ' + ', \\\n    '.join(PAT_LIST[std.name]) + '\n')
            f.write(f'#define {macro}_MAIN {MAIN[std.name]}   // its position\n')
            f.write(f'// bytes of row_start, delta, ypool, uvpool, flat, wtab, line_info, rowmap, img_pal\n')
            f.write(f'// (copied to RAM with the pattern; the image rows are copied by the firmware)\n')
            f.write(f'#define {macro}_SIZES ' + ', '.join(SIZES[std.name]) + '\n')
        n = {len(v) for v in PAT_LIST.values()}
        assert len(n) == 1
        f.write(f'#define PATTERNS20_COUNT {len(PAT_LIST["PAL"])}   // patterns per standard\n')
        f.write('#endif\n')
    return n.pop()


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
    base, base_pic = {}, {}
    for std, pic, hname, mname, what in jobs:
        tb = build(std, *pic)
        if std is PAL and not a.test_field:                 # FuBK: the upper text in its name box
            setup_text()
            tb['text'] = fubk_text_boxes(pic[0])
        base[std.name] = tb
        base_pic[std.name] = tuple(a[:1] for a in pic)      # preview: frame A
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
    items, tbs = [], {'PAL': [], 'NTSC': []}
    total = 0
    for std in (PAL, NTSC):
        for k, ent in enumerate(pt.ORDER[std.name]):
            if ent is None:                         # the standard's own test card (main tables)
                items.append((std, None, 'main'))
                tbs[std.name].append(base[std.name])
                tag = 'pal' if std is PAL else 'ntsc'
                m, offs = model(base[std.name], 1)
                m.tofile(os.path.join(outdir, f'model20_{tag}_pat{k:02d}.u8'))
                img, w = pt.to_rgb8(std, *base_pic[std.name])
                Image.fromarray(img).resize((w, img.shape[0]), Image.LANCZOS).save(
                    os.path.join(outdir, f'preview_{tag}_pat{k:02d}.png'))
                continue
            name, fn, kind = ent
            if kind == 'image':                     # non-interlaced 256-colour picture
                sp = PAL_PROG if std is PAL else NTSC_PROG
                idx, rgb = fn(sp)
                tb = build_image(sp, idx, rgb)
                tb['own_timing'] = True
                pic = []                            # preview: the palette colours, sample by sample
                for c in range(3):
                    a = np.zeros((1, sp.rows, sp.spl))
                    v = tb['pal_yuv'][tb['img'][:, tb['img_pix']], c] - (BLANK if c == 0 else 0)
                    a[0][:, sp.img_x0:sp.img_x0 + len(tb['img_pix'])] = v * LSB_MV
                    pic.append(a)
            else:
                pic = fn(std)
                tb = build(PAL_LOCK if kind == 'lock' and std is PAL else std, *pic,
                           flat=kind not in ('noflat', 'lock'))   # lock: 36 colours, 76 KB of flat waveforms
                if name == 'PM5644':                # both text lines in its two boxes
                    tb['text'] = pm5644_text_boxes(std, pic[0])
                if kind == 'noburst':               # same timing, burst off on every line
                    tb['line_info'] = tb['line_info'] & ~0x80
                    tb['own_timing'] = True
            items.append((std, tb, name))
            tbs[std.name].append(tb)
            sz = 2 * tb['row_start'].size + 4 * tb['delta'].size + tb['ypool'].size + tb['uvpool'].size \
                + tb['wtab'].size + (len(tb['flat']) if len(tb['wtab']) else 0)
            if tb.get('own_timing'):
                sz += 3 * tb['std'].lines
            if 'img' in tb:
                sz += tb['img'].size + 12 * 256 + 4 * tb['std'].rows
            total += sz
            col = (tb['uvmap'] != 0).sum(1).max()
            print(f'  {std.name} pattern {k:2d} {name:24s} {sz / 1024:6.1f} KB, {len(tb["wtab"]) // (1 + std.vswitch):2d} flat colours, '
                  f'colour blocks per line max {col}')
            m, offs = model(tb, 1)
            tag = 'pal' if std is PAL else 'ntsc'
            m.tofile(os.path.join(outdir, f'model20_{tag}_pat{k:02d}.u8'))
            img, w = pt.to_rgb8(tb['std'], *pic)
            Image.fromarray(img).resize((w, img.shape[0]), Image.LANCZOS).save(
                os.path.join(outdir, f'preview_{tag}_pat{k:02d}.png'))
    npat = write_patterns_header(os.path.join(main_dir, 'patterns20_tables.h'), items)
    T = setup_text()
    boxes = {'f20_text': base['PAL']['text']}
    for std in (PAL, NTSC):
        for k, tb in enumerate(tbs[std.name]):
            if 'text' in tb and tb is not base[std.name]:
                boxes[f'{std.prefix}p{k:02d}_text'] = tb['text']
    write_text_header(os.path.join(main_dir, 'text20_tables.h'), T['chars'], T['font'], T['lut'], boxes)
    for name, bl in boxes.items():
        print(f'  text {name}: ' + '; '.join(f'{b["n"]} characters at rows {b["row0"]}..{b["row0"] + TEXT_ROWS - 1}, '
                                           f'samples {b["x0"]}..{b["x0"] + b["nsamp"] - 1}'
                                           + (f' (covers {b["cx0"]}..{b["cx1"] - 1})' if b['cx1'] else '') for b in bl))
    print(f'  wrote firmware/main/patterns20_tables.h: {npat} patterns per standard, {total / 1024:.1f} KB in flash')
    # switching sequences (one frame each): PAL FuBK, crosshatch, bars, red, FuBK; NTSC SMPTE,
    # bars, green, SMPTE; to / from the non-interlaced image page (312 / 262-line fields); and
    # between the built-in card and PM5644
    for std, seq, name in ((PAL, [0, 2, 5, 8, 0], 'switch'), (NTSC, [1, 5, 9, 1], 'switch'),
                           (PAL, [0, 16, 16, 16, 15, 0], 'switch_prog'), (NTSC, [1, 16, 16, 16, 15, 1], 'switch_prog'),
                           (PAL, [0, 1, 1, 0], 'switch_pm'), (NTSC, [0, 1, 1, 0], 'switch_pm')):
        tag = 'pal' if std is PAL else 'ntsc'
        m, offs = model([tbs[std.name][k] for k in seq])
        m.tofile(os.path.join(outdir, f'model20_{tag}_{name}.u8'))
        print(f'  out/model20_{tag}_{name}.u8: patterns {seq}, one frame each')


if __name__ == '__main__':
    main()
