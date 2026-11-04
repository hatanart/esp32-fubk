// Host test: run fubk_synth.c for N frames and write plain uint8 DAC codes to a file.
// With -DFUBK_I2S16 it fills buffers exactly like the firmware (2 lines per buffer, or the long
// line alone in FUBK_FRAME_LOCK mode; 16-bit samples, pairs swapped) and undoes that layout
// before writing, so the output must be the same.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define FUBK_TABLES_DATA
#ifdef FUBK_TEST_FIELD
#include "fubk_tables_test.h"
#else
#include "fubk_tables.h"
#endif
#include "fubk_synth.h"

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <out.u8> <frames>\n", argv[0]);
        return 1;
    }
    int frames = atoi(argv[2]);
    fubk_tables_t t = {fubk_line_info, fubk_rowmap, fubk_tmpl, fubk_burst,
                       fubk_row_start, fubk_delta, fubk_ypool, fubk_uvpool};
    fubk_synth_init(&t);
    FILE *f = fopen(argv[1], "wb");
    if (!f)
        return 1;
    long lines = (long)frames * FUBK_LINES;
    static uint8_t out[2 * FUBK_SPL + 12];
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
#ifdef FUBK_I2S16
    static uint16_t buf[2 * FUBK_SPL + 12] __attribute__((aligned(4)));
    for (long l = 0; l < lines;) {
        uint32_t n;
        memset(buf, 0xAA, sizeof buf);
#ifdef FUBK_LINE1136
        uint32_t n1 = fubk_synth_next_len();          // as the firmware: 2 lines, back to back
        fubk_synth_line(buf, 0);
        n = n1;
        l += 1;
        if (l < lines) {
            n += fubk_synth_next_len();
            fubk_synth_line(buf, n1);
            l += 1;
        }
#else
        if (fubk_synth_next_len() != FUBK_SPL) {      // long line: a buffer of its own
            n = fubk_synth_next_len();
            fubk_synth_line(buf, 0);
            l += 1;
        } else {
            fubk_synth_line(buf, 0);
            if (fubk_synth_next_len() != FUBK_SPL)
                return 3;                             // a pair must never contain the long line
            fubk_synth_line(buf, FUBK_SPL);
            n = 2 * FUBK_SPL;
            l += 2;
        }
#endif
        if (n % 2)
            return 4;                                 // DMA length must be a multiple of 4 bytes
        for (uint32_t i = 0; i < n; i++) {
            if (buf[i ^ FUBK_SWAP] & 0xFF)
                return 2;                             // low byte must be 0
            out[i] = (uint8_t)(buf[i ^ FUBK_SWAP] >> 8);
        }
        fwrite(out, 1, n, f);
    }
#else
    for (long l = 0; l < lines; l++) {
        uint32_t n = fubk_synth_next_len();
        memset(out, 0xAA, n);
        fubk_synth_line(out, 0);
        fwrite(out, 1, n, f);
    }
#endif
    clock_gettime(CLOCK_MONOTONIC, &t1);
    fclose(f);
    double us = ((t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec)) / 1e3;
    fprintf(stderr, "%ld lines, %.3f us per line on this host\n", lines, us / lines);
    return 0;
}
