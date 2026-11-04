# ESP32 FuBK / SMPTE test pattern generator

Composite video test patterns from a plain ESP32 and one resistor:

- **PAL:** the German **FuBK** test card (Funkbetriebskommission). It is built from the original
  sample data of a ProTeleVision PT8631 generator, not drawn from a palette, so the circle,
  gratings, anti-PAL boxes and the 4.43 MHz orange patch are real.
- **NTSC:** **SMPTE EG-1 colour bars**, with castellations, −I / white / +Q, and PLUGE.

One button switches between them, and two LEDs show which one is on. A 1 kHz test tone, locked
to the picture, comes out on another pin.

The ESP32's internal 8-bit DAC sends samples at 20 MS/s. The colour subcarrier is calculated
for every sample, so the subcarrier frequency is exact and the 8-field PAL sequence (with the
25 Hz offset) is standard. The 20 MHz clock comes from the audio PLL with integer settings only,
so it has none of the sigma-delta clock jitter that makes slowly moving patterns on most ESP32
composite projects.

Tested on a Panasonic CRT, a 20-year-old plasma TV, a cheap reverse-camera monitor and a
RetroScaler 2X.

## Hardware

Any board with the **original ESP32** works, for example a DevKitC, a LOLIN D32 or a WROOM-32
module. The ESP32-S2, -S3, -C3 and -C6 do not work: they have no I2S-to-DAC mode. Early ESP32
revisions (rev 0) also work, because the clock uses integer PLL settings only.

### Schematic

```
                      ESP32
              +---------------------+
              |                     |        110R              RCA (video, yellow)
              |       GPIO25 (DAC1) o-------/\/\/\---------------o  centre
              |                     |                            |
              |                 GND o----------------------------o  shield
              |                     |                       (TV: 75R to GND inside)
              |                     |
              |                     |     push button
              |      GPIO4  ("D4")  o-------o  o------+
              |                     |                 |
              |                     |   330R    PAL   |
              |      GPIO12         o--/\/\/\---|>|---+
              |                     |                 |
              |                     |   330R    NTSC  |
              |      GPIO13         o--/\/\/\---|>|---+
              |                     |                 |
              |                 GND o-----------------+
              |                     |
              |                     |   3k3        10uF            RCA (audio)
              |      GPIO18         o--/\/\/\--+---||---+----------o  centre
              |                     |          |        |          |
              |                     |        ----     [10k]        |
              |                     |        ---- 10n   |          |
              |                     |          |        |          |
              |                 GND o----------+--------+----------o  shield
              +---------------------+
```

| Pin | Function |
|---|---|
| GPIO25 (DAC1) | Composite video. Use a **110 Ω series resistor** into the TV's 75 Ω (see below). |
| GPIO4 | Push button to GND (internal pull-up). Toggles PAL / NTSC; the choice is saved in flash and the board restarts. |
| GPIO12 | LED: on in PAL. **Wire it as LED + resistor to GND** (the pin drives it high). GPIO12 is a strapping pin: anything that pulls it high at reset sets the wrong flash voltage and the board won't boot. |
| GPIO13 | LED: on in NTSC (LED + resistor to GND). |
| GPIO18 | 1 kHz sine as 312.5 kHz PWM. Filter it with an RC low-pass (3.3 kΩ + 10 nF) and a coupling capacitor. |

**Why 110 Ω:** the DAC swings 0 to about 3.3 V in 255 steps (about 12.9 mV each). With 110 Ω in
series and the TV's 75 Ω to ground, that becomes 3.3 V × 75 / 185 / 255 ≈ 5.2 mV per step. The
tables assume 5 mV per step, which gives 1 V from sync tip to white. If your picture is too dark
or too bright, adjust the resistor (100 to 120 Ω).

**Audio level:** the tone is about 3 V peak-to-peak before the filter, which is too hot for most
line inputs. Lower `AUDIO_AMP` in `esp32/firmware/main/main20.c` (40 gives about 1 V p-p), or add
a divider after the capacitor.

## Flashing

### Option A: prebuilt binary (no toolchain)

Download `fubk-esp32.bin` from the [Releases](../../releases) page. It is one merged image that
goes to address 0x0.

- **In the browser** (Chrome or Edge): open <https://espressif.github.io/esptool-js/>, click
  *Connect*, choose the board's serial port, set *Flash Address* to `0x0`, pick the file, then
  click *Program*.
- **From the command line** (`pip install esptool`):

  ```
  esptool.py --chip esp32 -p /dev/ttyUSB0 -b 460800 write_flash 0x0 fubk-esp32.bin
  ```

  On Windows the port is `COM3` or similar; on macOS it is `/dev/cu.usbserial-*`. Many boards
  need a CH340 or CP210x USB driver. If the connection fails, hold the BOOT button while it
  starts.

### Option B: build from source (ESP-IDF)

1. Install [ESP-IDF v5.5](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/).
   The code was built and tested with v5.5.6.
2. Build and flash:

   ```
   . ~/esp/esp-idf-v5.5.6/export.sh          # Windows: use the "ESP-IDF" command prompt
   cd esp32/firmware
   idf.py set-target esp32
   idf.py -p /dev/ttyUSB0 flash monitor     # Ctrl-] leaves the monitor
   ```

   `sdkconfig.defaults` has everything the project needs (240 MHz CPU, and so on).
3. To make your own merged binary for a release:

   ```
   cd build && esptool.py --chip esp32 merge_bin -o ../fubk-esp32.bin @flash_args
   ```

### What the serial log should show

Every 2 s the firmware prints a status line:

```
fubk20: PAL | I2S flags (2 s): FIFO empty 0, TX hung 0, descriptor error 0; missed core0 0 core1 0, queue full 0; sample rate 20000000 Hz
fubk20: core 0: render 2 lines avg ... cycles = 41% / ...% of its time
fubk20: core 1: render 2 lines avg ... cycles = ...
```

`FIFO empty` and `missed` must stay at 0: anything else means the DMA ran dry and the picture will tear.
Average load is roughly 40 to 60 % per core.

## Use

Power the board and connect the video output to the TV. It starts in the standard it last used
(PAL the first time). Press the button to switch; the board restarts in the other standard
within a second.

## How it differs from the real patterns

These are good enough for adjusting and checking TVs, but they are not reference generators. The
main differences:

### Both standards

| | Real generator | This project |
|---|---|---|
| Frequency accuracy | Oven or TCXO reference; PAL fsc ±5 Hz, NTSC ±10 Hz | ESP32's 40 MHz crystal, typically ±10 ppm: fsc about ±44 Hz (PAL) / ±36 Hz (NTSC). TVs lock easily, but it fails a broadcast spec check. No genlock input. |
| Resolution | 10-bit (0.7 mV) | 8-bit DAC at 5 mV per step: 140 steps from black to white, so a slow ramp shows faint steps. |
| Level accuracy | Calibrated 1 V into 75 Ω | Depends on the 3.3 V supply and the resistor: about ±5 %. |
| Output impedance | 75 Ω (good return loss) | 110 Ω plus the DAC's own impedance: long cables can show slight reflections (ghosts). |
| Reconstruction filter | Yes | None. Each sample is held for 50 ns: chroma is 0.7 dB low at 4.43 MHz, but burst and colour drop by the same amount, so the TV's colour gain corrects it. Fine luma detail is about 1 dB low at 5 MHz. Image frequencies at 20 MHz − f are left in; TVs filter them out. |
| Vertical blanking | Optional test lines (VITS), teletext, WSS | Empty, black. |
| Audio | Usually on the RF carrier, or a separate balanced line output | A separate PWM pin, baseband only. |

### PAL FuBK

- **Data:** the PT8631's 13.5 MHz 4:2:2 samples (702 per line), resampled to 20 MHz with a
  6.2 MHz windowed sinc. Luma detail up to about 5.5 MHz is kept. Chroma is limited to about
  1.3 MHz, like a PAL encoder.
- **Date and time boxes removed:** the real card shows a station name, date and clock. Here those
  boxes are filled from the plain variant of the card.
- **Anti-PAL boxes:** these are correct. The two data frames from the PT8631 alternate, so the
  boxes show grey on a PAL-D (delay-line) TV and colour on a simple PAL-S decoder.
- **Timing:** standard 625/50: 64.000 µs lines, 4.7 µs sync, 10-cycle burst at 135° / 225°, and
  exact fsc 4 433 618.75 Hz. Burst amplitude is within 1 % and phase within 1° of nominal (8-bit rounding included). The
  SC-H phase (subcarrier to sync timing) is not set to a defined value.
- **Edges:** sync rise time is 250 ns (the standard allows 200 ± 100 ns).

### NTSC SMPTE bars

- **Line length:** 1271 samples = 63.55 µs instead of 63.5556 µs. The line rate is 15 735.6 Hz
  (+87 ppm) and the field rate is 59.945 Hz.
- **Subcarrier to line relation:** fsc is exactly 3 579 545.45 Hz, but a line is 227.48 cycles
  instead of 227.5. So the subcarrier drifts against sync (no fixed SC-H phase), there is no
  4-field colour sequence, and the chroma dots don't interleave perfectly from line to line.
  Mixing 1271- and 1272-sample lines to hit exactly 227.5 on average was tried: bar edges went
  wavy and a plasma TV showed snow, so it is off.
- **Bars:** EG-1 layout at 75 % amplitude with 7.5 IRE setup (NTSC-M as used in North America;
  Japanese NTSC-J has no setup). Bars fill the whole active line, 9.4 to 62.05 µs after the sync
  edge. Every area decodes within 3.3 mV (0.5 IRE) of the EG-1 values. Luma edges are 150 ns;
  chroma edges are 400 ns.
- **Burst:** 9 cycles, 40 IRE p-p at 180°.
- This is SMPTE EG-1 (SD analogue). It is not the HD bars of SMPTE RP 219.

## How it works (short)

- **Clock:** the APLL runs at 80 MHz (integer settings, so no jitter). I2S0 in DAC mode sends
  80 / 4 = 20 MS/s to DAC1.
- **Picture tables:** each line is built from 32-sample blocks of Y and U/V. A table only lists
  the blocks that change from one line to the next, so the whole FuBK card fits in 113 KB of
  internal RAM. No PSRAM is needed.
- **Colour:** each sample is `Y + (U·sin + V·cos)`, using one sine table read at a per-line
  offset that follows an exact free-running subcarrier phase. Flat colour areas (the NTSC bars)
  use precomputed waveforms.
- **Two cores:** the I2S interrupt on core 1 renders the odd buffers, and a task on core 0
  renders the even ones.
- **Tone:** the 1 kHz tone is stepped once per video line, so it is locked to the picture (PAL:
  exactly 40 cycles per frame).

Details, measurements and the experiments that were rejected are in
[`esp32/README.md`](esp32/README.md).

## Repository layout

```
esp32/firmware/          ESP-IDF project (main20.c, fubk_synth20.c, generated tables *_tables.h)
esp32/tools/             make_tables20.py (pattern data -> C tables + exact model), check20.py
esp32/host/              PC test: the firmware's synthesis code == the Python model, byte for byte
ref/                     Python reference: PT8631 data loader, PAL encoder, software PAL decoder
data/fubk4x3.zip         PT8631 FuBK sample data, unchanged (read directly by the tools)
```

The generated tables are checked in, so building the firmware needs only ESP-IDF.

### Regenerating and checking the tables (optional)

```
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cd esp32
../venv/bin/python tools/make_tables20.py          # writes firmware/main/*20_tables.h and out/
../venv/bin/python tools/make_tables20.py --test-field   # flat orange test field (used by the tests)
make -C host test20                                # C synthesis == model (PAL, NTSC, 2-core split)
../venv/bin/python tools/check20.py out/model20_4frames.u8          # PAL: burst, chroma, anti-PAL
../venv/bin/python tools/check20.py --ntsc out/model20_ntsc_2frames.u8   # NTSC: bars vs EG-1
```

### Older 4×fsc build

`idf.py -DFUBK_20MHZ=0 build` selects the first design: PAL only, 17.73 MS/s from a fractional
APLL. Its clock jitter makes slowly moving patterns. It is kept for comparison; see
`esp32/README.md`.

## Credits

- **FuBK pattern data:** the FuBK test card as stored in the **ProTeleVision PT8631** test
  pattern generator board (used in the PT5230 generator; ProTeleVision Technologies, Denmark,
  formerly Philips TV test equipment). The original pattern is by ProTeleVision. The data files,
  `PT5230/PT8631/TPD/BILLEDDATA/FUBK4X3` (the `ANTIPAL` set and the plain set), are preserved
  in Karsten Hervø Hansen's [KarstenHervoeHansen/PTV](https://github.com/KarstenHervoeHansen/PTV)
  repository. `data/fubk4x3.zip` holds those files unchanged (`FUBK4X3/ANTIPAL/` and
  `FUBK4X3/`). The tools turn them into the tables in `esp32/firmware/main/fubk20_tables.h`.
- Ideas and ESP32 I2S/DAC setup:
  - [rossumur/esp_8_bit](https://github.com/rossumur/esp_8_bit)
  - [bitluni/ESP32CompositeVideo](https://github.com/bitluni/ESP32CompositeVideo)
  - [marciot/ESP32CompositeColorVideo](https://github.com/marciot/ESP32CompositeColorVideo)
  - [nmur/ESP32-TestPatternGenerator](https://github.com/nmur/ESP32-TestPatternGenerator): the
    panel layout (button, LEDs, tone) follows a box built on it.
- SMPTE EG-1 colour bars, ITU-R BT.470 / BT.1700 for the PAL and NTSC timing.

## License

The code, tools and documentation of this project are released into the public domain under
[CC0 1.0 Universal](LICENSE). You can copy, modify and use them for anything, without asking.

This does **not** cover third-party material, which stays with its owners:
- the PT8631 FuBK pattern data, both in `data/fubk4x3.zip` and in the tables generated from it
  (`esp32/firmware/main/fubk*_tables*.h`)

See [Credits](#credits) for where it comes from.
