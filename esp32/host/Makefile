# Host test: the ESP32 synthesis code must reproduce the Python integer model byte for byte.
#   make test     (needs ../out/model_*4frames.u8 from ../tools/make_tables.py)
# Covers the standard 4-frame PAL sequence and FUBK_FRAME_LOCK, each in the plain uint8 layout
# and in the firmware's I2S16 buffer layout.
SRC = ../firmware/main
CFLAGS = -O2 -Wall -Wextra -Werror -I$(SRC)
DEPS = host_test.c $(SRC)/fubk_synth.c $(SRC)/fubk_synth.h $(SRC)/fubk_tables.h
MODEL = ../out/model_4frames.u8
MODEL_FL = ../out/model_framelock_4frames.u8
MODEL_L36 = ../out/model_line1136_4frames.u8
BINS = host_u8 host_i2s16 host_fl_u8 host_fl_i2s16 host_l36_u8 host_l36_i2s16

all: $(BINS)

host_u8: $(DEPS)
	$(CC) $(CFLAGS) -o $@ host_test.c $(SRC)/fubk_synth.c
host_i2s16: $(DEPS)
	$(CC) $(CFLAGS) -DFUBK_I2S16 -o $@ host_test.c $(SRC)/fubk_synth.c
host_fl_u8: $(DEPS)
	$(CC) $(CFLAGS) -DFUBK_FRAME_LOCK -o $@ host_test.c $(SRC)/fubk_synth.c
host_fl_i2s16: $(DEPS)
	$(CC) $(CFLAGS) -DFUBK_FRAME_LOCK -DFUBK_I2S16 -o $@ host_test.c $(SRC)/fubk_synth.c

host_l36_u8: $(DEPS)
	$(CC) $(CFLAGS) -DFUBK_LINE1136 -o $@ host_test.c $(SRC)/fubk_synth.c
host_l36_i2s16: $(DEPS)
	$(CC) $(CFLAGS) -DFUBK_LINE1136 -DFUBK_I2S16 -o $@ host_test.c $(SRC)/fubk_synth.c

test: all
	./host_u8 ../out/c_u8_4frames.u8 4
	cmp ../out/c_u8_4frames.u8 $(MODEL) && echo "standard, u8 layout: identical to the model"
	./host_i2s16 ../out/c_i2s16_4frames.u8 4
	cmp ../out/c_i2s16_4frames.u8 $(MODEL) && echo "standard, I2S16 layout: identical to the model"
	./host_u8 ../out/c_u8_8frames.u8 8
	tail -c $$(stat -c %s $(MODEL)) ../out/c_u8_8frames.u8 | cmp - $(MODEL) && echo "standard: frames 4-7 repeat frames 0-3"
	./host_fl_u8 ../out/c_fl_u8_4frames.u8 4
	cmp ../out/c_fl_u8_4frames.u8 $(MODEL_FL) && echo "frame lock, u8 layout: identical to the model"
	./host_fl_i2s16 ../out/c_fl_i2s16_4frames.u8 4
	cmp ../out/c_fl_i2s16_4frames.u8 $(MODEL_FL) && echo "frame lock, I2S16 layout: identical to the model"
	head -c 709376 ../out/c_fl_u8_4frames.u8 > ../out/c_fl_frame0.u8
	tail -c 709376 ../out/c_fl_u8_4frames.u8 | cmp - ../out/c_fl_frame0.u8 && echo "frame lock: frame 3 identical to frame 0"
	./host_l36_u8 ../out/c_l36_u8_4frames.u8 4
	cmp ../out/c_l36_u8_4frames.u8 $(MODEL_L36) && echo "1136 lines, u8 layout: identical to the model"
	./host_l36_i2s16 ../out/c_l36_i2s16_4frames.u8 4
	cmp ../out/c_l36_i2s16_4frames.u8 $(MODEL_L36) && echo "1136 lines, I2S16 layout: identical to the model"

# 20 MHz synthesis (fubk_synth20.c, PAL + NTSC) against make_tables20.py's model
DEPS20 = host20_test.c $(SRC)/fubk_synth20.c $(SRC)/fubk_synth20.h $(SRC)/fubk20_tables.h $(SRC)/ntsc20_tables.h \
         $(SRC)/patterns20_tables.h $(SRC)/vbi20.c $(SRC)/vbi20.h $(SRC)/vbi20_tables.h
SRC20 = host20_test.c $(SRC)/fubk_synth20.c $(SRC)/vbi20.c
host20_u8: $(DEPS20)
	$(CC) $(CFLAGS) -o $@ $(SRC20)
host20_i2s16: $(DEPS20)
	$(CC) $(CFLAGS) -DFUBK_I2S16 -o $@ $(SRC20)
host20_test_i2s16: $(DEPS20)
	$(CC) $(CFLAGS) -DFUBK_I2S16 -DFUBK_TEST_FIELD -o $@ $(SRC20)

test20: host20_u8 host20_i2s16 host20_test_i2s16
	./host20_u8 ../out/c20_u8.u8 4
	cmp ../out/c20_u8.u8 ../out/model20_4frames.u8 && echo "PAL, u8: identical to the model"
	./host20_i2s16 ../out/c20_split.u8 4 1
	cmp ../out/c20_split.u8 ../out/model20_4frames.u8 && echo "PAL, I2S16, split over 2 states: identical to the model"
	./host20_i2s16 ../out/c20_8.u8 8 1
	tail -c 3200000 ../out/c20_8.u8 | cmp - ../out/model20_4frames.u8 && echo "PAL: frames 4-7 repeat frames 0-3"
	./host20_test_i2s16 ../out/c20_test.u8 4 1
	cmp ../out/c20_test.u8 ../out/model20_test_4frames.u8 && echo "PAL test field: identical to the model"
	./host20_u8 ../out/c20_ntsc_u8.u8 2 0 1
	cmp ../out/c20_ntsc_u8.u8 ../out/model20_ntsc_2frames.u8 && echo "NTSC, u8: identical to the model"
	./host20_i2s16 ../out/c20_ntsc_split.u8 2 1 1
	cmp ../out/c20_ntsc_split.u8 ../out/model20_ntsc_2frames.u8 && echo "NTSC, I2S16, split over 2 states: identical to the model"
	@for k in 01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16; do \
	  ./host20_i2s16 ../out/c20_pal_pat$$k.u8 1 1 0 $$k && cmp ../out/c20_pal_pat$$k.u8 ../out/model20_pal_pat$$k.u8 && \
	  ./host20_i2s16 ../out/c20_ntsc_pat$$k.u8 1 1 1 $$k && cmp ../out/c20_ntsc_pat$$k.u8 ../out/model20_ntsc_pat$$k.u8 || exit 1; \
	done 2>/dev/null; ./host20_i2s16 ../out/c20_ntsc_pat00.u8 1 1 1 0 2>/dev/null && \
	  cmp ../out/c20_ntsc_pat00.u8 ../out/model20_ntsc_pat00.u8 && \
	  echo "patterns 0-16, PAL + NTSC (PAL 1 + NTSC 0 = PM5644), split: identical to the model"
	./host20_i2s16 ../out/c20_pal_switch.u8 5 1 0 0,2,5,8,0
	cmp ../out/c20_pal_switch.u8 ../out/model20_pal_switch.u8 && echo "PAL pattern switching (FuBK, crosshatch, bars, red, FuBK): identical to the model"
	./host20_u8 ../out/c20_ntsc_switch.u8 4 0 1 1,5,9,1
	cmp ../out/c20_ntsc_switch.u8 ../out/model20_ntsc_switch.u8 && echo "NTSC pattern switching (SMPTE, bars, green, SMPTE): identical to the model"
	./host20_i2s16 ../out/c20_pal_switch_prog.u8 6 1 0 0,16,16,16,15,0
	cmp ../out/c20_pal_switch_prog.u8 ../out/model20_pal_switch_prog.u8 && echo "PAL FuBK -> 313-line image page -> multiburst without burst -> FuBK: identical to the model"
	./host20_i2s16 ../out/c20_ntsc_switch_prog.u8 6 1 1 1,16,16,16,15,1
	cmp ../out/c20_ntsc_switch_prog.u8 ../out/model20_ntsc_switch_prog.u8 && echo "NTSC SMPTE -> 262-line image page -> multiburst without burst -> SMPTE: identical to the model"
	./host20_i2s16 ../out/c20_pal_switch_pm.u8 4 1 0 0,1,1,0
	cmp ../out/c20_pal_switch_pm.u8 ../out/model20_pal_switch_pm.u8 && echo "PAL FuBK -> PM5644 -> FuBK: identical to the model"
	./host20_u8 ../out/c20_ntsc_switch_pm.u8 4 0 1 0,1,1,0
	cmp ../out/c20_ntsc_switch_pm.u8 ../out/model20_ntsc_switch_pm.u8 && echo "NTSC PM5644 -> SMPTE -> PM5644: identical to the model"

# Data lines (teletext / captions): decoded back by tools/check_vbi20.py; all other lines must be
# unchanged. Includes the clock rollover 00:59:59 -> 01:00:00.
PY ?= python3
testvbi: host20_i2s16
	./host20_i2s16 ../out/v_pal_on.u8 14 1 0 0 1 0 && ./host20_i2s16 ../out/v_pal_off.u8 14 1 0 0 0 0
	$(PY) ../tools/check_vbi20.py pal ../out/v_pal_on.u8 ../out/v_pal_off.u8 0
	./host20_i2s16 ../out/v_pal_on.u8 26 1 0 0 1 89975 && ./host20_i2s16 ../out/v_pal_off.u8 26 1 0 0 0 89975
	$(PY) ../tools/check_vbi20.py pal ../out/v_pal_on.u8 ../out/v_pal_off.u8 89975
	./host20_i2s16 ../out/v_ntsc_on.u8 50 1 1 0 1 0 && ./host20_i2s16 ../out/v_ntsc_off.u8 50 1 1 0 0 0
	$(PY) ../tools/check_vbi20.py ntsc ../out/v_ntsc_on.u8 ../out/v_ntsc_off.u8 0
	./host20_i2s16 ../out/v_ntsc_on.u8 70 1 1 0 1 107850 && ./host20_i2s16 ../out/v_ntsc_off.u8 70 1 1 0 0 107850
	$(PY) ../tools/check_vbi20.py ntsc ../out/v_ntsc_on.u8 ../out/v_ntsc_off.u8 107850
	rm -f ../out/v_*.u8

clean20:
	rm -f host20_u8 host20_i2s16 host20_test_i2s16 ../out/c20_*.u8

clean:
	rm -f $(BINS) ../out/c_*.u8

# New pages (v1.3): the non-interlaced 256-colour image page (sync, burst and every palette colour
# decoded back by tools/check_prog20.py) and the multiburst without colour burst.
testprog: host20_i2s16
	./host20_i2s16 ../out/p16_pal.u8 3 1 0 16 && $(PY) ../tools/check_prog20.py pal ../out/p16_pal.u8
	./host20_i2s16 ../out/p16_ntsc.u8 3 1 1 16 && $(PY) ../tools/check_prog20.py ntsc ../out/p16_ntsc.u8
	./host20_i2s16 ../out/p15_pal.u8 2 1 0 15 && ./host20_i2s16 ../out/p12_pal.u8 2 1 0 12
	$(PY) ../tools/check_prog20.py pal --noburst ../out/p15_pal.u8 ../out/p12_pal.u8
	./host20_i2s16 ../out/p15_ntsc.u8 2 1 1 15 && ./host20_i2s16 ../out/p12_ntsc.u8 2 1 1 12
	$(PY) ../tools/check_prog20.py ntsc --noburst ../out/p15_ntsc.u8 ../out/p12_ntsc.u8
	rm -f ../out/p1[256]_*.u8

# Text in the test cards (v1.4): read back from the output by tools/check_text20.py
testtext: host20_i2s16
	./host20_i2s16 ../out/t_fubk.u8 1 1 0 0 && $(PY) ../tools/check_text20.py fubk ../out/t_fubk.u8
	./host20_i2s16 ../out/t_pm_pal.u8 1 1 0 1 && $(PY) ../tools/check_text20.py pm5644-pal ../out/t_pm_pal.u8
	./host20_i2s16 ../out/t_pm_ntsc.u8 1 1 1 0 && $(PY) ../tools/check_text20.py pm5644-ntsc ../out/t_pm_ntsc.u8
	rm -f ../out/t_*.u8
