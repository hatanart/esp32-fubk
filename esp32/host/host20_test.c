// Host test for fubk_synth20.c: write N frames of plain uint8 codes.
//   host20 <out.u8> <frames> [split] [ntsc] [patterns] [vbi] [first frame]
// patterns: comma list, frame f shows pattern list[min(f, len - 1)] (default "0"). The switch is
// requested before every buffer for the next frame start, as the firmware's pattern button does.
// split = 1: render like the dual-core firmware (main20.c): 2 lines per buffer, state 0 renders
// the even buffers, state 1 the odd ones; after rendering its buffer each state skips the next
// one, and state 1 starts by skipping buffer 0.
// With -DFUBK_I2S16 the buffers use the firmware layout (16-bit, pairs swapped) and are
// unpacked before writing, so the output must be the same in every mode.
// vbi = 1: teletext / captions on. first frame: the frame counter's start (data clocks).
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "fubk_synth20.h"

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: %s <out.u8> <frames> [split] [ntsc] [patterns] [vbi] [first frame]\n", argv[0]);
        return 1;
    }
    int split = argc > 3 && atoi(argv[3]);
    const fubk20_std_t *s = (argc > 4 && atoi(argv[4])) ? &fubk20_ntsc : &fubk20_pal;
    const fubk20_std_t *pats = s == &fubk20_ntsc ? fubk20_ntsc_pat : fubk20_pal_pat;
    int seq[64], nseq = 0;
    for (const char *c = argc > 5 ? argv[5] : "0"; *c && nseq < 64; ) {
        int k = atoi(c);
        if (k < 0 || k >= FUBK20_NPAT)
            return 4;
        seq[nseq++] = k;
        while (*c && *c != ',')
            c++;
        if (*c == ',')
            c++;
    }
    // as the firmware: render the extra patterns from RAM copies (checks fubk20_pattern_load)
    static fubk20_std_t ram[FUBK20_NPAT];
    const fubk20_std_t *use[FUBK20_NPAT];
    use[0] = s;
    for (int k = 1; k < FUBK20_NPAT; k++) {
        void *m = malloc(fubk20_pattern_bytes(pats, k));
        if (!m)
            return 5;
        memset(m, 0x5A, fubk20_pattern_bytes(pats, k));
        fubk20_pattern_load(pats, k, m, &ram[k]);
        use[k] = &ram[k];
    }
    s = use[seq[0]];
    long lines = atol(argv[2]) * s->lines;
    FILE *f = fopen(argv[1], "wb");
    if (!f)
        return 1;
    fubk20_state_t st[2];
    fubk20_init(&st[0], s);
    fubk20_init(&st[1], s);
    for (int w = 0; w < 2; w++) {
        st[w].vbi_on = argc > 6 && atoi(argv[6]);
        st[w].frame = argc > 7 ? (uint32_t)strtoul(argv[7], 0, 0) : 0;
    }
    if (split) {                                      // as main20.c: state 1 starts at buffer 1
        fubk20_skip(&st[1]);
        fubk20_skip(&st[1]);
    }
    static fubk_sample_t buf[2 * F20_MAX_SPL + 4] __attribute__((aligned(4)));
    static uint8_t out[2 * F20_MAX_SPL + 4];
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    for (long l = 0, k = 0; l < lines; l += 2, k++) {
        memset(buf, 0xAA, sizeof buf);
        int w = split ? (int)(k & 1) : 0;
        uint32_t nf = st[w].frame + 1;                // next frame start: its pattern
        fubk20_request(&st[w], use[seq[nf < (uint32_t)nseq ? nf : (uint32_t)nseq - 1]], nf);
        uint32_t n1 = fubk20_next_len(&st[w]);
        fubk20_line(&st[w], buf, 0);
        uint32_t n2 = fubk20_next_len(&st[w]);
        fubk20_line(&st[w], buf, n1);
        if (split) {                                  // as main20.c fill(): skip the next pair
            fubk20_skip(&st[w]);
            fubk20_skip(&st[w]);
        }
        if ((n1 + n2) % 2)
            return 3;                                 // a buffer must be whole 32-bit words
        for (uint32_t i = 0; i < n1 + n2; i++) {
#ifdef FUBK_I2S16
            if (buf[i ^ FUBK_SWAP] & 0xFF)
                return 2;
            out[i] = (uint8_t)(buf[i ^ FUBK_SWAP] >> 8);
#else
            out[i] = buf[i];
#endif
        }
        fwrite(out, 1, l + 2 <= lines ? n1 + n2 : n1, f);
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    fclose(f);
    double us = ((t1.tv_sec - t0.tv_sec) * 1e9 + (t1.tv_nsec - t0.tv_nsec)) / 1e3;
    fprintf(stderr, "%s: %ld lines, %.3f us per line on this host\n", s->name, lines, us / lines);
    return 0;
}
