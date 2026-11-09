// PAL / NTSC test pattern generator at 20 MS/s on the ESP32 internal DAC (GPIO25), ESP-IDF v5.x.
//   PAL : FuBK test card          NTSC: Philips PM5644 (then SMPTE colour bars)
//
// The APLL runs at 80 MHz with integer coefficients only (sdm0 = sdm1 = 0, sdm2 = 8, odiv = 1),
// so there is no sigma-delta clock jitter; I2S sends APLL / 4 = 20 MS/s. The subcarrier is
// computed per sample (fubk_synth20.c).
//
// Front panel (like the earlier box):
//   GPIO4  button to GND ("D4"): toggles PAL / NTSC (saved in NVS, then restart)
//   GPIO15 button to GND ("D15"): next pattern (PAL: PM5644; NTSC: SMPTE bars; then crosshatch,
//          dots, ...). Not saved: every start shows pattern 0 (PAL FuBK, NTSC PM5644)
//   GPIO12 LED: on in PAL      GPIO13 LED: on in NTSC
//   Data lines: PAL teletext page P100 (lines 20 + 333), NTSC captions CC1 (line 21); vbi20.c
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
#include "text20_tables.h"                 // FUBK20_NGLYPH (the font's data is in fubk_synth20.c)

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
static const fubk20_std_t *start_std;      // pattern 0 (PAL FuBK, NTSC PM5644 in RAM)
// Patterns: 0 is the test card (tables in RAM). The extra ones are in flash; the one on screen is
// copied to pat_ram first, because rendering from flash stalls on cache misses (up to 270 % of a
// buffer's time right after a switch). While pat_ram is refilled, the screen shows black from
// its own small RAM copy.
enum { SLOT_CARD, SLOT_BLACK, SLOT_RAM };
static uint32_t main_k;                    // position of the built-in card (PAL 0, NTSC 1)
static fubk20_std_t pat_ram, black_ram;
// RAM for the pattern on screen: pat_arena (ARENA_BYTES, allocated at start before the tasks
// take their stacks), then heap blocks for what does not fit. The tables that fill pat_arena
// best go there (PM5644 PAL, 90 KB: uvpool + delta + small ones, ypool on the heap); image rows
// (the image page: 90 KB) go in blocks of 16 rows, so no large free block is needed. The heap
// blocks are freed when the next pattern is loaded.
#define ARENA_BYTES (53 * 1024)
#define IMG_CHUNK_ROWS 16
static void *pat_arena;
static uint32_t arena_size, arena_used, arena_mask;     // mask: tables that go to pat_arena
static void *heap_blk[32];
static uint32_t n_heap_blk;
static const uint8_t *img_rows_ram[FUBK20_IMG_MAX_ROWS];
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
    // line lengths: PAL 1280 (image page: some 1282); NTSC 1271 + 1271, or 1272 + 1272 for every 9th pair. The
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
    fubk20_init(&st[0], start_std);
    fubk20_init(&st[1], start_std);
    st[0].vbi_on = st[1].vbi_on = 1;               // teletext (PAL) / captions (NTSC)
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

static void heap_free(void);

static void save_std_and_restart(uint8_t v)
{
    // stop the video first: no interrupt or DMA while the flash is written
    esp_intr_disable(isr_handle);
    I2S0.conf.tx_start = 0;
    I2S0.out_link.stop = 1;
    heap_free();                                   // the image page leaves little heap for NVS
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

// fubk20_place_t for one known-size block: *ctx is the next free byte
static void *bump(uint32_t table, uint32_t bytes, void *ctx)
{
    (void)table;
    uint8_t **p = ctx, *m = *p;
    *p += bytes;
    return m;
}

static void heap_free(void)
{
    while (n_heap_blk)
        heap_caps_free(heap_blk[--n_heap_blk]);
    arena_used = 0;
}

// fubk20_place_t: pat_arena for the tables in arena_mask (and image rows while it has room),
// else a heap block (0 if none)
static void *place(uint32_t table, uint32_t bytes, void *ctx)
{
    (void)ctx;
    bool arena = table >= FUBK20_NTABLES || (arena_mask >> table & 1);
    if (arena && pat_arena && arena_used + bytes <= arena_size) {
        void *m = (uint8_t *)pat_arena + arena_used;
        arena_used += bytes;
        return m;
    }
    if (n_heap_blk == sizeof heap_blk / sizeof heap_blk[0])
        return NULL;
    void *m = heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (m)
        heap_blk[n_heap_blk++] = m;
    return m;
}

// copy the rows of image page src to RAM (blocks of 16 rows from place()) and point pat_ram to
// them; rows that find no RAM stay in flash (slower). Returns the rows left in flash.
static uint32_t img_load(const fubk20_std_t *src)
{
    uint8_t *p = NULL, *end = NULL;
    uint32_t in_flash = 0;
    for (uint32_t r = 0; r < src->rows; r++) {
        if (!p || p + FUBK20_IMG_W > end) {
            uint32_t n = src->rows - r < IMG_CHUNK_ROWS ? src->rows - r : IMG_CHUNK_ROWS;
            p = place(FUBK20_NTABLES, n * FUBK20_IMG_W, NULL);
            end = p ? p + n * FUBK20_IMG_W : NULL;
        }
        if (p) {
            memcpy(p, src->img_rows[r], FUBK20_IMG_W);
            img_rows_ram[r] = p;
            p += FUBK20_IMG_W;
        } else {
            img_rows_ram[r] = src->img_rows[r];
            in_flash++;
        }
    }
    pat_ram.img_rows = img_rows_ram;
    return in_flash;
}

// copy pattern k to pat_ram (nothing may render from it now); logs what went where
static void load_ram(uint32_t k)
{
    int64_t t0 = esp_timer_get_time();
    heap_free();
    // the tables that fill pat_arena best (all 2^9 sets; most patterns fit completely)
    uint32_t sz[FUBK20_NTABLES], best = 0;
    fubk20_pattern_tables(PATS, k, sz);
    arena_mask = 0;
    for (uint32_t m = 1; m < 1u << FUBK20_NTABLES; m++) {
        uint32_t t = 0;
        for (int i = 0; i < FUBK20_NTABLES; i++)
            if (m >> i & 1)
                t += sz[i];
        if (t <= arena_size && t > best) {
            best = t;
            arena_mask = m;
        }
    }
    uint32_t fl = fubk20_pattern_load(PATS, k, place, NULL, &pat_ram);
    if (pat_ram.img_rows)
        fl += img_load(&PATS[k]) * FUBK20_IMG_W;
    int64_t t1 = esp_timer_get_time();
    ESP_LOGI(TAG, "pattern %lu: %s, copied to RAM in %lld us (%lu bytes in pattern RAM, %lu heap blocks, "
             "%lu bytes left in flash); free internal heap %lu, largest block %lu", (unsigned long)k,
             PATS[k].name, (long long)(t1 - t0), (unsigned long)arena_used, (unsigned long)n_heap_blk,
             (unsigned long)fl, (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
}

static uint32_t cur_pat;

static void next_pattern(void)
{
    uint32_t pat = cur_pat = (cur_pat + 1) % FUBK20_NPAT;
    if (pat == main_k || !pat_arena) {
        // built-in card (RAM already); or no RAM buffer: render straight from flash
        if (pat != main_k)
            slot_std[SLOT_RAM] = &PATS[pat];
        request_slot(pat == main_k ? SLOT_CARD : SLOT_RAM);
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
    load_ram(pat);
    request_slot(SLOT_RAM);
}

// before the video starts: pattern RAM, the black pattern, and the start pattern start_k (copied
// to RAM unless it is the built-in card). Returns it.
static const fubk20_std_t *patterns_init(uint8_t ntsc, uint32_t start_k)
{
    cur_pat = start_k;
    main_k = ntsc ? FUBK20_NTSC_MAIN : FUBK20_PAL_MAIN;
    slot_std[SLOT_CARD] = S;
    slot_std[SLOT_BLACK] = S;                      // fallback: never used if black_ram loads
    uint32_t mx = ARENA_BYTES, kb = 0;
    for (uint32_t k = 0; k < FUBK20_NPAT; k++)
        if (strstr(PATS[k].name, " black"))
            kb = k;
    assert(kb);
    void *b = heap_caps_malloc(fubk20_pattern_bytes(PATS, kb), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    pat_arena = heap_caps_malloc(mx, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    arena_size = pat_arena ? mx : 0;
    if (b) {
        uint8_t *bp = b;
        fubk20_pattern_load(PATS, kb, bump, &bp, &black_ram);
        slot_std[SLOT_BLACK] = &black_ram;
    }
    slot_std[SLOT_RAM] = &pat_ram;
    ESP_LOGI(TAG, "pattern RAM: %lu bytes %s; free internal heap %lu bytes, largest block %lu",
             (unsigned long)mx, pat_arena ? "allocated" : "NOT available (patterns render from flash)",
             (unsigned long)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
             (unsigned long)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (start_k == main_k)
        return S;
    if (!pat_arena)
        return &PATS[start_k];
    load_ram(start_k);
    return &pat_ram;
}

#ifdef FUBK_BURST_TEST
// Test build: the pattern button rotates the PAL burst against the picture, on top of the
// built-in rotation (make_tables20.py, -9.1 deg); positive = burst phase advanced, 1 step =
// 360/1024 deg. Presses 0..5 = 0, +1, -1, +2, -2 deg (3 and 6 steps), then 0 again.
static const int16_t burst_steps[] = { 0, 3, -3, 6, -6 };
static void burst_set(uint32_t k)
{
    int32_t steps = burst_steps[k];
    uint32_t n = fubk20_pal.car_n;
    fubk20_burst_ofs = ((uint32_t)(steps + (int32_t)n) % n) * fubk20_pal.car_inv % n;
    ESP_LOGI(TAG, "burst rotation %+ld steps = %+.1f deg extra", (long)steps, steps * 360.0 / n);
}
static void burst_step(void)
{
    static uint32_t k;
    k = (k + 1) % (sizeof burst_steps / sizeof burst_steps[0]);
    burst_set(k);
}
#endif

static void button_task(void *arg)
{
    uint8_t cur = (uint8_t)(uintptr_t)arg;
    // wait until the buttons are released (they may be held at power-up)
    while (gpio_get_level(PIN_BUTTON) == 0 || gpio_get_level(PIN_PATTERN) == 0)
        vTaskDelay(pdMS_TO_TICKS(20));
#ifdef FUBK_START_PATTERN
    vTaskDelay(pdMS_TO_TICKS(500));                // test builds: step to this pattern at start
    for (int i = 0; i < FUBK_START_PATTERN; i++)
        next_pattern();
#endif
    // each button acts when it is released (50 ms down, then 50 ms up: debounced), and only if
    // the other one was not pressed meanwhile: both together do nothing in normal use (the edit
    // mode is only entered by holding both at power-on)
    uint32_t da = 0, ua = 0, db = 0, ub = 0;
    bool pa = false, pb = false, both = false;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(10));
        bool a = gpio_get_level(PIN_PATTERN) == 0, b = gpio_get_level(PIN_BUTTON) == 0;
        da = a ? da + 1 : 0;
        ua = a ? 0 : ua + 1;
        db = b ? db + 1 : 0;
        ub = b ? 0 : ub + 1;
        if (da >= 5)
            pa = true;
        if (db >= 5)
            pb = true;
        if (pa && pb)
            both = true;
        if (pa && ua >= 5) {                       // pattern button released
            pa = false;
            if (!both) {
#ifdef FUBK_BURST_TEST
                burst_step();
#else
                next_pattern();
#endif
            }
        }
        if (pb && ub >= 5) {                       // PAL/NTSC button released
            pb = false;
            if (!both)
                save_std_and_restart(cur ^ 1);
        }
        if (!pa && !pb)
            both = false;
    }
}

// --- text (PM5644 boxes, FuBK name box) and the text edit mode ----------------------------------
// 33 positions: 0..7 PM5644 upper, 8..19 PM5644 lower, 20..32 FuBK. The edit mode edits the card
// it starts on (pattern 0): in PAL FuBK (20..32), in NTSC PM5644 (0..19; PAL's PM5644 shows
// the same text). Saved in NVS as blob "text" (glyph indices); without it the defaults from
// text20_tables.h stay.
#define TEXT_POS 33
#define TEXT_POS_FUBK 20

static uint8_t *text_at(uint32_t p)
{
    return p < 8 ? &fubk20_text[0][p] : p < 20 ? &fubk20_text[1][p - 8] : &fubk20_text[2][p - 20];
}

static void text_load(void)
{
    uint8_t t[TEXT_POS];
    size_t n = sizeof t;
    nvs_handle_t h;
    if (nvs_open("fubk", NVS_READONLY, &h) != ESP_OK)
        return;
    if (nvs_get_blob(h, "text", t, &n) == ESP_OK && n == sizeof t) {
        for (uint32_t p = 0; p < TEXT_POS; p++)
            *text_at(p) = t[p] < FUBK20_NGLYPH ? t[p] : 0;
        ESP_LOGI(TAG, "text loaded from flash (glyphs %u %u %u ...)", t[0], t[1], t[2]);
    }
    nvs_close(h);
}

static void text_save(void)
{
    uint8_t t[TEXT_POS];
    for (uint32_t p = 0; p < TEXT_POS; p++)
        t[p] = *text_at(p);
    nvs_handle_t h;
    if (nvs_open("fubk", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, "text", t, sizeof t);
        nvs_commit(h);
        nvs_close(h);
    }
    ESP_LOGI(TAG, "text saved");
}

// Saving the text writes the flash, which must not happen while the video runs (the interrupt
// watchdog resets the chip): the video stops, the text is saved, and the chip restarts straight
// into edit mode at the same position (edit_resume survives the restart).
#define EDIT_MAGIC 0xED170000u
static RTC_NOINIT_ATTR uint32_t edit_resume;

static void edit_save_and_restart(uint32_t pos)
{
    esp_intr_disable(isr_handle);
    I2S0.conf.tx_start = 0;
    I2S0.out_link.stop = 1;
    heap_free();
    text_save();
    edit_resume = EDIT_MAGIC | pos;
    vTaskDelay(pdMS_TO_TICKS(20));
    esp_restart();
}

// Edit mode (both buttons held at power-on): pattern 0 (PAL FuBK, NTSC PM5644) with a blinking
// cursor. After the last character comes the half space (only in a line's last place: moves the
// line half a character right, to centre it). Pattern button: next
// character, PAL/NTSC button: previous one (on release; held: repeats after 0.5 s, 8 per
// second); both together: next position (on release), both held 1 s: previous position. 3 s
// after the last change the text is saved (the picture blinks: see above), so switching off
// after that keeps it.
static void edit_task(void *arg)
{
    while (gpio_get_level(PIN_BUTTON) == 0 || gpio_get_level(PIN_PATTERN) == 0)
        vTaskDelay(pdMS_TO_TICKS(20));             // the power-on press
    // PAL: FuBK's 13 positions, NTSC: PM5644's 20
    const uint32_t first = S == &fubk20_pal ? TEXT_POS_FUBK : 0;
    const uint32_t npos = S == &fubk20_pal ? TEXT_POS - TEXT_POS_FUBK : TEXT_POS_FUBK;
    uint32_t pos = (uint32_t)(uintptr_t)arg, da = 0, db = 0, held = 0, tick = 0, quiet = 0, ct = 0;
    if (pos < first || pos >= first + npos)
        pos = first;
    bool active = false, chord = false, rep = false, dirty = false;
    int dir = 0;
    fubk20_cursor = (int8_t)pos;
    for (;; tick++) {
        vTaskDelay(pdMS_TO_TICKS(10));
        if (tick % 25 == 0)
            fubk20_cursor_on ^= 1;                 // 2 Hz blink
        da = gpio_get_level(PIN_PATTERN) == 0 ? da + 1 : 0;
        db = gpio_get_level(PIN_BUTTON) == 0 ? db + 1 : 0;
        bool a = da >= 3, b = db >= 3;             // 30 ms: debounced
        int step = 0;
        if (!active && (a || b)) {
            active = true;
            chord = rep = false;
            held = 0;
        }
        int move = 0;
        if (active) {
            if (a && b && !chord) {                // both: a position move (decided below)
                chord = true;
                ct = 0;
            }
            if (chord && ++ct == 100)              // both held 1 s: previous position
                move = -1;
            if (!chord && (a != b)) {
                dir = a ? 1 : -1;
                if (++held >= 50 && (held - 50) % 12 == 0) {
                    step = dir;
                    rep = true;
                }
            }
            if (!a && !b) {
                if (!chord && !rep)
                    step = dir;
                if (chord && ct < 100)             // short double press: next position
                    move = 1;
                active = false;
            }
        }
        if (move) {
            pos = first + (pos - first + npos + (uint32_t)move) % npos;
            fubk20_cursor = (int8_t)pos;
            fubk20_cursor_on = 1;
            tick = 1;
        }
        if (step) {
            uint8_t *g = text_at(pos);
            *g = (uint8_t)((*g + FUBK20_NGLYPH + step) % FUBK20_NGLYPH);
            // the half space only in a line's last place (7, 19, 32)
            if (*g == FUBK20_HALF && pos != 7 && pos != 19 && pos != TEXT_POS - 1)
                *g = (uint8_t)((*g + FUBK20_NGLYPH + step) % FUBK20_NGLYPH);
            fubk20_cursor_on = 1;                  // show the new character at once
            tick = 1;
            dirty = true;
            quiet = 0;
        }
        if (dirty && !active && ++quiet >= 300)    // 3 s without a change
            edit_save_and_restart(pos);
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
    text_load();
    vTaskDelay(pdMS_TO_TICKS(20));                 // pull-ups settled
    // edit mode: both buttons held at power-on (or a reset with the EN button), not after the
    // firmware's own restarts (the PAL / NTSC switch); or the restart after an edit-mode save
    uint32_t resume = edit_resume;
    edit_resume = 0;
    esp_reset_reason_t why = esp_reset_reason();
    bool resumed = why == ESP_RST_SW && (resume & 0xFFFF0000u) == EDIT_MAGIC && (resume & 0xFFu) < TEXT_POS;
    bool edit = resumed || (why != ESP_RST_SW && gpio_get_level(PIN_BUTTON) == 0 &&
                            gpio_get_level(PIN_PATTERN) == 0);

    for (int k = 0; k < NBUF; k++) {
        bufs[k] = heap_caps_aligned_alloc(4, (2 * F20_MAX_SPL + 4) * sizeof(uint16_t),
                                          MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        assert(bufs[k]);
    }
    start_std = patterns_init(ntsc, 0);           // pattern 0 (the edit mode edits its text)
    if (edit)
        ESP_LOGI(TAG, "text edit mode: pattern button = next character, PAL/NTSC button = previous, "
                 "both = next position (held 1 s: previous); saved 3 s after a change. PAL: FuBK's "
                 "text, NTSC: PM5644's");
#ifdef FUBK_BURST_TEST
    burst_set(0);
#endif
    q_core0 = xQueueCreate(NBUF, sizeof(uint32_t));
    xTaskCreatePinnedToCore(core0_worker, "fubk20w", 4096, NULL, configMAX_PRIORITIES - 1, NULL, 0);
    xTaskCreatePinnedToCore(start_video, "video", 4096, NULL, 20, NULL, 1);
    if (edit)
        xTaskCreatePinnedToCore(edit_task, "edit", 3072, (void *)(uintptr_t)(resumed ? resume & 0xFFu : 0u), 5, NULL, 0);
    else
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
