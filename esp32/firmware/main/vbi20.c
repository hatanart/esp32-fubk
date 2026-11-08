// Data lines at 20 MS/s: PAL teletext and NTSC closed captions. See vbi20.h.
// Sample 16 of a line is 0H (the line buffer starts 0.8 us before it).
#ifdef ESP_PLATFORM
#include "esp_attr.h"
#define VBI_ATTR DRAM_ATTR
#define VBI_HOT IRAM_ATTR
#else
#define VBI_ATTR
#define VBI_HOT
#endif
#include <string.h>
#include "vbi20.h"
#include "vbi20_tables.h"

#define BLANK 64
#define S0H 16                           // sample of 0H

static VBI_HOT uint8_t odd_parity(uint32_t c)
{
    c &= 0x7F;
    return (uint8_t)(c | (__builtin_parity(c) ? 0 : 0x80));
}

// --- teletext ------------------------------------------------------------------------------------
// One bit = 320/111 samples (6.9375 Mbit/s). The first bit starts 10.3 us after 0H.
#define TTX_S0 (S0H + 206)
#define TTX_I0 (TTX_S0 - 6)              // from 2 bits before the first bit ...
#define TTX_I1 (TTX_S0 + 1044)           // ... to 2.5 bits after the last (360 bits)

static VBI_HOT void ttx_render(uint32_t field, uint32_t frame, uint8_t *out)
{
    uint8_t pk[47];                      // the packet, 2 zero bytes for the bits after the end
    uint32_t n = ((frame % TTX_PACKETS) * 2 + field) % TTX_PACKETS;
    memcpy(pk, ttx_page[n], 45);
    pk[45] = pk[46] = 0;
    if (n == 0) {                        // header: clock hh:mm:ss since power-on
        uint32_t s = frame / 25, h = (s / 3600) % 24, m = (s / 60) % 60;
        s %= 60;
        uint8_t *c = pk + TTX_CLOCK_OFS;
        c[0] = odd_parity('0' + h / 10); c[1] = odd_parity('0' + h % 10);
        c[3] = odd_parity('0' + m / 10); c[4] = odd_parity('0' + m % 10);
        c[6] = odd_parity('0' + s / 10); c[7] = odd_parity('0' + s % 10);
    }
    // sample TTX_I0 is 6 samples = 666/320 bits before bit 0: in bit -3 at 294/320
    int32_t k = -3;                      // current bit
    uint32_t f = 294;                    // position in it, 1/320 bit
    uint32_t w = 0;                      // bits k-2 .. k+2 (bit 0 = k-2)
    for (uint32_t i = TTX_I0; i < TTX_I1; i++) {
        out[i] = (uint8_t)(BLANK + ttx_lut[f / 10][w]);
        f += 111;
        if (f >= 320) {
            f -= 320;
            k++;
            int32_t b = k + 2;
            uint32_t nb = b >= 0 ? (pk[b >> 3] >> (b & 7)) & 1 : 0;
            w = (w >> 1) | nb << 4;
        }
    }
}

// --- closed captions ---------------------------------------------------------------------------
// One bit = 1271/32 samples (32 fH). Run-in: 7 cycles of (1 - cos) from 10.5 us; start bit at
// 8.5 bits after that (27.38 us); then 16 data bits, LSB first.
#define CC_R (S0H + 210)
#define CC_I1 (CC_R + 1022)              // end of the last bit + the edge

static VBI_HOT uint32_t cc_byte_pair(uint32_t a, uint32_t b)
{
    return odd_parity(a) | (uint32_t)odd_parity(b) << 8;
}

// Each second: RCL, ENM, PAC row 15, 9 text pairs, EOC (control codes twice) = 17 frames,
// then null pairs.
VBI_HOT uint32_t vbi20_cc_pair(uint32_t frame)
{
    uint32_t sec = (uint32_t)((uint64_t)frame * 1001 / 30000);
    uint32_t first = (uint32_t)(((uint64_t)sec * 30000 + 1000) / 1001);
    uint32_t k = frame - first;
    if (k < 2) return cc_byte_pair(0x14, 0x20);           // RCL: resume caption loading (pop-on)
    if (k < 4) return cc_byte_pair(0x14, 0x2E);           // ENM: erase non-displayed memory
    if (k < 6) return cc_byte_pair(0x14, 0x60);           // PAC: row 15, white
    if (k < 15) {
        uint32_t h = (sec / 3600) % 24, m = (sec / 60) % 60, s = sec % 60;
        char t[18] = { 'N', 'T', 'S', 'C', ' ', 'T', 'E', 'S', 'T', ' ',
                       (char)('0' + h / 10), (char)('0' + h % 10), ':', (char)('0' + m / 10),
                       (char)('0' + m % 10), ':', (char)('0' + s / 10), (char)('0' + s % 10) };
        uint32_t j = 2 * (k - 6);
        return cc_byte_pair((uint8_t)t[j], (uint8_t)t[j + 1]);
    }
    if (k < 17) return cc_byte_pair(0x14, 0x2F);          // EOC: end of caption (show it)
    return cc_byte_pair(0, 0);                            // null pair
}

static VBI_HOT void cc_render(uint32_t frame, uint8_t *out)
{
    // cells from the start bit: 0 = start (1), 1..16 = data, others 0
    uint32_t bits = 1u | vbi20_cc_pair(frame) << 1;
    uint32_t u = 0;                                        // position in 1/1271 bit from 10.5 us
    for (uint32_t i = CC_R; i < CC_I1; i++, u += 32) {
        uint32_t v;
        if (u < 7 * 1271) {
            v = cc_runin[(u % 1271) * 64 / 1271];
        } else {
            // y: 1/2542 bit from 4 samples (half an edge) before the start bit's leading edge
            int32_t y = (int32_t)(2 * u) - 17 * 1271 + 256;
            if (y < 0) {
                v = 0;
            } else {
                uint32_t c = (uint32_t)y / 2542, g = (uint32_t)y % 2542;
                uint32_t cur = c < 17 ? (bits >> c) & 1 : 0;
                uint32_t prev = c > 0 && c <= 17 ? (bits >> (c - 1)) & 1 : 0;
                if (g < 64 * CC_RAMP && cur != prev)
                    v = cur ? cc_edge[g >> 6] : CC_A - cc_edge[g >> 6];
                else
                    v = cur ? CC_A : 0;
            }
        }
        out[i] = (uint8_t)(BLANK + v);
    }
}

VBI_HOT int vbi20_line(uint32_t kind, uint32_t line, uint32_t frame, uint8_t *out, uint32_t n0, uint32_t n1)
{
    if (kind == VBI20_TTX && (line == VBI20_TTX_LINE1 || line == VBI20_TTX_LINE2)) {
        if (n0 > TTX_I0 || n1 < TTX_I1)
            return 0;
        memset(out + n0, BLANK, n1 - n0);
        ttx_render(line == VBI20_TTX_LINE2, frame, out);
        return 1;
    }
    if (kind == VBI20_CC && line == VBI20_CC_LINE) {
        if (n0 > CC_R || n1 < CC_I1)
            return 0;
        memset(out + n0, BLANK, n1 - n0);
        cc_render(frame, out);
        return 1;
    }
    return 0;
}
