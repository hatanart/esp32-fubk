#!/usr/bin/env python3
"""The PM8503 character set (Philips text generator, as used with the PM5644): 63 characters of
9 x 18 dots, plus space.

Source: data/pm8503_charset.png, "Aliased character set of the PM8503" by Inaxeon on Wikimedia
Commons (CC0), rendered from the PROMs 4008-102-51330 and 4008-102-51340 and scaled up 4 x. The
image is 12 x 6 cells of 9 x 18 dots; the empty cell is used as space, the acute accent as the
apostrophe.

glyphs() returns the characters in edit order (space, A-Z, 0-9, punctuation, accented letters)
and their dots, [n, 18, 9] (True = white).
"""
import os

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
IMAGE = os.path.join(here, '..', '..', 'data', 'pm8503_charset.png')

# the image's cells, row by row ('' = empty)
GRID = ['ØABCDEFGHIJK', 'LMNOPQRSTUVW', 'XYZÄÖÜÊÎ ÀÂÆ', "Á%&'()Ï+,-.Ô", '0123456789:É', 'ÛÈßÑ']
ORDER = (' ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789' + "'%&()+,-.:" + 'ÄÖÜØÆÀÁÂÉÈÊÎÏÔÛÑß')
W, H = 9, 18


def glyphs():
    from PIL import Image
    a = np.array(Image.open(IMAGE).convert('RGBA'))
    on = (a[..., 3] > 128) & (a[..., :3].mean(-1) > 128)
    b = on.reshape(on.shape[0] // 4, 4, on.shape[1] // 4, 4)
    assert (b.all(axis=(1, 3)) == b.any(axis=(1, 3))).all(), 'not a clean 4 x upscale'
    dots = b.any(axis=(1, 3))
    cells = dots.reshape(6, H, 12, W).transpose(0, 2, 1, 3)
    cell = {}
    for r, row in enumerate(GRID):
        for c, ch in enumerate(row):
            cell[ch] = cells[r, c]
    assert not cell[' '].any(), 'the space cell is not empty'
    assert sorted(ORDER) == sorted(cell), 'ORDER and GRID differ'
    return list(ORDER), np.array([cell[ch] for ch in ORDER])


def encode(text):
    """Text (upper case; ß stays ß) -> glyph indices; characters not in the set become spaces."""
    up = ''.join(ch if ch == 'ß' else ch.upper() for ch in text)
    return [ORDER.index(ch) if ch in ORDER else 0 for ch in up]


if __name__ == '__main__':
    chars, g = glyphs()
    print(f'{len(chars)} characters ({len(chars) - 1} + space), {W} x {H} dots:', ''.join(chars))
    for ch in 'EP3':
        print('\n'.join(''.join('#' if v else '.' for v in line) for line in g[chars.index(ch)]))
