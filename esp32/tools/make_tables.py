#!/usr/bin/env python3
"""Build the ESP32 FuBK tables (fubk_tables.h) and an integer model of the C synthesis.

Usage: make_tables.py [--zip ../../data/fubk4x3.zip] [--header ../firmware/main/fubk_tables.h]
                      [--model ../out/model_4frames.u8]

Table format (all in DAC codes, 5 mV/LSB, blanking = 64):
  - Active window: samples [A0, A0+NA) of each line, cut into NB blocks of B samples.
  - Y blocks:  uint8, Y + 64.                 Deduplicated.
  - UV blocks: int8, U and V interleaved.     Deduplicated.
  - Picture rows (2 data frames x 576) are coded as changes against the row two above
    (= the previous line of the same field). Lines are generated in order, so the synth keeps
    the current block list and applies the changes. The first row of each field lists all
    NB blocks. Change entry (uint32): bits 0-5 block position, 6-16 Y block, 17-26 UV block.
  - Sync templates per line kind (uint8 codes, whole line) and burst snippets per
    (subcarrier phase, V-switch), added on top of the template.
The integer model below is the spec; the C code must reproduce it byte for byte.
"""
import argparse
import os
import sys
import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, '..', '..', 'ref'))
import fubk_data as fd          # noqa: E402
import pal_encode as pe         # noqa: E402

A0, NA, B = 192, 936, 24
NB = NA // B
BW0, BWN = 100, 64              # burst snippet window (4.84 .. 8.45 us after 0H)
POS_BITS, Y_BITS, UV_BITS = 6, 11, 10
LONG_LINE = 5                   # FUBK_FRAME_LOCK: this line (vertical sync, no burst) is 1136 samples
KINDS = ['N-', 'NE', 'EE', 'BB', 'BE', 'EB', 'E-']


def apll_coeffs(f_target, max_fsc_err_hz=None):
    """ESP32 APLL: f = xtal * (4 + sdm2 + sdm1/256 + sdm0/65536) / (2 * (odiv + 2)),
    VCO (the numerator) 350..500 MHz. Returns (odiv, sdm0, sdm1, sdm2, f).

    The fractional part is made by a sigma-delta modulator whose pattern repeats every
    65536 / gcd(frac, 65536) crystal cycles. A long pattern puts slow jitter on the clock
    (a wave in the picture). With max_fsc_err_hz, pick the fraction with the shortest pattern
    (most trailing zero bits) whose subcarrier error stays within that limit."""
    xtal = 40e6
    cands = []
    for odiv in range(32):
        vco = f_target * 2 * (odiv + 2)
        if not 350e6 <= vco <= 500e6:
            continue
        x = vco / xtal - 4
        sdm2 = int(x)
        exact = (x - sdm2) * 65536
        for frac in range(int(exact) - 64, int(exact) + 66):
            if not 0 <= frac < 65536:
                continue
            f = xtal * (4 + sdm2 + frac / 65536) / (2 * (odiv + 2))
            err_fsc = (f / f_target - 1) * 4433618.75
            tz = 16 if frac == 0 else (frac & -frac).bit_length() - 1
            cands.append((odiv, frac & 0xFF, frac >> 8, sdm2, f, err_fsc, tz))
    best_err = min(cands, key=lambda c: abs(c[5]))
    if max_fsc_err_hz is None:
        return best_err[:5]
    ok = [c for c in cands if abs(c[5]) <= max_fsc_err_hz]
    return max(ok, key=lambda c: (c[6], -abs(c[5])))[:5]


def sd_period(sdm0, sdm1):
    frac = sdm0 | (sdm1 << 8)
    return 1 if frac == 0 else 65536 // np.gcd(frac, 65536)


def test_field(y, by, ry, level=(350.0, -589.8, 224.0)):
    """Replace the picture with one flat colour (default: the orange 4.43 MHz patch), keeping the
    picture edges and the half lines of the original (mask = where the original has any picture)."""
    yy, bb, rr = (np.zeros_like(a) for a in (y, by, ry))
    c = np.arange(y.shape[-1])
    edge = 0.5 * (np.tanh((c - fd.PIC_COLS[0]) / 1.2) - np.tanh((c - fd.PIC_COLS[1]) / 1.2))
    half = np.ones(y.shape[1:])
    mid = int(fd.COL_0H + 32e-6 * fd.FS_SRC)
    half[0, :mid] = 0                  # line 23: second half only
    half[-1, mid:] = 0                 # line 623: first half only
    w = edge[None, None, :] * half[None]
    return level[0] * w * np.ones_like(y), level[1] * w * np.ones_like(y), level[2] * w * np.ones_like(y)


def build(zip_path, flat=False):
    y, by, ry = fd.load(zip_path)
    if flat:
        y, by, ry = test_field(y, by, ry)
    Y, U, V = pe.prepare_picture(y, by, ry)
    # Burst samples are +-150/sqrt(2) mV = 21.2 LSB and round to 21 (148.5 mV). A TV scales
    # chroma by the burst (ACC), so scale U and V by the same factor: the ratio stays exact.
    bamp = pe.BURST_MV / np.sqrt(2)
    cgain = np.round(bamp / pe.LSB_MV) * pe.LSB_MV / bamp
    Yq = np.round(Y / pe.LSB_MV).astype(int)
    Uq, Vq = (np.round(a * cgain / pe.LSB_MV).astype(int) for a in (U, V))
    outside = np.ones(pe.SPL, bool)
    outside[A0:A0 + NA] = False
    assert not (Yq[..., outside].any() or Uq[..., outside].any() or Vq[..., outside].any()), \
        'picture data outside the active window'

    w = slice(A0, A0 + NA)
    yb = (Yq[..., w] + pe.BLANK_CODE).reshape(2, fd.ROWS, NB, B)
    uvb = np.stack([Uq[..., w], Vq[..., w]], -1).reshape(2, fd.ROWS, NB, 2 * B)
    assert yb.min() >= 0 and yb.max() <= 255 and np.abs(uvb).max() <= 127

    def dedupe(a):
        flat = a.reshape(-1, a.shape[-1])
        u, inv = np.unique(flat, axis=0, return_inverse=True)
        return u, inv.reshape(a.shape[:3])

    ypool, yid = dedupe(yb)
    uvpool, uvid = dedupe(uvb)
    assert len(ypool) <= 1 << Y_BITS and len(uvpool) <= 1 << UV_BITS and NB <= 1 << POS_BITS

    delta, row_start = [], []
    for df in range(2):
        for r in range(fd.ROWS):
            row_start.append(len(delta))
            for b in range(NB):
                if r < 2 or yid[df, r, b] != yid[df, r - 2, b] or uvid[df, r, b] != uvid[df, r - 2, b]:
                    delta.append(b | (int(yid[df, r, b]) << POS_BITS) |
                                 (int(uvid[df, r, b]) << (POS_BITS + Y_BITS)))
    row_start.append(len(delta))
    assert len(delta) < 65536

    # sync templates and burst snippets
    sync = pe.sync_waveforms()
    kind_of_line = [KINDS.index(pe._line_kind(l)) for l in range(1, pe.LINES + 1)]
    tmpl = np.zeros((len(KINDS), pe.SPL), int)
    for l in range(1, pe.LINES + 1):
        tmpl[kind_of_line[l - 1]] = np.round(pe.mv_to_code(sync[l - 1]))
    benv = pe.burst_envelope()
    bw = slice(BW0, BW0 + BWN)
    assert np.abs(np.delete(benv, np.r_[bw])).max() * 150 / pe.LSB_MV < 0.1
    n = np.arange(BW0, BW0 + BWN)
    burst = np.zeros((8, BWN), int)
    for p in range(4):
        for si, s in enumerate((1, -1)):
            k = (p + n) % 4
            burst[p * 2 + si] = np.round(benv[bw] * (-bamp * pe.SIN4[k] + s * bamp * pe.COS4[k]) / pe.LSB_MV)
    line_info = np.array([kind_of_line[l - 1] | (0x80 if pe.has_burst(l) else 0)
                          for l in range(1, pe.LINES + 1)])
    rowmap = np.array([fd.line_to_row(l) if fd.line_to_row(l) is not None else 0xFFFF
                       for l in range(1, pe.LINES + 1)])
    return dict(cgain=cgain, Yq=Yq, Uq=Uq, Vq=Vq, ypool=ypool, uvpool=uvpool, delta=np.array(delta, np.uint32),
                row_start=np.array(row_start), tmpl=tmpl, burst=burst, line_info=line_info,
                rowmap=rowmap)


def make_line(t, l, p, si, df):
    """One line (integer codes), subcarrier phase p at its start, V-switch index si, data frame df."""
    n = np.arange(A0, A0 + NA)
    s = 1 - 2 * si
    info = t['line_info'][l - 1]
    line = t['tmpl'][info & 0x7F].copy()
    if info & 0x80:
        line[BW0:BW0 + BWN] += t['burst'][p * 2 + si]
    row = t['rowmap'][l - 1]
    if row != 0xFFFF:
        k = (p + n) % 4
        u, v = t['Uq'][df, row, A0:A0 + NA], t['Vq'][df, row, A0:A0 + NA]
        c = np.select([k == 0, k == 1, k == 2, k == 3], [s * v, u, -s * v, -u])
        line[A0:A0 + NA] += t['Yq'][df, row, A0:A0 + NA] + c
    return line


def model(t, n_frames):
    """Integer reference of the C synthesis, standard 4-frame sequence: (frames, 625, SPL) uint8."""
    out = np.zeros((n_frames, pe.LINES, pe.SPL), int)
    for f in range(n_frames):
        for l in range(1, pe.LINES + 1):
            g = (f % 4) * pe.LINES + l - 1
            out[f, l - 1] = make_line(t, l, (g * 3) % 4, g % 2, f % 2)   # p = (g * 1135) mod 4
    assert out.min() >= 0 and out.max() <= 255
    return out.astype(np.uint8)


def model_frame_lock(t, n_frames):
    """FUBK_FRAME_LOCK: line LONG_LINE gets one extra blanking sample (1136), so a frame is
    709376 samples = a whole number of subcarrier cycles. Phase, V-switch and data (frame A) then
    repeat every frame. Returns the raw sample stream and the start offset of every line."""
    lines, offs, pos = [], [], 0
    for f in range(n_frames):
        for l in range(1, pe.LINES + 1):
            p = ((l - 1) * 3 + (1 if l > LONG_LINE else 0)) % 4
            line = make_line(t, l, p, (l - 1) % 2, 0)
            if l == LONG_LINE:
                line = np.append(line, pe.BLANK_CODE)
            offs.append(pos)
            pos += len(line)
            lines.append(line)
    out = np.concatenate(lines)
    assert out.min() >= 0 and out.max() <= 255 and pos == n_frames * (pe.LINES * pe.SPL + 1)
    return out.astype(np.uint8), np.array(offs)


def model_line1136(t, n_frames):
    """FUBK_LINE1136: every line gets one extra blanking sample (1136 = 284 cycles), so every line
    starts at phase 0. V-switch alternates per line, data frame A.
    Lines 8-14 and 321-327 get 4 more blanking samples (1140): frame = 710056 = 3128 x 227 samples,
    the repeat length of the APLL 41/64 jitter. Returns the stream and the start of every line."""
    lines, offs, pos = [], [], 0
    for f in range(n_frames):
        for l in range(1, pe.LINES + 1):
            pad = 5 if (8 <= l <= 14 or 321 <= l <= 327) else 1
            line = np.append(make_line(t, l, 0, (l - 1) % 2, 0), [pe.BLANK_CODE] * pad)
            offs.append(pos)
            pos += len(line)
            lines.append(line)
    out = np.concatenate(lines)
    assert out.min() >= 0 and out.max() <= 255 and pos == n_frames * 710056
    return out.astype(np.uint8), np.array(offs)


def c_array(f, ctype, name, a, per_line=24):
    a = np.asarray(a).ravel()
    f.write(f'static const {ctype} FUBK_TABLE_ATTR {name}[{len(a)}] = {{\n')
    for i in range(0, len(a), per_line):
        f.write(','.join(str(int(x)) for x in a[i:i + per_line]) + ',\n')
    f.write('};\n')


def write_header(path, t, apll, apll_exact):
    with open(path, 'w') as f:
        f.write('// Generated by esp32/tools/make_tables.py from the PT8631 FuBK data. Do not edit.\n')
        f.write('#pragma once\n#include <stdint.h>\n\n')
        for k, v in [('FUBK_SPL', pe.SPL), ('FUBK_LINES', pe.LINES), ('FUBK_BLANK', pe.BLANK_CODE),
                     ('FUBK_A0', A0), ('FUBK_NA', NA), ('FUBK_B', B), ('FUBK_NB', NB),
                     ('FUBK_BW0', BW0), ('FUBK_LONG_LINE', LONG_LINE), ('FUBK_BWN', BWN), ('FUBK_NKINDS', len(KINDS)),
                     ('FUBK_NY', len(t['ypool'])), ('FUBK_NUV', len(t['uvpool'])),
                     ('FUBK_NDELTA', len(t['delta'])), ('FUBK_ROWS', 2 * fd.ROWS),
                     ('FUBK_POS_BITS', POS_BITS), ('FUBK_Y_BITS', Y_BITS), ('FUBK_UV_BITS', UV_BITS)]:
            f.write(f'#define {k} {v}\n')
        odiv, s0, s1, s2, fa = apll
        f.write(f'\n// APLL for 4 x 17.734475 MHz: {fa:.3f} Hz (I2S DAC sample rate = APLL / 4),\n'
                f'// sigma-delta pattern {sd_period(s0, s1)} crystal cycles\n')
        f.write(f'#define FUBK_APLL_ODIV {odiv}\n#define FUBK_APLL_SDM0 {s0}\n'
                f'#define FUBK_APLL_SDM1 {s1}\n#define FUBK_APLL_SDM2 {s2}\n')
        eo, e0, e1, e2, ef = apll_exact
        f.write(f'// most exact frequency: {ef:.3f} Hz, sigma-delta pattern {sd_period(e0, e1)} crystal cycles\n')
        f.write(f'#define FUBK_APLL_EXACT_ODIV {eo}\n#define FUBK_APLL_EXACT_SDM0 {e0}\n'
                f'#define FUBK_APLL_EXACT_SDM1 {e1}\n#define FUBK_APLL_EXACT_SDM2 {e2}\n\n')
        f.write('// The arrays are only compiled where FUBK_TABLES_DATA is defined (one .c file).\n')
        f.write('#ifdef FUBK_TABLES_DATA\n')
        f.write('#ifndef FUBK_TABLE_ATTR\n#define FUBK_TABLE_ATTR   // firmware: DRAM_ATTR (internal RAM, not flash)\n#endif\n')
        f.write('// line_info: bits 0-6 template kind, bit 7 burst on. rowmap: data row or 0xFFFF\n')
        c_array(f, 'uint8_t', 'fubk_line_info', t['line_info'])
        c_array(f, 'uint16_t', 'fubk_rowmap', t['rowmap'])
        c_array(f, 'uint8_t', 'fubk_tmpl', t['tmpl'], 40)
        c_array(f, "int8_t", "fubk_burst", t["burst"], 64)
        c_array(f, 'uint16_t', 'fubk_row_start', t['row_start'])
        c_array(f, 'uint32_t', 'fubk_delta', t['delta'], 12)
        c_array(f, 'uint8_t', 'fubk_ypool', t['ypool'], B)
        c_array(f, 'int8_t', 'fubk_uvpool', t['uvpool'], 2 * B)
        f.write('#endif\n')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--zip', default=os.path.join(here, '..', '..', 'data', 'fubk4x3.zip'))
    ap.add_argument('--header', default=os.path.join(here, '..', 'firmware', 'main', 'fubk_tables.h'))
    ap.add_argument('--model', default=os.path.join(here, '..', 'out', 'model_4frames.u8'))
    ap.add_argument('--ref', default=os.path.join(here, '..', '..', 'ref', 'out', 'fubk_pal_4fsc_4frames.u8'))
    ap.add_argument('--test-field', action='store_true',
                    help='flat orange picture (constant 4.43 MHz chroma) -> fubk_tables_test.h, no model')
    ap.add_argument('--lsb-mv', type=float, default=pe.LSB_MV,
                    help='mV per DAC code at the TV input (measure it on the loaded pin)')
    ap.add_argument('--blank-code', type=int, default=pe.BLANK_CODE, help='DAC code for blanking/black')
    ap.add_argument('--apll-max-fsc-err', type=float, default=30.0,
                    help='allowed subcarrier error (Hz) to get a short APLL sigma-delta pattern; '
                         '0 = most exact frequency')
    a = ap.parse_args()
    custom = (a.lsb_mv, a.blank_code) != (pe.LSB_MV, pe.BLANK_CODE)
    pe.LSB_MV, pe.BLANK_CODE = a.lsb_mv, a.blank_code
    print(f'levels: {pe.LSB_MV} mV/code, sync {pe.mv_to_code(-300):.1f}, blank {pe.BLANK_CODE}, '
          f'white {pe.mv_to_code(700):.1f}, 100% bars peak {pe.mv_to_code(933):.1f}')
    if a.test_field:
        t = build(a.zip, flat=True)
        apll = apll_coeffs(4 * pe.FS, a.apll_max_fsc_err or None)
        path = os.path.join(os.path.dirname(a.header), 'fubk_tables_test.h')
        write_header(path, t, apll, apll_coeffs(4 * pe.FS, None))
        print(f'wrote {path}: {len(t["ypool"])} Y blocks, {len(t["uvpool"])} UV blocks, '
              f'{len(t["delta"])} changes')
        sys.exit(0)
    t = build(a.zip)
    apll = apll_coeffs(4 * pe.FS, a.apll_max_fsc_err or None)
    apll_exact = apll_coeffs(4 * pe.FS, None)
    write_header(a.header, t, apll, apll_exact)

    sizes = {'Y blocks': t['ypool'].size, 'UV blocks': t['uvpool'].size, 'changes': t['delta'].size * 4,
             'row index': t['row_start'].size * 2, 'templates': t['tmpl'].size, 'burst': t['burst'].size,
             'line info + rowmap': 625 * 3}
    print('table sizes (bytes):')
    for k, v in sizes.items():
        print(f'  {k:20s} {v:7d}')
    print(f'  {"total":20s} {sum(sizes.values()):7d}  ({sum(sizes.values()) / 1024:.1f} KB)')
    print(f'  blocks: {len(t["ypool"])} Y, {len(t["uvpool"])} UV; {len(t["delta"])} changes, '
          f'max per line {np.diff(t["row_start"]).max()} (first lines of a field: {NB})')
    odiv, s0, s1, s2, fa = apll
    print(f'APLL sigma-delta pattern: {sd_period(s0, s1)} crystal cycles '
          f'({sd_period(s0, s1) / 40:.1f} us)')
    print(f'APLL odiv={odiv} sdm0={s0} sdm1={s1} sdm2={s2}: {fa / 1e6:.6f} MHz -> '
          f'{fa / 4:.2f} Hz sample rate ({(fa / 4 / pe.FS - 1) * 1e6:+.2f} ppm, '
          f'fsc error {fa / 16 - pe.FSC:+.2f} Hz)')

    print(f'chroma gain {t["cgain"]:.5f} (matches the quantised burst)')
    m = model(t, 4)
    os.makedirs(os.path.dirname(a.model), exist_ok=True)
    m.tofile(a.model)
    fl, offs = model_frame_lock(t, 4)
    fl_path = a.model.replace('model_', 'model_framelock_')
    fl.tofile(fl_path)
    np.save(fl_path.replace('.u8', '_offsets.npy'), offs)
    print(f'wrote {fl_path} (frame lock: {len(fl) // 4} samples per frame)')
    l36, l36_offs = model_line1136(t, 4)
    l36_path = a.model.replace('model_', 'model_line1136_')
    l36.tofile(l36_path)
    np.save(l36_path.replace('.u8', '_offsets.npy'), l36_offs)
    print(f'wrote {l36_path} (1136-sample lines: {len(l36) // 4} samples per frame)')
    print(f'wrote {a.header}\nwrote {a.model}')
    if os.path.exists(a.ref) and not custom:
        ref = np.fromfile(a.ref, np.uint8).reshape(m.shape)
        d = m.astype(int) - ref
        print(f'integer model vs float reference: {np.count_nonzero(d)} of {d.size} samples differ '
              f'({np.count_nonzero(d) / d.size * 100:.2f} %), max |diff| {np.abs(d).max()} LSB')
