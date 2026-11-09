# Technical notes

Design notes, measurements and rejected experiments. Start with the [main README](../README.md).
The first part describes the older 4×fsc design (`FUBK_20MHZ=0`); the current 20 MHz design follows
from "20 MHz design" on.

## Files (4×fsc design)

    tools/make_tables.py    FuBK -> firmware/main/fubk_tables.h + out/model_4frames.u8 (integer model)
    firmware/               ESP-IDF v5 project (idf.py build flash monitor), output on GPIO25
    firmware/main/fubk_synth.c   line synthesis, shared by firmware and host test
    host/                   make test: C output == Python integer model, byte for byte

Build the firmware (ESP-IDF v5.5.6 in `~/esp/esp-idf-v5.5.6`):

    . ~/esp/esp-idf-v5.5.6/export.sh
    cd firmware && idf.py build flash monitor

Rebuild the tables (needs the venv from the main README):

    ../venv/bin/python tools/make_tables.py && make -C host test
    ../venv/bin/python ../ref/run_ref.py --codes out/model_4frames.u8 --out out/check   # decoder checks

## Table format (94 KB, all in internal RAM)

| part | bytes | |
|---|---:|---|
| Y blocks (1229 x 24) | 29 496 | uint8, Y + 64 |
| UV blocks (415 x 24 x 2) | 19 920 | int8, U/V interleaved |
| block changes (8494 x 4) | 33 976 | per row: blocks that differ from the line above in the same field |
| row index | 2 306 | |
| sync templates (7 kinds) | 7 945 | whole lines |
| burst (4 phases x 2 V-switch x 64) | 512 | |
| line info, row map | 1 875 | |

- Active window: samples 192..1127 (936 = 39 blocks of 24).
- Lines are generated strictly in order. The synth keeps the current block list and applies each
  row's changes. The first row of each field lists all 39 blocks, so every field starts clean.
- Chroma tables are scaled by 0.99 to match the quantised burst (21 LSB = 148.5 mV), so a TV's
  ACC gets the saturation exactly right.
- Plain row/block dedupe is not enough (the circle makes 544 of 1152 rows unique: 140+ KB of index).

## RAM

tables 94 KB (`DRAM_ATTR`) + DMA buffers 4 x 4540 B = 18 KB + descriptors. No PSRAM needed.
Flash-mapped tables would not work: about 2.8 KB of table data per line at random places means
cache misses every line.

## CPU

From the compiled code: 6 instructions per active sample (2 loads, sign extend, add/sub, shift,
16-bit store) plus loop overhead. The sample-pair swap is folded into constant offsets, because
the two line starts in a buffer (offset 0 and 1135) have fixed alignments. About 8.5k cycles per
line against a budget of 15.4k at 240 MHz: roughly 55 % of core 1, in the I2S interrupt. The
firmware prints the measured average and worst case every 2 s.

## Levels (direct connection, no resistors)

The tables assume `--lsb-mv 5.0 --blank-code 64` (`tools/make_tables.py`): sync tip code 4,
black 64, white 204, i.e. 1.0 V sync-to-white at the TV. Measure the real mV per code on the pin
while it drives the TV's 75 ohm, then regenerate with that value.
- This test card spans -300 .. +781 mV, so the finest usable step is 1081 / 255 = 4.24 mV/code.
  5 mV is already close: a divider would gain at most ~15 % resolution (236 instead of
  200 codes from sync to white).
- More than ~5 mV/code loaded: the signal is too big. A series resistor or divider brings it back to
  5 mV/code and keeps the 200 codes (better than mapping onto fewer codes).
- Less than 4.24 mV/code: the full 1 V cannot be reached with passive parts. Use the
  finest mapping that fits; TVs set their gain from the sync amplitude and tolerate a low signal.

## DMA layout

- I2S0 in LCD/DAC mode, 16-bit mono, APLL odiv=1 sdm0=4 sdm1=164 sdm2=6 = 70.937907 MHz,
  sample rate APLL/4 = 17 734 476.7 Hz (+0.1 ppm; fsc +0.4 Hz; the crystal tolerance is larger).
  The same coefficients esp_8_bit uses for PAL. ESP32 rev 0 ignores sdm0/sdm1, so it needs rev 1 or later.
- One line is 2270 bytes (not a multiple of 4), so each DMA buffer holds 2 lines (4540 bytes) over
  two descriptors (2272 + 2268 bytes, max 4095 per descriptor). One interrupt per buffer.
- The DAC takes the high byte of each 16-bit sample, and sample pairs are swapped (`FUBK_SWAP`).

## Frame lock (default in the firmware build)

`FUBK_FRAME_LOCK` (in `firmware/main/CMakeLists.txt`): line 5 is 1136 samples, so a frame is
709 376 samples = a whole number of subcarrier cycles. The subcarrier stays continuous (no jump),
but phase, V-switch and data (anti-PAL frame A) now repeat every frame, so the picture and the
scope image stand still. Standard PAL repeats only every 4 frames: without the +25 Hz offset,
cross-luma dots and any anti-PAL residue then flicker at 6-12 Hz.
- At the frame wrap, lines 625 and 1 have the same V-switch sign (no burst there); TVs re-sync
  their PAL flip-flop from the burst within a few lines (the software decoder needs 1 correction).
- DMA: line 5 gets a buffer of its own (1136 samples = 2272 bytes), so there are 313 interrupts per
  frame instead of 312.5 (15 650 per 2 s in the log instead of 15 625).
- Check: `python ../ref/run_ref.py --frame-lock --codes out/model_framelock_4frames.u8 --out out/check_framelock`

## 1136-sample lines (`FUBK_LINE1136`)

Every line gets one extra blanking sample: 1136 = 284 whole subcarrier cycles, so every line starts
at the same subcarrier phase, as esp_8_bit / ESP32-TestPatternGenerator do. Line 64.056 us
(15 611 Hz), not standard PAL, but TVs lock to it. The APLL jitter pattern then sits at the same
place on every line instead of crawling. Data frame A, V-switch per line. DMA: 2 lines per buffer,
one per descriptor (2272 or 2280 bytes each). Excludes `FUBK_FRAME_LOCK`.
- The APLL fraction is never jitter-free. The exact one (sdm0=4) has a beat at 373.5 kHz (2.68 us)
  that does not fit the line and crawls. The 41/64 one (sdm0=0) repeats every 227 samples, so
  lines 8-14 and 321-327 are 1140 samples: frame = 710056 = 3128 x 227 samples, and the jitter
  is then the same in every frame. Use `FUBK_APLL_EXACT=0` (fsc -25 Hz).
- Check: `python ../ref/run_ref.py --line1136 --codes out/model_line1136_4frames.u8 --out out/check_line1136`

## 20 MHz design (`FUBK_20MHZ`): standard PAL without clock jitter

`firmware/main/main20.c` + `fubk_synth20.c`, tables from `tools/make_tables20.py`.

- APLL 80 MHz with integer coefficients only (sdm0 = sdm1 = 0, sdm2 = 8, odiv = 1): no
  sigma-delta jitter. I2S sends APLL / 4 = 20 MS/s.
- Line = 1280 samples = 64.000 us, 625 lines, exact fsc 4 433 618.75 Hz (283.75 fh + 25 Hz):
  the full 8-field sequence repeats after 4 frames (3 200 000 samples = 709 379 cycles).
- Chroma per sample: `Y + (U * car[n + ms] + V * car[n + mc] + 2^13) >> 14`, one 2304-entry
  sine table read at a per-line offset (phase in 1/1024 cycle, exact at mid-line, error < 0.5 deg).
  The V-switch is folded into the cos offset. Blocks without colour skip the multiplies.
- Tables 113 KB (33 blocks of 32 samples per line). DMA: 8 buffers x 2 lines (2 x 2560 bytes).
- Both cores render: the I2S interrupt (core 1) fills the odd buffers, a task on core 0 the even
  ones; each keeps its own state and skips the other's lines. The log prints the load per core.
- Checks: `../venv/bin/python tools/make_tables20.py [--test-field]`, `make -C host test20`
  (C == model, also split over 2 states), `../venv/bin/python tools/check20.py out/model20_4frames.u8`
  (burst 150 mV at 135/225 deg less the 9.1 deg DAC correction, chroma error < 1 LSB, anti-PAL
  grey with PAL-D).
- DAC phase correction (PAL only): the burst is rotated -26 carrier steps (-9.1 deg) against the
  picture (`PAL.burst_rot` in make_tables20.py, `burst_ofs` in the tables). The DAC output shifts
  the picture chroma (higher levels) about 9 deg against the burst (near blanking). Test build:
  `idf.py -DFUBK_BURST_TEST=1 build` (remembered; =0 back): the pattern button then adds 0, +1,
  -1, +2, -2 deg to the burst instead of changing the pattern.

### Hardware results (110 ohm series resistor into 75 ohm)

Default build = 20 MHz standard PAL (`FUBK_20MHZ=1`, the others off):
- Panasonic CRT, reverse-camera monitor, 20-year-old plasma TV: clean, stable colour.
- RetroScaler 2X and a 77" OLED TV's AV input: moving Hanover bars in saturated colours until
  2026-11-08. 41 % / 86 % CPU per core, no underruns.
  - An Amiga 600 (amigatestkit.adf, 75 % EBU bars) showed only faint ones on the same screens.
  - The generated samples demodulate to exact PAL (per line, no delay line: hue within 0.9 deg,
    saturation within 0.8 %), so the error is in the DAC output: the picture chroma about 9 deg
    late against the burst. PAL-D TVs (CRT, plasma, reverse-camera monitor) hid it in their delay
    line, but their anti-PAL boxes were not grey.
  - Fix: the burst is rotated -9.1 deg (sweep with `FUBK_BURST_TEST`: best between -9.5 and
    -8.4 deg). Then the bars are gone on every screen and the anti-PAL boxes are grey on the
    plasma and the reverse-camera monitor. NTSC looked right and was left alone.
- `FUBK20_LINE1281=1` (same phase every line): stripes stand still on the RetroScaler, but the
  subcarrier is +417 Hz off and the plasma's colour flashes on and off. No integer APLL clock
  gives same-phase lines closer to the right frequency (best: 25 MHz, +109 Hz, too much DMA/CPU).
- `FUBK20_NO_OFFSET=1`: no better on the RetroScaler, grey verticals got wavy. Keep off.
- The 4fsc builds (`FUBK_20MHZ=0`) use a fractional APLL whose sigma-delta jitter beats at
  373.5 kHz (exact fraction) and crawls; `FUBK_LINE1136` freezes it but is not standard PAL.

## Pattern button (GPIO15)

- `tools/patterns20.py` draws the extra patterns (2..16) on the same grid as the test
  cards. Geometry is in square units (4:3, one unit = one picture row). Rectangles get exact
  area coverage per sample; circles are 4 x 4 supersampled. Luma is shaped for 150 ns edges,
  chroma for 400 ns. PAL line 623 is blanked from 1 us before its half-line equalising pulse.
- `make_tables20.py` writes `firmware/main/patterns20_tables.h`: picture tables only
  (row_start, delta, Y/UV blocks, flat colours), in flash. The `PAT()` initialiser in
  fubk_synth20.c combines them with the standard's timing, sync, burst and carrier tables.
- PAL flat colours: one chroma waveform per V-switch sign (2 x 1056 bytes per colour), so the
  colour fields and bars need no multiplies. FuBK keeps the multiply path (few flat blocks; its
  tables are unchanged).
- RAM copy: rendering straight from flash worked, but right after a switch the first frame
  missed the cache everywhere (PAL colour bars: 400 misses for the chroma waveforms), one render
  took up to 270 % of its slot and the I2S interrupt fell more than 16 lines behind (the DMA then
  replays old lines; seen as a 10 kHz dip in the logged sample rate). NTSC circles stayed above
  100 % peak even when steady. Now the pattern on screen is copied to RAM (a 53 KB buffer plus
  heap blocks, see the PM5644 section; `fubk20_pattern_load`), and a 1.5 KB RAM copy of the black
  pattern covers the gap while it is refilled. If the buffer can't be allocated, patterns render
  from flash as before (logged at start).
- Switching: the button task writes `(slot + 1) << 24 | start frame`, 2 frames ahead (slots:
  built-in card, black, RAM copy).
  `fill()` passes it to its render state (`fubk20_request`), and both states switch at that
  frame start. The frame counter keeps running, so FuBK's anti-PAL A/B frames stay in step with
  the V-switch when you come back to it.
- `make -C host test20` checks every pattern in both standards (split over 2 states) and a
  switching sequence against the model. `out/preview_*.png` show the pictures.

### Patterns 15 and 16: own line timing

- `PATTERNS` entries carry a kind: `''` (the standard's timing), `'noburst'` (same, burst bit
  cleared in its own `line_info`) or `'image'`. Patterns with their own timing get their own
  `line_info` / `rowmap` arrays and line count in `patterns20_tables.h`; `PAT()` takes them.
  Both are copied to RAM with the pattern.
- Non-interlaced timing (`PAL_PROG`, `NTSC_PROG` in make_tables20.py): PAL 313 lines (1-2 broad,
  3 broad + equalising, 4-5 and 311-313 equalising, picture 23..310; 313, not 312: with an even
  count every line kept its PAL V-switch sign in every field, and the small remaining DAC phase
  error stood still as Hanover bars on the picture's sky; odd, it flips per field as in 625i), NTSC 262 lines (1-3 and
  7-9 equalising, 4-6 broad, picture 22..261). To the synth a field is a frame, so the frame
  counter counts fields there; the V-switch runs on unchanged. Teletext / captions
  are off (`vbi = 0`); the teletext clock runs twice as fast while the page is shown.
- Image rendering: palette entry `{y, a, k[2]}`, sample = `(y + a * car[(ms + k[vs]) % N + i]) >> 20`
  (`a` = A << 6, `k` = hue offset `t * car_inv mod N` for +V / -V lines; the hue is rounded to
  360/N degrees: PAL 0.35, NTSC 1.0). 8 pixels = 26 samples = 13 pairs per loop step, written
  straight to the DMA buffer (NTSC odd line starts: single 16-bit stores). `mul16()` forces
  `mul16s`: GCC otherwise uses the MAC16 accumulator (wsr / mula / rsr per sample).
- CPU on the image page (2 lines per buffer): PAL avg 56 % / max 62 %, NTSC 60 % / 67 %. Steps
  on the way (PAL): `U S + V C` per sample 146 % (the output fell to 13 MS/s); hue offset 89 %;
  mul16s 84 %; direct pair stores instead of a byte line + copy 56 %.
- RAM: the rows (PAL 288 x 320 = 90 KB) go in 16-row blocks (`img_load` in main20.c), first into
  `pat_arena` after the pattern's small tables, then into heap blocks, freed at the next pattern
  load. Rows that find no RAM would stay in flash (logged). Free heap with the PAL picture: 7.7 KB.
- NTSC field: 262 x 1271 samples would be 59598.36 subcarrier cycles, so the colour pattern
  moved 0.36 cycle per field (restless on a plasma). Line 262 is 10 samples shorter
  (`short_line` / `short_by`, used by `fubk20_next_len()` and the model): 332992 samples =
  59598 cycles, every field identical.
- PAL field: 313 x 1280 samples would be 88814.25 cycles, so the colour pattern crawled by a
  quarter cycle per field. Lines 14, 28, .. 308 are 2 samples longer (`ext_every` / `ext_n`; 2,
  not 1, so every 2-line DMA buffer stays whole 32-bit words): 400684 samples = 88824.0048
  cycles. The phase accumulator is set back to 0 at every field start (`ph_reset`), so the
  1.7 deg left over is a step in the vertical sync and fields 2 apart are identical (checked
  by `check_prog20.py`). On the screens the palm picture became as calm as the NTSC one. The picture's colour is softened before choosing the
  256 colours (400 ns, as the other patterns): about 35 % less fine chroma.
- Test option: `idf.py -DFUBK_START_PATTERN=16 build` steps to that pattern at start (for
  measuring a page over the serial log; remembered, =0 off).
- Check: `make -C host testprog` (tools/check_prog20.py): sync per line, no half lines, burst
  lines and phase, every palette colour fitted back from the samples (within 3 mV), and pattern 15
  = pattern 12 except a blank burst window.

### PM5644 (PAL pattern 1, NTSC pattern 0) and the pattern order

- `tools/pm5644.py` downloads the EPROM dumps (inaxeon/PTV_Preservation, fixed commit, checksum
  in each file name checked) to `data/ptv_pm5644/` and decodes them: 4 luma + 2 + 2 chroma
  EPROMs interleaved (128 bytes per row each), video inverted (PAL black 181 / white 41, NTSC
  170 / 38), per field one record (ctrl, edge, v) per line in the control EPROM: centre row
  2 v + ctrl bit 0, edge row 2 edge + ctrl bit 1; a line is edge band, centre, edge band on the
  720-sample BT.601 grid. `patterns20.pm5644()` resamples it to 20 MHz as FuBK (6.2 MHz sinc,
  1.3 MHz chroma). Rows 2k / 2k + 1 = record k of field 1 / 2 (the interleave with smooth
  edges); NTSC shows records 1..240 of 243 per field.
- Each standard has its own order (`patterns20.ORDER`): PAL FuBK, PM5644, extras; NTSC PM5644,
  SMPTE, extras. In C `F20_PATTERNS(MAIN)` lists them with the built-in card (main tables,
  always in RAM) at `FUBK20_PAL_MAIN` / `FUBK20_NTSC_MAIN`. NTSC copies PM5644 to RAM at start.
- RAM: `pat_arena` is 53 KB, allocated before the tasks take their stacks. `fubk20_pattern_load`
  calls `place(table, bytes)` per table; main20.c puts the set of tables that fills the arena
  best there (all 2^9 sets tried) and the rest in heap blocks. PM5644 PAL is built without flat
  colours (90 KB instead of 102): arena uvpool + delta + small tables, ypool (38 KB) on the
  heap; free heap then 15 KB. With 102 KB, delta did not fit (the largest free block is only
  49 KB once the tasks run) and stayed in flash. The image page leaves 7.7 KB free, so the
  heap blocks are freed before the PAL / NTSC switch writes NVS.
- CPU, PM5644 PAL: avg 49 % / max 81 % (FuBK 40 / 76).

## Card text (PM8503 font) and the edit mode

- `tools/pm8503_font.py` decodes `data/pm8503_charset.png` (an exact 4 x upscale of 12 x 6
  cells of 9 x 18 dots) into 64 glyphs in edit order (space, A-Z, 0-9, punctuation, accents);
  glyph 64 is the half space. `make_tables20.py` finds the boxes in the card pictures
  (`fubk_text_boxes`, `pm5644_text_boxes`) and writes `firmware/main/text20_tables.h`: the font,
  an edge table and the box positions. Placement: the capitals (dot rows 4..16) are centred in
  the box, but never higher than keeps the accents (rows 1..16) inside it.
- A dot is 80/27 samples (2 pixels at 13.5 MHz) wide and 2 frame rows (one per field) high.
  The edge table holds every sample for each of the 80 sub-positions and each (previous,
  current, next) dot, with 150 ns edges. Inside the box rows the text is white on the box's
  black; outside them (or where the box has other content) nothing is drawn. Only FuBK's centre
  line is covered: a cell that is not a space is drawn opaque over it.
- The half space in a line's last place shifts the line by 4.5 dots (half a character).
- Speed: `text_overlay` is `noinline` (inlined, its registers spilled in the render loop). Dots
  are read 32 at a time and runs of empty dots are skipped with `__builtin_clz`. With every
  place filled, max CPU is FuBK 85 %, PM5644 PAL 89 %, PM5644 NTSC 94 %; with empty boxes it is
  as without text.
- `cc_render` (NTSC captions) was rewritten in the same pass with running counters instead of a
  division per sample: line 21 is unchanged, byte for byte, and the NTSC peak fell from 116 %
  (from v1.2 on) to 94 %.
- Edit mode (`main20.c`): only when both buttons are held at a power-on or reset (not after a
  software restart). Writing NVS while the video runs triggered the interrupt watchdog, so the
  save stops I2S, writes the 33-byte blob `text`, sets an `RTC_NOINIT` resume word (magic +
  cursor position) and restarts; the next boot goes straight back into edit mode.
- Test: `make -C host testtext` renders FuBK and both PM5644 cards and `tools/check_text20.py`
  reads the text back from the samples (dot rows on the black box only, not FuBK's centre line).

## Teletext and closed captions (`vbi20.c`)

- **When:** `fubk20_line()` hands blank lines (no picture row) to `vbi20_line()` when
  `st->vbi_on` is set (the firmware sets it; `fubk20_init` leaves it off, so the exact model
  tests are unchanged). A data line is drawn into the state's `vbi_buf` and copied into the DMA
  buffer with `copy_tmpl`, which handles both buffer layouts.
- **What:** packet n = (2 · frame + field) mod 25, header clock = frame / 25 s. Captions: second
  = frame · 1001 / 30000; in each second, frames 0–16 send RCL, ENM, PAC row 15, 9 text pairs,
  EOC (control codes twice), then null pairs. Everything comes from the frame counter, which
  both render states keep, so they need no shared data.
- **Teletext waveform:** 6.9375 Mbit/s = 320/111 samples per bit. The first bit starts 10.3 µs
  after 0H; 0 = blanking, 1 = 462 mV. Each sample is `ttx_lut[phase][5 bits]`: phase = position
  in the bit in 32 steps, bits k − 2 .. k + 2, raised-cosine pulse with 50 % roll-off.
  Values −22 to +113 DAC steps around blanking.
- **Caption waveform:** 32 fH = 1271/32 samples per bit. Run-in: 7 cycles of (1 − cos) from
  10.5 µs; start bit edge 27.38 µs; 16 bits LSB first; 50 IRE; 400 ns raised-cosine edges.
- **Cost:** a data line costs about as much as a picture line, and it shares its buffer with a
  blank line. Measured: worst case 76 % per core (74 % without).
- **Check:** `make -C host testvbi` renders with and without data and runs
  `tools/check_vbi20.py`. It slices the lines independently (teletext through a 5 MHz low-pass),
  checks framing, Hamming, parity, every byte, the header clock and the caption text (a small
  EIA-608 pop-on decoder), and checks that all other lines are byte-identical. Teletext eye:
  111 of 144 ns wide, worst bit 183 mV from the threshold. Clock rollover 00:59:59 → 01:00:00 is
  tested in both standards.
- **Hardware:** teletext page 100 shows on a real TV. Captions not yet tried on a TV.

## Front panel and NTSC (20 MHz build)

| pin | function |
|---|---|
| GPIO25 | composite video (DAC1, 110 ohm series into 75 ohm) |
| GPIO4 ("D4") | button to GND (internal pull-up): toggles PAL / NTSC, saved in NVS, then restart |
| GPIO12 | LED on in PAL. Strapping pin: the LED must pull it LOW at reset (LED + resistor to GND), else the flash voltage is set wrong and the board does not boot |
| GPIO13 | LED on in NTSC |
| GPIO18 | 1 kHz sine as PWM (312.5 kHz, 8 bit), stepped once per video line, so locked to the picture (PAL: exactly 40 cycles per frame). RC low-pass (e.g. 3.3 k + 10 nF) and a coupling capacitor. `AUDIO_AMP` in main20.c sets the level (120 = ~3 V p-p before the filter) |

NTSC: SMPTE EG-1 colour bars (75 % bars, castellations, -I / white / +Q, PLUGE 3.5 / 7.5 / 11.5
IRE) across the full active line (9.4 .. 62.05 us after 0H), 7.5 IRE setup, 525 lines interlaced.
Every line is 1271 samples (63.55 us, 227.48 fsc cycles; the exact 227.5 would be 11440/9
samples). Mixing 1271- and 1272-sample lines to get exactly 227.5 on average (`long_period = 9` in
make_tables20.py) was tried: vertical edges got wavy and a plasma TV showed snow, so it is off. Exact fsc
3 579 545.45 Hz (63/352 of the sample rate: the carrier table is exact). Burst 40 IRE p-p at
180 deg. Flat-colour blocks use precomputed chroma (no multiplies). Tables 22 KB.
`tools/check20.py --ntsc out/model20_ntsc_2frames.u8` checks every bar against EG-1 (worst 3.3 mV).
- DMA buffers now come from the heap (static RAM holds both standards' tables).
- One EOF interrupt per line (for the tone); the finished buffers are worked out from the DMA
  position, so a delayed interrupt never loses a refill.
- The rejected PAL variants (1281-sample lines, no 25 Hz offset) were removed; the per-line phase
  is now computed at run time (same output as the old table, checked byte for byte).
