"""Contact sheet of the extra patterns (1..13) for the README: docs/patterns.png. PAL and NTSC
look the same (they differ in levels and multiburst frequencies), so it shows the PAL ones.
Reads out/preview_pal_patNN.png, which make_tables20.py writes. They are the ideal
pictures from the pattern data (not a decoded TV picture).
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


n = len(pt.PATTERNS)
rows = (n + COLS - 1) // COLS
panel_h = HEAD + rows * (TH + LAB + GAP)
W = COLS * (TW + GAP) + GAP
sheet = Image.new('RGB', (W, panel_h + GAP), BG)
d = ImageDraw.Draw(sheet)
f_head, f_lab = font(22, True), font(14)
for pi, (tag, title, card) in enumerate((('pal', 'Pattern button: PAL and NTSC', ''),)):
    y0 = pi * panel_h + GAP
    d.text((GAP, y0 + 6), title, fill=FG, font=f_head)
    d.text((W - GAP, y0 + 12), 'after pattern 0 (FuBK / SMPTE bars)', fill=DIM, font=f_lab, anchor='ra')
    for k, (name, _) in enumerate(pt.PATTERNS, 1):
        im = Image.open(os.path.join(outdir, f'preview_{tag}_pat{k:02d}.png')).convert('RGB')
        im = im.resize((TW, TH), Image.LANCZOS)
        r, c = divmod(k - 1, COLS)
        x, y = GAP + c * (TW + GAP), y0 + HEAD + r * (TH + LAB + GAP)
        sheet.paste(im, (x, y))
        d.rectangle((x - 1, y - 1, x + TW, y + TH), outline=(90, 90, 90))
        d.text((x, y + TH + 4), f'{k}  {name}', fill=FG, font=f_lab)
os.makedirs(os.path.dirname(dst), exist_ok=True)
sheet.save(dst, optimize=True)
print(f'wrote {dst} ({sheet.size[0]} x {sheet.size[1]}, {os.path.getsize(dst) // 1024} KB)')
