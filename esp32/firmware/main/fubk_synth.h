// FuBK PAL composite line synthesis at 4 x fsc (17.734475 MS/s, 1135 samples per line).
// Portable C: the same file runs in the ESP32 firmware and in the host test.
//
// Build options:
//   FUBK_I2S16      output uint16_t samples with the code in the high byte (ESP32 I2S DAC mode)
//                   instead of plain uint8_t codes
//   FUBK_SWAP=0/1   with FUBK_I2S16: swap each pair of 16-bit samples (ESP32 I2S order, default 1)
//   FUBK_FRAME_LOCK every frame identical: line FUBK_LONG_LINE gets 1136 samples, so a frame is a
//                   whole number of subcarrier cycles; V-switch and data (frame A) repeat per frame.
//                   Not the standard 4-frame PAL sequence, but a static picture and scope image.
#pragma once
// FUBK_LINE1136: every line 1136 samples (284 whole subcarrier cycles), same phase on every line,
//   as esp_8_bit does. Not standard PAL (line 64.056 us), but it pins any clock-jitter pattern to
//   the same place on every line. fubk_synth_next_len() is then always 1136.
#include <stdint.h>

#ifndef FUBK_SWAP
#define FUBK_SWAP 1
#endif

#ifdef FUBK_I2S16
typedef uint16_t fubk_sample_t;
#else
typedef uint8_t fubk_sample_t;
#endif

// Pointers to the tables from fubk_tables.h (the firmware copies them to internal RAM first).
typedef struct {
    const uint8_t *line_info;
    const uint16_t *rowmap;
    const uint8_t *tmpl;
    const int8_t *burst;
    const uint16_t *row_start;
    const uint32_t *delta;
    const uint8_t *ypool;
    const int8_t *uvpool;
} fubk_tables_t;

// Reset to frame 0, line 1.
void fubk_synth_init(const fubk_tables_t *t);

// Write the next line (1135 samples) to buf[off .. off + 1134]. Lines must be generated strictly
// in order: picture rows are coded as changes against the previous line of the same field.
// `off` is counted in samples from a 4-byte aligned buffer start (the swap pairs are relative to it).
void fubk_synth_line(fubk_sample_t *buf, uint32_t off);

// Number of samples the next fubk_synth_line() call writes: 1135, or 1136 for the long line
// in FUBK_FRAME_LOCK mode.
uint32_t fubk_synth_next_len(void);
