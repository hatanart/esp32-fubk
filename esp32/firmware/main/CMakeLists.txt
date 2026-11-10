# 20 MHz design (default): integer APLL (no clock jitter), PAL FuBK or NTSC SMPTE bars chosen
# with the GPIO4 ("D4") button, exact subcarrier, rendered on both cores (main20.c, fubk_synth20.c).   idf.py -DFUBK_20MHZ=1 build  (remembered)
option(FUBK_20MHZ "20 MS/s jitter-free design (default)" ON)
if(FUBK_20MHZ)
    set(FUBK_SRCS "main20.c" "fubk_synth20.c" "vbi20.c")
else()
    set(FUBK_SRCS "main.c" "fubk_synth.c")
endif()
idf_component_register(SRCS ${FUBK_SRCS}
                       INCLUDE_DIRS "."
                       REQUIRES driver esp_hw_support esp_rom soc log esp_timer nvs_flash)
# 16-bit samples, code in the high byte, sample pairs swapped (ESP32 I2S DAC order)
target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_I2S16 FUBK_SWAP=1)
# Frame lock: every frame identical (line 5 = 1136 samples). Off by default: with the APLL
# fraction 41/64 the clock jitter pattern is 227 samples = 1/5 line, so it is identical on every
# line only when all lines are 1135 samples. Turn on with:  idf.py -DFUBK_FRAME_LOCK=1 build
# (the setting is remembered; -DFUBK_FRAME_LOCK=0 turns it off again)
option(FUBK_FRAME_LOCK "every frame identical (line 5 = 1136 samples)" OFF)
if(FUBK_FRAME_LOCK)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_FRAME_LOCK)
endif()
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)
# APLL: default fraction 41/64 (short sigma-delta pattern, fsc -25 Hz). Exact one (fsc +0.4 Hz,
# 409.6 us pattern):  idf.py -DFUBK_APLL_EXACT=1 build   (remembered; =0 switches back)
option(FUBK_APLL_EXACT "use the most exact APLL fraction" OFF)
if(FUBK_APLL_EXACT)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_APLL_EXACT)
endif()
# Test field: whole picture one flat colour (constant 4.43 MHz chroma), for scope checks.
#   idf.py -DFUBK_TEST_FIELD=1 build   (remembered; =0 back to FuBK)
option(FUBK_TEST_FIELD "flat orange test field instead of FuBK" OFF)
if(FUBK_TEST_FIELD)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_TEST_FIELD)
endif()
# Clock test: APLL with integer coefficients only (sdm0 = sdm1 = 0, no sigma-delta jitter).
# 73.33 MHz instead of 70.94 (+3.4 %): NOT valid PAL (no colour, lines 3.4 % short), only for
# the scope, to see whether the moving pattern comes from the fractional APLL.
#   idf.py -DFUBK_APLL_INTEGER=1 build   (remembered; =0 switches back)
option(FUBK_APLL_INTEGER "integer-only APLL clock test (not valid PAL)" OFF)
if(FUBK_APLL_INTEGER)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_APLL_INTEGER)
endif()
# DMA/DAC test: play a fixed 4-sample pattern (173,77,95,191) forever, no synth, no sync.
#   idf.py -DFUBK_RAW_PATTERN=1 build   (remembered; =0 switches back)
option(FUBK_RAW_PATTERN "fixed 4-sample DMA pattern test" OFF)
if(FUBK_RAW_PATTERN)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_RAW_PATTERN)
endif()
# 1136-sample lines (284 whole cycles): the same subcarrier phase on every line, as esp_8_bit /
# ESP32-TestPatternGenerator do. Not standard PAL, but clock jitter then sits at the same place on
# every line. Lines 8-14 and 321-327 are 1140 samples, so a frame is 710056 = 3128 x 227 samples:
# with the default APLL fraction (41/64, jitter repeats every 227 samples) the jitter is then the
# same in every frame. Use with FUBK_APLL_EXACT=0. Replaces FUBK_FRAME_LOCK.
#   idf.py -DFUBK_LINE1136=1 -DFUBK_FRAME_LOCK=0 -DFUBK_APLL_EXACT=0 build   (remembered)
option(FUBK_LINE1136 "every line 1136 samples, same phase on every line" OFF)
if(FUBK_LINE1136)
    if(FUBK_FRAME_LOCK)
        message(FATAL_ERROR "FUBK_LINE1136 and FUBK_FRAME_LOCK exclude each other: add -DFUBK_FRAME_LOCK=0")
    endif()
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_LINE1136)
endif()
# PAL burst test: the pattern button rotates the burst against the picture in ~3 degree steps
# (0, +3 .. +13, -13 .. -3), to find colour (Hanover) bars from phase errors in the output stage.
#   idf.py -DFUBK_BURST_TEST=1 build   (remembered; =0 switches back)
option(FUBK_BURST_TEST "pattern button rotates the PAL burst (test)" OFF)
if(FUBK_BURST_TEST)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_BURST_TEST)
endif()
# Test: start on pattern n (as n presses of the pattern button), e.g. for measuring a page's CPU
# load over the serial log.   idf.py -DFUBK_START_PATTERN=15 build   (remembered; =0 = off)
set(FUBK_START_PATTERN 0 CACHE STRING "pattern to step to at start (test builds), 0 = off")
if(FUBK_START_PATTERN)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_START_PATTERN=${FUBK_START_PATTERN})
endif()
# Old clock (up to v1.4): I2S from the integer APLL (80 MHz) instead of PLL_D2 (160 MHz, the CPU's
# PLL). The APLL's phase against the bus is random at each start; some cold starts gave snow.
#   idf.py -DFUBK_CLK_APLL=1 build   (remembered; =0 switches back)
option(FUBK_CLK_APLL "I2S clock from the APLL instead of PLL_D2" OFF)
if(FUBK_CLK_APLL)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE FUBK_CLK_APLL)
endif()
