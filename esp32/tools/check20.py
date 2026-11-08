"""Check a 20 MHz composite stream (uint8 codes, spl x lines per frame) by synchronous
demodulation with the exact subcarrier from a free-running sample counter.

PAL (default): FuBK - burst 150 mV at 135/225 deg (less the DAC correction), chroma vs source, anti-PAL grey with PAL-D,
the orange patch. --test-field: the flat orange PAL field.
NTSC (--ntsc): SMPTE bars - burst 143 mV (20 IRE) at 180 deg, every bar/box against EG-1.
usage: check20.py codes.u8 [--test-field | --ntsc]
"""
import argparse
import os
import sys

import numpy as np
from scipy.ndimage import gaussian_filter1d

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import make_tables20 as mt   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('codes')
ap.add_argument('--test-field', action='store_true')
ap.add_argument('--ntsc', action='store_true')
a = ap.parse_args()
std = mt.NTSC if a.ntsc else mt.PAL

SPL, L = std.spl, std.lines
raw = np.fromfile(a.codes, np.uint8)
offp = a.codes.replace('.u8', '_offsets.npy')
offsets = np.load(offp) if os.path.exists(offp) else np.arange(raw.size // SPL) * SPL
nf = len(offsets) // L
offsets = offsets[:nf * L]
codes = np.stack([raw[o:o + SPL] for o in offsets]).reshape(nf, L, SPL).astype(float)
mv = (codes - mt.BLANK) * mt.LSB_MV
lens = np.diff(np.append(offsets, raw.size))
avg = lens[:-1].mean()
print(f'{std.name}: {nf} frames, lines {lens.min()}..{lens.max()} samples, average {avg:.4f} = '
      f'{avg / mt.FS * 1e6:.4f} us ({mt.FS / avg:.3f} Hz) = {avg * std.ph_num / std.ph_den:.4f} fsc cycles; '
      f'fsc {std.fsc:.2f} Hz')

n = np.arange(SPL)
SIG = 4.0                                    # demod low-pass, samples (removes 2 fsc)
if a.ntsc:
    pic = mt.smpte_picture()
elif a.test_field:
    pic = mt.orange_picture()
else:
    pic = mt.fubk_picture()
tb = mt.build(std, *pic)

amp, ang = [], []
Ud = np.zeros((nf, L, SPL))
Vd = np.zeros((nf, L, SPL))
for f in range(nf):
    for l in range(1, L + 1):
        g = f * L + l - 1
        s = (1 - 2 * (g & 1)) if std.vswitch else 1
        ph = ((int(offsets[g]) + n) * std.ph_num % std.ph_den) / std.ph_den   # true sample counter
        sn, cs = np.sin(2 * np.pi * ph), np.cos(2 * np.pi * ph)
        x = mv[f, l - 1]
        u = gaussian_filter1d(2 * x * sn, SIG)
        v = gaussian_filter1d(2 * x * cs, SIG)
        Ud[f, l - 1], Vd[f, l - 1] = u, s * v
        if tb['line_info'][l - 1] & 0x80:
            c = std.bw0 + std.bwn // 2
            amp.append(np.hypot(u[c], v[c]))
            ang.append((np.degrees(np.arctan2(v[c], u[c])) % 360, s))
amp = np.array(amp)
print(f'burst: amplitude {amp.min():.2f} .. {amp.max():.2f} mV (want {std.burst[2]:.1f})')
if std.vswitch:
    a1 = np.array([x for x, s in ang if s > 0])
    a2 = np.array([x for x, s in ang if s < 0])
    rot = std.burst_rot * 360 / std.car_n        # built-in rotation against the DAC (make_tables20.py)
    print(f'       phase {a1.min():.2f} .. {a1.max():.2f} deg on +V lines (want {135 + rot:.1f} = 135 {rot:+.1f}), '
          f'{a2.min():.2f} .. {a2.max():.2f} on -V lines (want {225 + rot:.1f})')
else:
    a1 = np.array([x for x, _ in ang])
    print(f'       phase {a1.min():.2f} .. {a1.max():.2f} deg (want 180)')

# chroma vs source (same demod filter applied to the quantised source values)
act = slice(std.a0 + 24, std.a0 + std.na - 24)
worst = 0
for f in range(nf):
    df = f % std.data_frames
    for l in range(1, L + 1):
        r = tb['rowmap'][l - 1]
        if r == 0xFFFF or tb['line_info'][l - 1] & 0x40:
            continue
        ur = gaussian_filter1d(tb['Uq'][df, r] * mt.LSB_MV, SIG)
        vr = gaussian_filter1d(tb['Vq'][df, r] * mt.LSB_MV, SIG)
        # luma near fsc (gratings, edges) leaks into the demodulated chroma: compare only where
        # the source luma is smooth
        yr = tb['Yq'][df, r] * mt.LSB_MV
        smooth = np.abs(yr - gaussian_filter1d(yr, 6.0)) < 1.0
        smooth = gaussian_filter1d(smooth.astype(float), 6.0) > 0.999
        m = np.zeros(SPL, bool)
        m[act] = True
        m &= smooth
        if m.any():
            e = max(np.abs(Ud[f, l - 1] - ur)[m].max(), np.abs(Vd[f, l - 1] - vr)[m].max())
            worst = max(worst, e)
print(f'chroma error vs source where the luma is smooth: max {worst:.2f} mV (1 LSB = {mt.LSB_MV} mV)')


def col(t_us):
    return int(round((t_us * 1e-6 + mt.T0) * mt.FS))


if a.ntsc:
    # every SMPTE area: decoded Y (low-passed composite), U, V against EG-1
    p0, p1 = mt.SMPTE_EDGES
    wbar = (p1 - p0) / 7
    print('SMPTE areas, decoded (want):           Y mV            U mV            V mV')
    werr = 0
    for start, segs in mt.smpte_values():
        r = start + 10
        l = next(l for l in range(1, L + 1) if tb['rowmap'][l - 1] == r)
        x = p0
        y_lp = gaussian_filter1d(mv[0, l - 1], 8.0)
        for w, (yv, uv, vv) in segs:
            tc = x + w * wbar / 2
            c = col(tc * 1e6)
            dy, du, dv = y_lp[c], Ud[0, l - 1, c], Vd[0, l - 1, c]
            werr = max(werr, abs(dy - yv), abs(du - uv), abs(dv - vv))
            print(f'  row {r:3d} at {tc * 1e6:5.1f} us   {dy:6.1f} ({yv:6.1f})  '
                  f'{du:6.1f} ({uv:6.1f})  {dv:6.1f} ({vv:6.1f})')
            x += w * wbar
    print(f'worst SMPTE area error: {werr:.2f} mV (includes 8-bit rounding, 1 LSB = 5 mV)')
elif not a.test_field:
    import fubk_data as fd
    for name, (rows, cols), D in (('V box', fd.ANTIPAL_V, Vd), ('U box', fd.ANTIPAL_U, Ud)):
        c0 = col((cols.start - fd.COL_0H) / fd.FS_SRC * 1e6) + 12
        c1 = col((cols.stop - fd.COL_0H) / fd.FS_SRC * 1e6) - 12
        ls = [l for l in range(1, L + 1) if tb['rowmap'][l - 1] != 0xFFFF
              and rows.start + 4 <= tb['rowmap'][l - 1] < rows.stop - 4]
        single = np.median(np.abs(D[0, [l - 1 for l in ls], c0:c1]))
        pal_d = np.median(np.abs(0.5 * (D[0, [l - 1 for l in ls[:-1]], c0:c1] + D[0, [l for l in ls[:-1]], c0:c1])))
        print(f'anti-PAL {name}: single line {single:6.1f} mV, PAL-D average {pal_d:5.1f} mV (want ~0)')
    r0, c0 = fd.ORANGE
    rr = [l for l in range(1, L + 1) if tb['rowmap'][l - 1] != 0xFFFF and r0.start + 3 <= tb['rowmap'][l - 1] < r0.stop - 3]
    cc = slice(col((c0.start - fd.COL_0H) / fd.FS_SRC * 1e6) + 12, col((c0.stop - fd.COL_0H) / fd.FS_SRC * 1e6) - 12)
    print(f'orange patch: B-Y {np.median(Ud[0, [l - 1 for l in rr], cc]) / 0.493:.1f} mV, '
          f'R-Y {np.median(Vd[0, [l - 1 for l in rr], cc]) / 0.877:.1f} mV (source -589.8 / 224.0)')

if std is mt.PAL:
    print(f'subcarrier: 4 frames = {4 * L * SPL} samples = {4 * L * SPL * std.ph_num / std.ph_den:.4f} cycles '
          f'(integer -> 8-field sequence)')
