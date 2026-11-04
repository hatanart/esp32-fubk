"""FuBK YUV -> PAL composite at 4 x fsc, 8 bit, exactly as the ESP32 should produce it.

- 1135 samples per line (= 283.75 subcarrier cycles), 625 lines, interlaced.
- Subcarrier phase comes from a free-running global sample counter (index mod 4), so it is
  continuous across lines and frames. Per line the start index moves by 1135 mod 4 = 3 samples.
- At 4 x fsc the carrier samples are only 0 and +-1, so chroma is just +-U or +-V per sample:
      C[N] = U * sin(pi/2 N) + s * V * cos(pi/2 N)       s = PAL V-switch, flips every line
- Line buffer sample 0 sits 0.8 us before 0H (in the front porch), so every edge of a line
  lies inside its own buffer.
"""
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.special import erf

import fubk_data as fd

FSC = 4433618.75
FS = 4 * FSC
SPL = 1135
LINES = 625
T0 = 0.8e-6                    # buffer start relative to 0H
SIN4 = np.array([0.0, 1.0, 0.0, -1.0])
COS4 = np.array([1.0, 0.0, -1.0, 0.0])

# Levels in mV relative to blanking, and the DAC mapping
SYNC_MV = -300.0
BURST_MV = 150.0               # amplitude (300 mV p-p)
LSB_MV = 5.0
BLANK_CODE = 64                # sync tip = 4, black = 64, white = 204, code 255 = +955 mV


def mv_to_code(mv):
    return BLANK_CODE + mv / LSB_MV


def code_to_mv(code):
    return (np.asarray(code, float) - BLANK_CODE) * LSB_MV


def t_line():
    """Time from 0H of each buffer sample, seconds."""
    return np.arange(SPL) / FS - T0


def _step(t, at, rise):
    """Smooth 0 -> 1 step, 50% at `at`, 10-90% rise time `rise` (Gaussian edge)."""
    return 0.5 * (1 + erf((t - at) / (rise / 2.563 * np.sqrt(2))))


def _pulse(t, start, width, rise=250e-9):
    return _step(t, start, rise) - _step(t, start + width, rise)


# --- sync and burst per line --------------------------------------------------------------
H2 = 32e-6
NORMAL, SHORT, BROAD = 4.7e-6, 2.35e-6, 27.3e-6


def _line_kind(line):
    if line in (1, 2, 314, 315):
        return 'BB'
    if line == 3:
        return 'BE'
    if line == 313:
        return 'EB'
    if line in (4, 5, 311, 312, 316, 317, 624, 625):
        return 'EE'
    if line == 318:
        return 'E-'
    if line == 623:
        return 'NE'
    return 'N-'


def sync_waveforms():
    """(625, SPL) sync signal in mV (0 = blanking)."""
    t = t_line()
    width = {'N': NORMAL, 'E': SHORT, 'B': BROAD}
    out = np.zeros((LINES, SPL))
    for line in range(1, LINES + 1):
        k = _line_kind(line)
        p = _pulse(t, 0.0, width[k[0]])
        if k[1] != '-':
            p = p + _pulse(t, H2, width[k[1]])
        out[line - 1] = SYNC_MV * p
    return out


def has_burst(line):
    # Simplified: no burst during the vertical sync area (the real 4-field Bruch sequence
    # moves this by one line from field to field; a TV does not care).
    return not (line <= 6 or 310 <= line <= 318 or line >= 623)


def burst_envelope():
    t = t_line()
    return _step(t, 5.6e-6, 300e-9) - _step(t, 5.6e-6 + 10 / FSC, 300e-9)


# --- picture -----------------------------------------------------------------------------
def resample_matrix(t_out, fs_in, n_in, t_in0, fc, half=12, beta=7.0):
    """Windowed-sinc interpolation matrix: out = M @ in.
    Input sample k is at time t_in0 + k / fs_in; fc is the low-pass cut-off in Hz."""
    pos = (t_out[:, None] - t_in0) * fs_in            # input sample position of each output
    k = np.arange(n_in)[None, :]
    d = pos - k                                       # distance in input samples
    x = d / fs_in                                     # seconds
    m = np.abs(d) < half
    w = np.zeros_like(d)
    w[m] = np.kaiser(2001, beta)[np.round((d[m] / half + 1) * 1000).astype(int)]
    M = 2 * fc / fs_in * np.sinc(2 * fc * x) * w
    full = (pos > half) & (pos < n_in - 1 - half)      # rows with the whole kernel inside
    M[full[:, 0]] /= M[full[:, 0]].sum(1, keepdims=True)   # exact DC gain 1
    return M


# Source column c is at t = (c - COL_0H) / 13.5 MHz after 0H
SRC_T0 = -fd.COL_0H / fd.FS_SRC
CHROMA_SIGMA = 1.38            # source samples: Gaussian, -3 dB at 1.3 MHz, ~ -28 dB at 4 MHz


def prepare_picture(y, by, ry):
    """Resample the source rows onto the 1135-sample line grid. Returns Y, U, V in mV,
    shape (frames, 576, SPL). Chroma is low-passed to ~1.3 MHz first, as in a PAL coder."""
    M = resample_matrix(t_line(), fd.FS_SRC, fd.COLS, SRC_T0, fc=6.2e6)
    u = 0.493 * gaussian_filter1d(by, CHROMA_SIGMA, axis=-1, mode='nearest')
    v = 0.877 * gaussian_filter1d(ry, CHROMA_SIGMA, axis=-1, mode='nearest')
    return tuple(a @ M.T for a in (y, u, v))


# --- composite -----------------------------------------------------------------------------
class Encoder:
    def __init__(self, y, by, ry, vswitch_phase=0, alternate_frames=True, phase_step=None):
        """vswitch_phase: 0 or 1, which V-switch state frame A of the data is paired with.
        alternate_frames: play the two anti-PAL data frames alternately (False = frame A only).
        phase_step: None = global sample counter (correct). An integer instead restarts each
        line at index (line_count * phase_step) mod 4; 3 (= -1) is equivalent to the counter,
        1 is the wrong '+1 per line' step."""
        self.Y, self.U, self.V = prepare_picture(y, by, ry)
        self.sync = sync_waveforms()
        self.benv = burst_envelope()
        self.vswitch_phase = vswitch_phase
        self.alternate = alternate_frames
        self.phase_step = phase_step
        self.rows = [fd.line_to_row(l) for l in range(1, LINES + 1)]

    def vswitch(self, frame, line):
        g = frame * LINES + (line - 1) + self.vswitch_phase
        return 1.0 if g % 2 == 0 else -1.0

    def frame_mv(self, frame):
        """One frame as float mV (before quantisation), shape (625, SPL)."""
        df = frame % 2 if self.alternate else 0
        out = self.sync.copy()
        n = np.arange(SPL)
        for line in range(1, LINES + 1):
            i = line - 1
            g = frame * LINES + i
            if self.phase_step is None:
                k = (g * SPL + n) % 4                      # global sample counter mod 4
            else:
                k = (g * self.phase_step + n) % 4
            sn, cs = SIN4[k], COS4[k]
            s = self.vswitch(frame, line)
            row = self.rows[i]
            if row is not None:
                out[i] += self.Y[df, row] + self.U[df, row] * sn + s * self.V[df, row] * cs
            if has_burst(line):
                # burst = colour vector (-U, +V) through the same modulator: 135 / 225 degrees
                b = BURST_MV / np.sqrt(2)
                out[i] += self.benv * (-b * sn + s * b * cs)
        return out

    def frames_codes(self, n_frames):
        mv = np.stack([self.frame_mv(f) for f in range(n_frames)])
        code = np.round(mv_to_code(mv))
        clipped = int(((code < 0) | (code > 255)).sum())
        return np.clip(code, 0, 255).astype(np.uint8), mv, clipped
