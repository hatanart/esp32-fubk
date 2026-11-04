// FuBK PAL composite line synthesis. See fubk_synth.h and esp32/tools/make_tables.py.
//
// Chroma at 4 x fsc: the carrier samples are only 0 and +-1, so with k = (p + n) mod 4
//   k = 0: +s*V   k = 1: +U   k = 2: -s*V   k = 3: -U
// p = subcarrier phase at the line start = (global line * 1135) mod 4, s = PAL V-switch.
#include <stddef.h>
#include "fubk_synth.h"
#ifdef FUBK_TEST_FIELD
#include "fubk_tables_test.h"     // flat orange field (make_tables.py --test-field)
#else
#include "fubk_tables.h"
#endif

#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define FUBK_HOT IRAM_ATTR
#else
#define FUBK_HOT
#endif

#ifdef FUBK_I2S16
#define PUT(i, v) (buf[(i) ^ FUBK_SWAP] = (uint16_t)((uint32_t)(v) << 8))
// Store sample j relative to pointer o, which sits at logical sample index i with i mod 4 = r.
// With r known at compile time the pair swap becomes a constant offset.
#define SWOFF(r, j) ((((r) + (j)) ^ FUBK_SWAP) - (r))
#define PUTP(o, r, j, v) ((o)[SWOFF(r, j)] = (uint16_t)((uint32_t)(v) << 8))
#else
#define PUT(i, v) (buf[i] = (uint8_t)(v))
#define PUTP(o, r, j, v) ((void)(r), (o)[j] = (uint8_t)(v))
#endif

#define POS_MASK ((1u << FUBK_POS_BITS) - 1)
#define Y_MASK ((1u << FUBK_Y_BITS) - 1)
#define UV_MASK ((1u << FUBK_UV_BITS) - 1)

static fubk_tables_t T;
static uint16_t cur_y[FUBK_NB], cur_uv[FUBK_NB];     // block list of the current field line
static uint32_t frame;                                // 0..3 (full subcarrier cycle)
static uint32_t line;                                 // 1..625

void fubk_synth_init(const fubk_tables_t *t)
{
    T = *t;
    frame = 0;
    line = 1;
    for (int b = 0; b < FUBK_NB; b++)
        cur_y[b] = cur_uv[b] = 0;
}

#define TERM(k, s, u, v) ((k) == 0 ? (s) * (v) : (k) == 1 ? (u) : (k) == 2 ? -(s) * (v) : -(u))

// Active picture with constant phase p, V-switch s and start alignment r = i mod 4:
// the compiler builds one copy per combination.
static inline __attribute__((always_inline))
void active(fubk_sample_t *buf, uint32_t i, const int p, const int s, const int r)
{
    fubk_sample_t *o = buf + i;
    for (int b = 0; b < FUBK_NB; b++) {
        const uint8_t *y = T.ypool + cur_y[b] * FUBK_B;
        const int8_t *uv = T.uvpool + cur_uv[b] * (2 * FUBK_B);
        for (int j = 0; j < FUBK_B; j += 4, y += 4, uv += 8, o += 4) {
            PUTP(o, r, 0, y[0] + TERM((p + 0) & 3, s, uv[0], uv[1]));
            PUTP(o, r, 1, y[1] + TERM((p + 1) & 3, s, uv[2], uv[3]));
            PUTP(o, r, 2, y[2] + TERM((p + 2) & 3, s, uv[4], uv[5]));
            PUTP(o, r, 3, y[3] + TERM((p + 3) & 3, s, uv[6], uv[7]));
        }
    }
}

// Slow generic path: line 623 (sync pulse inside the active window, t = its template) and
// unusual buffer offsets (t = NULL).
static FUBK_HOT void active_slow(fubk_sample_t *buf, uint32_t off, int p, int s, const uint8_t *t)
{
    for (int b = 0; b < FUBK_NB; b++) {
        const uint8_t *y = T.ypool + cur_y[b] * FUBK_B;
        const int8_t *uv = T.uvpool + cur_uv[b] * (2 * FUBK_B);
        for (int j = 0; j < FUBK_B; j++) {
            int n = FUBK_A0 + b * FUBK_B + j;
            int k = (p + j) & 3;
            int v = y[j] + TERM(k, s, uv[2 * j], uv[2 * j + 1]);
            if (t)
                v += t[n] - FUBK_BLANK;
            PUT(off + n, v);
        }
    }
}

#define CASES(r)                                    \
    switch (p * 2 + si) {                           \
    case 0: active(buf, i, 0, 1, r); break;         \
    case 1: active(buf, i, 0, -1, r); break;        \
    case 2: active(buf, i, 1, 1, r); break;         \
    case 3: active(buf, i, 1, -1, r); break;        \
    case 4: active(buf, i, 2, 1, r); break;         \
    case 5: active(buf, i, 2, -1, r); break;        \
    case 6: active(buf, i, 3, 1, r); break;         \
    default: active(buf, i, 3, -1, r); break;       \
    }

// FUBK_LINE1136: 14 blanking lines (8-14, 321-327) get 4 more samples (1140), so a frame is
// 710056 samples = 3128 x 227. With the APLL fraction 41/64 the clock jitter repeats every
// 227 samples, so it is then identical in every frame (and every line still starts at phase 0).
#define PAD_LINE(l) (((l) >= 8 && (l) <= 14) || ((l) >= 321 && (l) <= 327))

uint32_t fubk_synth_next_len(void)
{
#if defined(FUBK_LINE1136)
    return FUBK_SPL + 1 + (PAD_LINE(line) ? 4 : 0);
#elif defined(FUBK_FRAME_LOCK)
    return line == FUBK_LONG_LINE ? FUBK_SPL + 1 : FUBK_SPL;
#else
    return FUBK_SPL;
#endif
}

FUBK_HOT void fubk_synth_line(fubk_sample_t *buf, uint32_t off)
{
#if defined(FUBK_LINE1136)
    // every line 1136 samples = 284 whole cycles: the same subcarrier phase at every line start
    // (like esp_8_bit). Frame = 710000 samples, every frame identical, data frame A.
    const int p = 0;
    int si = (int)((line - 1) & 1);
    uint32_t df = 0;
#elif defined(FUBK_FRAME_LOCK)
    // every frame identical: 709376 samples (line FUBK_LONG_LINE has one extra), data frame A
    int p = (int)(((line - 1) * 3 + (line > FUBK_LONG_LINE ? 1 : 0)) & 3);
    int si = (int)((line - 1) & 1);
    uint32_t df = 0;
#else
    uint32_t g = frame * FUBK_LINES + line - 1;
    int p = (int)((g * 3) & 3);                       // = (g * 1135) mod 4
    int si = (int)(g & 1);                            // V-switch: 0 -> +1, 1 -> -1
    uint32_t df = frame & 1;
#endif
    uint8_t info = T.line_info[line - 1];
    const uint8_t *t = T.tmpl + (info & 0x7F) * FUBK_SPL;
    uint16_t row = T.rowmap[line - 1];

    // sync, blanking (+ burst)
    for (int n = 0; n < FUBK_A0; n++)
        PUT(off + n, t[n]);
    for (int n = FUBK_A0 + FUBK_NA; n < FUBK_SPL; n++)
        PUT(off + n, t[n]);
    if (info & 0x80) {
        const int8_t *bs = T.burst + (p * 2 + si) * FUBK_BWN;
        for (int j = 0; j < FUBK_BWN; j++)
            PUT(off + FUBK_BW0 + j, t[FUBK_BW0 + j] + bs[j]);
    }

    if (row == 0xFFFF) {
        for (int n = FUBK_A0; n < FUBK_A0 + FUBK_NA; n++)
            PUT(off + n, t[n]);
    } else {
        // apply this row's block changes
        uint32_t idx = df * (FUBK_ROWS / 2) + row;
        for (uint32_t e = T.row_start[idx]; e < T.row_start[idx + 1]; e++) {
            uint32_t d = T.delta[e];
            uint32_t pos = d & POS_MASK;
            cur_y[pos] = (uint16_t)((d >> FUBK_POS_BITS) & Y_MASK);
            cur_uv[pos] = (uint16_t)((d >> (FUBK_POS_BITS + FUBK_Y_BITS)) & UV_MASK);
        }
        uint32_t i = off + FUBK_A0;
        if (line == 623) {
            active_slow(buf, off, p, si ? -1 : 1, t);
        }
#ifdef FUBK_I2S16
        // fast paths for the two line starts in a 2-line buffer: off = 0 and off = 1135
        // (FUBK_LINE1136: off = 0 and 1136, both take the first path)
        else if ((i & 3) == (FUBK_A0 & 3)) {
            CASES(FUBK_A0 & 3)
        } else if ((i & 3) == ((FUBK_SPL + FUBK_A0) & 3)) {
            CASES((FUBK_SPL + FUBK_A0) & 3)
        } else {
            active_slow(buf, off, p, si ? -1 : 1, NULL);
        }
#else
        else {
            CASES(0)
        }
#endif
    }

#if defined(FUBK_LINE1136)
    PUT(off + FUBK_SPL, FUBK_BLANK);                  // the 1136th sample
    if (PAD_LINE(line))
        for (int n = 1; n <= 4; n++)
            PUT(off + FUBK_SPL + n, FUBK_BLANK);
#elif defined(FUBK_FRAME_LOCK)
    if (line == FUBK_LONG_LINE)
        PUT(off + FUBK_SPL, FUBK_BLANK);              // the extra sample
#endif
    if (++line > FUBK_LINES) {
        line = 1;
        frame = (frame + 1) & 3;
    }
}
