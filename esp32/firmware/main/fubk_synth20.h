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
#include "vbi20.h"

#ifndef FUBK_SWAP
#define FUBK_SWAP 1
#endif

#ifdef FUBK_I2S16
typedef uint16_t fubk_sample_t;
#else
typedef uint8_t fubk_sample_t;
#endif

#define F20_MAX_NB 34                    // blocks of 32 samples per line (NTSC 34, PAL 33)
#define F20_MAX_SPL 1282                 // longest line (PAL image page long lines: 1282)
#define FUBK20_NPAT 17                   // patterns per standard (the pattern button's list)
#define FUBK20_PAL_MAIN 0                // position of the built-in card: PAL 0 = FuBK (1 = PM5644),
#define FUBK20_NTSC_MAIN 1               // NTSC 1 = SMPTE bars (0 = PM5644)
#define FUBK20_IMG_W 320                 // image pages: pixels per line (4 pixels = 13 samples)
#define FUBK20_IMG_MAX_ROWS 288          // image pages: rows (PAL 288, NTSC 240)

// Text in the test cards (PM5644 upper / lower box, FuBK name box): a line of n PM8503
// characters (9 x 18 dots, a dot = 80/27 samples x 2 frame rows) from data row row0 on, starting
// at sample x0 (nsamp samples incl. the edges), showing text line `text`: 0 = PM5644 upper
// (8 characters), 1 = PM5644 lower (12), 2 = FuBK (13).
#define FUBK20_TEXT_MAX 13               // characters per text line
#define FUBK20_NTEXTS 3
// Only rows ylo..yhi (the box) are drawn. Samples cx0..cx1 - 1, where the box is not black (FuBK:
// the centre line), are covered by a character's whole cell; elsewhere only the letters are
// drawn. A half space (FUBK20_HALF) in the last place moves the line half a character right.
typedef struct { uint16_t row0, x0, nsamp, ylo, yhi, cx0, cx1; uint8_t n, text; } fubk20_textbox_t;
// the text (glyph indices, see text20_tables.h), the same in both standards; the edit mode's
// cursor (-1 = none, else a position: 0..7 upper, 8..19 lower, 20..32 FuBK) is drawn inverted
// while fubk20_cursor_on is set
extern uint8_t fubk20_text[FUBK20_NTEXTS][FUBK20_TEXT_MAX];
extern volatile int8_t fubk20_cursor;
extern volatile uint8_t fubk20_cursor_on;

// image page palette entry: sample = (y + a * car[(ms + k[vs]) % car_n + i]) >> 20, i.e.
// Y + A sin(carrier + hue): y = (Y + blanking) << 20 with the rounding, a = A << 6, k = the
// carrier table offset for the hue on +V / -V lines (make_tables20.py, palette_entries)
typedef struct { int32_t y; int16_t a; uint16_t k[2]; } fubk20_pal_t;

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
    uint16_t burst_ofs;                  // burst carrier index offset (PAL: rotated, see make_tables20.py)
    uint8_t vbi;                         // data lines: VBI20_TTX (PAL), VBI20_CC (NTSC), 0 = none
    // image pages (non-interlaced 256 colours), else 0: per row its FUBK20_IMG_W palette indices
    const uint8_t *const *img_rows;
    const fubk20_pal_t *img_pal;
    uint16_t img_x0;                     // sample of the first pixel
    uint16_t short_line;                 // this line is short_by samples shorter (0 = none; the
    uint8_t short_by;                    // NTSC non-interlaced page: whole subcarrier cycles per field)
    uint8_t ext_every, ext_n;            // lines ext_every, 2 ext_every, .. ext_n ext_every are 2
                                         // samples longer (0 = none; PAL non-interlaced page)
    uint8_t ph_reset;                    // subcarrier phase back to 0 at every field start
    const fubk20_textbox_t *text;        // text lines on this card (in RAM), ntext of them
    uint8_t ntext, txt_black;            // txt_black: black level of its text boxes (DAC steps)
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
    uint32_t vbi_on;                     // 1: teletext / captions on (fubk20_init: off)
    uint16_t cur_y[F20_MAX_NB], cur_uv[F20_MAX_NB];
    uint8_t vbi_buf[F20_MAX_SPL];        // a data line, before it goes to the buffer
} fubk20_state_t;

void fubk20_init(fubk20_state_t *st, const fubk20_std_t *s);
// samples in the next line: spl, or spl + 1 for NTSC long lines (exact 227.5 cycles on average)
static inline uint32_t fubk20_next_len(const fubk20_state_t *st)
{
    const fubk20_std_t *s = st->s;
    return s->spl + (s->long_period && st->g / 2 == s->long_period - 1u)
         - (st->line == s->short_line ? s->short_by : 0u)
         + 2u * (s->ext_every && st->line % s->ext_every == 0 && st->line / s->ext_every <= s->ext_n);
}
#ifdef FUBK_BURST_TEST
// PAL burst rotation test: extra burst carrier index offset, (steps * car_inv) % car_n for
// steps * 360 / car_n degrees on top of the standard's burst_ofs. Set by main20.c.
extern uint32_t fubk20_burst_ofs;
#endif
// render the next line at buf[off .. off + fubk20_next_len() - 1] (fast path for even off)
void fubk20_line(fubk20_state_t *st, fubk_sample_t *buf, uint32_t off);
// advance past the next line without rendering it (keeps the block list and phase up to date)
void fubk20_skip(fubk20_state_t *st);
// The patterns other than the built-in card live in flash. Rendering from flash stalls on cache
// misses (worst right after a switch), so the firmware copies the pattern it shows into RAM:
// fubk20_pattern_bytes() is the RAM it needs (0 for the built-in card, whose tables are in RAM
// already). fubk20_pattern_load() copies the tables of pattern k of pats (fubk20_pal_pat or
// fubk20_ntsc_pat), largest first, each to place(table, bytes, ctx) (4-byte aligned; table is
// the index into fubk20_pattern_tables()), and fills *dst; a table for which place() returns 0
// stays in flash. Returns the bytes left in flash. fubk20_pattern_tables() gives the RAM size of
// each of its FUBK20_NTABLES tables (0 = none).
#define FUBK20_NTABLES 9
typedef void *(*fubk20_place_t)(uint32_t table, uint32_t bytes, void *ctx);
uint32_t fubk20_pattern_bytes(const fubk20_std_t *pats, uint32_t k);
void fubk20_pattern_tables(const fubk20_std_t *pats, uint32_t k, uint32_t sz[FUBK20_NTABLES]);
uint32_t fubk20_pattern_load(const fubk20_std_t *pats, uint32_t k, fubk20_place_t place, void *ctx,
                             fubk20_std_t *dst);
// Image pages (img_rows != 0): fubk20_pattern_load() copies the palette and the line timing, not
// the image; dst->img_rows still points to the rows in flash. The caller copies the rows it wants
// in RAM (FUBK20_IMG_W bytes each, any addresses) and points dst->img_rows to its own row list.
// switch to pattern s (one of the same standard's patterns) when frame `frame` starts;
// timing, subcarrier phase and V-switch run on unchanged. A newer request replaces an older one.
static inline void fubk20_request(fubk20_state_t *st, const fubk20_std_t *s, uint32_t frame)
{
    st->pend = s;
    st->pend_frame = frame;
}
