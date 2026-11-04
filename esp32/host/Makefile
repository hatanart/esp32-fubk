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
DEPS20 = host20_test.c $(SRC)/fubk_synth20.c $(SRC)/fubk_synth20.h $(SRC)/fubk20_tables.h $(SRC)/ntsc20_tables.h
host20_u8: $(DEPS20)
	$(CC) $(CFLAGS) -o $@ host20_test.c $(SRC)/fubk_synth20.c
host20_i2s16: $(DEPS20)
	$(CC) $(CFLAGS) -DFUBK_I2S16 -o $@ host20_test.c $(SRC)/fubk_synth20.c
host20_test_i2s16: $(DEPS20)
	$(CC) $(CFLAGS) -DFUBK_I2S16 -DFUBK_TEST_FIELD -o $@ host20_test.c $(SRC)/fubk_synth20.c

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

clean20:
	rm -f host20_u8 host20_i2s16 host20_test_i2s16 ../out/c20_*.u8

clean:
	rm -f $(BINS) ../out/c_*.u8
