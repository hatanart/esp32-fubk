#!/usr/bin/env python3
"""Read the text back from the synth output (FuBK name box, PM5644 upper / lower box).

usage: check_text20.py fubk|pm5644-pal|pm5644-ntsc <out.u8>   (one frame of that card)

For every text line: the luma at the centre of each dot (frame rows of both fields), threshold
at half white, and each 9 x 18 cell matched against the font; the result must be the default
text. Dot rows outside the card's black box are not compared (white dots can't be seen there).
"""
import os
import sys

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import make_tables20 as mt  # noqa: E402
import patterns20 as pt  # noqa: E402
import pm8503_font as pf  # noqa: E402


def fail(msg):
    print('FAIL:', msg)
    sys.exit(1)


def main():
    card, path = sys.argv[1], sys.argv[2]
    std = mt.NTSC if card == 'pm5644-ntsc' else mt.PAL
    d = np.fromfile(path, np.uint8).astype(float)
    if d.size != std.lines * std.spl:
        fail('expected one frame')
    d = d.reshape(std.lines, std.spl)
    rows = np.zeros((std.rows, std.spl))
    for l in range(1, std.lines + 1):
        r = std.line_to_row(l)
        if r is not None:
            rows[r] = d[l - 1]
    black = 0.0 if std.name == 'PAL' else 7.5 * mt.IRE_MV
    lum = (rows - mt.BLANK) * mt.LSB_MV - black                      # mV above black
    Y = mt.fubk_picture()[0] if card == 'fubk' else pt.pm5644(std)[0]
    boxes = mt.fubk_text_boxes(Y) if card == 'fubk' else mt.pm5644_text_boxes(std, Y)
    dark = Y[0] < black + 25                                        # the card's black areas
    chars, glyphs = pf.glyphs()
    want = mt.text_default()
    for b in boxes:
        cells = b['n']
        dots = np.zeros((18, cells * 9), bool)
        x = b['x0'] + (np.arange(cells * 9) + 0.5) * mt.TEXT_DOT / mt.TEXT_DOT_STEP
        xi = np.round(x).astype(int)
        colok = np.ones(cells * 9, bool)
        if card == 'fubk':                                           # not the centre line
            colok = np.abs(x - (b['x0'] + b['nsamp'] / 2)) > 6
            xi = xi[colok]
        # dot rows inside the black box (elsewhere the card is bright: white dots can't be seen)
        valid = np.array([dark[b['row0'] + 2 * dr, xi].all() and dark[b['row0'] + 2 * dr + 1, xi].all()
                          for dr in range(18)])
        for dr in np.nonzero(valid)[0]:
            for f in range(2):                                       # both fields must agree
                r = b['row0'] + 2 * dr + f
                v = np.interp(x, np.arange(std.spl), lum[r]) > 350
                if f == 0:
                    dots[dr] = v
                elif not np.array_equal(v, dots[dr]):
                    fail(f'box at row {b["row0"]}: the two fields differ in dot row {dr}')
        got = []
        for c in range(cells):
            cols = colok[c * 9:(c + 1) * 9]                           # not on the FuBK centre line
            cell = dots[valid][:, c * 9:(c + 1) * 9][:, cols]
            m = [i for i, g in enumerate(glyphs) if np.array_equal(g[valid][:, cols], cell)]
            if not m:
                fail(f'box at row {b["row0"]}, cell {c}: no matching character')
            e = want[b['text']][c]
            got.append(e if e in m else m[0])                         # O and 0 look the same
        text = ''.join(chars[i] for i in got)
        exp = ''.join(chars[i] for i in want[b['text']][:b['n']])
        name = ('upper', 'lower', 'FuBK')[b['text']]
        print(f'  {card} {name} text: "{text}" ({valid.sum()} of 18 dot rows on black)')
        if text != exp:
            fail(f'expected "{exp}"')
        if valid.sum() < 15:
            fail(f'only {valid.sum()} dot rows on black')
    print('  OK')


if __name__ == '__main__':
    main()
