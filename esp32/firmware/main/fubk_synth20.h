// Composite video synthesis at 20 MS/s (jitter-free integer APLL), PAL or NTSC, chosen at run time.
//   PAL : FuBK test card, 1280 samples per line (64.000 us), 625 lines, fsc 4433618.75 Hz
//   NTSC: SMPTE colour bars, 1271 samples per line (63.55 us), 525 lines, fsc 3579545.45 Hz
// See esp32/tools/make_tables20.py.
//
// Every state produces lines strictly in order; two states can split the work (one renders,
// the other skips) so that two cores share it.
// FUBK_I2S16: 16-bit samples, code in the high byte, sample pairs swapped (FUBK_SWAP, default 1).
#pragma once
#include <stdint.h>

#ifndef FUBK_SWAP
#define FUBK_SWAP 1
#endif

#ifdef FUBK_I2S16
typedef uint16_t fubk_sample_t;
#else
typedef uint8_t fubk_sample_t;
#endif

#define F20_MAX_NB 34                    // blocks of 32 samples per line (NTSC 34, PAL 33)
#define F20_MAX_SPL 1280                 // longest line (NTSC long lines: 1272)
#define FUBK20_NPAT 14                   // patterns per standard: 0 = FuBK / SMPTE, 1.. extra

typedef struct {
    const char *name;
    uint16_t spl, lines, a0, nb, bw0, bwn, rows;
    uint16_t car_n, car_step, car_inv;
    uint32_t ph_num, ph_den;             // fsc / fs = ph_num / ph_den cycles per sample
    uint8_t vswitch, data_frames, pos_bits, y_bits;
    const uint8_t *tmpl, *line_info, *ypool;
    const uint16_t *rowmap, *row_start;
    const uint32_t *delta;
    const int8_t *uvpool;
    const int16_t *car, *bu, *bv;
    const uint8_t *flat;                 // per UV block 0, or 1 + its flat colour in wtab
    const int8_t *wtab;                  // flat-colour chroma, wlen entries per waveform, index
                                         // n + ms; PAL: 2 per colour (+V, -V line)
    uint16_t wlen;
    uint16_t long_period;                // 0 (off); n: every n-th line pair is 1 sample longer per line
} fubk20_std_t;

extern const fubk20_std_t fubk20_pal, fubk20_ntsc;
// all patterns of a standard (same timing); [0] is the same test card as fubk20_pal / _ntsc
extern const fubk20_std_t fubk20_pal_pat[FUBK20_NPAT], fubk20_ntsc_pat[FUBK20_NPAT];

typedef struct {
    const fubk20_std_t *s;
    uint32_t line;                       // 1..lines, the next line
    uint32_t frame;                      // counts frames (data frame = frame % data_frames)
    uint32_t acc;                        // subcarrier phase numerator at the next line's sample 0
    uint32_t vs;                         // PAL V-switch of the next line
    uint32_t g;                          // line counter mod 2 * long_period (line lengths)
    const fubk20_std_t *pend;            // pattern to switch to at the start of frame pend_frame
    uint32_t pend_frame;
    uint16_t cur_y[F20_MAX_NB], cur_uv[F20_MAX_NB];
} fubk20_state_t;

void fubk20_init(fubk20_state_t *st, const fubk20_std_t *s);
// samples in the next line: spl, or spl + 1 for NTSC long lines (exact 227.5 cycles on average)
static inline uint32_t fubk20_next_len(const fubk20_state_t *st)
{
    const fubk20_std_t *s = st->s;
    return s->spl + (s->long_period && st->g / 2 == s->long_period - 1u);
}
// render the next line at buf[off .. off + fubk20_next_len() - 1] (fast path for even off)
void fubk20_line(fubk20_state_t *st, fubk_sample_t *buf, uint32_t off);
// advance past the next line without rendering it (keeps the block list and phase up to date)
void fubk20_skip(fubk20_state_t *st);
// The extra patterns live in flash. Rendering from flash costs cache misses (worst right after a
// switch), so the firmware copies the pattern it shows into RAM: fubk20_pattern_bytes() is the
// RAM it needs (0 for pattern 0, whose tables are in RAM already), fubk20_pattern_load() copies
// pattern k of pats (fubk20_pal_pat or fubk20_ntsc_pat) to ram (4-byte aligned) and fills *dst.
uint32_t fubk20_pattern_bytes(const fubk20_std_t *pats, uint32_t k);
void fubk20_pattern_load(const fubk20_std_t *pats, uint32_t k, void *ram, fubk20_std_t *dst);
// switch to pattern s (one of the same standard's patterns) when frame `frame` starts;
// timing, subcarrier phase and V-switch run on unchanged. A newer request replaces an older one.
static inline void fubk20_request(fubk20_state_t *st, const fubk20_std_t *s, uint32_t frame)
{
    st->pend = s;
    st->pend_frame = frame;
}
