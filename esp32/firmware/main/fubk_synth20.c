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
#include "patterns20_tables.h"            // extra patterns, both standards

#define BLANK 64
#define B 32
#define CAR_Q 14
#define BURST_Q 6
#define RND (1 << (CAR_Q - 1))

_Static_assert(F20_NB <= F20_MAX_NB && N20_NB <= F20_MAX_NB, "too many blocks");
_Static_assert(F20_SPL <= F20_MAX_SPL && N20_SPL <= F20_MAX_SPL, "line too long");
// 32-bit phase arithmetic (see fubk20_line): den a multiple of N, no overflow
_Static_assert(F20_PH_DEN % F20_CAR_N == 0 && N20_PH_DEN % N20_CAR_N == 0, "phase grid");
_Static_assert((uint64_t)F20_SPL * F20_PH_NUM + F20_PH_DEN < (1ull << 32), "phase overflow");
_Static_assert((uint64_t)(N20_SPL + 1) * N20_PH_NUM + N20_PH_DEN < (1ull << 32), "phase overflow");

#define STD(p, P, nm) {                                                                  \
    nm, P##_SPL, P##_LINES, P##_A0, P##_NB, P##_BW0, P##_BWN, P##_ROWS,                  \
    P##_CAR_N, P##_CAR_STEP, P##_CAR_INV, P##_PH_NUM, P##_PH_DEN,                        \
    P##_VSWITCH, P##_DATA_FRAMES, P##_POS_BITS, P##_Y_BITS,                              \
    p##_tmpl, p##_line_info, p##_ypool, p##_rowmap, p##_row_start, p##_delta, p##_uvpool, \
    p##_car, p##_bu, p##_bv, p##_flat, p##_wtab, P##_WLEN, P##_LONG_PERIOD }
const fubk20_std_t fubk20_pal = STD(f20, F20, "PAL");
const fubk20_std_t fubk20_ntsc = STD(n20, N20, "NTSC");

// an extra pattern: the standard's timing, sync, burst and carrier with its own picture tables
#define PAT(p, P, nm, pb, yb, ypool, row_start, delta, uvpool, flat, wtab) {             \
    nm, P##_SPL, P##_LINES, P##_A0, P##_NB, P##_BW0, P##_BWN, P##_ROWS,                  \
    P##_CAR_N, P##_CAR_STEP, P##_CAR_INV, P##_PH_NUM, P##_PH_DEN,                        \
    P##_VSWITCH, 1, pb, yb,                                                              \
    p##_tmpl, p##_line_info, ypool, p##_rowmap, row_start, delta, uvpool,                \
    p##_car, p##_bu, p##_bv, flat, wtab, P##_WLEN, P##_LONG_PERIOD }
_Static_assert(PATTERNS20_COUNT + 1 == FUBK20_NPAT, "FUBK20_NPAT does not match patterns20_tables.h");
const fubk20_std_t fubk20_pal_pat[FUBK20_NPAT] = { STD(f20, F20, "PAL FuBK"), F20_PATTERNS };
const fubk20_std_t fubk20_ntsc_pat[FUBK20_NPAT] = { STD(n20, N20, "NTSC SMPTE bars"), N20_PATTERNS };

typedef struct { uint32_t row_start, delta, ypool, uvpool, flat, wtab; } pat_size_t;
static const pat_size_t pal_size[FUBK20_NPAT] = { { 0 }, F20_PATTERNS_SIZES };
static const pat_size_t ntsc_size[FUBK20_NPAT] = { { 0 }, N20_PATTERNS_SIZES };

#define AL4(n) (((n) + 3u) & ~3u)

uint32_t fubk20_pattern_bytes(const fubk20_std_t *pats, uint32_t k)
{
    const pat_size_t *z = &(pats == fubk20_ntsc_pat ? ntsc_size : pal_size)[k];
    return AL4(z->row_start) + AL4(z->delta) + AL4(z->ypool) + AL4(z->uvpool) + AL4(z->flat) + AL4(z->wtab);
}

void fubk20_pattern_load(const fubk20_std_t *pats, uint32_t k, void *ram, fubk20_std_t *dst)
{
    const fubk20_std_t *s = &pats[k];
    const pat_size_t *z = &(pats == fubk20_ntsc_pat ? ntsc_size : pal_size)[k];
    uint8_t *p = ram;
    *dst = *s;
#define CP(f) if (z->f) { memcpy(p, s->f, z->f); dst->f = (__typeof__(dst->f))(void *)p; p += AL4(z->f); }
    CP(row_start) CP(delta) CP(ypool) CP(uvpool) CP(flat) CP(wtab)
#undef CP
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
    for (int b = 0; b < F20_MAX_NB; b++)
        st->cur_y[b] = st->cur_uv[b] = 0;
}

// apply the block changes of the next line (if it has picture); returns its data row or 0xFFFF
static FUBK_HOT uint32_t apply_row(fubk20_state_t *st)
{
    const fubk20_std_t *s = st->s;
    uint32_t row = s->rowmap[st->line - 1];
    if (row != 0xFFFF) {
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
        const int16_t *S = Sb + bw0, *C = Cb + bw0;
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
        copy_tmpl(buf, off, t, a0, n_end);
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
    if (fubk20_next_len(st) > spl)
        PUT(off + spl, BLANK);                           // NTSC long line: one more sample
    advance(st);
}
