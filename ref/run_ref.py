#!/usr/bin/env python3
"""Step 1 reference: FuBK -> 4 x fsc 8-bit PAL composite -> software PAL decoder, with checks.

Usage: run_ref.py [--zip ../data/fubk4x3.zip] [--out out] [--frames 4]
Writes the composite stream (raw uint8, 1135 x 625 per frame), PNGs and report.txt.
"""
import argparse
import os
import numpy as np
from PIL import Image
from scipy.ndimage import minimum_filter, maximum_filter
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

import fubk_data as fd
import pal_encode as pe
from pal_decode import Decoder

here = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument('--zip', default=os.path.join(here, '..', 'data', 'fubk4x3.zip'))
ap.add_argument('--out', default=os.path.join(here, 'out'))
ap.add_argument('--frames', type=int, default=4)     # 4 frames = full subcarrier cycle
ap.add_argument('--frame-lock', action='store_true',
                help='--codes is a frame-lock stream (line 5 has 1136 samples, data frame A only)')
ap.add_argument('--codes', help='check this composite file (uint8, 1135 x 625 per frame) instead of '
                'the float encoder output, e.g. the ESP32 integer model')
ap.add_argument('--line1136', action='store_true',
                help='--codes is a 1136-sample-line stream (every line one extra sample, data frame A only)')
a = ap.parse_args()
a.frame_lock = a.frame_lock or a.line1136         # same checks: one data frame, real sample count
os.makedirs(a.out, exist_ok=True)
log = open(os.path.join(a.out, 'report.txt'), 'w')


def say(*x):
    s = ' '.join(str(v) for v in x)
    print(s)
    log.write(s + '\n')


X0, X1 = 142, 862                 # 720-sample window for images
DISP_W = 788                      # 720 samples at 4:3 (702 samples = 768 px)


def save_img(name, y, by, ry, frame=0):
    rgb = fd.to_rgb(y[frame], by[frame], ry[frame])[:, X0:X1]
    Image.fromarray(rgb).resize((DISP_W, fd.ROWS), Image.LANCZOS).save(os.path.join(a.out, name))


def flat_mask(*chs, rad=(3, 10)):
    """Pixels whose neighbourhood (rows +-rad[0], cols +-rad[1]) is constant in every channel."""
    m = np.ones(chs[0].shape, bool)
    size = (2 * rad[0] + 1, 2 * rad[1] + 1)
    for c in chs:
        m &= (maximum_filter(c, size) - minimum_filter(c, size)) < 0.5
    m[:, :X0 + 20] = False
    m[:, X1 - 20:] = False
    return m


# ---------------------------------------------------------------------------------------
y, by, ry = fd.load(a.zip)
say(f'source: {y.shape[0]} frames x {y.shape[1]} rows x {y.shape[2]} samples')
save_img('00_source.png', y, by, ry)

enc = pe.Encoder(y, by, ry)
codes, mv, clipped = enc.frames_codes(a.frames)
offsets = None
if a.codes and a.line1136:
    raw = np.fromfile(a.codes, np.uint8)
    offsets = np.load(a.codes.replace('.u8', '_offsets.npy'))
    nl = min(len(offsets), a.frames * pe.LINES)
    offsets = offsets[:nl]
    codes = np.stack([raw[o:o + pe.SPL] for o in offsets]).reshape(-1, pe.LINES, pe.SPL)  # drop extras
    mv = pe.code_to_mv(codes)
    say(f'checking 1136-sample-line stream {a.codes}')
elif a.codes and a.frame_lock:
    raw = np.fromfile(a.codes, np.uint8)
    nf = raw.size // (pe.LINES * pe.SPL + 1)
    fr = raw[:nf * (pe.LINES * pe.SPL + 1)].reshape(nf, -1)
    cut = 4 * pe.SPL + pe.SPL          # drop the extra blanking sample at the end of line 5
    codes = np.delete(fr, cut, axis=1).reshape(nf, pe.LINES, pe.SPL)[:a.frames]
    offsets = np.array([f * (pe.LINES * pe.SPL + 1) + (l * pe.SPL) + (1 if l >= 5 else 0)
                        for f in range(codes.shape[0]) for l in range(pe.LINES)])
    mv = pe.code_to_mv(codes)
    say(f'checking frame-lock stream {a.codes}')
elif a.codes:
    codes = np.fromfile(a.codes, np.uint8).reshape(-1, pe.LINES, pe.SPL)[:a.frames]
    mv = pe.code_to_mv(codes)
    say(f'checking {a.codes} (the "without rounding" line below then also uses these codes)')
else:
    codes.tofile(os.path.join(a.out, f'fubk_pal_4fsc_{a.frames}frames.u8'))
say(f'\n== composite: {pe.FS / 1e6:.6f} MS/s, {pe.SPL} samples/line, {pe.SPL / 4} cycles/line')
say(f'levels: sync {pe.mv_to_code(-300):.0f}, blank/black {pe.BLANK_CODE}, white {pe.mv_to_code(700):.0f}, '
    f'{pe.LSB_MV} mV/LSB')
say(f'composite range {mv.min():.1f} .. {mv.max():.1f} mV  -> codes {codes.min()} .. {codes.max()}, '
    f'clipped samples: {clipped}')
pic = mv[:, [fd.line_to_row(l) is not None and l != 623 for l in range(1, 626)]][:, :, 200:1100]
say(f'picture-only range {pic.min():.1f} .. {pic.max():.1f} mV')

# ---------------------------------------------------------------------------------------
say('\n== decode (PAL-D = delay line, PAL-S = simple)')
dec = {}
for name, d in [('pal_d', Decoder(True)), ('pal_s', Decoder(False)),
                ('pal_d_hue20', Decoder(True, 20)), ('pal_s_hue20', Decoder(False, 20))]:
    dec[name] = d.decode(codes, offsets)
    i = d.info
    say(f'{name:12s} burst {i["burst_mv"]:.1f} mV, phase err {i["burst_phase_err_deg"]:+.2f} deg, '
        f'spread {i["burst_phase_spread_deg"]:.3f} deg, ident errors {i["ident_errors"]} '
        f'(flip-flop corrections {i["ident_flips"]}), '
        f'sync {i["sync_mv_per_code"]:.3f} mV/code')
for k, (n, title) in enumerate([('pal_d', '01_pal_d.png'), ('pal_s', '02_pal_s.png'),
                                ('pal_d_hue20', '03_pal_d_hue20.png'), ('pal_s_hue20', '04_pal_s_hue20.png')]):
    save_img(title, *dec[n])

# ---------------------------------------------------------------------------------------
say('\n== flat colour areas, frame 0 (source -> PAL-D), mV')
Y0, B0, R0 = y[0], by[0], ry[0]
fm = flat_mask(Y0, B0, R0)
fm[fd.ANTIPAL_V] = False
fm[fd.ANTIPAL_U] = False
dy, db, dr = (c[0] for c in dec['pal_d'])
keys = np.round(np.stack([Y0, B0, R0], -1)[fm]).astype(int)
vals, inv, cnt = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
inv = inv.ravel()


def region_errors(d):
    """Mean decoded - source per flat colour area, [(key, count, (eY, eB, eR))]."""
    res = []
    for j in np.argsort(-cnt):
        if cnt[j] >= 200:
            sel = inv == j
            res.append((vals[j], cnt[j], [d[c][0][fm][sel].mean() - vals[j][c] for c in range(3)]))
    return res


say(f'{"Y":>5} {"B-Y":>5} {"R-Y":>5} {"px":>6} |  {"dY":>6} {"d(B-Y)":>7} {"d(R-Y)":>7}')
worst = 0
for v, n, e in region_errors(dec['pal_d']):
    worst = max(worst, max(abs(x) for x in e))
    say(f'{v[0]:5d} {v[1]:5d} {v[2]:5d} {n:6d} |  {e[0]:+6.2f} {e[1]:+7.2f} {e[2]:+7.2f}')
say(f'worst mean error in flat areas: {worst:.2f} mV (1 LSB = {pe.LSB_MV} mV)')
fq = Decoder(True).decode(pe.mv_to_code(mv[:2]), None if offsets is None else offsets[:2 * pe.LINES])          # same signal without 8-bit rounding
wq = max(max(abs(x) for x in e) for _, _, e in region_errors(fq))
say(f'same without 8-bit rounding: worst {wq:.2f} mV -> the errors above are quantisation')
bu_ = (slice(410, 483), slice(657, 714))
say(f'  (U anti-PAL box residual with delay line: {np.abs(fq[1][0][bu_]).mean():.1f} mV unquantised, '
    f'{np.abs(db[bu_]).mean():.1f} mV at 8 bit; this is the circle crossing the box)')

# ---------------------------------------------------------------------------------------
say('\n== anti-PAL boxes (interior), median |B-Y| and |R-Y| in mV (the circle crosses the U box)')
interior = lambda sl: (slice(sl[0].start + 3, sl[0].stop - 3), slice(sl[1].start + 8, sl[1].stop - 8))
bv, bu = interior(fd.ANTIPAL_V), interior(fd.ANTIPAL_U)
say(f'source:      V-box |R-Y| {np.median(np.abs(R0[bv])):6.1f}   U-box |B-Y| {np.median(np.abs(B0[bu])):6.1f}  '
    f'(alternating line by line)')
variants = {'normal': dec}
played = {'normal': (lambda f: 0) if a.frame_lock else (lambda f: f % 2)}
for vname, kw in [('V-switch phase flipped', dict(vswitch_phase=1)),
                  ('frame A only', dict(alternate_frames=False))]:
    c2, _, _ = pe.Encoder(y, by, ry, **kw).frames_codes(a.frames)
    variants[vname] = {'pal_d': Decoder(True).decode(c2), 'pal_s': Decoder(False).decode(c2)}
    played[vname] = (lambda f: 0) if vname == 'frame A only' else (lambda f: f % 2)
for vname, dd in variants.items():
    for n in ('pal_d', 'pal_s'):
        res = []
        for f in range(a.frames):
            _, b_, r_ = (c[f] for c in dd[n])
            res.append((np.median(np.abs(r_[bv])), np.median(np.abs(b_[bu])),
                        np.median(np.abs(r_[bv] - ry[played[vname](f)][bv])), np.median(np.abs(b_[bu] - by[played[vname](f)][bu]))))
        r = np.array(res)
        say(f'{vname:24s} {n}: V-box |R-Y| {r[:, 0].mean():6.1f}, U-box |B-Y| {r[:, 1].mean():6.1f}  '
            f'| vs data played: {r[:, 2].mean():6.1f} / {r[:, 3].mean():6.1f}')

# ---------------------------------------------------------------------------------------
say('\n== 4.43 MHz patch (orange)')
o = interior(fd.ORANGE)
say(f'source Y {Y0[o].mean():.1f}, B-Y {B0[o].mean():.1f}, R-Y {R0[o].mean():.1f} '
    f'-> chroma amplitude {np.hypot(0.493 * B0[o].mean(), 0.877 * R0[o].mean()):.1f} mV')
rows = [r for r in range(o[0].start, o[0].stop)]
lines_o = [l for l in range(1, 626) if fd.line_to_row(l) in rows]
ocols = slice(int((o[1].start + 10 - fd.COL_0H) / 13.5e6 * pe.FS + pe.T0 * pe.FS),
              int((o[1].stop - 10 - fd.COL_0H) / 13.5e6 * pe.FS + pe.T0 * pe.FS))
seg = mv[0][np.array(lines_o) - 1][:, ocols]
say(f'composite samples in patch: {seg.min():.1f} .. {seg.max():.1f} mV (samples sit at 0/90/180/270 deg, '
    f'Y+-U and Y+-V; the reconstructed sine peaks at Y +- amplitude)')
say(f'decoded PAL-D:  Y {dy[o].mean():.1f}, B-Y {db[o].mean():.1f}, R-Y {dr[o].mean():.1f}')

# ---------------------------------------------------------------------------------------
say('\n== gratings (luma, 700 mV p-p in the source): amplitude in composite and in decoded Y')
gr = 340
tl = pe.t_line()
line_g = [l for l in range(1, 626) if fd.line_to_row(l) == gr][0]
comp = mv[0][line_g - 1]


def amp(sig, t, f):
    A = np.stack([np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t), np.ones_like(t)], 1)
    c = np.linalg.lstsq(A, sig, rcond=None)[0]
    return 2 * np.hypot(c[0], c[1])


for c0, c1, f in fd.GRATINGS:
    ts = (np.arange(c0, c1) - fd.COL_0H) / fd.FS_SRC
    m = (tl >= ts[0]) & (tl <= ts[-1])
    a_s, a_c, a_d = amp(Y0[gr, c0:c1], ts, f), amp(comp[m], tl[m], f), amp(dy[gr, c0:c1], ts, f)
    say(f'  {f / 1e6:.0f} MHz  source {a_s:5.1f}  composite {a_c:5.1f} ({20 * np.log10(a_c / a_s):+.2f} dB)  '
        f'decoded Y (notch) {a_d:5.1f} ({20 * np.log10(a_d / a_s):+.1f} dB)  mV p-p')

# ---------------------------------------------------------------------------------------
say('\n== subcarrier phase step check (burst phase spread over all lines and frames)')
for name, step in [('global counter (correct)', None), ('-1 per line (= +3)', 3), ('+1 per line (notes)', 1)]:
    c3, _, _ = pe.Encoder(y, by, ry, phase_step=step).frames_codes(2)
    d3 = Decoder(True)
    out3 = d3.decode(c3)
    i = d3.info
    say(f'  {name:26s} spread {i["burst_phase_spread_deg"]:7.2f} deg, ident errors {i["ident_errors"]}')
    if step == 1:
        save_img('05_wrong_phase_step.png', *out3)

# ---------------------------------------------------------------------------------------
# waveform plots
fig, ax = plt.subplots(3, 1, figsize=(12, 10))
bar_line = [l for l in range(1, 626) if fd.line_to_row(l) == 150][0]
us = tl * 1e6
ax[0].plot(us, codes[0][bar_line - 1], lw=0.6)
ax[0].set_title(f'line {bar_line}: colour bars (DAC codes)')
ax[1].plot(us, codes[0][line_g - 1], lw=0.6)
ax[1].set_title(f'line {line_g}: gratings + 4.43 MHz patch (DAC codes)')
m = (tl > -0.5e-6) & (tl < 11e-6)
ax[2].step(us[m], codes[0][bar_line - 1][m], where='post', lw=0.8, label=f'line {bar_line}')
ax[2].step(us[m], codes[0][bar_line][m], where='post', lw=0.8, label=f'line {bar_line + 1}')
ax[2].set_title('sync + burst (135 / 225 deg alternating)')
ax[2].legend()
for x_ in ax:
    x_.grid(alpha=0.3)
    x_.set_xlabel('us from 0H')
    for lvl in (4, 64, 204):
        x_.axhline(lvl, color='k', lw=0.4, ls=':')
fig.tight_layout()
fig.savefig(os.path.join(a.out, '06_waveforms.png'), dpi=110)

# zoomed crops: anti-PAL boxes and gratings, PAL-D vs PAL-S vs source
crops = []
for n in (None, 'pal_d', 'pal_s'):
    yy, bb, rr = (y, by, ry) if n is None else dec[n]
    rgb = fd.to_rgb(yy[0], bb[0], rr[0])[320:490, 540:760]
    crops.append(np.kron(rgb, np.ones((3, 3, 1), np.uint8)))
sep = np.full((crops[0].shape[0], 6, 3), 255, np.uint8)
Image.fromarray(np.hstack([crops[0], sep, crops[1], sep, crops[2]])).save(
    os.path.join(a.out, '07_zoom_source_pald_pals.png'))
say(f'\nwrote {a.out}')
