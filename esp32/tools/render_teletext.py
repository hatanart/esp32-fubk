#!/usr/bin/env python3
"""Render the teletext page the firmware sends (P100: data/TEST25 with our header) as a picture,
the way a basic (level 1) teletext decoder shows it: docs/teletext-p100.png.

Level 1: 8 colours for text and block graphics (contiguous / separated), double height, new /
black background, hold graphics. "Conceal" text is shown as after pressing Reveal and "flash" as
steady, so everything on the page is visible. Later decoders (level 2.5) draw some of the
page's codes differently (see the README).

The font is Bedstead by Ben Harris (https://bjh21.me.uk/bedstead/, CC0), drawn after the
SAA5050 teletext character generator; its 12 x 20 bitmap version (bedstead-20.bdf) is downloaded
to data/ the first time.
usage: render_teletext.py [out.png]
"""
import os
import sys
import urllib.request

import numpy as np
from PIL import Image

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here)
import make_vbi20 as mv  # noqa: E402

FONT_URL = 'https://bjh21.me.uk/bedstead/bedstead-20.bdf'
FONT = os.path.join(here, '..', '..', 'data', 'bedstead-20.bdf')
PAGE = os.path.join(here, '..', '..', 'data', 'TEST25')
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, '..', '..', 'docs', 'teletext-p100.png')
CW, CH = 12, 20                                  # character cell (pixels)
COLOURS = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (255, 255, 0), (0, 0, 255), (255, 0, 255),
           (0, 255, 255), (255, 255, 255)]
# English G0 set: the codes that differ from ASCII (0x7F is a solid block)
ENGLISH = {0x23: [0xA3], 0x5B: [0x2190], 0x5C: [0xBD], 0x5D: [0x2192], 0x5E: [0x2191], 0x5F: [0x23],
           0x60: [0x2015, 0x2014, 0x2500], 0x7B: [0xBC], 0x7C: [0x2016], 0x7D: [0xBE], 0x7E: [0xF7],
           0x7F: [0x25A0]}


def load_bdf(path):
    if not os.path.exists(path):
        print('  downloading', FONT_URL)
        urllib.request.urlretrieve(FONT_URL, path)
    glyphs, enc, rows, inbm = {}, None, [], False
    for line in open(path, encoding='latin-1'):
        w = line.split()
        if not w:
            continue
        if w[0] == 'ENCODING':
            enc = int(w[1])
        elif w[0] == 'BITMAP':
            rows, inbm = [], True
        elif w[0] == 'ENDCHAR':
            inbm = False
            bits = np.array([[int(r, 16) >> (15 - x) & 1 for x in range(CW)] for r in rows], bool)
            if bits.shape == (CH, CW):
                glyphs[enc] = bits
        elif inbm:
            rows.append(w[0])
    return glyphs


def glyph(font, c):
    for u in ENGLISH.get(c, [c]):
        if u in font:
            return font[u]
    return np.zeros((CH, CW), bool)


def mosaic(c, separated):
    """Sextant block graphic: bits 0, 1 top, 2, 3 middle, 4, 6 bottom (left, right)."""
    m = np.zeros((CH, CW), bool)
    rows = [(0, 6), (6, 14), (14, 20)]
    for i, b in enumerate((0, 1, 2, 3, 4, 6)):
        if c >> b & 1:
            y0, y1 = rows[i // 2]
            x0, x1 = (0, 6) if i % 2 == 0 else (6, 12)
            if separated:
                x0, y1 = x0 + 2, y1 - 2
            m[y0:y1, x0:x1] = True
    return m


def decode_row(codes, font):
    """One row -> per cell (pixels 20 x 12 bool, fg, bg, double height)."""
    fg, bg, gfx, sep, hold, dh = 7, 0, False, False, False, False
    held = (0x20, False)
    cells = []
    for c in codes:
        c &= 0x7F
        # set-at codes act on this cell
        if c == 0x0C:
            dh, held = False, (0x20, False)
        elif c == 0x19:
            sep = False
        elif c == 0x1A:
            sep = True
        elif c == 0x1C:
            bg = 0
        elif c == 0x1D:
            bg = fg
        elif c == 0x1E:
            hold = True
        if c < 0x20:
            pix = mosaic(held[0], held[1]) if gfx and hold else np.zeros((CH, CW), bool)
        elif gfx and (c & 0x20):                     # block graphics (0x40-0x5F stay letters)
            pix = mosaic(c, sep)
            held = (c, sep)
        else:
            pix = glyph(font, c)
        cells.append((pix, fg, bg, dh))
        # set-after codes act from the next cell on
        if 0x01 <= c <= 0x07:
            fg, gfx, held = c, False, (0x20, False)
        elif 0x11 <= c <= 0x17:
            fg, gfx = c - 0x10, True
        elif c == 0x0D:
            dh, held = True, (0x20, False)
        elif c == 0x1F:
            hold = False
    return cells


def main():
    font = load_bdf(FONT)
    page = open(PAGE, 'rb').read()[:1000]
    rows = [[0x20] * 40 for _ in range(25)]
    hdr = mv.header_text().replace(b'00:00:00', b'00:00:42')
    rows[0] = list(b'  100   ') + list(hdr)                  # columns 0-7: the TV's page number
    for r in range(1, 25):
        rows[r] = list(page[r * 40:(r + 1) * 40])
    img = np.zeros((25 * CH, 40 * CW, 3), np.uint8)
    r = 0
    while r < 25:
        cells = decode_row(rows[r], font)
        has_dh = any(dh for _, _, _, dh in cells)
        for x, (pix, fg, bg, dh) in enumerate(cells):
            if dh and r < 24:
                big = np.repeat(pix, 2, axis=0)              # each line twice, over 2 rows
                tile = np.where(big[..., None], COLOURS[fg], COLOURS[bg])
                img[r * CH:(r + 2) * CH, x * CW:(x + 1) * CW] = tile
            else:
                tile = np.where(pix[..., None], COLOURS[fg], COLOURS[bg])
                img[r * CH:(r + 1) * CH, x * CW:(x + 1) * CW] = tile
                if has_dh and r < 24:                         # the row below: background only
                    img[(r + 1) * CH:(r + 2) * CH, x * CW:(x + 1) * CW] = COLOURS[bg]
        r += 2 if has_dh else 1
    # 2 x the cell grid, then to a 4:3-like shape as on a TV (40 x 12 wide, 25 x 20 high)
    im = Image.fromarray(img).resize((40 * CW * 2, 25 * CH * 2), Image.NEAREST)
    im = im.resize((1200, 1000), Image.LANCZOS)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    im.save(OUT, optimize=True)
    print(f'wrote {OUT} ({im.size[0]} x {im.size[1]})')


if __name__ == '__main__':
    main()
