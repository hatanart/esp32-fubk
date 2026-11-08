// Data lines in the vertical blanking, 20 MS/s (see vbi20.c, esp32/tools/make_vbi20.py).
//   PAL : teletext page P100 on lines 20 and 333 (one packet per field: the 25-row page repeats
//         every 0.5 s), header clock = time since power-on
//   NTSC: closed captions CC1 on line 21 (field 1), "NTSC TEST hh:mm:ss" (pop-on, every second)
// Everything follows from the frame counter, so the two render states need no shared state.
#pragma once
#include <stdint.h>

enum { VBI20_NONE, VBI20_TTX, VBI20_CC };

#define VBI20_TTX_LINE1 20
#define VBI20_TTX_LINE2 333
#define VBI20_CC_LINE 21

// If `line` of frame `frame` carries data for this kind, write samples [n0, n1) of it to out
// (blanking plus the data; n0 .. n1 must cover the line's data) and return 1; else return 0.
int vbi20_line(uint32_t kind, uint32_t line, uint32_t frame, uint8_t *out, uint32_t n0, uint32_t n1);

// the caption byte pair (with parity) sent in frame `frame` (for tests)
uint32_t vbi20_cc_pair(uint32_t frame);
