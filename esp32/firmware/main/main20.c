// PAL / NTSC test pattern generator at 20 MS/s on the ESP32 internal DAC (GPIO25), ESP-IDF v5.x.
//   PAL : FuBK test card          NTSC: SMPTE colour bars
//
// The APLL runs at 80 MHz with integer coefficients only (sdm0 = sdm1 = 0, sdm2 = 8, odiv = 1),
// so there is no sigma-delta clock jitter; I2S sends APLL / 4 = 20 MS/s. The subcarrier is
// computed per sample (fubk_synth20.c).
//
// Front panel (like the earlier box):
//   GPIO4  button to GND ("D4"): toggles PAL / NTSC (saved in NVS, then restart)
//   GPIO15 button to GND ("D15"): next pattern (crosshatch, dots, ...). Not saved: every start
//          shows the real test card (FuBK / SMPTE bars) first
//   GPIO12 LED: on in PAL      GPIO13 LED: on in NTSC
//   GPIO18 1 kHz sine as PWM (312.5 kHz, 8 bit; RC low-pass it), updated once per video line,
//          so it is locked to the picture (PAL: exactly 40 cycles per frame)
//
// NBUF DMA buffers of 2 lines each, one descriptor per line. Each line raises an EOF interrupt
// (on core 1) that steps the tone; when a buffer is done, the interrupt refills it itself (odd
// buffers) or hands it to a task on core 0 (even buffers). Each core keeps its own synthesis
// state, renders its buffers and skips the other core's lines.
#include <stdio.h>
#include <stdbool.h>
#include <string.h>
#include <assert.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_attr.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_intr_alloc.h"
#include "esp_heap_caps.h"
#include "esp_cpu.h"
#include "esp_clk_tree.h"
#include "esp_private/periph_ctrl.h"
#include "clk_ctrl_os.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "hal/ledc_ll.h"
#include "soc/periph_defs.h"
#include "soc/interrupts.h"
#include "soc/i2s_struct.h"
#include "soc/i2s_reg.h"
#include "soc/ledc_struct.h"
#include "soc/rtc.h"
#include "rom/lldesc.h"
#include "driver/dac.h"
#include "fubk_synth20.h"

static const char *TAG = "fubk20";

#define PIN_BUTTON GPIO_NUM_4              // "D4", to GND, internal pull-up
#define PIN_PATTERN GPIO_NUM_15            // "D15", to GND, internal pull-up (strapping pin: only
                                           // silences the boot log if held at reset)
#define PIN_LED_PAL GPIO_NUM_12            // strapping pin: the LED must pull it LOW at reset
#define PIN_LED_NTSC GPIO_NUM_13
#define PIN_AUDIO GPIO_NUM_18
#define AUDIO_HZ 1000
#define AUDIO_AMP 120                      // PWM duty swing around 128 (of 255)

#define NBUF 8                             // 8 x 2 lines = 1 ms of buffering
static uint16_t *bufs[NBUF];                // 2 lines each, DMA-capable heap (static RAM is full)
static lldesc_t DMA_ATTR desc[2 * NBUF];
static intr_handle_t isr_handle;
static QueueHandle_t q_core0;
static fubk20_state_t st[2];               // st[w] renders the buffers k with k % 2 == w
static const fubk20_std_t *S;              // the standard (timing); pattern 0 = its test card
static const fubk20_std_t *PATS;           // its patterns
// Patterns: 0 is the test card (tables in RAM). The extra ones are in flash; the one on screen is
// copied to pat_ram first, because rendering from flash stalls on cache misses (up to 270 % of a
// buffer's time right after a switch). While pat_ram is refilled, the screen shows black from
// its own small RAM copy.
enum { SLOT_CARD, SLOT_BLACK, SLOT_RAM };
#define PAT_BLACK 10                       // "black" in patterns20.py
static fubk20_std_t pat_ram, black_ram;
static void *pat_arena;
static const fubk20_std_t *slot_std[3];
// pattern request from the button task: (slot + 1) << 24 | frame (low 24 bits) it starts at;
// 0 = none. fill() hands it to both render states, which switch at that frame start.
static volatile uint32_t pat_req;
static uint32_t pat_seen[2];

static volatile uint32_t last_done = NBUF - 1;     // last buffer handed out for refilling
static volatile uint32_t last_di;                  // descriptor index at the previous interrupt
static volatile uint32_t next_buf[2];
static volatile uint32_t n_irq, n_lines, n_missed[2], n_qfull, cyc_max[2], cyc_sum[2], cyc_n[2];
static volatile uint32_t n_rempty, n_hung, n_dscr_err;

// --- tone: phase in 1/20000 cycle (1 kHz x one sample period = 1/20000 cycle) ----------------
static DRAM_ATTR int8_t sin256[256];
static uint32_t audio_acc;

static IRAM_ATTR void audio_step(uint32_t lines)
{
    audio_acc = (audio_acc + lines * S->spl * (AUDIO_HZ / 1000)) % 20000u;
    uint32_t duty = 128 + sin256[(audio_acc * 32u) / 2500u];      // * 256 / 20000
    ledc_ll_set_duty_int_part(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, duty);
    ledc_ll_set_duty_direction(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, LEDC_DUTY_DIR_INCREASE);
    ledc_ll_set_duty_num(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, 1);
    ledc_ll_set_duty_cycle(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, 1);
    ledc_ll_set_duty_scale(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0, 0);
    ledc_ll_set_duty_start(&LEDC, LEDC_HIGH_SPEED_MODE, LEDC_CHANNEL_0);
}

static void audio_init(void)
{
    for (int i = 0; i < 256; i++)
        sin256[i] = (int8_t)lrintf(AUDIO_AMP * sinf(2 * (float)M_PI * i / 256));
    ledc_timer_config_t t = {
        .speed_mode = LEDC_HIGH_SPEED_MODE, .duty_resolution = LEDC_TIMER_8_BIT,
        .timer_num = LEDC_TIMER_0, .freq_hz = 312500, .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_ERROR_CHECK(ledc_timer_config(&t));
    ledc_channel_config_t c = {
        .gpio_num = PIN_AUDIO, .speed_mode = LEDC_HIGH_SPEED_MODE, .channel = LEDC_CHANNEL_0,
        .timer_sel = LEDC_TIMER_0, .duty = 128, .hpoint = 0,
    };
    ESP_ERROR_CHECK(ledc_channel_config(&c));
}

// --- video ---------------------------------------------------------------------------------
// Refill buffer k with the next 2 lines (worker k % 2), then skip the 2 lines of buffer k + 1,
// which the other worker renders.
static IRAM_ATTR void fill(uint32_t k)
{
    fubk20_state_t *s = &st[k & 1];
    uint32_t r = pat_req;
    if (r != pat_seen[k & 1]) {
        pat_seen[k & 1] = r;
        // full frame number: the next one at or after s->frame with these low 24 bits
        uint32_t frame = s->frame + (((r & 0xFFFFFFu) - s->frame) & 0xFFFFFFu);
        fubk20_request(s, slot_std[(r >> 24) - 1], frame);
    }
    // line lengths: PAL always 1280; NTSC 1271 + 1271, or 1272 + 1272 for every 9th pair. The
    // DMA reaches this buffer only after the other NBUF - 1, so its descriptors can change here.
    uint32_t n1 = fubk20_next_len(s);
    fubk20_line(s, bufs[k], 0);
    uint32_t n2 = fubk20_next_len(s);
    fubk20_line(s, bufs[k], n1);
    lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
    uint32_t a_bytes = (n1 * 2u) & ~3u;            // both a multiple of 4 bytes (1271: 2540 + 2544)
    a->size = a->length = a_bytes;
    b->buf = (uint8_t *)bufs[k] + a_bytes;
    b->size = b->length = (n1 + n2) * 2u - a_bytes;
    fubk20_skip(s);
    fubk20_skip(s);
}

static IRAM_ATTR void timed_fill(uint32_t k)
{
    uint32_t w = k & 1;
    if (k != next_buf[w])
        n_missed[w]++;
    uint32_t t0 = esp_cpu_get_cycle_count();
    fill(k);
    uint32_t dt = esp_cpu_get_cycle_count() - t0;
    if (dt > cyc_max[w])
        cyc_max[w] = dt;
    cyc_sum[w] += dt;
    cyc_n[w]++;
    next_buf[w] = (k + 2) % NBUF;
}

static IRAM_ATTR void i2s_isr(void *arg)
{
    uint32_t st_ = I2S0.int_st.val;
    uint32_t raw = I2S0.int_raw.val;
    const uint32_t trouble = I2S_TX_REMPTY_INT_RAW_M | I2S_TX_HUNG_INT_RAW_M |
                             I2S_OUT_DSCR_ERR_INT_RAW_M;
    if (raw & I2S_TX_REMPTY_INT_RAW_M) n_rempty++;
    if (raw & I2S_TX_HUNG_INT_RAW_M) n_hung++;
    if (raw & I2S_OUT_DSCR_ERR_INT_RAW_M) n_dscr_err++;
    I2S0.int_clr.val = st_ | (raw & trouble);
    if (!(st_ & I2S_OUT_EOF_INT_ST_M))
        return;
    uint32_t di = (uint32_t)((lldesc_t *)I2S0.out_eof_des_addr - desc);   // last line sent
    if (di >= 2 * NBUF)
        return;
    n_irq++;
    // lines sent since the last interrupt (normally 1; more if an interrupt was delayed)
    uint32_t adv = (di + 2 * NBUF - last_di) % (2 * NBUF);
    last_di = di;
    n_lines += adv;
    audio_step(adv);
    // every buffer up to the last complete one goes out for refilling, in order
    uint32_t done = (di & 1) ? di / 2 : (di / 2 + NBUF - 1) % NBUF;
    while (last_done != done) {
        uint32_t k = (last_done + 1) % NBUF;
        last_done = k;
        if (k & 1) {
            timed_fill(k);                         // core 1, right here
        } else {
            BaseType_t woken = pdFALSE;
            if (xQueueSendFromISR(q_core0, &k, &woken) != pdTRUE)
                n_qfull++;
            portYIELD_FROM_ISR(woken);
        }
    }
}

static void core0_worker(void *arg)
{
    uint32_t k;
    for (;;) {
        if (xQueueReceive(q_core0, &k, portMAX_DELAY) == pdTRUE)
            timed_fill(k);
    }
}

static void setup_dma(void)
{
    // sizes are set by fill() for each buffer
    for (int k = 0; k < NBUF; k++) {
        lldesc_t *a = &desc[2 * k], *b = &desc[2 * k + 1];
        memset(a, 0, sizeof *a);
        memset(b, 0, sizeof *b);
        a->buf = (uint8_t *)bufs[k];
        a->owner = 1;
        a->eof = 1;                                // one interrupt per line (tone)
        a->qe.stqe_next = b;
        b->owner = 1;
        b->eof = 1;
        b->qe.stqe_next = &desc[(2 * k + 2) % (2 * NBUF)];
    }
    last_di = 2 * NBUF - 1;
}

static void start_video(void *arg)
{
    // runs on core 1, so the interrupt is serviced there
    periph_module_enable(PERIPH_I2S0_MODULE);
    ESP_ERROR_CHECK(esp_intr_alloc(ETS_I2S0_INTR_SOURCE, ESP_INTR_FLAG_LEVEL1 | ESP_INTR_FLAG_IRAM,
                                   i2s_isr, NULL, &isr_handle));

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
    // 20 MS/s x 2 bytes = 40 MB/s: let the DMA fetch descriptors and data in bursts, and refill
    // the FIFO early, else the FIFO runs dry
    I2S0.lc_conf.outdscr_burst_en = 1;
    I2S0.lc_conf.out_data_burst_en = 1;
    I2S0.fifo_conf.tx_data_num = 48;

    setup_dma();
    fubk20_init(&st[0], S);
    fubk20_init(&st[1], S);
    fubk20_skip(&st[1]);                           // core 1 starts at buffer 1: skip buffer 0
    fubk20_skip(&st[1]);
    for (int k = 0; k < NBUF; k++)
        fill(k);
    next_buf[0] = 0;
    next_buf[1] = 1;
    I2S0.out_link.addr = (uint32_t)&desc[0];

    // APLL = 40 MHz * (4 + 8) / (2 * (1 + 2)) = 80 MHz, integer only: no sigma-delta jitter
    periph_rtc_apll_acquire();
    rtc_clk_apll_coeff_set(1, 0, 0, 8);
    I2S0.clkm_conf.clkm_div_num = 1;
    I2S0.clkm_conf.clkm_div_b = 0;
    I2S0.clkm_conf.clkm_div_a = 1;
    I2S0.sample_rate_conf.tx_bck_div_num = 1;
    I2S0.clkm_conf.clka_en = 1;                    // clock from APLL; sample rate = APLL / 4

    dac_output_enable(DAC_CHAN_0);                 // GPIO25
    dac_i2s_enable();

    I2S0.int_clr.val = 0xFFFFFFFF;
    I2S0.int_ena.out_eof = 1;
    I2S0.out_link.start = 1;
    I2S0.conf.tx_start = 1;
    ESP_ERROR_CHECK(esp_intr_enable(isr_handle));
    ESP_LOGI(TAG, "video started on GPIO25: %s, 20 MS/s (integer APLL 80 MHz), %u samples/line, "
             "fsc %.2f Hz; 1 kHz tone on GPIO%d", S->name, S->spl,
             20e6 * S->ph_num / S->ph_den, PIN_AUDIO);
#ifdef FUBK_TEST_FIELD
    if (S == &fubk20_pal)
        ESP_LOGI(TAG, "TEST FIELD: flat orange picture (worst case CPU: colour everywhere)");
#endif
    vTaskDelete(NULL);
}

// --- standard selection, LEDs, button ----------------------------------------------------------
static uint8_t load_std(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        e = nvs_flash_init();
    }
    ESP_ERROR_CHECK(e);
    uint8_t v = 0;                                 // 0 = PAL (default), 1 = NTSC
    nvs_handle_t h;
    if (nvs_open("fubk", NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, "std", &v);
        nvs_close(h);
    }
    return v ? 1 : 0;
}

static void save_std_and_restart(uint8_t v)
{
    // stop the video first: no interrupt or DMA while the flash is written
    esp_intr_disable(isr_handle);
    I2S0.conf.tx_start = 0;
    I2S0.out_link.stop = 1;
    nvs_handle_t h;
    if (nvs_open("fubk", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, "std", v);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGI(TAG, "switching to %s, restarting", v ? "NTSC" : "PAL");
    vTaskDelay(pdMS_TO_TICKS(50));
    esp_restart();
}

static void request_slot(uint32_t slot)
{
    // start 2 frames ahead: both render states see the request long before (within 1 ms)
    uint32_t frame = *(volatile uint32_t *)&st[1].frame + 2;
    __sync_synchronize();                          // copied tables visible before the request
    pat_req = (slot + 1) << 24 | (frame & 0xFFFFFFu);
}

static bool ram_in_use(void)
{
    for (int w = 0; w < 2; w++) {
        if (*(const fubk20_std_t *volatile *)&st[w].s == &pat_ram ||
            *(const fubk20_std_t *volatile *)&st[w].pend == &pat_ram)
            return true;
    }
    return false;
}

static void next_pattern(void)
{
    static uint32_t pat;
    pat = (pat + 1) % FUBK20_NPAT;
    if (pat == 0 || !pat_arena) {
        // test card (RAM already); or no RAM buffer: render straight from flash
        if (!pat_arena)
            slot_std[SLOT_RAM] = &PATS[pat];
        request_slot(pat == 0 ? SLOT_CARD : SLOT_RAM);
        ESP_LOGI(TAG, "pattern %lu: %s", (unsigned long)pat, PATS[pat].name);
        return;
    }
    if (ram_in_use()) {                            // black while the RAM copy is replaced
        request_slot(SLOT_BLACK);
        for (int i = 0; i < 50 && ram_in_use(); i++)
            vTaskDelay(pdMS_TO_TICKS(10));
        if (ram_in_use()) {
            ESP_LOGW(TAG, "pattern switch timed out");
            return;
        }
    }
    int64_t t0 = esp_timer_get_time();
    fubk20_pattern_load(PATS, pat, pat_arena, &pat_ram);
    int64_t t1 = esp_timer_get_time();
    request_slot(SLOT_RAM);
    ESP_LOGI(TAG, "pattern %lu: %s (%lu bytes copied to RAM in %lld us)", (unsigned long)pat,
             PATS[pat].name, (unsigned long)fubk20_pattern_bytes(PATS, pat), (long long)(t1 - t0));
}

static void patterns_init(void)
{
    slot_std[SLOT_CARD] = S;
    slot_std[SLOT_BLACK] = &PATS[0];               // fallback: never used if black_ram loads
    uint32_t mx = 0;
    for (uint32_t k = 1; k < FUBK20_NPAT; k++) {
        uint32_t n = fubk20_pattern_bytes(PATS, k);
        if (n > mx)
            mx = n;
    }
    assert(strstr(PATS[PAT_BLACK].name, "black"));
    void *b = heap_caps_malloc(fubk20_pattern_bytes(PATS, PAT_BLACK), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    pat_arena = heap_caps_malloc(mx, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (b) {
        fubk20_pattern_load(PATS, PAT_BLACK, b, &black_ram);
        slot_std[SLOT_BLACK] = &black_ram;
    }
    slot_std[SLOT_RAM] = &pat_ram;
    ESP_LOGI(TAG, "pattern RAM: %lu bytes %s; free internal heap %lu bytes", (unsigned long)mx,
             pat_arena ? "allocated" : "NOT available (patterns render from flash)",
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
}

static void button_task(void *arg)
{
    uint8_t cur = (uint8_t)(uintptr_t)arg;
    // wait until the buttons are released (they may be held at power-up)
    while (gpio_get_level(PIN_BUTTON) == 0 || gpio_get_level(PIN_PATTERN) == 0)
        vTaskDelay(pdMS_TO_TICKS(20));
    int low = 0, plow = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10));
        low = gpio_get_level(PIN_BUTTON) == 0 ? low + 1 : 0;
        if (low == 5)                              // 50 ms pressed: debounced
            save_std_and_restart(cur ^ 1);
        // pattern button: one step per press (50 ms down; released for 50 ms before the next)
        if (gpio_get_level(PIN_PATTERN) == 0) {
            if (plow < 5 && ++plow == 5)
                next_pattern();
        } else if (plow >= 5) {
            plow = plow >= 9 ? 0 : plow + 1;
        } else {
            plow = 0;
        }
    }
}

static void gpio_init(uint8_t ntsc)
{
    gpio_config_t out = {
        .pin_bit_mask = (1ULL << PIN_LED_NTSC) | (1ULL << PIN_LED_PAL), .mode = GPIO_MODE_OUTPUT,
    };
    ESP_ERROR_CHECK(gpio_config(&out));
    gpio_set_level(PIN_LED_NTSC, ntsc);
    gpio_set_level(PIN_LED_PAL, !ntsc);
    gpio_config_t in = {
        .pin_bit_mask = (1ULL << PIN_BUTTON) | (1ULL << PIN_PATTERN), .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&in));
}

void app_main(void)
{
    uint8_t ntsc = load_std();
    S = ntsc ? &fubk20_ntsc : &fubk20_pal;
    PATS = ntsc ? fubk20_ntsc_pat : fubk20_pal_pat;
    gpio_init(ntsc);
    audio_init();

    for (int k = 0; k < NBUF; k++) {
        bufs[k] = heap_caps_aligned_alloc(4, (2 * F20_MAX_SPL + 4) * sizeof(uint16_t),
                                          MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        assert(bufs[k]);
    }
    patterns_init();
    q_core0 = xQueueCreate(NBUF, sizeof(uint32_t));
    xTaskCreatePinnedToCore(core0_worker, "fubk20w", 4096, NULL, configMAX_PRIORITIES - 1, NULL, 0);
    xTaskCreatePinnedToCore(start_video, "video", 4096, NULL, 20, NULL, 1);
    xTaskCreatePinnedToCore(button_task, "button", 3072, (void *)(uintptr_t)ntsc, 5, NULL, 0);

    uint32_t cpu_hz = 0;
    esp_clk_tree_src_get_freq_hz(SOC_MOD_CLK_CPU, ESP_CLK_TREE_SRC_FREQ_PRECISION_CACHED, &cpu_hz);
    // each core gets one buffer (2 lines) every 2 buffer periods = 4 lines
    double budget = cpu_hz * 4.0 * S->spl / 20e6;
    int64_t t_last = esp_timer_get_time();
    uint32_t lines_last = n_lines;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        int64_t t_now = esp_timer_get_time();
        uint32_t lines_now = n_lines;
        double avg_len = S->spl + (S->long_period ? 1.0 / S->long_period : 0.0);   // NTSC 1271.111
        double rate = (double)(lines_now - lines_last) * avg_len / ((t_now - t_last) * 1e-6);
        t_last = t_now;
        lines_last = lines_now;
        uint32_t mx[2], n[2], sum[2];
        for (int w = 0; w < 2; w++) {
            mx[w] = cyc_max[w];
            n[w] = cyc_n[w];
            sum[w] = cyc_sum[w];
            cyc_max[w] = cyc_sum[w] = cyc_n[w] = 0;
        }
        uint32_t re = n_rempty, hu = n_hung, de = n_dscr_err;
        n_rempty = n_hung = n_dscr_err = 0;
        ESP_LOGI(TAG, "%s | I2S flags (2 s): FIFO empty %lu, TX hung %lu, descriptor error %lu; "
                 "missed core0 %lu core1 %lu, queue full %lu; sample rate %.0f Hz",
                 S->name, (unsigned long)re, (unsigned long)hu, (unsigned long)de,
                 (unsigned long)n_missed[0], (unsigned long)n_missed[1], (unsigned long)n_qfull, rate);
        for (int w = 0; w < 2; w++)
            ESP_LOGI(TAG, "core %d: render 2 lines avg %lu max %lu cycles = %.0f%% / %.0f%% of its time",
                     w == 0 ? 0 : 1, (unsigned long)(n[w] ? sum[w] / n[w] : 0), (unsigned long)mx[w],
                     n[w] ? 100.0 * sum[w] / n[w] / budget : 0.0, 100.0 * mx[w] / budget);
    }
}
