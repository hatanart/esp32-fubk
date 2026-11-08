#!/usr/bin/env python3
"""Check the data lines of the 20 MS/s output with an independent decoder.

usage: check_vbi20.py pal|ntsc <with data .u8> <without data .u8> <first frame>

1. All other lines must be identical to the output without data lines.
2. Teletext (PAL): the line is low-passed (5 MHz, as a TV's data slicer sees it), the bit
   timing is found from the clock run-in + framing code, and all 45 bytes are sliced at the
   bit centres. Checks: framing code, Hamming codes, parity, the bytes themselves, header clock;
   the timing range where every bit decodes (eye width) and the smallest distance from the
   threshold (eye height).
3. Captions (NTSC): run-in cycles, start-bit edge time, the 2 bytes with parity; a small
   EIA-608 pop-on decoder turns the byte pairs into the caption text.
"""
import os
import sys

import numpy as np
from scipy.signal import butter, lfilter

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import make_vbi20 as mv  # noqa: E402

FS = 20e6
S0H = 16
LSB_MV = 5.0
BLANK = 64


def fail(msg):
    print('FAIL:', msg)
    sys.exit(1)


def hamming84(b):
    """Decode a Hamming 8/4 byte -> nibble (or None if it has an error)."""
    for v, code in enumerate(mv.HAM):
        if code == b:
            return v
    return None


def odd(b):
    return bin(b).count('1') % 2 == 1


def lines_of(path, spl, lines):
    d = np.fromfile(path, np.uint8)
    assert d.size % (spl * lines) == 0, 'not whole frames'
    return d.reshape(-1, lines, spl)


def compare_other(a, b, data_lines):
    diff = np.nonzero((a != b).any(axis=2))
    bad = [(f, l + 1) for f, l in zip(*diff) if l + 1 not in data_lines]
    if bad:
        fail(f'lines without data differ: {bad[:5]}')
    print(f'  all other lines identical to the output without data ({a.shape[0]} frames)')


# --- teletext ----------------------------------------------------------------------------------
TTX_T = 1 / 6.9375e6


def ttx_slice(mv_line, t0):
    """Sample 360 bits at the bit centres t0 + (k + 0.5) T (t0 from 0H, s)."""
    t = S0H / FS + t0 + (np.arange(360) + 0.5) * TTX_T
    return np.interp(t * FS, np.arange(mv_line.size), mv_line)


def ttx_decode(line_codes):
    mvl = (line_codes.astype(float) - BLANK) * LSB_MV
    b, a = butter(4, 5e6 / (FS / 2))
    mvl = lfilter(b, a, mvl)
    delay = 4 / (2 * np.pi * 5e6) * 0.65          # about the filter's group delay
    thr = mv.TTX_MV / 2
    good = []
    for t0 in np.arange(9.0e-6, 12.0e-6, 2e-9):
        v = ttx_slice(mvl, t0 + delay)
        bits = (v > thr).astype(int)
        by = [int(sum(bits[8 * i + j] << j for j in range(8))) for i in range(3)]
        if by == [0x55, 0x55, 0x27]:
            good.append(t0)
    if not good:
        return None
    # the framing code fits over a range of start times; take its middle
    t0 = (good[0] + good[-1]) / 2
    v = ttx_slice(mvl, t0 + delay)
    bits = (v > thr).astype(int)
    by = [int(sum(bits[8 * i + j] << j for j in range(8))) for i in range(45)]
    # eye width: start times around t0 where every bit still decodes the same
    width = 0
    for side in (-1, 1):
        dt = 0.0
        while abs(dt) < TTX_T:
            dt += side * 1e-9
            if not np.array_equal((ttx_slice(mvl, t0 + dt + delay) > thr).astype(int), bits):
                break
        width += abs(dt)
    margin = np.min(np.abs(v - thr))
    return by, t0, width, margin


def check_ttx(on, first):
    page = open(os.path.join(here, '..', '..', 'data', 'TEST25'), 'rb').read()[:1000]
    expect = mv.packets(page)
    rows = {}
    widths, margins, t0s = [], [], []
    for fi in range(on.shape[0]):
        frame = first + fi
        for field, line in enumerate((20, 333)):
            r = ttx_decode(on[fi, line - 1])
            if r is None:
                fail(f'frame {frame} line {line}: no framing code found')
            by, t0, width, margin = r
            mag_row = [hamming84(by[3]), hamming84(by[4])]
            if None in mag_row:
                fail(f'frame {frame} line {line}: Hamming error in the address')
            mag, row = mag_row[0] & 7, mag_row[0] >> 3 | mag_row[1] << 1
            n = ((frame % 25) * 2 + field) % 25
            exp = list(expect[n])
            if row == 0:
                s = frame // 25
                clk = '%02d:%02d:%02d' % (s // 3600 % 24, s // 60 % 60, s % 60)
                exp[mv.TTX_CLOCK_OFS:mv.TTX_CLOCK_OFS + 8] = [mv.par(ord(c)) for c in clk]
                if [hamming84(x) for x in by[5:13]] != [0] * 8:
                    fail('header: page / control bytes wrong')
            if by != exp:
                fail(f'frame {frame} line {line}: packet {n} bytes differ')
            if not all(odd(x) for x in by[5 if row else 13:]):
                fail(f'frame {frame} line {line}: parity error')
            if mag != 1 or row != n:
                fail(f'frame {frame} line {line}: address {mag}/{row}, expected 1/{n}')
            text = ''.join(chr(x & 0x7F) if 32 <= (x & 0x7F) < 127 else ' ' for x in by[5 if row else 13:])
            if row == 0:
                print(f'  frame {frame:6d} line {line}: header "{text.strip()}"')
            rows[row] = text
            widths.append(width)
            margins.append(margin)
            t0s.append(t0)
    print(f'  {len(widths)} packets, all 45 bytes as sent; rows seen: {len(rows)} of 25')
    print(f'  first bit at {np.mean(t0s) * 1e6:.2f} us after 0H (after the 5 MHz filter)')
    print(f'  eye width {min(widths) * 1e9:.0f} ns of {TTX_T * 1e9:.0f} ns, eye height: '
          f'{min(margins):.0f} mV from the {mv.TTX_MV / 2:.0f} mV threshold (worst bit)')
    for r in (1, 17, 20):
        if r in rows:
            print(f'  row {r:2d}: {rows[r]}')


# --- captions ----------------------------------------------------------------------------------
CC_T = 1271 / 32 / FS


def cc_decode(line_codes):
    mvl = (line_codes.astype(float) - BLANK) * LSB_MV
    thr = mv.CC_MV / 2
    t = (np.arange(mvl.size) - S0H) / FS
    above = mvl > thr
    rise = np.nonzero(~above[:-1] & above[1:])[0]
    # rising crossings with linear interpolation
    tr = [(i + (thr - mvl[i]) / (mvl[i + 1] - mvl[i]) - S0H) / FS for i in rise]
    runin = [x for x in tr if x < 25e-6]
    start = [x for x in tr if x >= 25e-6]
    if len(runin) != 7 or not start:
        return None
    ts = start[0]
    v = np.interp(ts + (np.arange(17) + 0.5) * CC_T, t, mvl)
    bits = (v > thr).astype(int)
    if bits[0] != 1:
        return None
    b1 = int(sum(bits[1 + j] << j for j in range(8)))
    b2 = int(sum(bits[9 + j] << j for j in range(8)))
    return b1, b2, runin[0], ts, np.min(np.abs(v - thr))


def check_cc(on, first):
    shown, buf, last = [], {}, None
    starts, margins = [], []
    for fi in range(on.shape[0]):
        frame = first + fi
        r = cc_decode(on[fi, 20])                   # line 21
        if r is None:
            fail(f'frame {frame}: no caption run-in / start bit')
        b1, b2, tr0, ts, margin = r
        if not (odd(b1) and odd(b2)):
            fail(f'frame {frame}: parity error')
        starts.append((tr0, ts))
        margins.append(margin)
        c1, c2 = b1 & 0x7F, b2 & 0x7F
        if c1 == 0 and c2 == 0:
            last = None
            continue
        if 0x10 <= c1 < 0x20:                      # control code (sent twice: act once)
            if (c1, c2) == last:
                last = None
                continue
            last = (c1, c2)
            if (c1, c2) == (0x14, 0x20):
                pass                                # RCL: pop-on
            elif (c1, c2) == (0x14, 0x2E):
                buf = {}                            # ENM
            elif c1 == 0x14 and 0x40 <= c2 <= 0x7F:
                buf = {'row': 14 if c2 < 0x60 else 15, 'text': ''}
            elif (c1, c2) == (0x14, 0x2F):          # EOC: show
                shown.append((frame, buf.get('row'), buf.get('text', '')))
            continue
        last = None
        buf['text'] = buf.get('text', '') + ''.join(chr(c) for c in (c1, c2) if c >= 0x20)
    tr0 = np.mean([s[0] for s in starts]) * 1e6
    ts = np.mean([s[1] for s in starts]) * 1e6
    print(f'  {len(starts)} frames: run-in 7 cycles, first rising crossing at {tr0:.2f} us, '
          f'start bit edge at {ts:.2f} us (EIA-608: 27.38 us); eye height {min(margins):.0f} mV')
    for frame, row, text in shown:
        print(f'  frame {frame:6d}: caption shown on row {row}: "{text}"')
    if not shown:
        fail('no caption completed')
    for frame, row, text in shown:
        sec = (frame * 1001) // 30000
        exp = 'NTSC TEST %02d:%02d:%02d' % (sec // 3600 % 24, sec // 60 % 60, sec % 60)
        if text != exp or row != 15:
            fail(f'caption "{text}" on row {row}, expected "{exp}" on row 15')


def main():
    std, f_on, f_off, first = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    spl, lines = (1280, 625) if std == 'pal' else (1271, 525)
    on, off = lines_of(f_on, spl, lines), lines_of(f_off, spl, lines)
    print(f'{std.upper()} data lines, frames {first}..{first + on.shape[0] - 1}:')
    if std == 'pal':
        compare_other(on, off, (20, 333))
        check_ttx(on, first)
    else:
        compare_other(on, off, (21,))
        check_cc(on, first)
    print('  OK')


if __name__ == '__main__':
    main()
