// Composite line synthesis at 20 MS/s. See fubk_synth20.h and make_tables20.py.
//
//   sample = Y + (U * car[n + ms] + V * car[n + mc] + 2^13) >> 14
// car[k] = 2^14 sin(2 pi STEP k / N). ms and mc give the line its subcarrier phase from a
// free-running sample counter (exact at mid-line); for PAL mc also carries the V-switch.
// Blocks without colour skip the multiplies; NTSC flat-colour blocks read a precomputed
// chroma waveform instead.
//
// Speed notes: all table parameters are copied into locals first (stores into the 16-bit sample
// buffer could otherwise alias the 16-bit fields of the standard and force reloads), and both
// line alignments write with constant offsets (no per-sample index xor).
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define FUBK_TABLE_ATTR DRAM_ATTR        // internal RAM: flash cache misses would be too slow
#define FUBK_HOT IRAM_ATTR
#else
#define FUBK_TABLE_ATTR
#define FUBK_HOT
#endif
#define FUBK_PAT_ATTR                    // extra patterns: flash (simple pictures, cache-friendly)
#include <string.h>
#include "fubk_synth20.h"
#define FUBK20_TABLES_DATA
#ifdef FUBK_TEST_FIELD
#include "fubk20_tables_test.h"          // PAL: flat orange field
#else
#include "fubk20_tables.h"               // PAL: FuBK
#endif
#include "ntsc20_tables.h"               // NTSC: SMPTE bars
#include "text20_tables.h"               // text: PM8503 font, edge shaping, text boxes
#include "patterns20_tables.h"            // extra patterns, both standards

#define BLANK 64
#define F20_VBI VBI20_TTX                // PAL: teletext
#define N20_VBI VBI20_CC                 // NTSC: closed captions
#define B 32
#define CAR_Q 14
#define BURST_Q 6
#define RND (1 << (CAR_Q - 1))

_Static_assert(F20_NB <= F20_MAX_NB && N20_NB <= F20_MAX_NB, "too many blocks");
_Static_assert(F20_SPL <= F20_MAX_SPL && N20_SPL <= F20_MAX_SPL, "line too long");
// 32-bit phase arithmetic (see fubk20_line): den a multiple of N, no overflow
_Static_assert(F20_PH_DEN % F20_CAR_N == 0 && N20_PH_DEN % N20_CAR_N == 0, "phase grid");
_Static_assert((uint64_t)(F20_SPL + 2) * F20_PH_NUM + F20_PH_DEN < (1ull << 32), "phase overflow");
_Static_assert((uint64_t)(N20_SPL + 1) * N20_PH_NUM + N20_PH_DEN < (1ull << 32), "phase overflow");

#define STD(p, P, nm) {                                                                  \
    nm, P##_SPL, P##_LINES, P##_A0, P##_NB, P##_BW0, P##_BWN, P##_ROWS,                  \
    P##_CAR_N, P##_CAR_STEP, P##_CAR_INV, P##_PH_NUM, P##_PH_DEN,                        \
    P##_VSWITCH, P##_DATA_FRAMES, P##_POS_BITS, P##_Y_BITS,                              \
    p##_tmpl, p##_line_info, p##_ypool, p##_rowmap, p##_row_start, p##_delta, p##_uvpool, \
    p##_car, p##_bu, p##_bv, p##_flat, p##_wtab, P##_WLEN, P##_LONG_PERIOD, P##_BURST_OFS, P##_VBI, 0, 0, P##_IMG_X0, 0, 0, 0, 0, 0,    \
    P##_TEXT, P##_NTEXT, P##_TXT_BLACK }
const fubk20_std_t fubk20_pal = STD(f20, F20, "PAL");
const fubk20_std_t fubk20_ntsc = STD(n20, N20, "NTSC");

// an extra pattern: the standard's sync, burst and carrier with its own picture tables; the line
// timing is the standard's (lines = P##_LINES, line_info = p##_line_info, ...) or its own
#define PAT(p, P, nm, pb, yb, ypool, row_start, delta, uvpool, flat, wtab,                  \
            lines, rows, line_info, rowmap, vbi, img_rows, img_pal, short_line, short_by,    \
            ext_every, ext_n, ph_reset, text, ntext) {                                    \
    nm, P##_SPL, lines, P##_A0, P##_NB, P##_BW0, P##_BWN, rows,                          \
    P##_CAR_N, P##_CAR_STEP, P##_CAR_INV, P##_PH_NUM, P##_PH_DEN,                        \
    P##_VSWITCH, 1, pb, yb,                                                              \
    p##_tmpl, line_info, ypool, rowmap, row_start, delta, uvpool,                        \
    p##_car, p##_bu, p##_bv, flat, wtab, P##_WLEN, P##_LONG_PERIOD, P##_BURST_OFS, vbi,  \
    img_rows, img_pal, P##_IMG_X0, short_line, short_by, ext_every, ext_n, ph_reset,       \
    text, ntext, P##_TXT_BLACK }
_Static_assert(PATTERNS20_COUNT == FUBK20_NPAT, "FUBK20_NPAT does not match patterns20_tables.h");
_Static_assert(F20_PATTERNS_MAIN == FUBK20_PAL_MAIN && N20_PATTERNS_MAIN == FUBK20_NTSC_MAIN, "main card position");
const fubk20_std_t fubk20_pal_pat[FUBK20_NPAT] = { F20_PATTERNS(STD(f20, F20, "PAL FuBK")) };
const fubk20_std_t fubk20_ntsc_pat[FUBK20_NPAT] = { N20_PATTERNS(STD(n20, N20, "NTSC SMPTE bars")) };

typedef struct { uint32_t row_start, delta, ypool, uvpool, flat, wtab, line_info, rowmap, img_pal; } pat_size_t;
static const pat_size_t pal_size[FUBK20_NPAT] = { F20_PATTERNS_SIZES };
static const pat_size_t ntsc_size[FUBK20_NPAT] = { N20_PATTERNS_SIZES };

#ifdef FUBK_BURST_TEST
uint32_t fubk20_burst_ofs;
#endif

#define AL4(n) (((n) + 3u) & ~3u)

uint8_t fubk20_text[FUBK20_NTEXTS][FUBK20_TEXT_MAX] = FUBK20_TEXT_DEFAULT;
volatile int8_t fubk20_cursor = -1;
volatile uint8_t fubk20_cursor_on;

// 16 x 16 -> 32 bit signed multiply. On the ESP32 as one mul16s: GCC would otherwise use the
// MAC16 accumulator for y + a * c (wsr / mula / rsr per sample: about twice as slow).
static inline __attribute__((always_inline)) int32_t mul16(int32_t a, int32_t b)
{
#ifdef __XTENSA__
    int32_t p;
    __asm__("mul16s %0, %1, %2" : "=r"(p) : "r"(a), "r"(b));
    return p;
#else
    return (int16_t)a * (int16_t)b;
#endif
}

uint32_t fubk20_pattern_bytes(const fubk20_std_t *pats, uint32_t k)
{
    const pat_size_t *z = &(pats == fubk20_ntsc_pat ? ntsc_size : pal_size)[k];
    return AL4(z->row_start) + AL4(z->delta) + AL4(z->ypool) + AL4(z->uvpool) + AL4(z->flat) + AL4(z->wtab)
         + AL4(z->line_info) + AL4(z->rowmap) + AL4(z->img_pal);
}

void fubk20_pattern_tables(const fubk20_std_t *pats, uint32_t k, uint32_t sz[FUBK20_NTABLES])
{
    const pat_size_t *z = &(pats == fubk20_ntsc_pat ? ntsc_size : pal_size)[k];
    const uint32_t t[FUBK20_NTABLES] = { z->row_start, z->delta, z->ypool, z->uvpool, z->flat, z->wtab,
                                         z->line_info, z->rowmap, z->img_pal };
    for (int i = 0; i < FUBK20_NTABLES; i++)
        sz[i] = AL4(t[i]);
}

uint32_t fubk20_pattern_load(const fubk20_std_t *pats, uint32_t k, fubk20_place_t place, void *ctx,
                             fubk20_std_t *dst)
{
    const fubk20_std_t *s = &pats[k];
    const pat_size_t *z = &(pats == fubk20_ntsc_pat ? ntsc_size : pal_size)[k];
    *dst = *s;
    // largest tables first, so they get the large free spaces
    enum { NT = FUBK20_NTABLES };
    const uint32_t sz[NT] = { z->row_start, z->delta, z->ypool, z->uvpool, z->flat, z->wtab,
                              z->line_info, z->rowmap, z->img_pal };
    const void *const src[NT] = { s->row_start, s->delta, s->ypool, s->uvpool, s->flat, s->wtab,
                                  s->line_info, s->rowmap, s->img_pal };
    const void *ram[NT] = { 0 };
    uint32_t done = 0, missed = 0;
    for (int n = 0; n < NT; n++) {
        int b = -1;
        for (int i = 0; i < NT; i++)
            if (sz[i] && !(done >> i & 1) && (b < 0 || sz[i] > sz[b]))
                b = i;
        if (b < 0)
            break;
        done |= 1u << b;
        void *m = place((uint32_t)b, AL4(sz[b]), ctx);
        if (m)
            ram[b] = memcpy(m, src[b], sz[b]);
        else
            missed += sz[b];
    }
#define SET(i, f) if (ram[i]) dst->f = (__typeof__(dst->f))ram[i];
    SET(0, row_start) SET(1, delta) SET(2, ypool) SET(3, uvpool) SET(4, flat) SET(5, wtab)
    SET(6, line_info) SET(7, rowmap) SET(8, img_pal)
#undef SET
    return missed;
}

#ifdef FUBK_I2S16
// The DAC takes the high byte of each 16-bit sample; with FUBK_SWAP the two samples of each
// 32-bit word are swapped: logical sample i sits at buf[i ^ 1].
#define PUT(i, v) (buf[(i) ^ FUBK_SWAP] = (uint16_t)((uint32_t)(v) << 8))
typedef uint32_t __attribute__((may_alias)) pair_t;
#define PAIR(v0, v1) (FUBK_SWAP ? ((uint32_t)(v1) << 8 | (uint32_t)(v0) << 24) \
                                : ((uint32_t)(v0) << 8 | (uint32_t)(v1) << 24))
#define W16(v) ((uint16_t)((uint32_t)(v) << 8))
// with p = buf + i (i odd), samples j (even) and j + 1 go to these offsets from p
#define ODD0(j) ((j) - FUBK_SWAP)
#define ODD1(j) ((j) + 1 + FUBK_SWAP)
#else
#define PUT(i, v) (buf[i] = (uint8_t)(v))
typedef uint16_t __attribute__((may_alias)) pair_t;
#define PAIR(v0, v1) ((uint16_t)(((uint32_t)(v0) & 0xFF) | ((uint32_t)(v1) & 0xFF) << 8))
#define W16(v) ((uint8_t)(v))
#define ODD0(j) (j)
#define ODD1(j) ((j) + 1)
#endif

void fubk20_init(fubk20_state_t *st, const fubk20_std_t *s)
{
    st->s = s;
    st->line = 1;
    st->frame = 0;
    st->acc = 0;
    st->vs = 0;
    st->g = 0;
    st->pend = 0;
    st->pend_frame = 0;
    st->vbi_on = 0;
    for (int b = 0; b < F20_MAX_NB; b++)
        st->cur_y[b] = st->cur_uv[b] = 0;
}

// apply the block changes of the next line (if it has picture); returns its data row or 0xFFFF
static FUBK_HOT uint32_t apply_row(fubk20_state_t *st)
{
    const fubk20_std_t *s = st->s;
    uint32_t row = s->rowmap[st->line - 1];
    if (row != 0xFFFF && !s->img_rows) {
        uint32_t idx = (st->frame % s->data_frames) * s->rows + row;
        uint32_t pb = s->pos_bits, yb = s->y_bits;
        uint32_t pmask = (1u << pb) - 1, ymask = (1u << yb) - 1;
        const uint32_t *d = s->delta;
        for (uint32_t e = s->row_start[idx], e1 = s->row_start[idx + 1]; e < e1; e++) {
            uint32_t v = d[e];
            uint32_t pos = v & pmask;
            st->cur_y[pos] = (uint16_t)((v >> pb) & ymask);
            st->cur_uv[pos] = (uint16_t)(v >> (pb + yb));
        }
    }
    return row;
}

static inline void advance(fubk20_state_t *st)
{
    const fubk20_std_t *s = st->s;
    uint32_t len = fubk20_next_len(st);
    st->acc = (st->acc + len * s->ph_num) % s->ph_den;        // fits 32 bits (static assert)
    st->vs ^= s->vswitch;
    if (s->long_period && ++st->g == 2u * s->long_period)
        st->g = 0;
    if (++st->line > s->lines) {
        st->line = 1;
        st->frame++;
        if (st->pend && st->frame == st->pend_frame) {   // pattern switch: the first rows of a
            st->s = st->pend;                            // field list all blocks, so the block
            st->pend = 0;                                // list needs no reset
        }
        if (st->s->ph_reset)                             // PAL image page: same phase every field
            st->acc = 0;
    }
}

FUBK_HOT void fubk20_skip(fubk20_state_t *st)
{
    apply_row(st);
    advance(st);
}

// copy template samples [n0, n1) (n0 even) of a line starting at buffer offset off
static inline __attribute__((always_inline))
void copy_tmpl(fubk_sample_t *buf, uint32_t off, const uint8_t *t, uint32_t n0, uint32_t n1)
{
    uint32_t n = n0;
    if (!(off & 1)) {
        pair_t *o = (pair_t *)(buf + off + n0);
        for (; n + 1 < n1; n += 2)
            *o++ = PAIR(t[n], t[n + 1]);
    } else {
        fubk_sample_t *p = buf + off + n0;
        for (; n + 1 < n1; n += 2, p += 2) {
            p[ODD0(0)] = W16(t[n]);
            p[ODD1(0)] = W16(t[n + 1]);
        }
    }
    if (n < n1)
        PUT(off + n, t[n]);
}

// the text lines of this card on data row `row`: each sample from its position in the dot and
// the dot before / after (text_lut: 150 ns edges). A character's cell is drawn whole, black
// included; a space keeps what is there (FuBK: the centre line shows through a space in the
// middle). Works dot by dot: a black dot with black neighbours outside a character is skipped
// in one step; a dot covers 2 or 3 samples (80/27).
// (not inlined: inside fubk20_line it ran out of registers and spilled several per sample)
static FUBK_HOT __attribute__((noinline)) void text_overlay(const fubk20_std_t *s, uint32_t row,
                                                            fubk_sample_t *buf, uint32_t off)
{
    for (uint32_t t = 0; t < s->ntext; t++) {
        const fubk20_textbox_t *b = &s->text[t];
        if (row < b->row0 || row >= b->row0 + (uint32_t)FUBK20_TEXT_ROWS || row < b->ylo || row > b->yhi)
            continue;
        const uint32_t dr = (row - b->row0) >> 1, base = b->text == 0 ? 0 : b->text == 1 ? 8 : 20;
        const int32_t cur = fubk20_cursor_on ? fubk20_cursor : -1;
        // the dots of this row, 1 bit each, MSB first; 32-bit words
        uint32_t w[(FUBK20_TEXT_MAX * 9 + 7 + 31) / 32] = { 0 };
        uint32_t ow[(FUBK20_TEXT_MAX * 9 + 7 + 31) / 32] = { 0 };    // opaque: character cells
        // half space in the last place: the line starts 4.5 dots later (5 empty dots, from 1/2)
        const uint32_t sh = fubk20_text[b->text][b->n - 1] == FUBK20_HALF;
        uint32_t nd = sh ? 5 : 0;
#define PUT9(a, bits) do {                                                                \
            uint32_t _b = (bits), _s = nd & 31;                                           \
            a[nd >> 5] |= _s + 9 <= 32 ? _b << (23 - _s) : _b >> (_s - 23);               \
            if (_s + 9 > 32)                                                              \
                a[(nd >> 5) + 1] |= _b << (55 - _s);                                      \
        } while (0)
        for (uint32_t c = 0; c < b->n; c++) {
            uint32_t g = fubk20_text[b->text][c];
            uint32_t bits = fubk20_font[(g < FUBK20_NGLYPH ? g : 0) * 18 + dr];
            if (cur == (int32_t)(base + c))
                bits ^= 0x1FF;                           // the edit cursor: inverted cell
            PUT9(w, bits);
            if (b->cx1 && ((g && g != FUBK20_HALF) || cur == (int32_t)(base + c)))
                PUT9(ow, 0x1FFu);                        // (only boxes with a bright strip)
            nd += 9;
        }
#undef PUT9
        // the dots are read through 32-bit windows in locals (registers): the stores to buf could
        // otherwise force a reload of w[] for every dot (no strict aliasing)
        const uint32_t blk = BLANK + s->txt_black, cx0 = off + b->cx0, cx1 = off + b->cx1;
        uint32_t nb = w[0] << 1, nleft = 31, nwi = 0;          // next dots: from dot 1 on
        uint32_t ob = ow[0], oleft = 32, owi = 0;             // opaque: from dot 0 on
        uint32_t i = off + b->x0, pos = sh ? FUBK20_TEXT_DOT / 2 : 0;
        uint32_t combo = w[0] >> 31;                          // prev, cur, next dot
        for (uint32_t d = 0; d <= nd; d++) {
            if (!nleft) {
                nb = w[++nwi];
                nleft = 32;
            }
            combo = (combo << 1 | nb >> 31) & 7;
            nb <<= 1;
            nleft--;
            uint32_t op = 0;
            if (b->cx1) {                                     // only boxes with a bright strip
                if (!oleft) {
                    ob = ow[++owi];
                    oleft = 32;
                }
                op = ob >> 31 && i + 3 > cx0 && i < cx1;      // a character's cell over it
                ob <<= 1;
                oleft--;
            }
            if (!combo && !op) {                              // black around: skip
                // this dot and the black run after it (dots up to the next lit one; the next
                // dots are the bits of nb), unless that comes near FuBK's covered strip
                uint32_t z = 0;
                // before FuBK's covered strip: only as far as it (room in dots, 80/27 samples each)
                uint32_t room = b->cx1 && i < cx1 ? (cx0 > i + 6 ? (cx0 - i - 6) * FUBK20_TEXT_DOT_STEP / FUBK20_TEXT_DOT : 0) : 32u;
                if (room) {
                    z = nb ? (uint32_t)__builtin_clz(nb) : 32u;
                    if (z > room - 1)
                        z = room - 1;
                    if (z >= nleft) {
                        z = nleft;
                        nleft = 0;                            // (reloaded at the next dot)
                    } else {
                        nb <<= z;
                        nleft -= z;
                    }
                    if (b->cx1) {                             // the opaque bits move along
                        uint32_t k = z;
                        while (k >= oleft) {
                            k -= oleft;
                            ob = ow[++owi];
                            oleft = 32;
                        }
                        ob <<= k;
                        oleft -= k;
                    }
                    d += z;
                }
                uint32_t n = ((z + 1) * FUBK20_TEXT_DOT - pos + FUBK20_TEXT_DOT_STEP - 1) / FUBK20_TEXT_DOT_STEP;
                i += n;
                pos += n * FUBK20_TEXT_DOT_STEP - (z + 1) * FUBK20_TEXT_DOT;
                continue;
            }
            const uint8_t *L = fubk20_text_lut + combo + pos * 8;
            if (op) {                                         // (FuBK: over the centre line)
                do {
                    uint32_t v = *L;
                    if (v || (i >= cx0 && i < cx1))
                        PUT(i, blk + v);
                    i++;
                    L += FUBK20_TEXT_DOT_STEP * 8;
                    pos += FUBK20_TEXT_DOT_STEP;
                } while (pos < FUBK20_TEXT_DOT);
            } else {
                do {
                    uint32_t v = *L;
                    if (v)
                        PUT(i, blk + v);
                    i++;
                    L += FUBK20_TEXT_DOT_STEP * 8;
                    pos += FUBK20_TEXT_DOT_STEP;
                } while (pos < FUBK20_TEXT_DOT);
            }
            pos -= FUBK20_TEXT_DOT;
        }
    }
}

FUBK_HOT void fubk20_line(fubk20_state_t *st, fubk_sample_t *buf, uint32_t off)
{
    const fubk20_std_t *s = st->s;
    // parameters into locals (see the speed notes at the top)
    const uint32_t l = st->line, spl = s->spl, a0 = s->a0, nb = s->nb, n_end = a0 + B * nb;
    const uint32_t bw0 = s->bw0, bwn = s->bwn, n = s->car_n, wlen = s->wlen;
    const uint8_t *ypool = s->ypool, *flat = s->flat;
    const int8_t *uvpool = s->uvpool;
    // flat-colour waveforms: PAL has one per V-switch sign (+V, -V) for each colour
    const int8_t *wtab = s->wtab ? s->wtab + (st->vs ? wlen : 0) : 0;
    const uint32_t wstride = wlen << s->vswitch;
    const int16_t *bu = s->bu, *bv = s->bv;

    // carrier offsets for this line: index exact at mid-line (same steps as make_tables20.py:
    // round(mid * N / den) with den = d * N)
    uint32_t half = spl / 2, d = s->ph_den / n;
    uint32_t mid = (st->acc + half * s->ph_num) % s->ph_den;
    uint32_t i_mid = (2 * mid + d) / (2 * d);
    uint32_t idx0 = (i_mid + n - (s->car_step * half) % n) % n;
    uint32_t ms = (idx0 * s->car_inv) % n;
    uint32_t mc = ((idx0 + (st->vs ? 3 : 1) * (n / 4)) * s->car_inv) % n;
    const int16_t *Sb = s->car + ms, *Cb = s->car + mc;

    uint8_t info = s->line_info[l - 1];
    const uint8_t *t = s->tmpl + (info & 0x3F) * spl;
    uint32_t row = apply_row(st);

    // sync and blanking (+ burst)
    if (info & 0x80) {
        copy_tmpl(buf, off, t, 0, bw0);
        // burst carrier: rotated against the picture by burst_ofs (index < car_n + spl: ms, r < n)
        uint32_t r = s->burst_ofs;
#ifdef FUBK_BURST_TEST
        if (s->vswitch)
            r = (r + fubk20_burst_ofs) % n;
#endif
        const int16_t *S = Sb + bw0 + r, *C = Cb + bw0 + r;
        for (uint32_t j = 0; j < bwn; j++) {
            int32_t c = (bu[j] * S[j] + bv[j] * C[j] + (1 << (CAR_Q + BURST_Q - 1))) >> (CAR_Q + BURST_Q);
            PUT(off + bw0 + j, t[bw0 + j] + c);
        }
        copy_tmpl(buf, off, t, bw0 + bwn, a0);
    } else {
        copy_tmpl(buf, off, t, 0, a0);
    }
    copy_tmpl(buf, off, t, n_end, spl);

    if (row == 0xFFFF) {
        // no picture: blanking, or a data line (teletext / captions; drawn on blanking)
        if (st->vbi_on && s->vbi && vbi20_line(s->vbi, l, st->frame, st->vbi_buf, a0, n_end))
            copy_tmpl(buf, off, st->vbi_buf, a0, n_end);
        else
            copy_tmpl(buf, off, t, a0, n_end);
    } else if (s->img_rows) {
        // image page: 4 pixels per 13 samples (the first pixel 4, the others 3), each one palette
        // colour: Y + A sin(carrier + hue) as one multiply, the hue as an offset into car.
        // 8 pixels = 26 samples = 13 sample pairs per step, written straight to the buffer.
        const uint32_t x0 = s->img_x0, x1 = x0 + FUBK20_IMG_W * 13 / 4, vs = st->vs;
        const uint8_t *px = s->img_rows[row];
        const fubk20_pal_t *pal = s->img_pal;
        const int16_t *car = s->car;
        copy_tmpl(buf, off, t, a0, x0);
        copy_tmpl(buf, off, t, x1, n_end);
        // pixel q of this step starts at sample j: its colour and carrier read pointer
#define IMG_PX(q) do {                                                                    \
            const fubk20_pal_t *e = &pal[px[q]];                                          \
            y = e->y;                                                                     \
            am = e->a;                                                                    \
            uint32_t kk = ms + e->k[vs];                                                  \
            cp = car + (kk >= n ? kk - n : kk) + i;                                       \
        } while (0)
#define IMG_SV(j) ((y + mul16(am, cp[j])) >> 20)
        // samples j, j + 1 (j even) of this step; h holds a sample whose pixel ended
#define IMG_STEP(W) do {                                                                  \
            int32_t y, am, h;                                                             \
            const int16_t *cp;                                                            \
            IMG_PX(0); W(0, IMG_SV(0), IMG_SV(1)); W(2, IMG_SV(2), IMG_SV(3));            \
            IMG_PX(1); W(4, IMG_SV(4), IMG_SV(5)); h = IMG_SV(6);                         \
            IMG_PX(2); W(6, h, IMG_SV(7)); W(8, IMG_SV(8), IMG_SV(9));                    \
            IMG_PX(3); W(10, IMG_SV(10), IMG_SV(11)); h = IMG_SV(12);                     \
            IMG_PX(4); W(12, h, IMG_SV(13)); W(14, IMG_SV(14), IMG_SV(15)); h = IMG_SV(16); \
            IMG_PX(5); W(16, h, IMG_SV(17)); W(18, IMG_SV(18), IMG_SV(19));               \
            IMG_PX(6); W(20, IMG_SV(20), IMG_SV(21)); h = IMG_SV(22);                     \
            IMG_PX(7); W(22, h, IMG_SV(23)); W(24, IMG_SV(24), IMG_SV(25));               \
        } while (0)
#define IMG_W_EVEN(j, a, b) (((pair_t *)p)[(j) / 2] = PAIR(a, b))
#define IMG_W_ODD(j, a, b) (p[ODD0(j)] = W16(a), p[ODD1(j)] = W16(b))
        if (!((off + x0) & 1)) {                         // PAL, NTSC 1st line: whole pairs
            for (uint32_t i = x0; i < x1; i += 26, px += 8) {
                fubk_sample_t *p = buf + off + i;
                IMG_STEP(IMG_W_EVEN);
            }
        } else {                                         // NTSC 2nd line (odd start)
            for (uint32_t i = x0; i < x1; i += 26, px += 8) {
                fubk_sample_t *p = buf + off + i;
                IMG_STEP(IMG_W_ODD);
            }
        }
#undef IMG_PX
#undef IMG_SV
#undef IMG_STEP
#undef IMG_W_EVEN
#undef IMG_W_ODD
    } else if (info & 0x40) {
        // a sync pulse sits inside the picture (PAL line 623)
        for (uint32_t b = 0; b < nb; b++) {
            const uint8_t *y = ypool + st->cur_y[b] * B;
            const int8_t *uv = uvpool + st->cur_uv[b] * (2 * B);
            for (int j = 0; j < B; j++) {
                uint32_t i = a0 + b * B + j;
                int32_t c = (uv[2 * j] * Sb[i] + uv[2 * j + 1] * Cb[i] + RND) >> CAR_Q;
                PUT(off + i, y[j] + t[i] - BLANK + c);
            }
        }
    } else if (off & 1) {
        // odd start (NTSC: the 2nd line of a buffer starts at 1271); a0 and B are even, so every
        // block starts odd too: sample j goes to p[ODD0(j)], sample j + 1 to p[ODD1(j)]
        fubk_sample_t *p = buf + off + a0;
        const int16_t *S = Sb + a0, *C = Cb + a0;
        for (uint32_t b = 0; b < nb; b++, p += B, S += B, C += B) {
            const uint8_t *y = ypool + st->cur_y[b] * B;
            uint32_t ui = st->cur_uv[b];
            uint32_t fid = flat ? flat[ui] : 0;
            if (ui == 0) {
                for (int j = 0; j < B; j += 2) {
                    p[ODD0(j)] = W16(y[j]);
                    p[ODD1(j)] = W16(y[j + 1]);
                }
            } else if (fid) {                            // flat colour: precomputed chroma
                const int8_t *w = wtab + (fid - 1) * wstride + (a0 + b * B + ms) % n;
                for (int j = 0; j < B; j += 2) {
                    p[ODD0(j)] = W16(y[j] + w[j]);
                    p[ODD1(j)] = W16(y[j + 1] + w[j + 1]);
                }
            } else {
                const int8_t *uv = uvpool + ui * (2 * B);
                for (int j = 0; j < B; j += 2) {
                    p[ODD0(j)] = W16(y[j] + ((uv[2 * j] * S[j] + uv[2 * j + 1] * C[j] + RND) >> CAR_Q));
                    p[ODD1(j)] = W16(y[j + 1] + ((uv[2 * j + 2] * S[j + 1] + uv[2 * j + 3] * C[j + 1] + RND) >> CAR_Q));
                }
            }
        }
    } else {
        pair_t *o = (pair_t *)(buf + off + a0);          // even start: whole pairs
        const int16_t *S = Sb + a0, *C = Cb + a0;
        for (uint32_t b = 0; b < nb; b++, o += B / 2, S += B, C += B) {
            const uint8_t *y = ypool + st->cur_y[b] * B;
            uint32_t ui = st->cur_uv[b];
            uint32_t fid = flat ? flat[ui] : 0;
            pair_t *op = o;
            if (ui == 0) {                               // no colour: luma only
                for (int j = 0; j < B; j += 2)
                    *op++ = PAIR(y[j], y[j + 1]);
            } else if (fid) {                            // flat colour: precomputed chroma
                const int8_t *w = wtab + (fid - 1) * wstride + (a0 + b * B + ms) % n;
                for (int j = 0; j < B; j += 2)
                    *op++ = PAIR(y[j] + w[j], y[j + 1] + w[j + 1]);
            } else {
                const int8_t *uv = uvpool + ui * (2 * B);
                for (int j = 0; j < B; j += 2) {
                    int32_t v0 = y[j] + ((uv[2 * j] * S[j] + uv[2 * j + 1] * C[j] + RND) >> CAR_Q);
                    int32_t v1 = y[j + 1] + ((uv[2 * j + 2] * S[j + 1] + uv[2 * j + 3] * C[j + 1] + RND) >> CAR_Q);
                    *op++ = PAIR(v0, v1);
                }
            }
        }
    }
    if (s->ntext && row != 0xFFFF)
        text_overlay(s, row, buf, off);
    for (uint32_t i = spl, n = fubk20_next_len(st); i < n; i++)
        PUT(off + i, BLANK);                             // long line: 1 or 2 more samples
    advance(st);
}
