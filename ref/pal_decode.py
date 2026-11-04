"""Software PAL decoder for the 4 x fsc composite from pal_encode.py.

Line-locked to the known 1135-sample timing (no sync PLL). Everything else is measured from the
signal, the way a TV does it:
  - levels: sync tip and back porch (clamp + AGC)
  - Y/C split with a 4.43 MHz notch
  - burst -> reference phase (averaged over all lines), ACC gain and the PAL ident (V-switch)
  - optional delay line (PAL-D): average U and V with the previous line
"""
import numpy as np
from scipy.signal import firwin, fftconvolve

import fubk_data as fd
from pal_encode import (FS, SPL, LINES, T0, SIN4, COS4, BURST_MV, SYNC_MV,
                        t_line, has_burst, resample_matrix)

NOTCH = firwin(41, [3.2e6, 5.7e6], fs=FS)        # band-stop around fsc for luma
CHROMA_LP = firwin(41, 1.6e6, fs=FS)              # after demodulation


def _filt(x, h):
    return fftconvolve(x, h, mode='same')


class Decoder:
    def __init__(self, delay_line=True, hue_error_deg=0.0):
        self.delay_line = delay_line
        self.hue_error = np.deg2rad(hue_error_deg)
        t = t_line()
        self.w_tip = (t > 0.5e-6) & (t < 2.0e-6)
        self.w_porch = (t > 8.4e-6) & (t < 10.1e-6)
        b0 = int(round((6.0e-6 + T0) * FS))
        self.w_burst = slice(b0, b0 + 24)       # 6 whole cycles inside the burst: no luma/DC leak
        cols_t = (np.arange(fd.COLS) - fd.COL_0H) / fd.FS_SRC
        self.M_back = resample_matrix(cols_t, FS, SPL, -T0, fc=6.0e6)
        self.info = {}

    def decode(self, codes, line_offsets=None):
        """codes: (frames, 625, SPL) uint8 -> Y, B-Y, R-Y in mV, (frames, 576, 1024).
        line_offsets: start sample of every line in the real stream, when lines are not all
        1135 samples (frame lock). The subcarrier reference follows the real sample count."""
        nf = codes.shape[0]
        x = codes.reshape(-1, SPL).astype(float)
        nl = x.shape[0]
        lines = np.tile(np.arange(1, LINES + 1), nf)
        normal = np.array([fd.line_to_row(l) is not None for l in lines])

        # Clamp each line to its back porch, AGC from the sync amplitude
        porch = x[:, self.w_porch].mean(1)
        tip = x[:, self.w_tip].mean(1)
        gain = -SYNC_MV / np.median((porch - tip)[normal])
        mv = (x - porch[:, None]) * gain
        self.info['sync_mv_per_code'] = gain

        # Y/C separation
        s = mv.reshape(-1)
        Y = _filt(s, NOTCH)
        C = s - Y

        # Demodulate against the free-running 4 x fsc reference
        if line_offsets is None:
            k = np.arange(s.size) % 4
        else:
            k = ((np.asarray(line_offsets)[:, None] + np.arange(SPL)[None, :]) % 4).ravel()
        Ud = _filt(2 * C * SIN4[k], CHROMA_LP).reshape(nl, SPL)
        Vd = _filt(2 * C * COS4[k], CHROMA_LP).reshape(nl, SPL)

        # Burst: phase, amplitude, ident
        bl = np.array([has_burst(l) for l in lines])
        # measured on the raw signal over whole cycles (the chroma filters would smear the edges)
        kb = k.reshape(nl, SPL)[:, self.w_burst]
        xb = mv[:, self.w_burst]
        z = (2 * xb * SIN4[kb]).mean(1) + 1j * (2 * xb * COS4[kb]).mean(1)
        ident = np.where(z.imag >= 0, 1.0, -1.0)
        expect = BURST_MV * np.exp(1j * np.deg2rad(180 - 45 * ident))     # 135 / 225 degrees
        rot = z / expect * np.abs(expect)       # burst vector with the swing removed
        ref = rot[bl].mean()
        theta = np.angle(ref)
        acc = BURST_MV / np.abs(z[bl]).mean()
        # V-switch flip-flop as in a TV: toggles every line, and flips when the burst ident
        # disagrees for a few lines in a row (leaky integrator)
        sw = np.empty(nl)
        state, acc, flips = 1.0, 0.0, 0
        for i in range(nl):
            if i:
                state = -state
            if bl[i]:
                acc = 0.6 * acc + 0.4 * ident[i] * state
                if acc < -0.5:
                    state, acc, flips = -state, -acc, flips + 1
            sw[i] = state
        self.info['ident_flips'] = flips
        self.info.update(burst_phase_err_deg=np.rad2deg(theta),
                         burst_phase_spread_deg=np.rad2deg(np.std(np.angle(rot[bl] / ref))),
                         burst_mv=np.abs(z[bl]).mean(), acc=acc,
                         ident_errors=int((ident[bl] != sw[bl]).sum()))

        w = (Ud + 1j * Vd) * np.exp(-1j * theta) * acc * np.exp(1j * self.hue_error)
        U = w.real
        V = w.imag * sw[:, None]
        if self.delay_line:
            U = 0.5 * (U + np.vstack([U[:1], U[:-1]]))
            V = 0.5 * (V + np.vstack([V[:1], V[:-1]]))

        # Back onto the 13.5 MHz source grid, frame row order
        Y = Y.reshape(nl, SPL)
        out = np.zeros((3, nf, fd.ROWS, SPL))
        for f in range(nf):
            for li in range(LINES):
                row = fd.line_to_row(li + 1)
                if row is None:
                    continue
                g = f * LINES + li
                out[0, f, row] = Y[g]
                out[1, f, row] = U[g] / 0.493
                out[2, f, row] = V[g] / 0.877
        out = out @ self.M_back.T
        return out[0], out[1], out[2]
