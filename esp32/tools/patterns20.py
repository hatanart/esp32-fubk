"""Extra test patterns for the 20 MHz build (the GPIO15 pattern button). Pattern 0 is always the
real test card (FuBK in PAL, SMPTE bars in NTSC, made in make_tables20.py); these follow it.

Every pattern returns Y, U, V in mV on the standard's grid, shape (1, rows, spl), like
make_tables20.fubk_picture(). Geometry is in "units" of one picture row: the picture is
H = rows high and W = 4/3 H wide, so equal units are square on a 4:3 screen.
  PAL : picture 10.5 .. 62.5 us after 0H, 576 rows, black 0 mV, white 700 mV
  NTSC: picture 9.4 .. 62.05 us after 0H, 480 rows, black 7.5 IRE, white 100 IRE
Edges: rectangles get exact area coverage per sample and row, circles 4 x 4 supersampling. Then
luma is low-passed for a 150 ns rise, chroma for 400 ns (as the SMPTE bars), and everything is
cut by the soft-edged picture window.
"""
import numpy as np
from scipy.ndimage import gaussian_filter1d

import make_tables20 as mt

LUMA_SIGMA = 150e-9 / 2.563 * mt.FS          # samples (10-90 % rise 150 ns)
CHROMA_SIGMA = 400e-9 / 2.563 * mt.FS


class Canvas:
    def __init__(self, std):
        self.std = std
        self.t0, self.t1 = (10.5e-6, 62.5e-6) if std.name == 'PAL' else mt.SMPTE_EDGES
        self.H = std.rows
        self.W = self.H * 4 / 3
        self.cell = self.H / 12                      # 16 x 12 square cells
        t = mt.t_line(std)
        self.t = t
        self.dx = self.W / ((self.t1 - self.t0) * mt.FS)     # units per sample
        self.x = (t - self.t0) / (self.t1 - self.t0) * self.W   # sample centres
        self.y = np.arange(self.H) + 0.5
        if std.name == 'PAL':
            self.black, self.white = 0.0, 700.0
        else:
            self.black, self.white = 7.5 * mt.IRE_MV, 100 * mt.IRE_MV
        self.span = self.white - self.black
        self.rgb = np.zeros((3, self.H, std.spl))   # R'G'B' (0..1 = black..white)
        self.extra_y = np.zeros((self.H, std.spl))  # mV added after filtering (multiburst)
        self.units_per_us = self.W / ((self.t1 - self.t0) * 1e6)

    # coverage helpers ---------------------------------------------------------------------
    def _cov1(self, c, w, a, b):
        """Overlap of cells [c - w/2, c + w/2] with [a, b], as a fraction of w."""
        return np.clip((np.minimum(c + w / 2, b) - np.maximum(c - w / 2, a)) / w, 0, 1)

    def rect(self, x0, x1, y0, y1):
        return np.outer(self._cov1(self.y, 1.0, y0, y1), self._cov1(self.x, self.dx, x0, x1))

    def ring(self, cx, cy, r, thick, ss=4):
        """Coverage of a ring (centre cx, cy, radius r, radial thickness thick)."""
        cov = np.zeros((self.H, self.std.spl))
        r0, r1 = r - thick / 2, r + thick / 2
        rows = np.arange(max(0, int(cy - r1) - 1), min(self.H, int(cy + r1) + 2))
        cols = np.nonzero((self.x > cx - r1 - 2 * self.dx) & (self.x < cx + r1 + 2 * self.dx))[0]
        sub = (np.arange(ss) + 0.5) / ss - 0.5
        xs = (self.x[cols][:, None] + sub[None, :] * self.dx).ravel()
        for r_ in rows:
            ys = self.y[r_] + sub
            d = np.sqrt((xs[None, :] - cx) ** 2 + (ys[:, None] - cy) ** 2)
            inside = ((d >= r0) & (d <= r1)).reshape(ss, len(cols), ss)
            cov[r_, cols] = inside.mean(axis=(0, 2))
        return cov

    def paint(self, cov, rgb):
        rgb = np.asarray(rgb, float).reshape(3, 1, 1)
        self.rgb = self.rgb * (1 - cov) + rgb * cov

    def fill(self, rgb):
        self.paint(np.ones((self.H, self.std.spl)), rgb)

    def hline(self, y, rgb, x0=None, x1=None):
        """Horizontal line 2 rows thick (one line in each field), centred on y."""
        self.paint(self.rect(0 if x0 is None else x0, self.W if x1 is None else x1, y - 1, y + 1), rgb)

    def vline(self, x, rgb, y0=None, y1=None, width_us=0.25):
        w = width_us * self.units_per_us
        self.paint(self.rect(x - w / 2, x + w / 2, 0 if y0 is None else y0, self.H if y1 is None else y1), rgb)

    def frame(self, x0, x1, y0, y1, rgb):
        """Rectangle outline: edges inside [x0, x1] x [y0, y1]."""
        w = 0.25 * self.units_per_us
        self.paint(self.rect(x0, x1, y0, y0 + 2), rgb)
        self.paint(self.rect(x0, x1, y1 - 2, y1), rgb)
        self.paint(self.rect(x0, x0 + w, y0, y1), rgb)
        self.paint(self.rect(x1 - w, x1, y0, y1), rgb)

    # output -------------------------------------------------------------------------------
    def yuv(self):
        r, g, b = self.rgb
        y = 0.299 * r + 0.587 * g + 0.114 * b
        u = 0.493 * (b - y)
        v = 0.877 * (r - y)
        area = mt.pe._step(self.t, self.t0, 150e-9) - mt.pe._step(self.t, self.t1, 150e-9)
        Y = gaussian_filter1d(self.black + self.span * y, LUMA_SIGMA, axis=-1, mode='nearest')
        Y = (Y + self.extra_y) * area
        U = gaussian_filter1d(self.span * u, CHROMA_SIGMA, axis=-1, mode='nearest') * area
        V = gaussian_filter1d(self.span * v, CHROMA_SIGMA, axis=-1, mode='nearest') * area
        # a picture line that holds a 2nd sync pulse (PAL line 623: equalising pulse at half
        # line) is blanked from 1 us before that pulse, as in a real signal
        h2 = self.std.spl / 2 / mt.FS if self.std.name == 'NTSC' else mt.pe.H2
        half = mt.pe._step(self.t, h2 - 1e-6, 150e-9)
        for l in range(1, self.std.lines + 1):
            r = self.std.line_to_row(l)
            if r is not None and self.std.line_kind(l)[1] != '-':
                for a in (Y, U, V):
                    a[r] *= 1 - half
        return tuple(a[None] for a in (Y, U, V))


WHITE, BLACK = (1, 1, 1), (0, 0, 0)


def grey(p):
    return (p, p, p)


def _grid(c, rgb):
    for k in range(13):
        c.hline(k * c.cell, rgb)
    for k in range(17):
        c.vline(k * c.cell, rgb)


def crosshatch(std):
    """16 x 12 square cells, white on black; the middle lines are the centre cross."""
    c = Canvas(std)
    _grid(c, WHITE)
    return c.yuv()


def dots(std):
    """A white dot at every crosshatch crossing (static convergence)."""
    c = Canvas(std)
    w = 0.25 * c.units_per_us
    for j in range(13):
        for k in range(17):
            x, y = k * c.cell, j * c.cell
            c.paint(c.rect(x - w / 2, x + w / 2, y - 1, y + 1), WHITE)
    return c.yuv()


def circles(std):
    """Grey crosshatch with a large circle and four corner circles in white (linearity)."""
    c = Canvas(std)
    _grid(c, grey(0.6))
    th = 2.5
    c.paint(c.ring(c.W / 2, c.H / 2, 5.5 * c.cell, th), WHITE)
    for cx in (2 * c.cell, c.W - 2 * c.cell):
        for cy in (2 * c.cell, c.H - 2 * c.cell):
            c.paint(c.ring(cx, cy, 1.5 * c.cell, th), WHITE)
    return c.yuv()


def colour_bars(std):
    """Full-field 100/0/75/0 bars: white 100 %, colours 75 %, black."""
    c = Canvas(std)
    bars = [WHITE, (.75, .75, 0), (0, .75, .75), (0, .75, 0), (.75, 0, .75), (.75, 0, 0), (0, 0, .75), BLACK]
    for i, rgb in enumerate(bars):
        c.paint(c.rect(i * c.W / 8, (i + 1) * c.W / 8, 0, c.H), rgb)
    return c.yuv()


def greyscale_pluge(std):
    """Top two thirds: 11 grey steps 0..100 %. Bottom: PLUGE on black (PAL -2 % / +2 %,
    NTSC 3.5 / 11.5 IRE) and a white patch."""
    c = Canvas(std)
    h = round(c.H * 2 / 3 / 2) * 2
    for i in range(11):
        c.paint(c.rect(i * c.W / 11, (i + 1) * c.W / 11, 0, h), grey(i / 10))
    d = 0.02 if std.name == 'PAL' else 4 / 92.5
    for i, rgb in enumerate([BLACK, grey(-d), BLACK, grey(d), BLACK, WHITE, BLACK]):
        c.paint(c.rect(i * c.W / 7, (i + 1) * c.W / 7, h, c.H), rgb)
    return c.yuv()


def field(rgb):
    def f(std):
        c = Canvas(std)
        c.fill(rgb)
        return c.yuv()
    return f


def multiburst(std):
    """White flag, then six luma bursts (60 % p-p on a 50 % pedestal).
    PAL 0.5, 1, 2, 4, 4.8, 5.8 MHz (CCIR); NTSC 0.5, 1.25, 2, 3, 3.58, 4.1 MHz."""
    c = Canvas(std)
    freqs = [0.5e6, 1e6, 2e6, 4e6, 4.8e6, 5.8e6] if std.name == 'PAL' else [0.5e6, 1.25e6, 2e6, 3e6, 3.58e6, 4.1e6]
    c.paint(c.rect(0, 0.10 * c.W, 0, c.H), WHITE)
    c.paint(c.rect(0.16 * c.W, c.W, 0, c.H), grey(0.5))
    slot = (1.0 - 0.18) * c.W / 6
    env_edge = 300e-9
    for i, f in enumerate(freqs):
        xa = 0.18 * c.W + i * slot + 0.1 * slot
        xb = xa + 0.8 * slot
        ta = c.t0 + xa / c.W * (c.t1 - c.t0)
        tb = c.t0 + xb / c.W * (c.t1 - c.t0)
        env = mt.pe._step(c.t, ta, env_edge) - mt.pe._step(c.t, tb, env_edge)
        c.extra_y += (0.3 * c.span * env * np.sin(2 * np.pi * f * (c.t - ta)))[None, :]
    return c.yuv()


def overscan(std):
    """White outline at the picture edge, 5 % (action safe, 75 % grey) and 10 % (title safe,
    50 % grey) outlines, and a centre cross."""
    c = Canvas(std)
    W, H = c.W, c.H
    c.frame(0, W, 0, H, WHITE)
    c.frame(0.05 * W, 0.95 * W, round(0.05 * H / 2) * 2, H - round(0.05 * H / 2) * 2, grey(0.75))
    c.frame(0.10 * W, 0.90 * W, round(0.10 * H / 2) * 2, H - round(0.10 * H / 2) * 2, grey(0.5))
    c.hline(H / 2, WHITE, W / 2 - c.cell, W / 2 + c.cell)
    c.vline(W / 2, WHITE, H / 2 - c.cell, H / 2 + c.cell)
    return c.yuv()


def window(std):
    """White window, half the width and height, on black."""
    c = Canvas(std)
    c.paint(c.rect(c.W / 4, 3 * c.W / 4, round(c.H / 8) * 2, round(3 * c.H / 8) * 2), WHITE)
    return c.yuv()


# The order is fixed: add new patterns at the end, never remove or reorder.
PATTERNS = [
    ('crosshatch', crosshatch),
    ('dots', dots),
    ('circles', circles),
    ('colour bars 100/0/75/0', colour_bars),
    ('greyscale + PLUGE', greyscale_pluge),
    ('white', field(WHITE)),
    ('red', field((1, 0, 0))),
    ('green', field((0, 1, 0))),
    ('blue', field((0, 0, 1))),
    ('black', field(BLACK)),
    ('multiburst', multiburst),
    ('overscan / safe areas', overscan),
    ('window', window),
]


def to_rgb8(std, Y, U, V):
    """Preview: mV -> 8-bit RGB (black..white), 4:3 aspect."""
    c = Canvas(std)
    y, u, v = Y[0] - c.black, U[0], V[0]
    r = y + v / 0.877
    b = y + u / 0.493
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    img = np.clip(np.stack([r, g, b], -1) / c.span * 255, 0, 255)
    a0, a1 = (int(round((t + mt.T0) * mt.FS)) for t in (c.t0, c.t1))
    return img[:, a0:a1].astype(np.uint8), round(c.W)
