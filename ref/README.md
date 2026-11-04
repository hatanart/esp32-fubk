# Python reference (no hardware)

FuBK YUV (from `../data/fubk4x3.zip`, read directly) -> PAL composite at 4 x fsc, 8 bit -> software
PAL decoder (with and without delay line) -> images and measurements.

    ../venv/bin/python run_ref.py      # venv from the main README; ~1.5 min, writes out/

- `fubk_data.py`  load the PT8631 data (Y, B-Y, R-Y in mV), date boxes removed
- `pal_encode.py` composite synthesis, the model for the ESP32 code
- `pal_decode.py` software TV: clamp/AGC, notch, burst PLL + ACC + ident, optional delay line
- `run_ref.py`    runs everything, writes `out/report.txt`, PNGs and
  `out/fubk_pal_4fsc_4frames.u8` (raw DAC codes, 1135 x 625 per frame, 4 frames)

Signal choices (see the docstring in `pal_encode.py`):
- 17.734475 MS/s, 1135 samples/line; subcarrier index = global sample counter mod 4
  (per line the start index moves by 3, i.e. -1, never +1)
- DAC codes: sync tip 4, blanking/black 64, white 204, 5 mV/LSB (255 = +955 mV)
- line buffer sample 0 = 0.8 us before 0H
