"""Load the PT8631 FuBK 4:3 (anti-PAL) pattern from data/fubk4x3.zip as Y, B-Y, R-Y in mV.

Format: CHANNEL1/2/3.DAT = Y, B-Y, R-Y, 16-bit little endian, 1024 samples per row, rows in
frame order. Y mV = (3072 - code) / (0.3125 * 10.25) (stored inverted),
B-Y mV = -2.5424 * (code - 512), R-Y mV = -1.4265 * (code - 512) (fitted on the 75 % bars).
Arrays come back as (frames, 576, 1024):
2 frames, 576 rows in frame order (row 0 = line 23, row 1 = line 336, row 2 = line 24, ...),
1024 samples per row at 13.5 MHz.

Data source: ProTeleVision PT8631 pattern data, PT5230/PT8631/TPD/BILLEDDATA/FUBK4X3, from
https://github.com/KarstenHervoeHansen/PTV (third-party data, not covered by this project's CC0).
"""
import os
import zipfile
import numpy as np

SRC = 'FUBK4X3/ANTIPAL/'            # anti-PAL set, 2 frames
SRC_PLAIN = 'FUBK4X3/'              # plain set, 1 frame (used to remove the date boxes)
ZIP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'fubk4x3.zip')
ROWS, COLS = 576, 1024
FS_SRC = 13.5e6
COL_0H = 10.0      # source column at 0H (the BT.601 720 window starts at col 142 = 0H + 132 samples)
PIC_COLS = (151.5, 853.5)   # 50% picture edges (10.5 us .. 62.5 us after 0H)

# Known areas (frame rows / source columns), used by the checks
ANTIPAL_V = (slice(407, 486), slice(576, 649))   # V alternates line by line
ANTIPAL_U = (slice(407, 486), slice(649, 722))   # U alternates line by line
GRATING_ROWS = slice(328, 367)
ORANGE = (slice(327, 367), slice(592, 703))
GRATINGS = [(338, 394, 1e6), (410, 470, 2e6), (500, 572, 3e6)]   # row 328..366, cols, Hz


def _read(z, d, n):
    return np.frombuffer(z.read(f'{d}CHANNEL{n}.DAT'), '<u2').reshape(-1, COLS).astype(float)


def load(zip_path=ZIP, remove_dates=True):
    with zipfile.ZipFile(zip_path) as z:
        Y, A, B = (_read(z, SRC, n) for n in (1, 2, 3))
        if remove_dates:
            # copy the plain set into the two date boxes (rows 488..525) where the luma differs
            Yp, Ap, Bp = (_read(z, SRC_PLAIN, n) for n in (1, 2, 3))
            for fr in range(Y.shape[0] // ROWS):
                sl = slice(fr * ROWS, (fr + 1) * ROWS)
                m = Y[sl] != Yp
                m[:488] = False
                m[526:] = False
                Y[sl][m] = Yp[m]
                A[sl][m] = Ap[m]
                B[sl][m] = Bp[m]
    y = (3072 - Y) / (0.3125 * 10.25)
    by = -2.5424 * (A - 512)
    ry = -1.4265 * (B - 512)
    shape = (-1, ROWS, COLS)
    return y.reshape(shape), by.reshape(shape), ry.reshape(shape)


def line_to_row(line):
    """Frame line number (1..625) -> data row, or None for lines without picture."""
    if 23 <= line <= 310:
        return 2 * (line - 23)
    if 336 <= line <= 623:
        return 2 * (line - 336) + 1
    return None


def to_rgb(y, by, ry):
    """Gamma-corrected Y, B-Y, R-Y in mV -> 8-bit RGB (700 mV = 255)."""
    r = y + ry
    b = y + by
    g = y - 0.50935 * ry - 0.19421 * by
    return np.clip(np.round(np.stack([r, g, b], -1) * 255 / 700), 0, 255).astype(np.uint8)
