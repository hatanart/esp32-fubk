// FuBK PAL test card on the ESP32 internal DAC (GPIO25), ESP-IDF v5.x. NOT YET TESTED ON HARDWARE.
//
// I2S0 runs in LCD/DAC mode from the APLL at 4 x fsc = 17.734475 MS/s. Each DMA buffer holds
// 2 lines (2 x 1135 16-bit samples = 4540 bytes; one line alone is 2270 bytes, not a multiple of 4).
// A buffer is split over two descriptors because one descriptor holds at most 4095 bytes.
// When a buffer has been sent, the I2S interrupt (on core 1) refills it with the next 2 lines.
// With FUBK_FRAME_LOCK, the 1136-sample line 5 gets a buffer of its own (2272 bytes).
//
// The I2S/DAC register setup follows the approach of rossumur/esp_8_bit (video_out.h).
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_intr_alloc.h"
#include "esp_chip_info.h"
#include "esp_cpu.h"
#include "esp_clk_tree.h"
#include "esp_private/periph_ctrl.h"
#include "clk_ctrl_os.h"
#include "soc/periph_defs.h"
#include "soc/interrupts.h"
#include "soc/i2s_struct.h"
#include "soc/i2s_reg.h"
#include "soc/rtc.h"
#include "rom/lldesc.h"
#include "driver/dac.h"            // legacy DAC driver (deprecated in v5, still the simplest way)

#define FUBK_TABLES_DATA
#define FUBK_TABLE_ATTR DRAM_ATTR  // tables in internal RAM: flash cache misses would be too slow
#ifdef FUBK_TEST_FIELD
#include "fubk_tables_test.h"     // flat orange field (make_tables.py --test-field)
#else
#include "fubk_tables.h"
#endif
#include "fubk_synth.h"

static const char *TAG = "fubk";

#define NBUF 4                                     // 4 x 2 lines = 512 us of buffering
#ifdef FUBK_LINE1136
#define BUF_SAMPLES (2 * (FUBK_SPL + 5))           // 2 lines of 1136 or 1140, one per descriptor
#else
#define BUF_SAMPLES (2 * FUBK_SPL)
#endif
#define DESC0_BYTES 2272                           // both parts a multiple of 4 bytes
#define DESC1_BYTES (BUF_SAMPLES * 2 - DESC0_BYTES)

static uint16_t DMA_ATTR WORD_ALIGNED_ATTR bufs[NBUF][BUF_SAMPLES];
static lldesc_t DMA_ATTR desc[2 * NBUF];
static intr_handle_t isr_handle;

static volatile uint32_t next_buf;                 // buffer we expect to finish next
static volatile uint32_t n_irq, n_missed, cyc_max, cyc_sum, cyc_n;
// I2S trouble flags, sampled from int_raw in the interrupt (FIFO empty = underrun, TX hung,
// descriptor error)
static volatile uint32_t n_rempty, n_hung, n_dscr_err;

// Fill buffer k with the next 2 lines, or with the long line alone (FUBK_FRAME_LOCK: 1136
// samples, split 1136 + 1136 bytes). The DMA reaches this buffer again only after the other
// NBUF - 1 buffers, so its descriptors can be changed here.
#ifdef FUBK_RAW_PATTERN
// DMA/DAC test: every buffer is the same fixed 4-sample orange pattern, no synth, no sync.
// 2268 samples per buffer (a multiple of 4), so the pattern runs on without a break.
static IRAM_ATTR void fill(uint32_t k)
{
    static const uint8_t pat[4] = {173, 77, 95, 191};
    lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
    for (uint32_t i = 0; i < 2268; i++)
        bufs[k][i ^ FUBK_SWAP] = (uint16_t)(pat[i & 3] << 8);
    a->size = a->length = 2272;
    b->buf = (uint8_t *)bufs[k] + 2272;
    b->size = b->length = 2268 * 2 - 2272;
}
#elif defined(FUBK_LINE1136)
// two lines per buffer, one per descriptor (1136 or 1140 samples = 2272 or 2280 bytes)
static IRAM_ATTR void fill(uint32_t k)
{
    lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
    uint32_t n1 = fubk_synth_next_len();
    fubk_synth_line(bufs[k], 0);
    uint32_t n2 = fubk_synth_next_len();
    fubk_synth_line(bufs[k], n1);
    a->size = a->length = 2 * n1;
    b->buf = (uint8_t *)bufs[k] + 2 * n1;
    b->size = b->length = 2 * n2;
}
#else
static IRAM_ATTR void fill(uint32_t k)
{
    lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
    uint32_t n = fubk_synth_next_len();
    if (n != FUBK_SPL) {
        fubk_synth_line(bufs[k], 0);
        a->size = a->length = n;                   // n samples = 2n bytes, half per descriptor
        b->buf = (uint8_t *)bufs[k] + n;
        b->size = b->length = n;
    } else {
        fubk_synth_line(bufs[k], 0);
        fubk_synth_line(bufs[k], FUBK_SPL);
        a->size = a->length = DESC0_BYTES;
        b->buf = (uint8_t *)bufs[k] + DESC0_BYTES;
        b->size = b->length = DESC1_BYTES;
    }
}
#endif

static IRAM_ATTR void i2s_isr(void *arg)
{
    uint32_t st = I2S0.int_st.val;
    uint32_t raw = I2S0.int_raw.val;
    const uint32_t trouble = I2S_TX_REMPTY_INT_RAW_M | I2S_TX_HUNG_INT_RAW_M |
                             I2S_OUT_DSCR_ERR_INT_RAW_M;
    if (raw & I2S_TX_REMPTY_INT_RAW_M) n_rempty++;
    if (raw & I2S_TX_HUNG_INT_RAW_M) n_hung++;
    if (raw & I2S_OUT_DSCR_ERR_INT_RAW_M) n_dscr_err++;
    I2S0.int_clr.val = st | (raw & trouble);
    if (!(st & I2S_OUT_EOF_INT_ST_M))
        return;
    lldesc_t *d = (lldesc_t *)I2S0.out_eof_des_addr;
    uint32_t k = (uint32_t)(d - desc) / 2;         // eof sits on the 2nd descriptor of a buffer
    if (k >= NBUF)
        return;
    if (k != next_buf)
        n_missed++;                                // lines are still generated in order
    uint32_t t0 = esp_cpu_get_cycle_count();
#ifndef FUBK_RAW_PATTERN
    fill(k);                                       // raw pattern: buffers filled once at start
#endif
    uint32_t dt = esp_cpu_get_cycle_count() - t0;
    if (dt > cyc_max)
        cyc_max = dt;
    cyc_sum += dt;
    cyc_n++;
    n_irq++;
    next_buf = (k + 1) % NBUF;
}

static void setup_dma(void)
{
    for (int k = 0; k < NBUF; k++) {
        lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
        memset(a, 0, sizeof *a);
        memset(b, 0, sizeof *b);
        a->size = a->length = DESC0_BYTES;
        a->buf = (uint8_t *)bufs[k];
        a->owner = 1;
        a->qe.stqe_next = b;
        b->size = b->length = DESC1_BYTES;
        b->buf = (uint8_t *)bufs[k] + DESC0_BYTES;
        b->owner = 1;
        b->eof = 1;                                // one interrupt per buffer (2 lines)
        b->qe.stqe_next = &desc[(2 * k + 2) % (2 * NBUF)];
    }
}

static void start_video(void *arg)
{
    // runs on core 1, so the interrupt is serviced there
    periph_module_enable(PERIPH_I2S0_MODULE);
    ESP_ERROR_CHECK(esp_intr_alloc(ETS_I2S0_INTR_SOURCE, ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM,
                                   i2s_isr, NULL, &isr_handle));

    // reset I2S, its FIFO and DMA
    I2S0.conf.val = 0;
    I2S0.conf.tx_reset = 1;
    I2S0.conf.tx_reset = 0;
    I2S0.conf.tx_fifo_reset = 1;
    I2S0.conf.tx_fifo_reset = 0;
    I2S0.lc_conf.out_rst = 1;
    I2S0.lc_conf.out_rst = 0;
    I2S0.lc_conf.ahbm_rst = 1;
    I2S0.lc_conf.ahbm_rst = 0;
    I2S0.lc_conf.ahbm_fifo_rst = 1;
    I2S0.lc_conf.ahbm_fifo_rst = 0;

    I2S0.conf.tx_right_first = 1;
    I2S0.conf.tx_mono = 1;
    I2S0.conf2.lcd_en = 1;                         // LCD mode drives the built-in DAC
    I2S0.fifo_conf.tx_fifo_mod_force_en = 1;
    I2S0.fifo_conf.tx_fifo_mod = 1;                // 16-bit single channel
    I2S0.fifo_conf.dscr_en = 1;
    I2S0.sample_rate_conf.tx_bits_mod = 16;
    I2S0.conf_chan.tx_chan_mod = 1;

    setup_dma();
    for (int k = 0; k < NBUF; k++)
        fill(k);
    next_buf = 0;
    I2S0.out_link.addr = (uint32_t)&desc[0];

    // APLL = 4 x 17.734475 MHz (coefficients from make_tables.py); I2S sample rate = APLL / 4
    periph_rtc_apll_acquire();
#if defined(FUBK_APLL_INTEGER)
    rtc_clk_apll_coeff_set(1, 0, 0, 7);            // 40 MHz * 11 / 6 = 73.33 MHz, no fraction
#elif defined(FUBK_APLL_EXACT)
    rtc_clk_apll_coeff_set(FUBK_APLL_EXACT_ODIV, FUBK_APLL_EXACT_SDM0, FUBK_APLL_EXACT_SDM1,
                           FUBK_APLL_EXACT_SDM2);      // fsc +0.4 Hz, long sigma-delta pattern
#else
    rtc_clk_apll_coeff_set(FUBK_APLL_ODIV, FUBK_APLL_SDM0, FUBK_APLL_SDM1, FUBK_APLL_SDM2);
#endif
    I2S0.clkm_conf.clkm_div_num = 1;
    I2S0.clkm_conf.clkm_div_b = 0;
    I2S0.clkm_conf.clkm_div_a = 1;
    I2S0.sample_rate_conf.tx_bck_div_num = 1;
    I2S0.clkm_conf.clka_en = 1;                    // clock from APLL

    dac_output_enable(DAC_CHAN_0);                 // GPIO25 (= old DAC_CHANNEL_1)
    dac_i2s_enable();

    I2S0.int_clr.val = 0xFFFFFFFF;
    I2S0.int_ena.out_eof = 1;
    I2S0.out_link.start = 1;
    I2S0.conf.tx_start = 1;
    ESP_ERROR_CHECK(esp_intr_enable(isr_handle));
#if defined(FUBK_LINE1136)
    ESP_LOGI(TAG, "video started on GPIO25 (1136-sample lines: same phase every line, like esp_8_bit)");
#ifdef FUBK_APLL_EXACT
    ESP_LOGW(TAG, "1136 lines want FUBK_APLL_EXACT=0: only the 41/64 fraction keeps the jitter still");
#endif
#elif defined(FUBK_FRAME_LOCK)
    ESP_LOGI(TAG, "video started on GPIO25 (frame lock: every frame identical)");
#else
    ESP_LOGI(TAG, "video started on GPIO25 (standard 4-frame PAL sequence)");
#endif
#ifdef FUBK_RAW_PATTERN
    ESP_LOGW(TAG, "RAW PATTERN TEST: fixed 173,77,95,191 loop, no sync (trigger on edge)");
#endif
#ifdef FUBK_TEST_FIELD
    ESP_LOGI(TAG, "TEST FIELD: flat orange picture");
#endif
#if defined(FUBK_APLL_INTEGER)
    ESP_LOGW(TAG, "APLL: INTEGER CLOCK TEST, 73.33 MHz (sample rate 18.33 MHz, not valid PAL)");
#elif defined(FUBK_APLL_EXACT)
    ESP_LOGI(TAG, "APLL: exact fraction (sdm0=%d sdm1=%d sdm2=%d)", FUBK_APLL_EXACT_SDM0,
             FUBK_APLL_EXACT_SDM1, FUBK_APLL_EXACT_SDM2);
#else
    ESP_LOGI(TAG, "APLL: short-pattern fraction (sdm0=%d sdm1=%d sdm2=%d)", FUBK_APLL_SDM0,
             FUBK_APLL_SDM1, FUBK_APLL_SDM2);
#endif
    vTaskDelete(NULL);
}

void app_main(void)
{
    esp_chip_info_t chip;
    esp_chip_info(&chip);
    if (chip.revision < 100)
        ESP_LOGW(TAG, "chip revision %d: the rev 0 APLL ignores sdm0/sdm1, the frequency will be wrong",
                 chip.revision);

    fubk_tables_t t = {fubk_line_info, fubk_rowmap, fubk_tmpl, fubk_burst,
                       fubk_row_start, fubk_delta, fubk_ypool, fubk_uvpool};
    fubk_synth_init(&t);
    xTaskCreatePinnedToCore(start_video, "video", 4096, NULL, 20, NULL, 1);

    uint32_t cpu_hz = 0;
    esp_clk_tree_src_get_freq_hz(SOC_MOD_CLK_CPU, ESP_CLK_TREE_SRC_FREQ_PRECISION_CACHED, &cpu_hz);
    double budget = cpu_hz * (double)BUF_SAMPLES / (4.0 * 4433618.75);   // cycles per buffer
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        uint32_t mx = cyc_max, n = cyc_n, sum = cyc_sum;
        uint32_t re = n_rempty, hu = n_hung, de = n_dscr_err;
        cyc_max = cyc_sum = cyc_n = 0;
        n_rempty = n_hung = n_dscr_err = 0;
        ESP_LOGI(TAG, "I2S flags in the last 2 s (interrupts that saw them): FIFO empty %lu, "
                 "TX hung %lu, descriptor error %lu",
                 (unsigned long)re, (unsigned long)hu, (unsigned long)de);
        ESP_LOGI(TAG, "irq %lu, missed %lu, fill 2 lines: avg %lu max %lu cycles = %.0f%% / %.0f%% of core 1",
                 (unsigned long)n_irq, (unsigned long)n_missed, (unsigned long)(n ? sum / n : 0),
                 (unsigned long)mx, n ? 100.0 * sum / n / budget : 0.0, 100.0 * mx / budget);
    }
}
