"""Contact sheet of all patterns for the README: docs/patterns.png. First the four test cards
(PAL 0 FuBK, PAL 1 PM5644, NTSC 0 PM5644, NTSC 1 SMPTE bars), then 2..16 (PAL; in NTSC they look
the same and differ in levels and multiburst frequencies). Reads out/preview_*_patNN.png, which
make_tables20.py writes: ideal pictures from the pattern data, not a decoded TV picture.
usage: preview_sheet.py [out.png]
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

import patterns20 as pt

here = os.path.dirname(os.path.abspath(__file__))
outdir = os.path.join(here, '..', 'out')
dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, '..', '..', 'docs', 'patterns.png')

COLS, TW, TH, GAP, LAB, HEAD = 5, 200, 150, 10, 22, 40
BG, FG, DIM = (32, 32, 32), (235, 235, 235), (160, 160, 160)


def font(size, bold=False):
    for f in ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',):
        for d in ('/usr/share/fonts/truetype/dejavu', '/usr/share/fonts/TTF', '/Library/Fonts'):
            p = os.path.join(d, f)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def label(std, k):
    e = pt.ORDER[std][k]
    if e is None:
        return 'FuBK' if std == 'PAL' else 'SMPTE bars'
    return e[0]


tiles = [('pal', 0, 'PAL 0  ' + label('PAL', 0)), ('pal', 1, 'PAL 1  ' + label('PAL', 1)),
         ('ntsc', 0, 'NTSC 0  ' + label('NTSC', 0)), ('ntsc', 1, 'NTSC 1  ' + label('NTSC', 1))]
tiles += [('pal', k, f'{k}  {label("PAL", k)}') for k in range(2, len(pt.ORDER['PAL']))]
n = len(tiles)
rows = (n + COLS - 1) // COLS
W = COLS * (TW + GAP) + GAP
H = HEAD + rows * (TH + LAB + GAP) + GAP
sheet = Image.new('RGB', (W, H), BG)
d = ImageDraw.Draw(sheet)
f_head, f_lab = font(22, True), font(14)
d.text((GAP, GAP + 6), 'All patterns (pattern button)', fill=FG, font=f_head)
d.text((W - GAP, GAP + 12), '2-16: the same in PAL and NTSC', fill=DIM, font=f_lab, anchor='ra')
for j, (tag, k, name) in enumerate(tiles):
    im = Image.open(os.path.join(outdir, f'preview_{tag}_pat{k:02d}.png')).convert('RGB')
    im = im.resize((TW, TH), Image.LANCZOS)
    r, c = divmod(j, COLS)
    x, y = GAP + c * (TW + GAP), GAP + HEAD + r * (TH + LAB + GAP)
    sheet.paste(im, (x, y))
    d.rectangle((x - 1, y - 1, x + TW, y + TH), outline=(90, 90, 90))
    d.text((x, y + TH + 4), name, fill=FG, font=f_lab)
os.makedirs(os.path.dirname(dst), exist_ok=True)
sheet.save(dst, optimize=True)
print(f'wrote {dst} ({sheet.size[0]} x {sheet.size[1]}, {os.path.getsize(dst) // 1024} KB)')
