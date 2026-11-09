#!/usr/bin/env python3
"""Philips PM5644 colour circle pattern from its EPROM dumps (PAL G00, NTSC M00).

The dumps come from https://github.com/inaxeon/PTV_Preservation (PM5644/PM5644G00 and
PM5644M00, ROM_Dumps). They are not part of this repository: download() fetches the files
needed, at a fixed commit, into data/ptv_pm5644/ and checks the checksum in each file name.
The tables generated from them are checked in, so building the firmware does not need them.

How the data is stored (both units):
  - 4 luma EPROMs interleaved byte by byte = 512 luma samples per ROM row (13.5 MHz), and 2 + 2
    chroma EPROMs (U, V) interleaved = 256 samples per row each (6.75 MHz). 128 bytes per row
    per EPROM.
  - The video is stored inverted: PAL black 181, white 41; NTSC black 170, white 38. U and V
    are 0.493 (B - Y) and 0.877 (R - Y) on the same scale around 128 (NTSC V has the opposite
    sign).
  - A line is three parts: a left band from an "edge" row, the centre (the circle) from a
    "centre" row, and a right band from the same edge row. The control EPROM holds, per field,
    one record (ctrl, edge, v) per active line: centre row = 2 v + (ctrl bit 0), edge row =
    2 edge + (ctrl bit 1).
  - On the 720-sample BT.601 line (sample 0 = 132 (PAL) / 122 (NTSC) samples after 0H at
    13.5 MHz): blank, edge cols, centre cols, edge cols, blank. PAL: 10 + 110 (edge 146..255)
    + 480 (centre 0..479) + 112 (edge 256..367) + 8; NTSC: 4 + 120 (136..255) + 512 + 80
    (256..335) + 4.
"""
import glob
import hashlib
import os
import re
import urllib.request

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(here, '..', '..', 'data', 'ptv_pm5644')
REPO = 'https://raw.githubusercontent.com/inaxeon/PTV_Preservation'
COMMIT = '34f3dd33536b857185aa8939f69e02655c9c875d'

UNITS = {
    'PAL': dict(dir='PM5644G00', rows=512, nrec=288, black=181, white=41, vsign=1,
                y=('56191_CSUM_9A6A', '56201_CSUM_4E67', '56211_CSUM_3172', '56221_CSUM_7F95'),
                u=('56261_CSUM_2DA9', '56271_CSUM_2E0F'), v=('56241_CSUM_F0DF', '56251_CSUM_F397'),
                ctrl='59371_CSUM_A100', tables=(0x718C, 0x6E26),
                t0=132, pad_l=10, edge_l=(146, 256), centre=480, edge_r=(256, 368), pad_r=8),
    'NTSC': dict(dir='PM5644M00', rows=410, nrec=243, black=170, white=38, vsign=-1,
                 y=('56741_CSUM_0D4A', '56751_CSUM_F7B6', '56761_CSUM_03DF', '56771_CSUM_1A71'),
                 u=('56811_CSUM_B3AC', '56821_CSUM_B3EC'), v=('56791_CSUM_C1EA', '56801_CSUM_C1D0'),
                 ctrl='59401_CSUM_7300', tables=(0x5E90, 0x616F),
                 t0=122, pad_l=4, edge_l=(136, 256), centre=512, edge_r=(256, 336), pad_r=4),
}
FS_SRC = 13.5e6


def _name(part):
    return f'EPROM_4008_102_{part}.bin'


def _check(path):
    want = int(re.search(r'CSUM_([0-9A-F]{4})', path).group(1), 16)
    return sum(open(path, 'rb').read()) & 0xFFFF == want


def download():
    """Fetch the EPROM dumps that are missing (or damaged) into data/ptv_pm5644/."""
    for u in UNITS.values():
        d = os.path.join(DATA, u['dir'])
        os.makedirs(d, exist_ok=True)
        for part in u['y'] + u['u'] + u['v'] + (u['ctrl'],):
            path = os.path.join(d, _name(part))
            if os.path.exists(path) and _check(path):
                continue
            url = f'{REPO}/{COMMIT}/PM5644/{u["dir"]}/ROM_Dumps/{_name(part)}'
            print('  downloading', url)
            urllib.request.urlretrieve(url, path)
            if not _check(path):
                raise SystemExit(f'{path}: checksum does not match its name')


def _rom(u, part, n):
    path = os.path.join(DATA, u['dir'], _name(part))
    if not os.path.exists(path):
        raise SystemExit(f'{path} missing: run tools/pm5644.py once to download the PM5644 dumps')
    return np.frombuffer(open(path, 'rb').read()[:n], np.uint8)


def fields(std_name):
    """The two fields as BT.601 lines: (Y, U, V), each (2, nrec, 720) in units of the white
    level (Y 0 = black .. 1 = white; U = 0.493 (B - Y), V = 0.877 (R - Y)). Chroma repeated per
    luma sample pair."""
    u = UNITS[std_name]
    n = u['rows'] * 128
    Y = np.stack([_rom(u, p, n) for p in u['y']], -1).reshape(u['rows'], 512).astype(float)
    U = np.stack([_rom(u, p, n) for p in u['u']], -1).reshape(u['rows'], 256).astype(float)
    V = np.stack([_rom(u, p, n) for p in u['v']], -1).reshape(u['rows'], 256).astype(float)
    ctrl = _rom(u, u['ctrl'], 65536)
    sc = u['black'] - u['white']
    Y = (u['black'] - Y) / sc
    U = np.repeat((128 - U) / sc, 2, axis=1)                 # to luma sample positions
    V = np.repeat(u['vsign'] * (128 - V) / sc, 2, axis=1)
    out = np.zeros((3, 2, u['nrec'], 720))
    for f, base in enumerate(u['tables']):
        for i in range(u['nrec']):
            c, e, v = (int(x) for x in ctrl[base + 6 + 3 * i:base + 9 + 3 * i])
            cr, er = 2 * v + (c & 1), 2 * e + ((c >> 1) & 1)
            for k, a in enumerate((Y, U, V)):
                blank = 0.0
                line = np.concatenate([np.full(u['pad_l'], blank), a[er, u['edge_l'][0]:u['edge_l'][1]],
                                       a[cr, :u['centre']], a[er, u['edge_r'][0]:u['edge_r'][1]],
                                       np.full(u['pad_r'], blank)])
                assert line.size == 720
                out[k, f, i] = line
    return out


if __name__ == '__main__':
    download()
    for s in UNITS:
        y, _, _ = fields(s)
        print(f'{s}: 2 fields x {y.shape[1]} lines, luma {y.min():.2f} .. {y.max():.2f} (0 = black, 1 = white)')
